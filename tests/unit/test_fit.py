"""Agent D: the out-of-memory gate.

The acceptance list from the brief, plus the traps. Every refusal has to name
the term that blew the budget and a change that would work; a test that only
checks the verdict is not testing the product.
"""

from __future__ import annotations

import dataclasses
import math

import pytest

from control_plane.fit.constants import DECODE_EFFICIENCY, DECODE_EFFICIENCY_BEST
from control_plane.contracts import (
    COMM_BUFFER_BYTES,
    DEGRADED_TPS_THRESHOLD,
    EP_EXTRA_BUFFER_BYTES,
    FRAMEWORK_OVERHEAD,
    FitRequest,
    ModelShape,
    ParallelismKind,
    ParallelismPlan,
    Verdict,
)
from control_plane.fit import (
    FitCalculator,
    StubFit,
    activation_bytes,
    kv_bytes_per_token,
    kv_cache_bytes,
    kv_divisor,
    memory_breakdown,
    min_nodes_required,
    predict_decode_tps,
    stage_fraction,
    weight_bytes_per_rank,
)
from tests.fixtures import (
    DEEPSEEK_V3,
    GPT_OSS_120B,
    LLAMA_3_3_70B,
    QWEN3_30B_A3B,
    SPARK_01,
    SPARK_02,
    WS_3090,
)

GIB = 1024**3
TWO_SPARKS = [SPARK_01, SPARK_02]
ONE_SPARK = [SPARK_01]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def make_plan(tp=1, pp=1, ep=1, dp=1, node_ids=()) -> ParallelismPlan:
    world = tp * pp * dp
    return ParallelismPlan(
        kind=ParallelismKind.SINGLE_NODE if world == 1 else ParallelismKind.HYBRID,
        tensor_parallel=tp,
        pipeline_parallel=pp,
        expert_parallel=ep,
        data_parallel=dp,
        node_ids=list(node_ids),
        reason="test plan",
        measured_link_gbps=10.2,
        rejected=[],
    )


def request(
    shape, context=8192, seqs=16, kv_dtype="auto", plan=None, weight_bytes=None
) -> FitRequest:
    return FitRequest(
        shape=shape,
        context_length=context,
        max_concurrent_seqs=seqs,
        kv_dtype=kv_dtype,
        plan=plan or make_plan(),
        weight_bytes=weight_bytes,
    )


@pytest.fixture
def fit() -> FitCalculator:
    return FitCalculator()


LLAMA_70B_FP8 = dataclasses.replace(LLAMA_3_3_70B, dtype="fp8")


# --------------------------------------------------------------------------
# acceptance
# --------------------------------------------------------------------------


def test_llama70b_bf16_one_spark_wont_fit_on_weights(fit):
    """141 GiB of bf16 weights against 107.7 GiB usable. Nothing about context
    or concurrency can rescue it."""
    result = fit.check(request(LLAMA_3_3_70B, context=8192, seqs=16), ONE_SPARK)

    assert result.verdict is Verdict.WONT_FIT
    assert not result.ok
    assert result.limiting_term == "weights"
    assert min_nodes_required(LLAMA_3_3_70B, SPARK_01, 8192, 16) == 2
    # The refusal has to say how many nodes, not just that it failed.
    assert "2 nodes" in result.reason


def test_llama70b_fp8_one_spark_fits_but_degraded(fit):
    """Loads. Decodes at a couple of tokens a second. Say so before the load,
    not after."""
    result = fit.check(
        request(LLAMA_70B_FP8, context=4096, seqs=16), ONE_SPARK
    )

    assert result.verdict is Verdict.FITS_DEGRADED
    assert result.ok, "FITS_DEGRADED loads; callers must not treat it as failure"
    assert result.predicted_decode_tps < DEGRADED_TPS_THRESHOLD
    assert result.limiting_term == "bandwidth"
    assert "bandwidth bound" in result.reason.lower()
    assert "adding nodes will not fix" in result.reason.lower()


def test_gpt_oss_120b_fits_one_spark_and_is_fast(fit):
    """116.8B total at 4.25 bits fits, and 5.1B active is why it is an order of
    magnitude faster than a dense 70B on the same box."""
    result = fit.check(request(GPT_OSS_120B, context=32768, seqs=16), ONE_SPARK)

    assert result.verdict is Verdict.FITS
    assert result.predicted_decode_tps > DEGRADED_TPS_THRESHOLD * 2

    dense_equivalent = fit.check(
        request(LLAMA_70B_FP8, context=32768, seqs=16), ONE_SPARK
    )
    assert (
        result.predicted_decode_tps > 10 * dense_equivalent.predicted_decode_tps
    ), "active params, not total, are what drive decode speed"


def test_gqa_kv_uses_kv_heads_not_attention_heads():
    """The most common error in the field. Llama 3.3 70B has 64 query heads
    over 8 KV heads, so the mistake over-charges by exactly 8x."""
    correct = kv_bytes_per_token(LLAMA_3_3_70B, "bf16")
    mistaken = kv_bytes_per_token(
        dataclasses.replace(
            LLAMA_3_3_70B, num_kv_heads=LLAMA_3_3_70B.num_attention_heads
        ),
        "bf16",
    )
    grouping_ratio = LLAMA_3_3_70B.num_attention_heads / LLAMA_3_3_70B.num_kv_heads

    assert grouping_ratio == 8
    assert mistaken == pytest.approx(correct * grouping_ratio)


def test_sliding_window_is_below_the_naive_full_cache():
    """GPT-OSS windows 18 of its 36 layers at 128 tokens. Charging the full
    context on every layer over-estimates by multiples."""
    context = 32768
    windowed = kv_cache_bytes(GPT_OSS_120B, context, batch=1, kv_dtype="bf16")
    naive_full = kv_bytes_per_token(GPT_OSS_120B, "bf16") * context

    assert windowed < naive_full
    # 18 full layers at 32768 plus 18 windowed at 128, against 36 x 32768.
    expected_ratio = (18 * context + 18 * 128) / (36 * context)
    assert windowed / naive_full == pytest.approx(expected_ratio)
    assert windowed / naive_full < 0.51


def test_mla_kv_is_an_order_of_magnitude_below_the_head_wise_equivalent():
    """DeepSeek caches one compressed latent per layer per token, not per-head
    K and V."""
    mla = kv_bytes_per_token(DEEPSEEK_V3, "bf16")
    head_wise = kv_bytes_per_token(
        dataclasses.replace(DEEPSEEK_V3, mla_latent_dim=None), "bf16"
    )

    assert head_wise / mla >= 10, "MLA should be at least an order of magnitude cheaper"


def test_mla_kv_charges_the_decoupled_rope_component_alongside_the_latent():
    """The latent alone under-counts DeepSeek-family caches by about 11
    percent, in the OOM direction: ``qk_rope_head_dim`` (64 here, via
    ``mla_rope_dim``) is cached per token alongside the 512-wide latent, not
    folded into it. Before this fix, per-token-per-layer bytes were
    ``512 * elem`` (1024 bytes at bf16); charging the latent plus the RoPE
    component makes it ``576 * elem`` (1152 bytes), a 12.5% increase -- the
    complementary 512/576 = 88.9% is the ~11% under-count the audit found.
    The head-wise-versus-MLA order-of-magnitude margin still comfortably
    holds: 65536 / 1152 is about 56x, not the roughly 64x it would be against
    the latent alone."""
    assert DEEPSEEK_V3.mla_rope_dim == 64
    assert DEEPSEEK_V3.effective_mla_rope_dim == 64

    per_layer_per_token = kv_bytes_per_token(DEEPSEEK_V3, "bf16") / DEEPSEEK_V3.num_layers
    assert per_layer_per_token == pytest.approx((512 + 64) * 2)  # bf16 = 2 bytes/elem

    latent_alone = 512 * 2
    assert per_layer_per_token / latent_alone == pytest.approx(576 / 512)

    head_wise = kv_bytes_per_token(
        dataclasses.replace(DEEPSEEK_V3, mla_latent_dim=None), "bf16"
    ) / DEEPSEEK_V3.num_layers
    assert head_wise == pytest.approx(65536)
    assert head_wise / per_layer_per_token == pytest.approx(65536 / 1152)
    assert head_wise / per_layer_per_token >= 10


