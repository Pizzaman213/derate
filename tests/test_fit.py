"""Agent D: the out-of-memory gate.

The acceptance list from the brief, plus the traps. Every refusal has to name
the term that blew the budget and a change that would work; a test that only
checks the verdict is not testing the product.
"""

from __future__ import annotations

import dataclasses
import math

import pytest

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
    shape, context=8192, seqs=16, kv_dtype="auto", plan=None
) -> FitRequest:
    return FitRequest(
        shape=shape,
        context_length=context,
        max_concurrent_seqs=seqs,
        kv_dtype=kv_dtype,
        plan=plan or make_plan(),
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