# --------------------------------------------------------------------------
# every refusal is actionable
# --------------------------------------------------------------------------

ACTIONABLE = (
    "node",
    "nodes",
    "requantize",
    "drop context",
    "reduce concurrency",
    "quantize the kv cache",
    "replan",
)

REFUSAL_CASES = [
    ("70b bf16, one spark", request(LLAMA_3_3_70B), ONE_SPARK),
    (
        "70b fp8, tp2, 128k x 32",
        request(
            LLAMA_70B_FP8,
            context=131072,
            seqs=32,
            kv_dtype="bf16",
            plan=make_plan(tp=2, node_ids=("spark-01", "spark-02")),
        ),
        TWO_SPARKS,
    ),
    (
        "gpt-oss at 256k x 64",
        request(GPT_OSS_120B, context=262144, seqs=64, kv_dtype="bf16"),
        ONE_SPARK,
    ),
    (
        "30b-a3b at 128k x 32",
        request(QWEN3_30B_A3B, context=131072, seqs=32, kv_dtype="bf16"),
        ONE_SPARK,
    ),
    ("deepseek fp8, one spark", request(DEEPSEEK_V3), ONE_SPARK),
    (
        "deepseek fp8, tp2 + ep2",
        request(
            DEEPSEEK_V3, plan=make_plan(tp=2, ep=2, node_ids=("spark-01", "spark-02"))
        ),
        TWO_SPARKS,
    ),
    ("70b fp8 on a 3090", request(LLAMA_70B_FP8), [WS_3090]),
    ("gpt-oss on a 3090", request(GPT_OSS_120B), [WS_3090]),
    (
        "deepseek tp6 at 8192 sequences",
        request(DEEPSEEK_V3, context=4096, seqs=8192, plan=make_plan(tp=6)),
        [SPARK_01, SPARK_02, SPARK_01, SPARK_02, SPARK_01, SPARK_02],
    ),
]


@pytest.mark.parametrize(
    "label,req,nodes", REFUSAL_CASES, ids=[c[0] for c in REFUSAL_CASES]
)
def test_every_refusal_names_a_term_and_a_fix(fit, label, req, nodes):
    result = fit.check(req, nodes)
    assert result.verdict is Verdict.WONT_FIT, f"{label} was expected to refuse"

    assert result.limiting_term, "a refusal with no limiting term has failed"
    assert result.limiting_term in ("weights", "kv_cache", "bandwidth", "combined")
    assert "GiB" in result.reason, "a refusal has to quantify the overflow"
    assert any(
        hint in result.reason.lower() for hint in ACTIONABLE
    ), f"{label}: no actionable change in {result.reason!r}"
    assert len(result.reason) > 60, "a reason that says only 'does not fit' has failed"
    assert result.headroom < 0


@pytest.mark.parametrize(
    "label,req,nodes", REFUSAL_CASES, ids=[c[0] for c in REFUSAL_CASES]
)
def test_kv_limited_refusals_round_trip_their_context_suggestion(
    fit, label, req, nodes
):
    """Never report a context suggestion we have not verified fits."""
    result = fit.check(req, nodes)
    if result.limiting_term != "kv_cache":
        pytest.skip(f"{label} is limited by {result.limiting_term}")

    suggested = result.max_context_that_fits
    assert suggested is not None and suggested > 0
    assert str(suggested) in result.reason

    replayed = fit.check(
        dataclasses.replace(req, context_length=suggested), nodes
    )
    assert replayed.ok, f"{label}: suggested {suggested} tokens and it did not fit"


def test_70b_bf16_fits_across_two_sparks(fit):
    """The model that refuses on one node loads on two. The gate has to let
    that through, and still say it will be slow."""
    result = fit.check(
        request(
            LLAMA_3_3_70B,
            context=8192,
            seqs=16,
            plan=make_plan(tp=2, node_ids=("spark-01", "spark-02")),
        ),
        TWO_SPARKS,
    )
    assert result.ok
    assert result.verdict is Verdict.FITS_DEGRADED
    assert result.breakdown.comm_buffers == COMM_BUFFER_BYTES


def test_the_demo_plan_fits(fit):
    """PP=2 over the measured 10.2 GB/s link: what the planner recommends and
    the fit gate has to clear."""
    from tests.fixtures import pp2_plan

    result = fit.check(
        request(LLAMA_70B_FP8, context=8192, seqs=16, kv_dtype="fp8", plan=pp2_plan()),
        TWO_SPARKS,
    )
    assert result.ok
    assert result.breakdown.weights == pytest.approx(
        LLAMA_3_3_70B.total_params * 1.0 / 2, rel=1e-9
    )


def test_combined_refusal_lists_every_term(fit):
    """When no single term dominates, naming one of them would be a lie."""
    result = fit.check(
        request(DEEPSEEK_V3, context=4096, seqs=8192, plan=make_plan(tp=6)),
        [SPARK_01, SPARK_02] * 3,
    )
    assert result.verdict is Verdict.WONT_FIT
    assert result.limiting_term == "combined"
    for term in ("weights", "KV", "activations", "comm buffers", "framework"):
        assert term in result.reason

    # KV is 90 percent of this budget. Saying "no single term dominates" here
    # would be a lie, so the reason names it and explains why cutting it alone
    # is not enough.
    assert "KV is the biggest term" in result.reason
    assert "will not close the gap" in result.reason
    assert "reduce concurrency" in result.reason


def test_deepseek_two_sparks_guardrail_derivation_is_not_a_literal(fit):
    """Brief D acceptance #2: DeepSeek-V3 at fp8 on two Sparks is WONT_FIT on
    weights, needing a minimum of 7 nodes. The overflow figure has to be
    derived from ``GB10_ADDRESSABLE * guardrail``, never a copy-pasted
    literal -- so changing the guardrail on a fresh calculator must move the
    percentage and the usable-GiB figure the reason names, and the reason
    string as a whole must change with it."""
    req = request(DEEPSEEK_V3, context=8192, seqs=16)

    result = fit.check(req, TWO_SPARKS)
    assert result.verdict is Verdict.WONT_FIT
    assert result.limiting_term == "weights"
    assert min_nodes_required(DEEPSEEK_V3, SPARK_01, 8192, 16) == 7
    assert "7 nodes" in result.reason

    stricter = FitCalculator(guardrail=0.80)  # 80% is a smaller usable pool than 90%
    changed = stricter.check(req, TWO_SPARKS)

    assert changed.verdict is Verdict.WONT_FIT
    assert changed.limiting_term == "weights"
    assert changed.reason != result.reason
    assert result.usable_per_node != changed.usable_per_node
    # The percentage named in the reason moves with the guardrail...
    assert "90%" in result.reason
    assert "80%" in changed.reason
    # ...and so does the usable-GiB figure it is derived from, computed the
    # same way the reason string computes it (never re-typed as a literal).
    assert f"{result.usable_per_node / GIB:.1f} GiB" in result.reason
    assert f"{changed.usable_per_node / GIB:.1f} GiB" in changed.reason


def test_max_context_is_the_largest_that_fits(fit):
    """One rounding step past the suggestion must fail, or we are being
    needlessly conservative."""
    plan = make_plan()
    ctx = fit.max_context(QWEN3_30B_A3B, plan, ONE_SPARK, max_seqs=32, kv_dtype="bf16")

    assert ctx > 0 and ctx % 512 == 0
    assert fit.check(request(QWEN3_30B_A3B, ctx, 32, "bf16", plan), ONE_SPARK).ok
    assert not fit.check(
        request(QWEN3_30B_A3B, ctx + 512, 32, "bf16", plan), ONE_SPARK
    ).ok


def test_max_context_is_zero_when_the_weights_alone_do_not_fit(fit):
    assert fit.max_context(LLAMA_3_3_70B, make_plan(), ONE_SPARK, 16, "bf16") == 0


def test_max_context_that_fits_is_none_not_zero_when_weights_alone_wont_fit(fit):
    """0 is a context we verified fits. When weights alone (plus anything
    replicated) already blow the budget -- the "context and concurrency
    cannot fix this" refusal -- there is no such context, and the contract
    sentinel for that is ``None``, never 0."""
    result = fit.check(request(LLAMA_3_3_70B, context=8192, seqs=16), ONE_SPARK)

    assert result.verdict is Verdict.WONT_FIT
    assert result.limiting_term == "weights"
    assert result.max_context_that_fits is None
    assert "context and concurrency cannot fix this" in result.reason.lower()


@pytest.mark.parametrize(
    "label,req,nodes", REFUSAL_CASES, ids=[c[0] for c in REFUSAL_CASES]
)
def test_refusals_report_none_not_zero_when_no_context_helps(fit, label, req, nodes):
    """0 is a context we verified fits. Every refusal's
    ``max_context_that_fits`` must therefore be either ``None`` (no context,
    not even zero tokens of cache, was found to fit) or a positive, verified
    context -- never a bare 0 -- and this has to hold regardless of which
    term the diagnosis names. A ``combined`` refusal where nothing fits is
    exactly as unhelpful a "try 0 tokens" suggestion as a ``weights`` one, so
    unlike the narrower predecessor of this test, nothing here is skipped."""
    result = fit.check(req, nodes)
    assert result.verdict is Verdict.WONT_FIT, f"{label} was expected to refuse"
    assert result.max_context_that_fits != 0, (
        f"{label} ({result.limiting_term}): max_context_that_fits must be "
        f"None, not 0, when no context is being verified to fit"
    )
    if result.max_context_that_fits is not None:
        assert result.max_context_that_fits > 0


def test_weights_limited_refusals_report_none_not_zero(fit):
    """The narrow case the earlier version of this test covered, kept as a
    direct regression check: weights alone (plus anything replicated) blow
    the budget, so no context -- not even zero tokens -- can help."""
    result = fit.check(request(LLAMA_3_3_70B, context=8192, seqs=16), ONE_SPARK)
    assert result.limiting_term == "weights"
    assert result.max_context_that_fits is None


def test_combined_limited_refusal_also_reports_none_not_zero(fit):
    """The instance of this defect that survived the first pass: a refusal
    diagnosed as ``combined`` (not ``weights``) where nothing fits still has
    to report ``None``, not 0. 'deepseek tp6 at 8192 sequences' from
    REFUSAL_CASES is exactly this case."""
    result = fit.check(
        request(DEEPSEEK_V3, context=4096, seqs=8192, plan=make_plan(tp=6)),
        [SPARK_01, SPARK_02, SPARK_01, SPARK_02, SPARK_01, SPARK_02],
    )
    assert result.verdict is Verdict.WONT_FIT
    assert result.limiting_term == "combined"
    assert result.max_context_that_fits is None
    assert result.headroom < 0
    original_req = request(DEEPSEEK_V3, context=4096, seqs=8192, plan=make_plan(tp=6))
    replay_at_zero = fit.check(
        dataclasses.replace(original_req, context_length=0),
        [SPARK_01, SPARK_02, SPARK_01, SPARK_02, SPARK_01, SPARK_02],
    )
    # Proof the 0 that used to be reported here was a lie: replaying at the
    # context it would have suggested does NOT fit.
    assert not replay_at_zero.ok


# --------------------------------------------------------------------------
# the traps
# --------------------------------------------------------------------------


def test_pipeline_charges_the_busiest_stage_not_the_average():
    """80 layers over 3 stages is 27/27/26. The node that OOMs holds 27."""
    assert stage_fraction(LLAMA_3_3_70B, 3) == pytest.approx(27 / 80)
    assert stage_fraction(LLAMA_3_3_70B, 3) > 1 / 3

    uneven = weight_bytes_per_rank(LLAMA_3_3_70B, make_plan(pp=3))
    naive_even = LLAMA_3_3_70B.total_params * LLAMA_3_3_70B.bytes_per_param() / 3
    assert uneven > naive_even

    # Even splits stay exact.
    assert stage_fraction(LLAMA_3_3_70B, 2) == pytest.approx(0.5)


class TestExpertParallelDividesRoutedWeights:
    """Until 2026-09-11 ``expert_parallel`` divided nothing.

    ``weight_bytes_per_rank`` divided by tensor parallel and the pipeline
    stage fraction and by nothing else, so an EP plan was charged the whole
    checkpoint on every rank -- while ``deploy/recipes.py`` passed
    ``--enable-expert-parallel`` on exactly those plans and the engine really
    did place ``num_experts/ep`` of the experts per rank. Nothing in the suite
    related ``breakdown.weights`` to ``expert_parallel`` in either direction,
    so the over-count was invisible.
    """

    def test_expert_parallel_divides_the_routed_share(self):
        """Measured on the real thing: DeepSeek-V4-Flash was charged 276.0 GiB
        per rank at ep=8 across eight nodes and 276.0 GiB at ep=1 on one --
        byte-identical."""
        whole = weight_bytes_per_rank(DEEPSEEK_V3, make_plan())
        sharded = weight_bytes_per_rank(
            DEEPSEEK_V3, make_plan(ep=8, dp=8, node_ids=tuple(f"n{i}" for i in range(8)))
        )
        assert sharded < whole, "expert parallel must divide the routed experts"

        # Exactly: the dense remainder whole, the routed share over ep.
        routed_fraction = (
            DEEPSEEK_V3.routed_expert_params / DEEPSEEK_V3.total_params
        )
        expected = whole * (1 - routed_fraction) + whole * routed_fraction / 8
        assert sharded == pytest.approx(expected)

    def test_a_dense_model_is_untouched_by_expert_parallel(self):
        """The field is 0 on a dense checkpoint, and 0 must divide nothing."""
        assert LLAMA_3_3_70B.routed_expert_params == 0
        plan_ids = tuple(f"n{i}" for i in range(8))
        assert weight_bytes_per_rank(
            LLAMA_3_3_70B, make_plan(ep=8, dp=8, node_ids=plan_ids)
        ) == weight_bytes_per_rank(LLAMA_3_3_70B, make_plan())

    def test_an_unsized_expert_split_charges_the_experts_whole(self):
        """0 means NOT DERIVED, never "no experts", and it has to degrade in
        the refusing direction -- which is the behaviour every model had
        before the field existed."""
        unsized = dataclasses.replace(DEEPSEEK_V3, routed_expert_params=0)
        plan_ids = tuple(f"n{i}" for i in range(8))
        assert weight_bytes_per_rank(
            unsized, make_plan(ep=8, dp=8, node_ids=plan_ids)
        ) == weight_bytes_per_rank(unsized, make_plan())

    def test_tensor_parallel_is_never_double_counted_against_expert_parallel(self):
        """``max(tp, ep)``, not ``tp * ep``. A gate may be too strict and may
        not be too generous, and the planner emits only ``tp>1, ep=1`` or
        ``tp=1, ep=world`` -- so on anything else this under-divides."""
        plan_ids = ("n0", "n1", "n2", "n3")
        both = weight_bytes_per_rank(DEEPSEEK_V3, make_plan(tp=2, ep=2, node_ids=plan_ids))
        tp_only = weight_bytes_per_rank(DEEPSEEK_V3, make_plan(tp=2, node_ids=plan_ids))
        assert both == tp_only, "ep below tp must not divide a second time"


class TestAWeightsRefusalNamesItsSplit:
    """"weights alone are 141.2 GiB per rank" reads identically whether that is
    a third of the checkpoint or the whole of it, so "it needs three nodes" and
    "it needs two nodes and we counted twice" were indistinguishable."""

    def _refusal(self, shape, plan, nodes):
        result = FitCalculator().check(
            request(shape, context=8192, seqs=1, plan=plan), list(nodes)
        )
        assert result.verdict is Verdict.WONT_FIT
        assert result.limiting_term == "weights"
        return result.reason

    def test_a_one_kv_head_checkpoint_says_tensor_parallel_is_impossible(self):
        """DeepSeek-V4-Flash publishes ONE KV head, so no TP degree above 1
        divides both head counts -- it is charged whole correctly, and nothing
        on screen used to say so."""
        one_head = dataclasses.replace(
            DEEPSEEK_V3, num_kv_heads=1, num_attention_heads=64, mla_latent_dim=None
        )
        reason = self._refusal(
            one_head,
            make_plan(dp=2, node_ids=("spark-01", "spark-02")),
            (SPARK_01, SPARK_02),
        )
        assert "1 KV head" in reason
        assert "no tensor-parallel degree above 1" in reason

    def test_a_split_plan_names_the_degrees_it_used(self):
        reason = self._refusal(
            DEEPSEEK_V3,
            make_plan(tp=2, node_ids=("spark-01", "spark-02")),
            (SPARK_01, SPARK_02),
        )
        assert "Charged at TP 2 x PP 1" in reason

    def test_data_parallel_says_it_replicates(self):
        """Adding data-parallel ranks is the fix an operator reaches for, and
        it is the one fix that cannot work: every rank holds a full copy."""
        reason = self._refusal(
            LLAMA_3_3_70B,
            make_plan(dp=2, node_ids=("spark-01", "spark-02")),
            (SPARK_01, SPARK_02),
        )
        assert "replicates the model" in reason

    def test_the_overage_says_it_is_not_weights_alone(self):
        """``over`` is the whole six-term overage interpolated into a sentence
        whose subject is weights, which is why the two numbers never
        reconciled by hand."""
        reason = self._refusal(
            LLAMA_3_3_70B, make_plan(), (SPARK_01,)
        )
        assert "not by weights alone" in reason


def test_expert_parallel_buffers_are_charged():
    """The term other planners forget, which is why their configurations OOM on
    the first batch."""
    with_ep, warnings = memory_breakdown(
        QWEN3_30B_A3B, make_plan(tp=2, ep=2), 8192, 16, "auto"
    )
    without_ep, _ = memory_breakdown(
        QWEN3_30B_A3B, make_plan(tp=2), 8192, 16, "auto"
    )

    assert without_ep.comm_buffers == COMM_BUFFER_BYTES
    assert with_ep.comm_buffers == COMM_BUFFER_BYTES + EP_EXTRA_BUFFER_BYTES
    assert any("expert parallel" in w for w in warnings), "EP surcharge must be said out loud"


def test_single_node_pays_no_comm_buffers():
    breakdown, _ = memory_breakdown(QWEN3_30B_A3B, make_plan(), 8192, 16, "auto")
    assert breakdown.comm_buffers == 0
    assert breakdown.framework_overhead == FRAMEWORK_OVERHEAD


def test_fits_degraded_is_not_a_failure(fit):
    result = fit.check(request(LLAMA_70B_FP8, context=4096, seqs=16), ONE_SPARK)
    assert result.verdict is Verdict.FITS_DEGRADED
    assert result.ok
    assert result.headroom > 0


def test_tensor_parallel_cannot_shard_past_the_kv_head_count():
    """A runtime asked for TP=8 on 4 KV heads replicates them. Assuming an 8x
    split is how a fit check passes and the load OOMs."""
    plan = make_plan(tp=8)
    assert QWEN3_30B_A3B.num_kv_heads == 4
    assert kv_divisor(QWEN3_30B_A3B, plan) == 4

    _, warnings = memory_breakdown(QWEN3_30B_A3B, plan, 8192, 16, "auto")
    assert any("KV heads" in w for w in warnings)


def test_mla_cache_is_not_sharded_by_tensor_parallel():
    """There is one latent per token; there are no heads to split."""
    assert kv_divisor(DEEPSEEK_V3, make_plan(tp=8)) == 1
    assert kv_divisor(DEEPSEEK_V3, make_plan(tp=8, pp=2)) == pytest.approx(
        1 / stage_fraction(DEEPSEEK_V3, 2)
    )


def test_kv_and_weight_sharding_multiply():
    plan = make_plan(tp=2, pp=2)
    assert kv_divisor(LLAMA_3_3_70B, plan) == pytest.approx(4.0)


# --------------------------------------------------------------------------
# term-level behaviour
# --------------------------------------------------------------------------


def test_logits_are_charged_per_emitted_token_not_per_context():
    """One token per sequence per decode step. Charging the context would
    inflate this by four orders of magnitude."""
    short = activation_bytes(GPT_OSS_120B, max_seqs=16, context=2048)
    long = activation_bytes(GPT_OSS_120B, max_seqs=16, context=131072)
    assert short == long

    doubled = activation_bytes(GPT_OSS_120B, max_seqs=32, context=2048)
    assert doubled > long
    assert doubled - long == GPT_OSS_120B.vocab_size * 16 * 4


def test_fp8_kv_cache_halves_a_bf16_cache():
    bf16 = kv_cache_bytes(LLAMA_3_3_70B, 8192, 16, "bf16")
    fp8 = kv_cache_bytes(LLAMA_3_3_70B, 8192, 16, "fp8")
    assert fp8 == pytest.approx(bf16 / 2)


def test_unknown_kv_dtype_is_charged_conservatively_and_warned():
    _, warnings = memory_breakdown(LLAMA_3_3_70B, make_plan(), 8192, 16, "fp3_maybe")
    assert any("unknown KV cache dtype" in w for w in warnings)
    assert kv_cache_bytes(LLAMA_3_3_70B, 8192, 16, "fp3_maybe") == kv_cache_bytes(
        LLAMA_3_3_70B, 8192, 16, "bf16"
    )


def test_vision_towers_are_replicated_not_split():
    """A vision tower lives whole on every rank; splitting it under-charges."""
    vision = dataclasses.replace(
        LLAMA_3_3_70B, total_params=90_000_000_000, vision_params=20_000_000_000
    )
    breakdown, _ = memory_breakdown(vision, make_plan(tp=4), 8192, 8, "auto")

    assert breakdown.replicated == int(20_000_000_000 * 2)
    assert breakdown.weights == int(70_000_000_000 * 2 / 4)


def test_vision_towers_with_measured_weight_bytes_still_split_correctly():
    """The one branch the H-10 spec explicitly called out as needing a
    guard -- ``max(0.0, total_weight_bytes - vision_bytes)`` -- exercised
    with a real vision-bearing shape, not just probed by hand. The vision
    share is still priced from ``vision_params * bytes_per_param()`` and
    subtracted out of the measured total; only the shardable remainder
    should move."""
    vision = dataclasses.replace(
        LLAMA_3_3_70B, total_params=90_000_000_000, vision_params=20_000_000_000
    )
    plan = make_plan(tp=4)
    vision_bytes = 20_000_000_000 * 2  # bf16

    formula, _ = memory_breakdown(vision, plan, 8192, 8, "auto")
    measured_total = int(weight_bytes_per_rank(vision, plan)) * 4 + vision_bytes
    measured_total += 3 * GIB  # the resolver's on-disk gap, distributed over tp
    measured, _ = memory_breakdown(
        vision, plan, 8192, 8, "auto", weight_bytes=measured_total
    )

    # Replicated (vision) share is untouched -- it is priced from
    # vision_params, never from the measured total.
    assert measured.replicated == formula.replicated == int(20_000_000_000 * 2)
    # Only the shardable remainder absorbs the +3 GiB, divided by tp.
    assert measured.weights - formula.weights == 3 * GIB // 4

    # The guard: a measured total at or below the vision share alone must
    # floor the shardable part at zero rather than go negative.
    starved = weight_bytes_per_rank(vision, plan, total_weight_bytes=1)
    assert starved == 0.0
    at_vision_share = weight_bytes_per_rank(
        vision, plan, total_weight_bytes=vision_bytes - 1
    )
    assert at_vision_share == 0.0


def test_min_nodes_required_and_nodes_phrase_use_the_measured_basis():
    """H-10: a refusal's "it needs N nodes" fix has to be computed on the
    same weight basis as the refusal itself, or the suggested node count can
    be one that does not actually hold the measured checkpoint. LLAMA_3_3_70B
    plus 60 GiB of measured overhead needs a 3rd node; the formula-only
    search would (wrongly) still say 2."""
    full_formula_bytes = weight_bytes_per_rank(LLAMA_3_3_70B, make_plan(tp=1, pp=1))
    measured_bytes = int(full_formula_bytes) + 60 * GIB

    formula_n = min_nodes_required(LLAMA_3_3_70B, SPARK_01, 8192, 16)
    measured_n = min_nodes_required(
        LLAMA_3_3_70B, SPARK_01, 8192, 16, weight_bytes=measured_bytes
    )
    assert formula_n == 2
    assert measured_n == 3
    assert measured_n != formula_n

    # And the reason string threads the same basis through _nodes_phrase:
    # the suggested node count must actually hold the measured checkpoint,
    # not the (smaller, formula-only) count that would be wrong here.
    fit = FitCalculator()
    req = request(LLAMA_3_3_70B, context=8192, seqs=16, weight_bytes=measured_bytes)
    result = fit.check(req, ONE_SPARK)
    assert result.verdict is Verdict.WONT_FIT
    assert f"{measured_n} nodes" in result.reason
    assert f"{formula_n} nodes" not in result.reason


# --------------------------------------------------------------------------
# H-10: the resolver's measured weight_bytes beats the dtype formula
# --------------------------------------------------------------------------


def test_weight_bytes_shifts_the_breakdown_by_exactly_the_measured_gap():
    """NOTES.md: the resolver's measured on-disk figure beats
    ``total_params * bytes_per_param()`` -- on GPT-OSS-120B that gap is
    close to 3 GiB, "the difference between fitting and not". Every other
    term must be untouched."""
    plan = make_plan()
    formula, _ = memory_breakdown(GPT_OSS_120B, plan, 32768, 16, "auto")
    measured_bytes = int(weight_bytes_per_rank(GPT_OSS_120B, plan)) + 3 * GIB
    measured, _ = memory_breakdown(
        GPT_OSS_120B, plan, 32768, 16, "auto", weight_bytes=measured_bytes
    )

    assert measured.weights - formula.weights == 3 * GIB
    assert measured.total - formula.total == 3 * GIB
    assert measured.kv_cache == formula.kv_cache
    assert measured.activations == formula.activations
    assert measured.comm_buffers == formula.comm_buffers
    assert measured.replicated == formula.replicated
    assert measured.framework_overhead == formula.framework_overhead


def test_weight_bytes_none_or_non_positive_behaves_exactly_as_before(fit):
    """``None`` -- and anything <= 0 -- must be pure no-ops: the formula
    path, unchanged."""
    plan = make_plan()
    explicit_none, _ = memory_breakdown(
        GPT_OSS_120B, plan, 32768, 16, "auto", weight_bytes=None
    )
    omitted, _ = memory_breakdown(GPT_OSS_120B, plan, 32768, 16, "auto")
    zero, _ = memory_breakdown(GPT_OSS_120B, plan, 32768, 16, "auto", weight_bytes=0)
    negative, _ = memory_breakdown(
        GPT_OSS_120B, plan, 32768, 16, "auto", weight_bytes=-5
    )
    assert explicit_none == omitted == zero == negative

    baseline = fit.check(request(GPT_OSS_120B, 32768, 16, plan=plan), ONE_SPARK)
    with_none = fit.check(
        request(GPT_OSS_120B, 32768, 16, plan=plan, weight_bytes=None), ONE_SPARK
    )
    assert with_none.breakdown == baseline.breakdown
    assert with_none.verdict == baseline.verdict
    assert with_none.reason == baseline.reason


def test_weight_bytes_can_flip_a_near_boundary_verdict(fit):
    """Not a paper 3 GiB shift somewhere with headroom to spare -- close
    enough to the budget that the measured figure alone moves the verdict,
    exactly the GPT-OSS-120B scenario NOTES.md warns about."""
    plan = make_plan()
    formula_weights = weight_bytes_per_rank(GPT_OSS_120B, plan)
    baseline, _ = memory_breakdown(GPT_OSS_120B, plan, 32768, 16, "auto")
    # A node with 1.5 GiB of headroom over the formula total: the formula
    # fits comfortably, and the +3 GiB measured figure alone is enough to
    # push it over.
    usable_target = baseline.total + int(1.5 * GIB)
    node = dataclasses.replace(
        SPARK_01, addressable_memory=int(usable_target / fit.guardrail)
    )

    formula_result = fit.check(request(GPT_OSS_120B, 32768, 16, plan=plan), [node])
    assert formula_result.verdict is Verdict.FITS
    assert formula_result.headroom > 0

    measured_result = fit.check(
        request(
            GPT_OSS_120B,
            32768,
            16,
            plan=plan,
            weight_bytes=int(formula_weights) + 3 * GIB,
        ),
        [node],
    )
    assert measured_result.verdict is Verdict.WONT_FIT
    assert measured_result.headroom < 0
    assert measured_result.limiting_term
    assert measured_result.reason != formula_result.reason


def test_usable_memory_is_the_guardrail_not_the_nameplate(fit):
    """107.7 GiB of the 119.7 addressable, not 128."""
    result = fit.check(request(QWEN3_30B_A3B, 4096, 8), ONE_SPARK)
    assert result.usable_per_node == int(SPARK_01.addressable_memory * 0.90)
    assert result.usable_per_node / GIB == pytest.approx(107.7, abs=0.1)
    assert result.usable_per_node < 108 * GIB


def test_heterogeneous_cluster_budgets_against_the_smallest_node(fit):
    result = fit.check(
        request(QWEN3_30B_A3B, 4096, 8, plan=make_plan(tp=2)),
        [SPARK_01, WS_3090],
    )
    assert result.usable_per_node == WS_3090.usable_memory(0.90)
    assert any("not identical" in w for w in result.warnings)


def test_plan_wanting_more_ranks_than_gpus_is_refused(fit):
    result = fit.check(request(QWEN3_30B_A3B, 4096, 8, plan=make_plan(tp=4)), TWO_SPARKS)
    assert result.verdict is Verdict.WONT_FIT
    assert "4 ranks" in result.reason
    assert result.limiting_term


def test_check_needs_at_least_one_node(fit):
    with pytest.raises(ValueError):
        fit.check(request(QWEN3_30B_A3B), [])


# --------------------------------------------------------------------------
# exports Agent E depends on
# --------------------------------------------------------------------------


def test_min_nodes_required_grows_with_context():
    small = min_nodes_required(LLAMA_3_3_70B, SPARK_01, 4096, 8)
    large = min_nodes_required(LLAMA_3_3_70B, SPARK_01, 131072, 32)
    assert small == 2
    assert large > small


def test_min_nodes_required_returns_one_when_it_already_fits():
    assert min_nodes_required(QWEN3_30B_A3B, SPARK_01, 8192, 16) == 1


def test_min_nodes_required_reports_impossible_rather_than_lying():
    tiny = dataclasses.replace(WS_3090, addressable_memory=2 * GIB)
    assert min_nodes_required(DEEPSEEK_V3, tiny, 131072, 64) == -1


def test_min_nodes_required_never_counts_an_illegal_tp_shard():
    """The candidate search used to try every TP degree that divides the node
    count, with no check that the shard is legal. On a 1-KV-head model that
    reported min_nodes=4 via an impossible TP=4 shard (num_kv_heads % tp !=
    0, replicated in the runtime, not split -- exactly what
    ``planner/legality.py``'s ``valid_tp_degrees`` exists to forbid). With
    the head-divisibility check, this shape has no legal shard at any node
    count up to ``MAX_SEARCH_NODES`` -- 1 layer caps pipeline parallel at
    PP=1, and 1 KV head caps tensor parallel at TP=1 -- so the honest answer
    is -1, not a count reached by cheating the shard."""
    no_legal_shard = ModelShape(
        model_id="test/no-legal-shard",
        num_layers=1,
        hidden_size=4096,
        num_attention_heads=4,
        num_kv_heads=1,
        vocab_size=32000,
        total_params=200_000_000_000,
        dtype="bf16",
    )
    assert min_nodes_required(no_legal_shard, SPARK_01, 4096, 8) == -1


def test_predict_decode_tps_uses_active_params():
    """5.1B active is why GPT-OSS-120B outruns a dense 70B on the same box."""
    moe = predict_decode_tps(GPT_OSS_120B, 273.0, kv_read_bytes=0)
    dense_120b = predict_decode_tps(
        dataclasses.replace(GPT_OSS_120B, active_params=None), 273.0, kv_read_bytes=0
    )
    assert moe > 20 * dense_120b


def test_predict_decode_tps_falls_off_as_the_cache_grows():
    empty = predict_decode_tps(LLAMA_3_3_70B, 273.0, 0)
    loaded = predict_decode_tps(LLAMA_3_3_70B, 273.0, 40e9)
    assert loaded < empty


def test_the_reported_decode_figure_is_a_range_and_the_ends_bracket_reality(fit):
    """Decode reads the cache for the tokens actually PRESENT, so one number
    cannot describe both a fresh request and one that has filled the context.

    Measured on a real GB10: the fit gate predicted 61.5 tok/s for Qwen3-0.6B at
    8192 context and the engine's own counters reported 120.3 over ~320-token
    requests. The prediction was not wrong about full context -- it was answering
    a question nobody asked. Both ends now ship, and a mid-cache rate has to sit
    between them or the range is not a range.
    """
    result = fit.check(request(GPT_OSS_120B, context=32768, seqs=1), ONE_SPARK)
    low = result.predicted_decode_tps
    high = result.predicted_decode_tps_empty
    assert low is not None and high is not None
    assert high > low, "an empty cache decodes faster than a full one"

    mid = predict_decode_tps(
        GPT_OSS_120B, 273.0, kv_cache_bytes(GPT_OSS_120B, 16384, 1, "auto")
    )
    assert low < mid < high, "half the context lands inside the range"


def test_the_low_end_is_exactly_what_shipped_before_the_range(fit):
    """The additive half of the change, pinned.

    `predicted_decode_tps` still charges the cache at the FULL requested
    context, byte for byte as before, because the degraded threshold and the
    routing strength rung both read it and neither should move for a display
    change. Only the second, faster end is new.
    """
    for context in (2048, 8192, 32768):
        result = fit.check(request(GPT_OSS_120B, context=context, seqs=1), ONE_SPARK)
        expected = predict_decode_tps(
            GPT_OSS_120B, 273.0, kv_cache_bytes(GPT_OSS_120B, context, 1, "auto")
        )
        assert result.predicted_decode_tps == pytest.approx(expected, abs=1e-9)


def test_the_spread_widens_with_context_rather_than_being_a_constant(fit):
    """Why the old single figure was wrong by a VARYING amount, not a fixed one.

    The gap between the two ends is the cache term, so it grows with the context
    asked for. Measured: 1.96x at 8192 and 2.85x at 40960 on the same hardware.
    A single corrective constant could never have fixed this.
    """
    narrow = fit.check(request(GPT_OSS_120B, context=2048, seqs=1), ONE_SPARK)
    wide = fit.check(request(GPT_OSS_120B, context=131072, seqs=1), ONE_SPARK)
    assert (
        wide.predicted_decode_tps_empty / wide.predicted_decode_tps
        > narrow.predicted_decode_tps_empty / narrow.predicted_decode_tps
    )


def test_a_refusal_still_says_how_fast_it_would_have_been(fit):
    """Both ends ride every verdict, refusals included -- a plan that will not
    fit is exactly when somebody wants to know what they would have got."""
    result = fit.check(request(LLAMA_3_3_70B, context=8192, seqs=16), ONE_SPARK)
    assert result.verdict is Verdict.WONT_FIT
    assert result.predicted_decode_tps is not None
    assert result.predicted_decode_tps_empty is not None


def test_the_range_states_both_ends_and_the_context_they_belong_to(fit):
    """The string is the product. It has to name the context the slow end is
    for, or the two numbers are a spread with no units on the spread."""
    result = fit.check(request(GPT_OSS_120B, context=32768, seqs=1), ONE_SPARK)
    assert "at short context" in result.reason
    assert "falling to" in result.reason
    assert "32768" in result.reason
    assert "per sequence" in result.reason.lower()


# --------------------------------------------------------------------------
# day 0 stub
# --------------------------------------------------------------------------


def test_stub_returns_valid_contract_types():
    """A stub that fails to return valid contract types is worse than no stub."""
    stub = StubFit()
    result = stub.check(request(QWEN3_30B_A3B, 4096, 8), ONE_SPARK)

    assert isinstance(result.verdict, Verdict)
    assert result.breakdown.total > 0
    assert isinstance(result.limiting_term, str) and result.limiting_term
    assert result.warnings and "stub" in result.warnings[0]
    assert stub.max_context(QWEN3_30B_A3B, make_plan(), ONE_SPARK, 8, "auto") > 0

    big = stub.check(request(LLAMA_3_3_70B, 4096, 8), ONE_SPARK)
    assert big.verdict is Verdict.WONT_FIT
    # The stub has to exercise the None sentinel too, or every consumer
    # tested against it gets zero coverage of the branch the real
    # calculator's contract just gained.
    assert big.max_context_that_fits is None
    assert result.max_context_that_fits is not None and result.max_context_that_fits > 0


# --------------------------------------------------------------------------
# calibration against real models
#
# Reference footprints are hand-derived below from each model's published
# config.json and true parameter count, with the arithmetic written out rather
# than computed by the code under test, so this is a cross-check and not a
# tautology. The weight figures are externally checkable: vLLM prints
# "Loading model weights took X GiB" at startup and these match it.
#
# NOT YET ANCHORED TO MEASURED PEAKS. Closing the brief's "within 20 percent of
# measured peak memory" criterion needs nvidia-smi peaks captured from real
# loads on the cluster; re-anchor REFERENCE_FOOTPRINTS against those at
# integration and tighten the tolerance.
# --------------------------------------------------------------------------

LLAMA_3_1_8B = ModelShape(
    model_id="meta-llama/Llama-3.1-8B-Instruct",
    num_layers=32,
    hidden_size=4096,
    num_attention_heads=32,
    num_kv_heads=8,
    vocab_size=128256,
    total_params=8_030_261_248,
    dtype="bf16",
    head_dim=128,
)

QWEN2_5_7B = ModelShape(
    model_id="Qwen/Qwen2.5-7B-Instruct",
    num_layers=28,
    hidden_size=3584,
    num_attention_heads=28,
    num_kv_heads=4,
    vocab_size=152064,
    total_params=7_615_616_512,
    dtype="bf16",
    head_dim=128,
)

REFERENCE_FOOTPRINTS = [
    # (shape, context, seqs, kv_dtype, reported weight GiB, hand-derived total B)
    (
        LLAMA_3_1_8B,
        8192,
        16,
        "bf16",
        14.99,  # vLLM: "Loading model weights took 14.99 GiB"
        # 16_060_522_496 weights + 17_179_869_184 KV (131072 B/token x 8192 x 16)
        # + 209_534_976 activations + 1_073_741_824 framework
        34_523_668_480,
    ),
    (
        QWEN2_5_7B,
        4096,
        8,
        "bf16",
        14.19,
        # 15_231_233_024 + 1_879_048_192 (57344 B/token x 4096 x 8)
        # + 181_026_816 + 1_073_741_824
        18_365_049_856,
    ),
    (
        LLAMA_70B_FP8,
        4096,
        16,
        "bf16",
        65.71,
        # 70_553_706_496 + 21_474_836_480 (327680 B/token x 4096 x 16)
        # + 410_861_568 + 1_073_741_824
        93_513_146_368,
    ),
]


@pytest.mark.parametrize(
    "shape,context,seqs,kv_dtype,reported_weight_gib,reference_total",
    REFERENCE_FOOTPRINTS,
    ids=[r[0].model_id for r in REFERENCE_FOOTPRINTS],
)
def test_footprint_within_20pct_of_reference(
    shape, context, seqs, kv_dtype, reported_weight_gib, reference_total
):
    breakdown, _ = memory_breakdown(shape, make_plan(), context, seqs, kv_dtype)

    assert breakdown.weights / GIB == pytest.approx(reported_weight_gib, rel=0.01)

    error = abs(breakdown.total - reference_total) / reference_total
    assert error < 0.20, (
        f"{shape.model_id}: predicted {breakdown.total / GIB:.1f} GiB against a "
        f"reference of {reference_total / GIB:.1f} GiB, {error:.1%} out"
    )


def test_gqa_models_are_not_over_charged_by_the_grouping_ratio():
    """The regression this whole component exists to prevent: an 8x KV
    over-estimate turns a model that fits into a refusal."""
    breakdown, _ = memory_breakdown(LLAMA_3_1_8B, make_plan(), 8192, 16, "bf16")
    mistaken, _ = memory_breakdown(
        dataclasses.replace(LLAMA_3_1_8B, num_kv_heads=32),
        make_plan(),
        8192,
        16,
        "bf16",
    )
    assert mistaken.kv_cache == breakdown.kv_cache * 4
    assert breakdown.total < 40 * GIB < mistaken.total


@pytest.mark.parametrize(
    "label,req,nodes", REFUSAL_CASES, ids=[c[0] for c in REFUSAL_CASES]
)
def test_concurrency_suggestions_are_verified_too(fit, label, req, nodes):
    """Same discipline as the context suggestion: if we print a number, we have
    already checked it fits."""
    import re

    result = fit.check(req, nodes)
    match = re.search(r"reduce concurrency to (\d+) sequences", result.reason)
    if not match:
        pytest.skip(f"{label} offers no concurrency reduction")

    suggested = int(match.group(1))
    assert suggested < req.max_concurrent_seqs
    replayed = fit.check(
        dataclasses.replace(req, max_concurrent_seqs=suggested), nodes
    )
    assert replayed.ok, f"{label}: suggested {suggested} sequences and it did not fit"


class TestRefusalOnAMachineWithNoGPU:
    """The refusal a GPU-less machine gets, which is now a reachable screen.

    `/api/capacity` used to 503 on such a cluster, so nobody got as far as a
    Serve button and this string was rarely seen. It answers now, so the string
    is on the path a person actually walks — and it was saying two false
    things.
    """

    def _profile(self):
        import dataclasses

        from tests.fixtures import SPARK_01

        return dataclasses.replace(
            SPARK_01,
            node_id="connor-pi",
            gpu_name="",
            gpu_count=0,
            total_memory=0,
            addressable_memory=0,
            memory_bandwidth_gbps=0.0,
        )

    def _reason(self, fit):
        from tests.fixtures import QWEN3_30B_A3B

        node = self._profile()
        result = fit.check(
            request(QWEN3_30B_A3B, plan=make_plan(node_ids=[node.node_id])),
            [node],
        )
        assert result.verdict is Verdict.WONT_FIT
        return result.reason

    def test_it_does_not_name_an_empty_device(self, fit):
        """`gpu_name` is "" on a machine the probe found no GPU on, so the
        general branch rendered "90% of 0.0 GiB addressable on " with nothing
        after "on" -- a GPU with nothing left, rather than no GPU."""
        reason = self._reason(fit)
        assert "addressable on " not in reason, reason
        assert "connor-pi has no GPU memory at all" in reason, reason

    def test_it_does_not_send_somebody_shopping_for_64_machines(self, fit):
        """The remedy trap, stated as a number.

        `min_nodes_required` returns -1 here and its own docstring says that
        means "a different machine or a smaller quantization, NOT more Sparks".
        The phrase rendered it as "more than 64 nodes" -- the one remedy the
        search had just ruled out, and one that can never work: a machine with
        no memory to spend does not acquire some by being bought twice.
        """
        reason = self._reason(fit)
        assert "64" not in reason, reason
        assert "no number of these holds it" in reason, reason

    def test_a_real_machine_still_gets_a_node_count(self, fit):
        """The counts that ARE achievable must be untouched: this phrase is
        shared with every over-budget refusal on real hardware."""
        from tests.fixtures import DEEPSEEK_V3, SPARK_01

        reason = fit.check(request(DEEPSEEK_V3), [SPARK_01]).reason
        assert "7 nodes" in reason, reason
        assert "NVIDIA GB10" in reason, reason


class TestRefusalOnAMachineDeliberatelyServingFromItsCPU:
    """The same two sentences, for a machine that is doing what it was chosen
    to do and has simply run out of RAM.

    The class above covers a node with `addressable_memory == 0` and
    `device_class` GB10 -- a Spark whose probe came back empty, where "has no
    GPU memory at all" is the right sentence and the GPU is where to look. A
    Pi serving on `llamacpp` reaches the same branch with the same zero and a
    completely different meaning, and the old strings described it as broken
    rather than as small.
    """

    def _profile(self):
        from control_plane.contracts import DeviceClass, NodeProfile

        return NodeProfile(
            node_id="connor-pi",
            hostname="connor-pi",
            address="10.0.0.9",
            device_class=DeviceClass.CPU,
            gpu_name="",
            gpu_count=0,
            total_memory=0,
            addressable_memory=0,
            memory_bandwidth_gbps=0.0,
            compute_capability="",
            driver_version="",
        )

    def _reason(self, fit):
        from tests.fixtures import QWEN3_30B_A3B

        node = self._profile()
        result = fit.check(
            request(QWEN3_30B_A3B, plan=make_plan(node_ids=[node.node_id])),
            [node],
        )
        assert result.verdict is Verdict.WONT_FIT
        return result.reason

    def test_it_does_not_call_a_working_machine_broken(self, fit):
        reason = self._reason(fit)
        assert "has no GPU memory at all" not in reason, reason
        assert "serves from host memory" in reason, reason

    def test_the_remedy_is_more_memory_rather_than_different_hardware(self, fit):
        """"A machine with GPU memory" is the right remedy for the Spark case
        and the wrong one here: it reads as "buy different hardware" when the
        real answer is that this model does not fit in this machine's RAM at
        any quantity."""
        reason = self._reason(fit)
        assert "a machine with GPU memory" not in reason, reason
        assert "more memory than this machine has" in reason, reason
        # Still no node count: a CPU node cannot carry a rank of a multi-node
        # plan either, since the only runtime that places here does not shard.
        assert "no number of these holds it" in reason, reason


def test_an_unmeasured_bandwidth_predicts_no_rate_rather_than_zero(fit):
    """`predict_decode_tps` returns 0.0 for an unknown bandwidth, which is the
    right answer for a function returning a float and the wrong one to put on
    the wire.

    `FitResult.predicted_decode_tps` is `float | None`, every renderer tests
    `!= null`, and `Readout` prints a finite 0 as "0.0" -- so a machine nobody
    has measured would have advertised "predicted decode 0.0 tok/s", a rate
    stated with a decimal point that no measurement produced.

    A CPU node is the first hardware here that reaches it: `probe.py` leaves
    `memory_bandwidth_gbps` at 0.0 for anything not in its known table, on the
    stated grounds that guessing "would be indistinguishable from a
    measurement".
    """
    import dataclasses

    from tests.fixtures import QWEN3_30B_A3B, SPARK_01

    unmeasured = dataclasses.replace(SPARK_01, memory_bandwidth_gbps=0.0)
    result = fit.check(
        request(QWEN3_30B_A3B, plan=make_plan(node_ids=[unmeasured.node_id])),
        [unmeasured],
    )

    assert result.predicted_decode_tps is None
    assert result.predicted_decode_tps_empty is None

    # And a machine that HAS been measured is untouched: the guard must not
    # suppress a real figure.
    measured = fit.check(
        request(QWEN3_30B_A3B, plan=make_plan(node_ids=[SPARK_01.node_id])),
        [SPARK_01],
    )
    assert measured.predicted_decode_tps and measured.predicted_decode_tps > 0


# ---------------------------------------------------------------------------
# A refusal never reports a quantity of nothing
# ---------------------------------------------------------------------------


def test_a_near_miss_never_says_it_is_over_budget_by_zero():
    """The bug this exists for, verbatim from the screen it reached:

        "Over budget by 0.0 GiB. KV cache is the problem: 15.6 GiB per rank at
         42496 tokens x 1 sequences, against 15.6 GiB left..."

    Every number in it was correct. `_gib` rendered one decimal place of GiB, so
    an overage of a few tens of MiB rounded to zero and the two compared figures
    rounded to each other. The sentence then says a thing does not fit, that it
    is over by nothing, and that 15.6 does not go into 15.6 -- which reads as a
    broken calculation rather than as the near miss it is.

    Planner and fit strings are the product (CLAUDE.md). A refusal that looks
    like arithmetic nobody checked costs more than the memory it is about.
    """
    from control_plane.fit.calculator import GIB, MIB, _gib, _gib_vs

    # Nothing non-zero renders as zero, at any scale.
    assert _gib(0) == "0.0 GiB", "a real zero should still look like one"
    assert _gib(1) == "1 byte"
    assert _gib(900) == "900 bytes"
    assert _gib(512 * 1024) == "512 KiB"
    assert _gib(40 * MIB) == "40 MiB"
    assert _gib(15.6 * GIB) == "15.6 GiB"
    for n in (1, 1024, MIB, 40 * MIB, int(0.049 * GIB)):
        assert not _gib(n).startswith("0.0 "), f"{n} bytes rendered as zero"

    # Two figures printed against each other are told apart when it is useful.
    need, have = _gib_vs(15.62 * GIB, 15.58 * GIB)
    assert need != have, "a 40 MiB gap must not print as the same number twice"
    assert (need, have) == ("15.62 GiB", "15.58 GiB")

    # Closer than a hundredth of a GiB, they are equal for every purpose a
    # reader has, and the exact overage carries the difference instead. Chasing
    # it into smaller units gives "15936 MiB against 15936 MiB", which is wider
    # and still looks identical.
    same_need, same_have = _gib_vs(15.6 * GIB, 15.6 * GIB + 1)
    assert same_need == same_have == "15.6 GiB"


class TestLadderContextSkipsRungsThatHoldNothing:
    """`ladder_context` choosing the question the whole ladder is judged at.

    The regression this class exists for: the rungs arrive best-quality first,
    and `max_context` answers 0 for a rung whose weights alone blow the budget.
    `_clamp_context` maps that 0 to the model's own window -- correct on the
    single-model path, where there is no lower rung and the gate must be left
    to refuse in its own words -- so the first rung cleared `MIN_USEFUL_CONTEXT`
    trivially and won. A ladder of 13 GiB variants was handed the 262,144-token
    window of a 48 GiB rung that had been refused outright, and every row then
    refused on KV cache.

    Nothing above caught it because `_walk` re-quantizes at the derived context
    and recovers. The variant ladder passes `ladder=()` on purpose -- each
    published file is judged as it ships -- so there is nothing to recover to.
    """

    NATIVE = 262144

    def _ctx(self, rungs, allocatable=None):
        from control_plane.fit.capacity import ladder_context

        return ladder_context(
            rungs,
            ONE_SPARK,
            make_plan(node_ids=(SPARK_01.node_id,)),
            max_seqs=1,
            kv_dtype="fp16",
            allocatable=allocatable,
            native_window=self.NATIVE,
        )

    def _rung(self, dtype):
        return (dataclasses.replace(QWEN3_30B_A3B, dtype=dtype), dtype, None)

    def test_a_rung_that_does_not_fit_does_not_choose_the_context(self):
        """The bug, at its smallest. bf16 is refused for its weights alone on a
        tight budget; it must not hand its native window to the rungs below."""
        tight = {SPARK_01.node_id: 20 * GIB}
        context, rung = self._ctx(
            [self._rung("bf16"), self._rung("q4_k_m")], allocatable=tight
        )
        assert rung == "q4_k_m", (
            f"chose {rung}, a rung that holds nothing on this budget"
        )
        assert context < self.NATIVE, (
            f"judged the ladder at {context} tokens, the native window of a "
            f"rung that was refused outright"
        )

    def test_the_context_is_one_the_chosen_rung_actually_holds(self):
        """Feed it back in. A context nobody verified is not a measurement."""
        from control_plane.fit.capacity import FitCalculator

        tight = {SPARK_01.node_id: 20 * GIB}
        context, rung = self._ctx(
            [self._rung("bf16"), self._rung("q4_k_m")], allocatable=tight
        )
        result = FitCalculator().check(
            FitRequest(
                shape=dataclasses.replace(QWEN3_30B_A3B, dtype=rung),
                context_length=context,
                max_concurrent_seqs=1,
                kv_dtype="fp16",
                plan=make_plan(node_ids=(SPARK_01.node_id,)),
            ),
            ONE_SPARK,
            allocatable=tight,
        )
        assert result.verdict is not Verdict.WONT_FIT, result.reason

    def test_a_roomy_budget_still_takes_the_best_rung(self):
        """The skip must not cost quality where quality is affordable."""
        context, rung = self._ctx([self._rung("bf16"), self._rung("q4_k_m")])
        assert rung == "bf16"
        assert context == self.NATIVE, (
            "clamped below the model's own window on a machine that holds it"
        )

    def test_nothing_holds_it_and_the_gate_is_left_to_say_so(self):
        """When no rung fits at any context this returns no rung at all, so the
        walk that follows produces the gate's own refusal -- naming the term
        and the overflow -- rather than a sentence invented here."""
        nothing = {SPARK_01.node_id: 1 * GIB}
        context, rung = self._ctx([self._rung("bf16")], allocatable=nothing)
        assert rung is None
        assert context > 0, "a context of 0 is a refusal dressed as a choice"


# --- the two ends of the decode range, and the corpus behind them -----------

#: What seven real checkpoints actually decoded at on a GB10 at 273 GB/s,
#: measured through `tests/decode_sweep.py` off vLLM's own counters:
#: (model, active weight bytes, measured tok/s). The efficiency each implies
#: runs 0.560 to 0.738 -- which is why `DECODE_EFFICIENCY` stayed conservative
#: and grew a companion rather than being replaced by their average.
MEASURED_GB10 = (
    ("LiquidAI/LFM2.5-350M", 0.71e9, 214.9),
    ("Qwen/Qwen2.5-0.5B-Instruct", 0.99e9, 159.0),
    ("Qwen/Qwen3-0.6B", 1.50e9, 123.2),
    ("Qwen/Qwen3-4B-AWQ", 2.67e9, 71.2),
    ("Qwen/Qwen3-1.7B", 4.06e9, 49.2),
    ("microsoft/Phi-3.5-mini-instruct", 7.64e9, 22.9),
    ("Qwen/Qwen3-4B", 8.04e9, 21.7),
)


def test_the_optimistic_end_is_an_upper_bound_on_every_measured_model():
    """The property the second constant exists for.

    A range whose top end the hardware beats is not a range. Every one of the
    seven measured checkpoints has to land at or below the empty-cache figure,
    and at 0.70 one of them (Qwen3-1.7B, which achieved 0.738) does not -- which
    is why the constant is 0.75 and not the corpus mean of 0.652.
    """
    escaped = [
        name
        for name, weights, measured in MEASURED_GB10
        if measured > 273e9 / weights * DECODE_EFFICIENCY_BEST
    ]
    assert not escaped, f"measured above the optimistic end: {escaped}"


def test_the_conservative_end_never_over_promises():
    """The other half of the same argument.

    `DECODE_EFFICIENCY` is at or below every efficiency observed, so the figure
    the degraded threshold judges and the router seeds from is never optimistic.
    Over-promising is the failure that matters here: a model marked FITS that
    decodes unusably slowly is worse than one marked degraded that does not.
    """
    for name, weights, measured in MEASURED_GB10:
        conservative = 273e9 / weights * DECODE_EFFICIENCY
        assert conservative <= measured, f"{name} over-promised by the low end"


def test_the_two_constants_stay_in_order():
    """Trivial, and worth pinning: swapping them would invert every range on
    every card while every test above still passed."""
    assert DECODE_EFFICIENCY < DECODE_EFFICIENCY_BEST


def test_the_efficiency_argument_defaults_to_the_conservative_one():
    """Every caller that predates the choice must keep getting what it got.

    `head_scan` ranks heads against this function and `speculative.ts::speedup`
    divides two of its results, so a changed default would move a scan baseline
    and a speedup multiple that nothing in this change is about.
    """
    assert predict_decode_tps(LLAMA_3_3_70B, 273.0, 0) == predict_decode_tps(
        LLAMA_3_3_70B, 273.0, 0, efficiency=DECODE_EFFICIENCY
    )
    assert predict_decode_tps(
        LLAMA_3_3_70B, 273.0, 0, efficiency=DECODE_EFFICIENCY_BEST
    ) > predict_decode_tps(LLAMA_3_3_70B, 273.0, 0)
