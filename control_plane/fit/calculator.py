"""The blocking pre-launch out-of-memory gate.

Per rank:

    weights + kv_cache + activations + comm_buffers + replicated
        + framework_overhead  <=  usable_memory

Everything else in this space estimates and then lets you launch anyway. This
refuses, and when it refuses it names the term that blew the budget and the
specific change that would work. A reason string that says only "does not fit"
is a bug in this file.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from collections.abc import Mapping, Sequence

from control_plane.humanize import binary_bytes
from control_plane.contracts import (
    BYTES_PER_PARAM,
    COMM_BUFFER_BYTES,
    DEFAULT_GUARDRAIL,
    DEGRADED_TPS_THRESHOLD,
    DeviceClass,
    EP_EXTRA_BUFFER_BYTES,
    FRAMEWORK_OVERHEAD,
    FitRequest,
    FitResult,
    MemoryBreakdown,
    ModelShape,
    NodeProfile,
    ParallelismKind,
    ParallelismPlan,
    SpeculativeSpec,
    Verdict,
)

from .constants import (
    ACTIVATION_CHUNK_TOKENS,
    CONTEXT_ROUNDING,
    DECODE_EFFICIENCY,
    DECODE_EFFICIENCY_BEST,
    MAX_CONTEXT_SEARCH,
    MAX_SEARCH_NODES,
    QUANT_SUGGESTION_ORDER,
)
from .kv import (
    is_known_kv_dtype,
    kv_bytes_per_token,
    kv_cache_bytes,
    kv_divisor,
    kv_elem_bytes,
    stage_fraction,
)

GIB = 1024**3
MIB = 1024**2
KIB = 1024


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------


#: A byte count in the largest binary unit that does not round it away. Lives
#: in ``control_plane/humanize.py`` because the gateway's download strings need
#: the same ladder and had grown a one-line divide that did not have it.
_gib = binary_bytes


def _gib_vs(a: float, b: float) -> tuple[str, str]:
    """Two byte counts that will be printed side by side, at a precision that
    tells them apart.

    "15.6 GiB per rank ... against 15.6 GiB left" is a true statement and an
    unreadable one: the two differ by less than the rounding, so the sentence
    appears to say a thing does not fit inside itself. Widen the precision until
    the strings differ, and only then give up and print both the same -- which
    at that point means they really are the same number.
    """
    for places in (1, 2):
        left = f"{a / GIB:.{places}f} GiB"
        right = f"{b / GIB:.{places}f} GiB"
        if left != right:
            return left, right
    # Closer than a hundredth of a GiB. Two figures that far apart are equal for
    # every purpose a reader has, and chasing the difference into more decimals
    # or smaller units ("15936 MiB against 15936 MiB") trades a readable number
    # for a wider one that still looks identical. Print them plainly and let the
    # overage clause carry the difference -- it is exact, and it is the sentence
    # that says what to change.
    return _gib(a), _gib(b)


def _options(fixes: list[str], *, capitalize: bool = False) -> str:
    """Join fixes as a sentence: "a", "a or b", "a, b, or c"."""
    if len(fixes) == 1:
        joined = fixes[0]
    else:
        head = ", ".join(fixes[:-1])
        joined = (
            f"{head}, or {fixes[-1]}" if len(fixes) > 2 else f"{head} or {fixes[-1]}"
        )
    return joined[0].upper() + joined[1:] if capitalize else joined


def _reported_context(max_ctx: int) -> int | None:
    """The contract sentinel for ``max_context_that_fits``.

    ``max_ctx`` comes from ``_largest_context``, which returns 0 to mean
    "not even one rounding step of cache fits" -- never "zero tokens of
    context fits", which is not a claim we have verified. 0 must never cross
    the port boundary: the sentinel for "there is no such context" is
    ``None``. This applies to every refusal alike, regardless of which term
    the diagnosis names -- a ``combined`` refusal where nothing at all fits
    is exactly as unhelpful a "try 0 tokens" as a ``weights`` one.
    """
    return None if max_ctx == 0 else max_ctx


def _budget_word(basis: str) -> str:
    """What to call the budget in a reason string.

    Under a live budget "usable" is the wrong word: it is not what the hardware
    could spend, it is what was left at the moment we looked.
    """
    return "allocatable right now" if basis == "live" else "usable"


def _dedup(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


# --------------------------------------------------------------------------
# exported helpers, called by the planner
# --------------------------------------------------------------------------


def predict_decode_tps(
    shape: ModelShape,
    bandwidth_gbps: float,
    kv_read_bytes: float,
    efficiency: float = DECODE_EFFICIENCY,
) -> float:
    """Decode tokens per second, single stream, memory-bandwidth bound.

    Decoding one token reads every active weight plus the whole cache for that
    sequence. **Active** parameters, not total: GPT-OSS-120B moves 2.7 GB per
    token off 5.1B active params, a dense 70B moves 140 GB. That difference is
    an order of magnitude of decode speed on identical hardware, and it is the
    most useful thing we can say before someone waits five minutes for a load.

    ``kv_read_bytes`` is the unsharded cache for one sequence, matching the
    unsharded weight term: tensor parallel divides both the bytes and multiplies
    the aggregate bandwidth, so the ratio holds.

    ``efficiency`` defaults to the conservative constant, which is what every
    caller wanted before there was a choice. The fit gate passes
    ``DECODE_EFFICIENCY_BEST`` for the optimistic end of the range it reports;
    see ``fit/constants.py``, which carries the measured corpus behind both
    numbers. Nothing else should pass it -- a caller that wants the honest
    spread wants both ends, not the flattering one.
    """
    if bandwidth_gbps <= 0:
        return 0.0
    weight_bytes_per_token = shape.effective_active_params * shape.bytes_per_param()
    bytes_per_token = weight_bytes_per_token + max(0.0, kv_read_bytes)
    if bytes_per_token <= 0:
        return 0.0
    ceiling = (bandwidth_gbps * 1e9) / bytes_per_token
    return ceiling * efficiency


def speculative_decode_tps_range(
    shape: ModelShape,
    bandwidth_gbps: float,
    kv_read_bytes: float,
    spec: SpeculativeSpec,
) -> tuple[float, float]:
    """Decode tokens per second under speculative decoding, floor and ceiling.

    One verify step reads every active weight once, exactly as an ordinary
    decode step does, and settles up to ``k + 1`` positions instead of one. It
    also reads the draft's own weights ``k`` times to produce those tokens. So
    against the ordinary rate:

    * **ceiling** -- every drafted token accepted::

          base * (k + 1) / (1 + k * draft_ratio)

    * **floor** -- none accepted, and the draft read for nothing::

          base / (1 + k * draft_ratio)

    where ``draft_ratio`` is the draft's parameters over the model's active
    ones, so the draft's own bandwidth cost is already subtracted from both
    ends. That makes the ceiling tighter than a bare ``(k+1)x``, and it makes
    the floor **lower than the ordinary rate**, which is the half of this that
    a recommendation must not hide: speculative decoding on a workload that
    never accepts a draft is slower than not using it.

    What decides where in that range a real workload lands is the acceptance
    rate, and derate does not measure acceptance rate. There is deliberately no
    assumed constant in this function -- an estimate here would be a throughput
    figure this project cannot back, which is the one thing every number on
    these screens is supposed not to be. Both ends are returned; the caller
    states them both.

    ngram has ``draft_params == 0``, so its floor is the ordinary rate exactly
    and its ceiling is ``(k + 1)x``: it reads no weights to draft with.
    """
    base = predict_decode_tps(shape, bandwidth_gbps, kv_read_bytes)
    k = max(0, int(spec.num_speculative_tokens))
    if base <= 0 or k == 0:
        return base, base
    overhead = speculative_overhead(k, draft_ratio(shape, spec.draft_params))
    return base / overhead, base * (k + 1) / overhead


def draft_ratio(shape: ModelShape, draft_params: int) -> float:
    """The draft's parameters over the model's active ones.

    Zero for ngram, which reads no weights to draft with, and zero for a shape
    that reports no active parameters -- in both cases the draft costs no
    bandwidth and the overhead term below collapses to 1.
    """
    active = shape.effective_active_params or 0
    return (max(0, draft_params) / active) if active > 0 else 0.0


def speculative_overhead(k: int, ratio: float) -> float:
    """What a verify step costs in bandwidth, relative to an ordinary one.

    ``1 + k * ratio``: the target's weights are read once either way, plus the
    draft's weights once per drafted token. Factored out rather than written
    twice because it is the denominator of BOTH the range this module states
    and the measured figure ``speculative_best_k`` solves for -- and a second
    copy is how the two would come to disagree about the same launch.
    """
    return 1.0 + max(0, int(k)) * max(0.0, ratio)


@dataclass(frozen=True)
class KPoint:
    """What one drafted-token count is worth, given measured acceptance."""

    k: int
    #: Drafted tokens settled per step, measured. Not counting the bonus token.
    expected_accepted: float
    tps: float


def speculative_best_k(
    base_tps: float, ratio: float, accept_cumulative: Sequence[float]
) -> list[KPoint]:
    """Throughput at every drafted-token count, from measured acceptance.

    ``accept_cumulative[i]`` is the fraction of draft rounds in which position
    ``i`` was accepted -- vLLM's own
    ``num_accepted_tokens_per_pos / num_drafts``. It is CUMULATIVE, not
    conditional: a draft is only checked at position 2 if position 1 was
    accepted first. That is what makes the sum below correct without assuming
    the positions are independent, which they are not.

        E[accepted](k) = sum(accept_cumulative[:k])
        TPS(k)         = base * (1 + E[accepted](k)) / (1 + k * ratio)

    The two ends of ``speculative_decode_tps_range`` are the two ends of this:
    all-ones acceptance reproduces its ceiling exactly and all-zeros its floor,
    so a measured point always lands inside the range the screen already
    showed. Both are asserted in the tests, because that invariant is the only
    thing tying this number to the claim it narrows.

    Returns a point per ``k`` from 1 to however many positions were measured,
    in order. ``max(..., key=tps)`` is the best one; it is deliberately not
    computed here, so a caller can show the curve rather than only its peak --
    the shape is what says whether the optimum is sharp or flat.
    """
    if base_tps <= 0:
        return []
    points: list[KPoint] = []
    running = 0.0
    for k, share in enumerate(accept_cumulative, start=1):
        running += max(0.0, float(share))
        points.append(
            KPoint(
                k=k,
                expected_accepted=running,
                tps=base_tps * (1.0 + running) / speculative_overhead(k, ratio),
            )
        )
    return points


def speculative_weight_bytes_per_rank(
    plan: ParallelismPlan, spec: SpeculativeSpec | None
) -> float:
    """The draft's weights on the rank that carries them.

    Divided by tensor parallel, which shards the draft head with the model, and
    NOT multiplied by ``stage_fraction``: a runtime places the draft head on one
    pipeline stage -- the last -- rather than spreading it over all of them.
    Charging it a stage's share would under-budget precisely the rank that
    holds it, and the budget here is taken against the busiest rank everywhere
    else too (see the uneven-stages warning in :func:`memory_breakdown`).
    """
    if spec is None:
        return 0.0
    return max(0, spec.draft_bytes) / max(1, plan.tensor_parallel)


def speculative_kv_bytes_per_rank(
    shape: ModelShape,
    plan: ParallelismPlan,
    max_seqs: int,
    kv_dtype: str,
    spec: SpeculativeSpec | None,
    context: int = 0,
) -> float:
    """Cache the draft costs: its drafted positions, and its own KV.

    Two terms, and leaving the second out killed real launches.

    **The drafted positions.** A verify step holds ``k`` speculative positions
    per sequence in the target's cache alongside the accepted context, at the
    ordinary full-attention rate. Small -- a few hundred KB at k=5 and one
    sequence.

    **The draft's own cache.** A separately loaded head is a transformer with
    its own layers, and vLLM allocates a KV cache for it out of the same
    budget. That term is NOT small, and it does not scale with ``k`` at all --
    it scales with context, exactly like the target's. Which is also why
    walking ``k`` down does not rescue a launch this term sank.

    Leaving it out made the fit gate say `fits` and the engine refuse to
    start::

        ValueError: To serve at least one request with the model's max seq len
        (8192), 1.37 GiB KV cache is needed, which is larger than the
        available KV cache memory (1.17 GiB)

    1.17 GiB is what derate handed vLLM. The head was 5 layers against
    Qwen3-4B's 36, and 5/36 of 1.17 is 0.16 -- the gap, to within rounding.
    ``draft_kv_ratio`` is that fraction, computed from the head's own shape by
    ``resolver/speculators.py`` rather than guessed here.
    """
    if spec is None:
        return 0.0
    divisor = kv_divisor(shape, plan)
    drafted = 0.0
    k = max(0, int(spec.num_speculative_tokens))
    if k > 0:
        drafted = kv_bytes_per_token(shape, kv_dtype) * k * max(0, int(max_seqs))
    own = 0.0
    ratio = max(0.0, float(getattr(spec, "draft_kv_ratio", 0.0) or 0.0))
    if ratio > 0:
        # The target's whole cache at this context, scaled by the head's share.
        # Sharded by the target's divisor: tensor parallel splits the draft
        # alongside the model, and a head is built to the target's attention
        # geometry, which is what lets one ratio answer for both.
        own = kv_cache_bytes(shape, context, max_seqs, kv_dtype) * ratio
    return (drafted + own) / divisor


def _candidate_shards(n: int, shape: ModelShape) -> list[ParallelismPlan]:
    """Every way to split a model across n ranks, as synthetic plans.

    Both degrees have to be tried. Tensor parallel cannot shard the cache past
    ``num_kv_heads``, so on a model with 8 KV heads a pure TP split stops
    helping at TP=8 no matter how many nodes are added; pipeline parallel keeps
    shedding cache by layer past that point. Searching only TP reports
    "impossible" for configurations that a pipeline split holds comfortably.
    """
    plans: list[ParallelismPlan] = []
    for tp in range(1, n + 1):
        if n % tp:
            continue
        if shape.num_attention_heads % tp or shape.num_kv_heads % tp:
            # Mirrors planner/legality.py's valid_tp_degrees: a TP degree
            # that does not divide both head counts fails at model load, so
            # it is never a legal shard here either.
            continue
        pp = n // tp
        if pp > shape.num_layers:
            continue
        if tp == 1 and pp == 1:
            kind = ParallelismKind.SINGLE_NODE
        elif pp == 1:
            kind = ParallelismKind.TENSOR
        elif tp == 1:
            kind = ParallelismKind.PIPELINE
        else:
            kind = ParallelismKind.HYBRID
        plans.append(
            ParallelismPlan(
                kind=kind,
                tensor_parallel=tp,
                pipeline_parallel=pp,
                expert_parallel=1,
                data_parallel=1,
                node_ids=[],
                reason="synthetic plan for a node-count search",
                measured_link_gbps=0.0,
                rejected=[],
            )
        )
    return plans


def min_nodes_required(
    shape: ModelShape,
    node_profile: NodeProfile,
    context: int,
    max_seqs: int,
    kv_dtype: str = "auto",
    guardrail: float = DEFAULT_GUARDRAIL,
    weight_bytes: int | None = None,
    *,
    usable: int | None = None,
) -> int:
    """Fewest nodes of this kind that hold the model at this context and
    concurrency, under the most memory-efficient shard available at each size.

    A memory question only. Whether that shard is a *good* plan at the measured
    link bandwidth is the planner's call, not this function's.

    ``weight_bytes``, when given, is the resolver's measured on-disk total
    (``FitRequest.weight_bytes``) -- forwarded to :func:`memory_breakdown` so
    the search is run on the same basis the caller will actually launch
    against, never a formula estimate that names a node count the measured
    checkpoint would not actually fit into.

    Returns -1 when no configuration up to ``MAX_SEARCH_NODES`` fits, which
    means the answer is a different machine or a smaller quantization, not more
    Sparks.

    ``usable``, when given, replaces the static ceiling as the per-node budget.
    A caller gating on live allocatable memory must pass it: otherwise the
    node count comes back computed against memory that is not available, and
    "spread it over 5 nodes" names a number that would still OOM.

    Exported for the planner.
    """
    if usable is None:
        usable = node_profile.usable_memory(guardrail)
    for n in range(1, MAX_SEARCH_NODES + 1):
        for plan in _candidate_shards(n, shape):
            breakdown, _ = memory_breakdown(
                shape, plan, context, max_seqs, kv_dtype, weight_bytes=weight_bytes
            )
            if breakdown.total <= usable:
                return n
    return -1


# --------------------------------------------------------------------------
# the budget
# --------------------------------------------------------------------------


def weight_bytes_per_rank(
    shape: ModelShape,
    plan: ParallelismPlan,
    total_weight_bytes: float | None = None,
) -> float:
    """Shardable weights carried by the busiest rank.

    Vision towers are replicated on every rank and are charged separately, so
    they come out of the shardable pool here.

    ``total_weight_bytes`` is the resolver's measured on-disk figure
    (``FitRequest.weight_bytes``), preferred over ``total_params *
    bytes_per_param()`` whenever it is a positive number: dtype formulas miss
    padding, tied embeddings, and packing overhead the actual checkpoint
    carries, and on GPT-OSS-120B that gap is close to 3 GiB -- the difference
    between fitting and not. A ``None`` or non-positive value falls back to
    the formula unchanged. Vision bytes are still priced from
    ``vision_params * bytes_per_param()`` and subtracted out of the measured
    total to get the shardable remainder, floored at zero so a measured total
    smaller than the computed vision share cannot go negative.

    **Routed experts are divided by the expert-parallel degree.** Until
    2026-09-11 this divided by tensor parallel and the pipeline stage fraction
    and by nothing else, so an EP plan was charged the entire checkpoint on
    every rank: DeepSeek-V4-Flash measured 276.0 GiB per rank at ``ep=8, dp=8``
    across eight nodes and 276.0 GiB at ``ep=1`` on one -- byte-identical --
    while ``deploy/recipes.py`` passed ``--enable-expert-parallel`` on exactly
    those plans and the engine really did place ``num_experts/ep`` of them per
    rank. The planner emits that candidate for every MoE checkpoint
    (``legality.py``'s cross-node EP, and ``planner.py``'s single-node
    multi-GPU upgrade), so the over-count was reachable on any multi-GPU MoE
    box.

    ``shape.routed_expert_params`` is 0 when the split could not be derived,
    and 0 means the whole checkpoint stays dense here -- the behaviour every
    model had before the field existed, which is the refusing direction.
    Shared experts are deliberately not in that figure: every rank reads them
    on every token, so they shard like any dense weight.
    """
    bpp = shape.bytes_per_param()
    vision = min(max(0, shape.vision_params), shape.total_params)
    vision_bytes = vision * bpp
    if total_weight_bytes is not None and total_weight_bytes > 0:
        shardable_bytes = max(0.0, total_weight_bytes - vision_bytes)
    else:
        shardable_bytes = (shape.total_params - vision) * bpp

    tp = max(1, plan.tensor_parallel)
    ep = max(1, plan.expert_parallel)

    # Proportional, never re-derived from params: a measured on-disk total is
    # authoritative for capacity (resolver/params.py says so for the same
    # reason), so the routed share is taken as a FRACTION of whatever basis
    # won above rather than priced separately from the dtype formula. Pricing
    # it separately would mix a measured total with a formula subtrahend and
    # could drive the dense remainder negative.
    routed_bytes = 0.0
    if ep > tp and shape.routed_expert_params > 0 and shape.total_params > 0:
        routed_fraction = min(
            1.0, shape.routed_expert_params / float(shape.total_params)
        )
        routed_bytes = shardable_bytes * routed_fraction

    dense_bytes = shardable_bytes - routed_bytes
    # `max(tp, ep)` rather than `tp * ep`, deliberately conservative: the
    # planner only ever emits `tp>1, ep=1` or `tp=1, ep=world`, and on any
    # other combination this under-divides rather than over-divides. A gate
    # may be too strict and may not be too generous.
    per_rank = dense_bytes / tp + routed_bytes / max(tp, ep)
    return per_rank * stage_fraction(shape, plan.pipeline_parallel)


def replicated_bytes_per_rank(shape: ModelShape) -> float:
    """Never split, held whole by every rank."""
    vision = min(max(0, shape.vision_params), shape.total_params)
    return vision * shape.bytes_per_param()


def activation_bytes(
    shape: ModelShape,
    max_seqs: int,
    context: int,
    chunk_tokens: int = ACTIVATION_CHUNK_TOKENS,
) -> float:
    """Logits buffer plus compute scratch.

    The logits term is charged for tokens actually emitted per step, one per
    sequence in flight, not for the full context. It still dominates on a large
    vocabulary: GPT-OSS's 201k vocab is 12.9 MB per emitted token in fp32.
    """
    batch = max(1, int(max_seqs))
    chunk = min(max(1, int(chunk_tokens)), max(1, int(context)))
    logits = shape.vocab_size * batch * 4
    scratch = chunk * shape.hidden_size * 4 * 6
    return float(logits + scratch)


def comm_buffer_bytes(plan: ParallelismPlan) -> float:
    """NCCL staging. Expert parallel is the term other planners forget, and
    forgetting it is what produces a configuration that passes a fit check and
    then OOMs on the first batch. Conservative on purpose.
    """
    ep_active = plan.expert_parallel > 1
    if plan.world_size <= 1 and not ep_active:
        return 0.0
    total = float(COMM_BUFFER_BYTES)
    if ep_active:
        total += EP_EXTRA_BUFFER_BYTES
    return total


def memory_breakdown(
    shape: ModelShape,
    plan: ParallelismPlan,
    context: int,
    max_seqs: int,
    kv_dtype: str,
    chunk_tokens: int = ACTIVATION_CHUNK_TOKENS,
    weight_bytes: int | None = None,
    speculative: SpeculativeSpec | None = None,
) -> tuple[MemoryBreakdown, list[str]]:
    """Per-rank memory, and everything worth warning about while computing it.

    ``weight_bytes`` is the resolver's measured total, forwarded to
    :func:`weight_bytes_per_rank`; ``None`` (the default) prices weights from
    the dtype formula instead.

    ``speculative`` adds the draft's weights and the cache for its drafted
    positions to the existing terms rather than to new ones. That is the whole
    reason it is charged here: every refusal string, ``_largest_context``,
    ``max_context_that_fits`` and the headroom arithmetic downstream then work
    unchanged, instead of each needing to learn about a second kind of memory.
    """
    warnings: list[str] = []

    if not is_known_kv_dtype(kv_dtype):
        warnings.append(
            f"unknown KV cache dtype {kv_dtype!r}; charging "
            f"{kv_elem_bytes(kv_dtype, shape):.0f} bytes per element"
        )

    tp = max(1, plan.tensor_parallel)
    pp = max(1, plan.pipeline_parallel)

    if pp > shape.num_layers:
        warnings.append(
            f"PP={pp} exceeds {shape.num_layers} layers; stages will be empty"
        )
    if pp > 1 and shape.num_layers % pp:
        busiest = math.ceil(shape.num_layers / pp)
        warnings.append(
            f"pipeline stages are uneven: {shape.num_layers} layers over {pp} "
            f"stages puts {busiest} on the busiest rank; budgeting against that one"
        )
    if not shape.mla_latent_dim and tp > shape.num_kv_heads:
        warnings.append(
            f"TP={tp} exceeds {shape.num_kv_heads} KV heads; the runtime "
            f"replicates KV heads rather than splitting them, so the cache "
            f"shards only {shape.num_kv_heads}x"
        )
    if shape.mla_latent_dim and tp > 1:
        warnings.append(
            "MLA caches one latent per token, so there is nothing to shard by "
            "head: every TP rank holds the whole cache"
        )
    if plan.expert_parallel > 1:
        warnings.append(
            f"expert parallel is active: charging an extra "
            f"{_gib(EP_EXTRA_BUFFER_BYTES)} of expert-staging buffers on top of "
            f"the {_gib(COMM_BUFFER_BYTES)} collective buffers"
        )
    if shape.is_encoder_decoder:
        warnings.append(
            "encoder-decoder shape: kv_cache below prices the decoder's "
            "self-attention only and is a floor, not the real figure -- "
            "cross-attention cache over the encoder's own output is not "
            "modeled"
        )

    spec_weights = speculative_weight_bytes_per_rank(plan, speculative)
    spec_kv = speculative_kv_bytes_per_rank(
        shape, plan, max_seqs, kv_dtype, speculative, context
    )
    if speculative is not None:
        k = speculative.num_speculative_tokens
        warnings.append(
            f"speculative decoding ({speculative.method.value}, {k} drafted "
            f"token{'s' if k != 1 else ''}) is charged "
            f"{_gib(spec_weights)} of draft weights and {_gib(spec_kv)} of cache "
            f"for the drafted positions"
        )
        if spec_weights > 0 and plan.pipeline_parallel > 1:
            warnings.append(
                "the draft head sits on one pipeline stage rather than being "
                "split across them, so its weights are charged whole to the "
                "rank that holds it rather than divided by "
                f"PP={plan.pipeline_parallel}"
            )

    kv_total = kv_cache_bytes(shape, context, max_seqs, kv_dtype)
    breakdown = MemoryBreakdown(
        weights=int(weight_bytes_per_rank(shape, plan, weight_bytes) + spec_weights),
        kv_cache=int(kv_total / kv_divisor(shape, plan) + spec_kv),
        activations=int(activation_bytes(shape, max_seqs, context, chunk_tokens)),
        comm_buffers=int(comm_buffer_bytes(plan)),
        replicated=int(replicated_bytes_per_rank(shape)),
        framework_overhead=int(FRAMEWORK_OVERHEAD),
    )
    return breakdown, warnings


# --------------------------------------------------------------------------
# the gate
# --------------------------------------------------------------------------


class FitCalculator:
    """Implements ``FitPort``.

    ``check`` is the blocking gate: the deployment manager refuses to launch
    on its verdict, the gateway refuses to admit on its numbers. ``FITS_DEGRADED`` is not a
    failure — the model loads, and sometimes that is exactly what someone
    wants. Callers must branch on ``FitResult.ok``, not on ``verdict is FITS``.

    ``max_context_that_fits`` is populated on every verdict, not only on
    refusals, so the UI can offer a headroom figure without a second call. It
    is always a context we have actually checked, never an extrapolation --
    except when no context at all was found to fit (not even zero tokens of
    cache), where it is ``None`` regardless of which term the refusal names.
    0 is a context we verified fits; when the search finds none, reporting 0
    would claim the opposite, so every code path that reports
    ``max_context_that_fits`` on a refusal goes through ``_reported_context``.
    """

    def __init__(
        self,
        guardrail: float = DEFAULT_GUARDRAIL,
        chunk_tokens: int = ACTIVATION_CHUNK_TOKENS,
    ) -> None:
        self.guardrail = guardrail
        self.chunk_tokens = chunk_tokens

    # -- FitPort ----------------------------------------------------------

    def check(
        self,
        req: FitRequest,
        nodes: list[NodeProfile],
        *,
        allocatable: Mapping[str, int] | None = None,
    ) -> FitResult:
        """Gate this request against these nodes.

        ``allocatable`` maps node_id -> bytes the node can actually hand out
        right now. Given, the budget is that figure and ``budget_basis`` on the
        result is ``"live"``; omitted, it is the static addressable ceiling
        under the guardrail, exactly as before. Keyword-only and defaulted, so
        every existing call site is unchanged.

        A node absent from the mapping falls back to its static ceiling and
        says so in ``warnings`` -- an unpolled node must not read as having no
        capacity, or a cold coordinator would refuse every launch.
        """
        shape, plan = req.shape, req.plan
        used, warnings = self._participating(plan, nodes)
        node, usable, basis, budget_notes = self._budget(used, allocatable)
        warnings.extend(budget_notes)
        bandwidth = min(n.memory_bandwidth_gbps for n in used)

        if len({(n.addressable_memory, n.gpu_name) for n in used}) > 1:
            warnings.append(
                f"nodes are not identical; budgeting against the smallest, "
                f"{node.node_id} at {_gib(node.addressable_memory)} addressable"
            )
        if any(n.gpu_count > 1 for n in used):
            warnings.append(
                "a node reports more than one GPU; the budget is per rank and "
                "assumes one rank per GPU with the full addressable pool each"
            )

        breakdown, budget_warnings = memory_breakdown(
            shape,
            plan,
            req.context_length,
            req.max_concurrent_seqs,
            req.kv_dtype,
            self.chunk_tokens,
            req.weight_bytes,
            req.speculative,
        )
        warnings.extend(budget_warnings)
        headroom = usable - breakdown.total

        # ONE sequence, deliberately and regardless of `req.max_concurrent_seqs`:
        # `predict_decode_tps` is a single-stream, bandwidth-bound model, and
        # the cache it must be handed is therefore one sequence's. Passing the
        # plan's concurrency here would not produce an aggregate rate -- it
        # would produce a slower per-sequence one and label it neither. What
        # this figure IS gets stated on screen instead; see `_speculative_range`
        # for the same boundary drawn around the speculative range.
        kv_read = kv_cache_bytes(shape, req.context_length, 1, req.kv_dtype)
        # `None` rather than 0.0 when nothing knows this hardware's memory
        # bandwidth. `predict_decode_tps` returns 0.0 for an unknown bandwidth,
        # which is the right answer for a function returning a float and the
        # wrong one to put on the wire: `FitResult.predicted_decode_tps` is
        # `float | None`, every renderer tests `!= null`, and `Readout` prints
        # a finite 0 as "0.0". So a machine nobody has measured would have
        # advertised "predicted decode 0.0 tok/s" -- a rate, stated with a
        # decimal point, that no measurement produced.
        #
        # A CPU node is the first hardware here that reaches this: `probe.py`
        # leaves `memory_bandwidth_gbps` at 0.0 for anything not in its known
        # table, on the stated grounds that "guessing a number here would be
        # indistinguishable from a measurement". None carries that same
        # position one layer out, and the card drops the line instead.
        tps = predict_decode_tps(shape, bandwidth, kv_read) if bandwidth > 0 else None
        # The other end of the same range, and the honest half of the answer.
        # `kv_read` above is the cache for a sequence at the FULL requested
        # context, so `tps` is the rate once the context is full -- the slowest
        # this will ever decode. Decoding reads the tokens actually present, so
        # a fresh request is faster, and measured on this hardware the gap is
        # roughly 2x: 61.5 predicted against 120.3 measured for Qwen3-0.6B at
        # 8192 with ~320-token requests.
        #
        # An empty cache rather than some notional typical length, because a
        # typical is a guess and this is not: it is the same call `head_scan`
        # makes for its own baseline, so the card and the scan now agree on one
        # number instead of quietly disagreeing about two.
        tps_empty = (
            predict_decode_tps(
                shape, bandwidth, 0.0, efficiency=DECODE_EFFICIENCY_BEST
            )
            if bandwidth > 0
            else None
        )
        # Computed once and attached to every return below, refusals included:
        # a launch that will not fit is exactly when somebody wants to know
        # what they would have got, and the range is the same arithmetic
        # whichever verdict the memory produces.
        spec_floor, spec_ceiling, spec_reason = self._speculative_range(
            req, shape, bandwidth, kv_read, tps
        )
        max_ctx = self._largest_context(
            shape,
            plan,
            usable,
            req.max_concurrent_seqs,
            req.kv_dtype,
            weight_bytes=req.weight_bytes,
            speculative=req.speculative,
        )

        ranks = sum(max(1, n.gpu_count) for n in used)
        if plan.world_size > ranks:
            return FitResult(
                verdict=Verdict.WONT_FIT,
                breakdown=breakdown,
                usable_per_node=usable,
                headroom=headroom,
                reason=(
                    f"Won't fit: the plan wants {plan.world_size} ranks "
                    f"(TP {plan.tensor_parallel} x PP {plan.pipeline_parallel} "
                    f"x DP {plan.data_parallel}"
                    # world_size is TP x PP x DP and does not include EP, so
                    # naming only those three describes an expert-parallel
                    # plan without the degree that decides its memory. Named
                    # separately rather than multiplied in, because that is
                    # exactly what it is.
                    + (
                        f", with EP {plan.expert_parallel}"
                        if plan.expert_parallel > 1
                        else ""
                    )
                    + f") but only {ranks} GPU"
                    f"{'s' if ranks != 1 else ''} were supplied. Add "
                    f"{plan.world_size - ranks} more, or replan for {ranks}."
                ),
                limiting_term="combined",
                # A plan that cannot be placed on the supplied ranks has no
                # meaningful context suggestion: max_ctx was computed under a
                # sharding that does not exist here and would not round-trip.
                max_context_that_fits=None,
                predicted_decode_tps=tps,
                predicted_decode_tps_empty=tps_empty,
                speculative_decode_tps_floor=spec_floor,
                speculative_decode_tps_ceiling=spec_ceiling,
                speculative_reason=spec_reason,
                warnings=_dedup(warnings),
                budget_basis=basis,
            )

        if headroom < 0:
            term, reason = self._diagnose(
                req, breakdown, usable, node, max_ctx, basis
            )
            # The sentinel keys on whether any context at all was found to
            # fit (max_ctx == 0), not on which term the diagnosis names: a
            # "combined" refusal where nothing fits is exactly as unhelpful a
            # "0 tokens" suggestion as a "weights" one. See
            # _reported_context.
            reported_ctx = _reported_context(max_ctx)
            return FitResult(
                verdict=Verdict.WONT_FIT,
                breakdown=breakdown,
                usable_per_node=usable,
                headroom=headroom,
                reason=reason,
                limiting_term=term,
                max_context_that_fits=reported_ctx,
                predicted_decode_tps=tps,
                predicted_decode_tps_empty=tps_empty,
                speculative_decode_tps_floor=spec_floor,
                speculative_decode_tps_ceiling=spec_ceiling,
                speculative_reason=spec_reason,
                warnings=_dedup(warnings),
                budget_basis=basis,
            )

        # Memory has nothing against this request, but a context past the
        # model's own trained window is not a memory question at all -- the
        # runtime's own config validation refuses to start regardless of how
        # much GPU is free (a launch here died with `max_model_len (780800)
        # is greater than ... max_position_embeddings (40960.0)`, thirty
        # minutes into a readiness wait, because nothing upstream of the
        # runtime knew to say no first). Checked after headroom, not before:
        # a model whose weights alone do not fit is refused for that reason
        # first, since a smaller context would not fix it either, and
        # `_diagnose` already owns that priority order.
        if (
            req.native_window is not None
            and req.native_window > 0
            and req.context_length > req.native_window
        ):
            return FitResult(
                verdict=Verdict.WONT_FIT,
                breakdown=breakdown,
                usable_per_node=usable,
                headroom=headroom,
                reason=(
                    f"Won't fit: {req.context_length} tokens of context was "
                    f"requested, but {shape.model_id} was trained on {req.native_window}"
                    f" -- the runtime refuses to start past a model's own "
                    f"max_position_embeddings, however much memory is free. "
                    f"Lower context to at most {req.native_window}."
                ),
                limiting_term="context",
                max_context_that_fits=_reported_context(
                    min(max_ctx, req.native_window)
                ),
                predicted_decode_tps=tps,
                predicted_decode_tps_empty=tps_empty,
                speculative_decode_tps_floor=spec_floor,
                speculative_decode_tps_ceiling=spec_ceiling,
                speculative_reason=spec_reason,
                warnings=_dedup(warnings),
                budget_basis=basis,
            )

        # `tps is not None` and not a truthiness test, and the difference is
        # the whole point of making this nullable: None means nobody has
        # measured this machine's memory bandwidth, and a verdict of
        # FITS_DEGRADED is a claim about a rate. Refusing to judge is the only
        # honest answer -- the memory fits, and how fast it will decode is a
        # question this build cannot answer for this hardware.
        #
        # It errs toward FITS, which is worth stating: a slow machine will be
        # reported as fitting rather than as degraded until somebody measures
        # its bandwidth. That is the same direction every other unmeasured
        # quantity in this project errs, and the alternative is a degraded
        # badge derived from an absence.
        if tps is not None and tps < DEGRADED_TPS_THRESHOLD:
            bytes_per_token = (
                shape.effective_active_params * shape.bytes_per_param() + kv_read
            )
            return FitResult(
                verdict=Verdict.FITS_DEGRADED,
                breakdown=breakdown,
                usable_per_node=usable,
                headroom=headroom,
                reason=(
                    f"Loads with {_gib(headroom)} to spare, but predicted decode "
                    f"is {tps:.1f} tok/s, under the "
                    f"{DEGRADED_TPS_THRESHOLD:.0f} tok/s usability threshold. "
                    f"Bandwidth bound: {bytes_per_token / 1e9:.1f} GB moves per "
                    f"decoded token at {bandwidth:.0f} GB/s. Adding nodes will "
                    f"not fix this — a smaller quantization, a smaller model, or "
                    f"an MoE with fewer active parameters will."
                ),
                limiting_term="bandwidth",
                max_context_that_fits=max_ctx,
                predicted_decode_tps=tps,
                predicted_decode_tps_empty=tps_empty,
                speculative_decode_tps_floor=spec_floor,
                speculative_decode_tps_ceiling=spec_ceiling,
                speculative_reason=spec_reason,
                warnings=_dedup(warnings),
                budget_basis=basis,
            )

        ceiling = "at least " if max_ctx >= MAX_CONTEXT_SEARCH else ""
        return FitResult(
            verdict=Verdict.FITS,
            breakdown=breakdown,
            usable_per_node=usable,
            headroom=headroom,
            reason=(
                f"Fits: {_gib(breakdown.total)} of {_gib(usable)} "
                f"{_budget_word(basis)} per "
                f"rank, {_gib(headroom)} headroom. "
                # The rate clause is dropped whole rather than printed with a
                # zero in it. `probe.py` refuses to guess a memory bandwidth
                # because "guessing a number here would be indistinguishable
                # from a measurement"; a sentence claiming 0 tok/s would be
                # worse than that -- it is a measurement nobody made, and it
                # is also wrong.
                + (
                    f"Predicted decode {tps_empty:.0f} tok/s at short context, "
                    f"falling to {tps:.0f} tok/s at {req.context_length}. "
                    f"Per sequence. "
                    if tps is not None and tps_empty is not None
                    else "Nothing has measured this machine's memory bandwidth, "
                    "so no decode rate is predicted. "
                )
                + f"Context could go to {ceiling}"
                f"{max_ctx} tokens at {req.max_concurrent_seqs} sequences."
            ),
            limiting_term="none",
            max_context_that_fits=max_ctx,
            predicted_decode_tps=tps,
            predicted_decode_tps_empty=tps_empty,
            speculative_decode_tps_floor=spec_floor,
            speculative_decode_tps_ceiling=spec_ceiling,
            speculative_reason=spec_reason,
            warnings=_dedup(warnings),
            budget_basis=basis,
        )

    def _speculative_range(
        self,
        req: FitRequest,
        shape: ModelShape,
        bandwidth: float,
        kv_read: float,
        base_tps: float | None,
    ) -> tuple[float | None, float | None, str]:
        """The two ends of the speculative range, and the sentence for them.

        ``(None, None, "")`` when the request did not ask for speculative
        decoding, which is the overwhelmingly common case and must add nothing
        to the result.

        The sentence states both ends and then states that derate does not know
        where between them a workload lands. That last clause is not hedging --
        it is the difference between this and a throughput claim, and removing
        it would leave a number on screen that nothing here measured.
        """
        spec = req.speculative
        if spec is None:
            return None, None, ""
        # No bandwidth reading, no range. Every number below is that reading
        # multiplied by something, so a range computed without it would be a
        # pair of zeros presented as a floor and a ceiling -- and this is the
        # card that says out loud it will not turn a range into a single
        # number. It must not turn an absence into a range either.
        if base_tps is None:
            return (
                None,
                None,
                "Nothing has measured this machine's memory bandwidth, so the "
                "speculative range cannot be computed for it.",
            )
        floor, ceiling = speculative_decode_tps_range(shape, bandwidth, kv_read, spec)
        k = spec.num_speculative_tokens
        drafted = f"{k} drafted token{'s' if k != 1 else ''}"
        if spec.draft_params <= 0:
            cost = (
                "It drafts without loading any weights, so the floor is the "
                "ordinary rate rather than below it"
            )
        else:
            cost = (
                f"The floor is BELOW the {base_tps:.0f} tok/s above: nothing "
                f"accepted means the draft head was read for nothing"
            )
        # Both ends are PER SEQUENCE, because `predict_decode_tps` is single
        # stream and `kv_read` above is deliberately computed for one sequence.
        # At a batch the arithmetic stops describing the machine: the target's
        # weights are read once for the whole batch either way, so drafting k
        # extra positions per sequence buys back no bandwidth and costs a
        # verify pass over k+1 times as many positions. That is a compute
        # question and derate has no compute model, so this says where its own
        # answer stops rather than extrapolating one.
        batched = (
            ""
            if req.max_concurrent_seqs <= 1
            else (
                f" Both figures are for ONE sequence, and this plan is sized "
                f"for {req.max_concurrent_seqs}: speculative decoding pays "
                f"most when the batch is small enough that decoding is bound "
                f"by memory bandwidth, and derate has not measured where that "
                f"stops on this hardware."
            )
        )
        return (
            floor,
            ceiling,
            f"{spec.method.value}, {drafted}: between {floor:.0f} and "
            f"{ceiling:.0f} tok/s per sequence. The ceiling is every drafted "
            f"token accepted. {cost}. Where in that range a real workload "
            f"lands depends on the acceptance rate, which derate does not "
            f"measure, so this build cannot narrow it further.{batched}",
        )

    def _budget(
        self,
        used: list[NodeProfile],
        allocatable: Mapping[str, int] | None,
    ) -> tuple[NodeProfile, int, str, list[str]]:
        """The binding node, its budget, which basis produced it, and notes.

        Static basis is the addressable ceiling under the guardrail: what this
        hardware could ever spend with nothing else running. Live basis is what
        the node can hand out at this moment, which on a unified-memory part is
        the only one a launch can rely on -- the operating system and any
        process this control plane did not start are spending from the same
        pool.

        The binding node is the argmin over whichever budget is in force, not
        over the static ceiling, because the smallest ceiling and the smallest
        live figure need not be the same node.
        """
        notes: list[str] = []
        if not allocatable:
            node = min(used, key=lambda n: n.usable_memory(self.guardrail))
            return node, node.usable_memory(self.guardrail), "static", notes

        budgets: dict[str, int] = {}
        for n in used:
            ceiling = n.usable_memory(self.guardrail)
            live = allocatable.get(n.node_id)
            if live is None:
                # Never read an unpolled node as having no capacity.
                budgets[n.node_id] = ceiling
                notes.append(
                    f"no live memory reading for {n.node_id}; budgeting against "
                    f"its {_gib(ceiling)} static ceiling"
                )
            else:
                budgets[n.node_id] = live

        node = min(used, key=lambda n: budgets[n.node_id])
        usable = budgets[node.node_id]
        if allocatable.get(node.node_id) is None:
            # The binding node is one we could not read, so the verdict is not
            # a live one however many other nodes we did read.
            return node, usable, "static", notes

        ceiling = node.usable_memory(self.guardrail)
        if usable < ceiling:
            notes.append(
                f"{node.node_id} can allocate {_gib(usable)} right now, "
                f"{_gib(ceiling - usable)} below its {_gib(ceiling)} static "
                f"ceiling"
            )
        return node, usable, "live", notes

    def max_context(
        self,
        shape: ModelShape,
        plan: ParallelismPlan,
        nodes: list[NodeProfile],
        max_seqs: int,
        kv_dtype: str,
        weight_bytes: int | None = None,
        *,
        allocatable: Mapping[str, int] | None = None,
        speculative: SpeculativeSpec | None = None,
    ) -> int:
        """Largest context that fits this plan on these nodes, on a
        ``CONTEXT_ROUNDING`` grain. Verified, never extrapolated. 0 means not
        even one page of cache fits.

        ``weight_bytes``, when given, prices the weights term from the
        resolver's measured total instead of the dtype formula -- see
        :func:`weight_bytes_per_rank`. Optional and additive: every existing
        caller that omits it gets the formula basis unchanged.

        ``speculative`` is additive on the same terms, and it has to be here
        rather than only in ``evaluate``: this is what a request with no
        context is judged at, so deriving one without the draft's weights would
        pick a window the very next fit check then refuses.
        """
        used, _ = self._participating(plan, nodes)
        _, usable, _, _ = self._budget(used, allocatable)
        return self._largest_context(
            shape,
            plan,
            usable,
            max_seqs,
            kv_dtype,
            weight_bytes=weight_bytes,
            speculative=speculative,
        )

    def max_seqs(
        self,
        shape: ModelShape,
        plan: ParallelismPlan,
        nodes: list[NodeProfile],
        context: int,
        kv_dtype: str,
        ceiling: int,
        weight_bytes: int | None = None,
        *,
        allocatable: Mapping[str, int] | None = None,
    ) -> int:
        """Most concurrent sequences that fit at this context. The public seam
        onto the search ``_largest_max_seqs`` already performs.

        The mirror of ``max_context`` above, and it exists for the same reason:
        a request that names no concurrency has to be given one, and picking a
        number before the placement is known is exactly what the context
        derivation refuses to do. Until 2026-09-11 nothing asked -- absence
        meant 1, so every deployment on this cluster served one sequence at a
        time and a decode step's weight read produced a single token.

        0 means not even one sequence fits, which is a refusal for the gate to
        phrase, not a concurrency to launch at.
        """
        used, _ = self._participating(plan, nodes)
        _, usable, _, _ = self._budget(used, allocatable)
        return self._largest_max_seqs(
            shape, plan, usable, context, kv_dtype, ceiling, weight_bytes
        )

    # -- internals --------------------------------------------------------

    def _participating(
        self, plan: ParallelismPlan, nodes: list[NodeProfile]
    ) -> tuple[list[NodeProfile], list[str]]:
        if not nodes:
            raise ValueError("fit check needs at least one NodeProfile")
        warnings: list[str] = []
        if not plan.node_ids:
            return list(nodes), warnings
        by_id = {n.node_id: n for n in nodes}
        chosen = [by_id[i] for i in plan.node_ids if i in by_id]
        missing = [i for i in plan.node_ids if i not in by_id]
        if missing:
            warnings.append(
                f"plan names node(s) with no profile supplied "
                f"({', '.join(missing)}); budgeting against the rest"
            )
        if not chosen:
            warnings.append(
                "no profile matched any node in the plan; budgeting against "
                "every node supplied"
            )
            return list(nodes), warnings
        return chosen, warnings

    def _fits_at(
        self,
        shape: ModelShape,
        plan: ParallelismPlan,
        context: int,
        max_seqs: int,
        kv_dtype: str,
        usable: int,
        weight_bytes: int | None = None,
        speculative: SpeculativeSpec | None = None,
    ) -> bool:
        breakdown, _ = memory_breakdown(
            shape,
            plan,
            context,
            max_seqs,
            kv_dtype,
            self.chunk_tokens,
            weight_bytes,
            speculative,
        )
        return breakdown.total <= usable

    def _largest_max_seqs(
        self,
        shape: ModelShape,
        plan: ParallelismPlan,
        usable: int,
        context: int,
        kv_dtype: str,
        ceiling: int,
        weight_bytes: int | None = None,
    ) -> int:
        """Most concurrent sequences that fit at this context. Verified, like
        the context search: both KV and the logits buffer scale with it."""

        def fits(seqs: int) -> bool:
            breakdown, _ = memory_breakdown(
                shape, plan, context, seqs, kv_dtype, self.chunk_tokens, weight_bytes
            )
            return breakdown.total <= usable

        if ceiling < 1 or not fits(1):
            return 0
        lo, hi = 1, max(1, ceiling)
        if fits(hi):
            return hi
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if fits(mid):
                lo = mid
            else:
                hi = mid - 1
        return lo

    def _largest_context(
        self,
        shape: ModelShape,
        plan: ParallelismPlan,
        usable: int,
        max_seqs: int,
        kv_dtype: str,
        weight_bytes: int | None = None,
        speculative: SpeculativeSpec | None = None,
    ) -> int:
        step = CONTEXT_ROUNDING

        def fits(units: int) -> bool:
            return self._fits_at(
                shape,
                plan,
                units * step,
                max_seqs,
                kv_dtype,
                usable,
                weight_bytes,
                speculative,
            )

        if not fits(1):
            return 0
        hi = MAX_CONTEXT_SEARCH // step
        if fits(hi):
            return hi * step
        lo = 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if fits(mid):
                lo = mid
            else:
                hi = mid - 1
        result = lo * step
        # Never report a context we have not verified. The search is monotonic
        # so this loop should not run, and it costs nothing if it does not.
        while result > 0 and not self._fits_at(
            shape, plan, result, max_seqs, kv_dtype, usable, weight_bytes, speculative
        ):
            result -= step
        return max(0, result)

    # -- diagnosis --------------------------------------------------------
    #
    # When the verdict is WONT_FIT the reason string is the product. Work out
    # which term is responsible and say what to change.

    def _diagnose(
        self,
        req: FitRequest,
        breakdown: MemoryBreakdown,
        usable: int,
        node: NodeProfile,
        max_ctx: int,
        basis: str = "static",
    ) -> tuple[str, str]:
        over = breakdown.total - usable
        weights_held = breakdown.weights + breakdown.replicated

        if weights_held > usable:
            return "weights", self._weights_reason(
                req, breakdown, usable, node, over, basis
            )

        without_kv = breakdown.total - breakdown.kv_cache
        if without_kv <= usable and breakdown.kv_cache > 0:
            # Everything else fits. Context, concurrency, or cache dtype closes
            # the gap, and we can say by how much.
            return "kv_cache", self._kv_reason(
                req, breakdown, usable, node, max_ctx, over, basis
            )

        return "combined", self._combined_reason(
            req, breakdown, usable, node, over, basis
        )

    def _nodes_phrase(
        self, req: FitRequest, node: NodeProfile, usable: int | None = None
    ) -> str:
        # Under a live budget the static ceiling is not available, so a node
        # count derived from it would name a number that still OOMs.
        if node.addressable_memory <= 0:
            # This machine has no GPU at all, so it does not become one by
            # being bought twice. The search below would walk to the cap and
            # return -1, and rendering that as a node count sends somebody
            # shopping for hardware that cannot run this at any quantity -- a
            # remedy nobody evaluated, stated as a number.
            #
            # Keyed on the machine's ADDRESSABLE ceiling, never on the budget
            # in force: under a live basis the budget is what the node can
            # hand out this second, and a GB10 whose pool is momentarily full
            # is a machine that plainly does have GPU memory. Saying otherwise
            # about real hardware would be a worse lie than the one this
            # branch exists to remove.
            #
            # A CPU node reaches this branch too, and the old sentence was
            # wrong for it in a way worth separating out. "A machine with GPU
            # memory" is a remedy for a Spark that reported nothing; for a Pi
            # deliberately serving on its CPU it reads as "buy different
            # hardware" when the real answer is that this model does not fit
            # in this machine's RAM at any quantity. Both are still "no number
            # of these holds it" -- a CPU node cannot carry a rank of a
            # multi-node plan either, since `shards=False` on the only runtime
            # that places here -- so only the noun changes.
            if node.device_class is DeviceClass.CPU:
                return "more memory than this machine has; no number of these holds it"
            return "a machine with GPU memory; no number of these holds it"
        n = min_nodes_required(
            req.shape,
            node,
            req.context_length,
            req.max_concurrent_seqs,
            req.kv_dtype,
            self.guardrail,
            req.weight_bytes,
            usable=usable,
        )
        if n < 0:
            # -1 does not mean "more of these". `min_nodes_required`'s own
            # docstring: it means "a different machine or a smaller
            # quantization, not more Sparks". Rendering it as "more than 64
            # nodes" named the single remedy the search had just ruled out.
            # The quantization half is left to `_suggest_quant`, which is
            # appended beside this and has actually checked whether one fits.
            return "a different machine"
        return f"{n} node{'s' if n != 1 else ''}"

    def _suggest_quant(
        self, req: FitRequest, breakdown: MemoryBreakdown, usable: int
    ) -> tuple[str, float] | None:
        """The best-quality quantization whose weights would fit this plan,
        holding every other term fixed. None when no supported scheme is small
        enough."""
        shape, plan = req.shape, req.plan
        budget = usable - (breakdown.total - breakdown.weights)
        if budget <= 0:
            return None
        vision = min(max(0, shape.vision_params), shape.total_params)
        params_per_rank = (
            (shape.total_params - vision)
            * stage_fraction(shape, plan.pipeline_parallel)
            / max(1, plan.tensor_parallel)
        )
        if params_per_rank <= 0:
            return None
        allowed_bpp = budget / params_per_rank
        current = shape.bytes_per_param()
        for name in QUANT_SUGGESTION_ORDER:
            bpp = BYTES_PER_PARAM[name]
            if bpp < current and bpp <= allowed_bpp:
                return name, params_per_rank * bpp
        return None

    @staticmethod
    def _split_note(req: FitRequest) -> str:
        """How the per-rank weight figure was divided, and -- when it was not
        -- why not.

        Without this, "weights alone are 141.2 GiB per rank" reads identically
        whether that is a third of the checkpoint or the whole of it, and the
        operator cannot tell "it needs three nodes" from "it needs two nodes
        and we counted twice". Both were seen on the same day:
        ``DeepSeek-V4-Flash-0731`` publishes ONE KV head, so no tensor-parallel
        degree above 1 is legal and it is charged whole correctly, while
        ``gpt-oss-120b`` has eight and splits. Nothing on screen distinguished
        them.

        The legality rule is the one ``_candidate_shards`` already applies: a
        TP degree must divide both head counts.
        """
        shape = req.shape
        tp = max(1, req.plan.tensor_parallel)
        pp = max(1, req.plan.pipeline_parallel)
        ep = max(1, req.plan.expert_parallel)

        # Why TP is 1, when it is: a degree must divide both head counts, so a
        # checkpoint with one KV head has no legal degree above 1 however many
        # machines are free. That is the difference between "buy another node"
        # and "no number of nodes will tensor-shard this".
        tp_impossible = bool(shape.num_kv_heads) and not any(
            shape.num_attention_heads % d == 0 and shape.num_kv_heads % d == 0
            for d in range(2, max(2, req.plan.world_size) + 1)
        )
        heads_clause = (
            f"this checkpoint publishes {shape.num_kv_heads} KV head"
            f"{'s' if shape.num_kv_heads != 1 else ''} against "
            f"{shape.num_attention_heads} attention heads, which admits no "
            f"tensor-parallel degree above 1"
        )

        # When expert parallel really did divide something, "charged whole"
        # would contradict the clause that follows it. Only the dense
        # remainder is whole in that case, and the sentence has to say which.
        whole = (
            "Dense weights charged whole"
            if ep > 1 and shape.routed_expert_params > 0
            else "Charged whole"
        )

        parts: list[str] = []
        if tp > 1 or pp > 1:
            parts.append(f"Charged at TP {tp} x PP {pp}")
        elif req.plan.world_size <= 1:
            head = f"{whole}: one rank, so there is nothing to split"
            parts.append(f"{head} -- and {heads_clause}" if tp_impossible else head)
        elif max(1, req.plan.data_parallel) > 1:
            # Data parallel replicates rather than shards. Every rank holds a
            # full copy by design, so this is correct and needs saying: the
            # per-rank figure is not going to fall by adding data-parallel
            # ranks, which is the fix an operator would otherwise reach for.
            parts.append(
                f"{whole}: data parallel {req.plan.data_parallel} "
                f"replicates the model, so every rank holds a full copy"
                + (f", and {heads_clause}" if tp_impossible else "")
            )
        elif tp_impossible:
            parts.append(f"{whole}: {heads_clause}")
        else:
            parts.append(f"{whole}: this plan splits no dense weights")

        if ep > 1:
            if shape.routed_expert_params > 0:
                share = shape.routed_expert_params / float(shape.total_params or 1)
                parts.append(
                    f"its routed experts are {share:.0%} of the checkpoint and "
                    f"were divided across {ep} expert-parallel ranks"
                )
            else:
                # 0 means the split was never derived, and the gate then
                # charges them whole. Saying so is the difference between a
                # refusal an operator can act on and one they cannot.
                parts.append(
                    f"expert parallel is {ep}, but this checkpoint's routed "
                    f"expert parameters could not be sized, so they are charged "
                    f"whole to every rank"
                )
        return "; ".join(parts)

    def _weights_reason(
        self,
        req: FitRequest,
        breakdown: MemoryBreakdown,
        usable: int,
        node: NodeProfile,
        over: int,
        basis: str = "static",
    ) -> str:
        shape = req.shape
        ranks = req.plan.world_size
        held = breakdown.weights + breakdown.replicated
        replicated_note = (
            f" (including {_gib(breakdown.replicated)} of replicated vision "
            f"weights)"
            if breakdown.replicated
            else ""
        )
        live = basis == "live"
        fixes = [self._nodes_phrase(req, node, usable if live else None)]
        suggestion = self._suggest_quant(req, breakdown, usable)
        if suggestion:
            name, size = suggestion
            fixes.append(f"requantize to {name} ({_gib(size)} per rank)")
        if live and node.addressable_memory <= 0:
            # Under a live basis the sentence normally cites the static
            # ceiling as well, so a reader can see how much of the gap is the
            # operating system. A machine with no GPU has no such ceiling --
            # `usable_memory` is `addressable_memory * guardrail` and both are
            # 0 -- so the clause rendered "its 90% static ceiling is 0.0 GiB",
            # which is the same "quantity of nothing" the branch below was
            # written to remove, reintroduced through the other arm.
            #
            # For a CPU node the live reading is not one of two numbers worth
            # comparing: it is the only number there is.
            where = f"on {node.node_id}, which serves from host memory"
        elif live:
            where = (
                f"on {node.node_id}; its {self.guardrail:.0%} static ceiling is "
                f"{_gib(node.usable_memory(self.guardrail))}"
            )
        elif node.addressable_memory <= 0:
            # No GPU was found on this machine, so there is no ceiling for a
            # percentage to be of. The general branch rendered "90% of 0.0 GiB
            # addressable on " -- with an empty device name, because
            # `gpu_name` is "" on such a profile -- which describes a GPU with
            # nothing left rather than a machine that has no GPU.
            #
            # Split in two once a machine with no GPU could be a serving node.
            # "has no GPU memory at all" is the right sentence for a Spark
            # whose probe came back empty -- something is wrong and the GPU is
            # where to look. It is the wrong sentence for a Pi that is doing
            # exactly what it was chosen to do and has simply run out of RAM,
            # because it describes the machine as broken rather than as small.
            where = (
                f"{node.node_id} serves from host memory and has no GPU"
                if node.device_class is DeviceClass.CPU
                else f"{node.node_id} has no GPU memory at all"
            )
        else:
            where = (
                f"{self.guardrail:.0%} of {_gib(node.addressable_memory)} "
                f"addressable on {node.gpu_name or node.node_id}"
            )
        return (
            f"Won't fit: weights alone are {_gib(held)} per rank{replicated_note} "
            f"against {_gib(usable)} {_budget_word(basis)} "
            f"({where}). {self._split_note(req)}. "
            f"Over budget by {_gib(over)} across the whole per-rank budget, not "
            f"by weights alone. Context and concurrency cannot fix this at "
            f"{shape.dtype} on {ranks} rank{'s' if ranks != 1 else ''} — it "
            f"needs {_options(fixes)}."
        )

    def _kv_reason(
        self,
        req: FitRequest,
        breakdown: MemoryBreakdown,
        usable: int,
        node: NodeProfile,
        max_ctx: int,
        over: int,
        basis: str = "static",
    ) -> str:
        shape, plan = req.shape, req.plan
        divisor = kv_divisor(shape, plan)
        per_seq = kv_cache_bytes(shape, req.context_length, 1, req.kv_dtype) / divisor
        kv_budget = usable - (breakdown.total - breakdown.kv_cache)

        fixes: list[str] = []
        if max_ctx >= CONTEXT_ROUNDING:
            fixes.append(f"drop context to {max_ctx} tokens")
        seqs = self._largest_max_seqs(
            shape, plan, usable, req.context_length, req.kv_dtype,
            req.max_concurrent_seqs - 1, req.weight_bytes,
        )
        if seqs >= 1:
            fixes.append(f"reduce concurrency to {seqs} sequences")
        elem = kv_elem_bytes(req.kv_dtype, shape)
        if elem > 1.0:
            kv_fp8 = breakdown.kv_cache * (1.0 / elem)
            if (breakdown.total - breakdown.kv_cache) + kv_fp8 <= usable:
                fixes.append(
                    f"quantize the KV cache to fp8 ({_gib(kv_fp8)}, which fits)"
                )
            else:
                fixes.append(
                    f"quantize the KV cache to fp8 ({_gib(kv_fp8)}, still short)"
                )
        fixes.append(
            f"spread it over "
            f"{self._nodes_phrase(req, node, usable if basis == 'live' else None)}"
        )

        # The two figures are printed against each other, so they are rendered
        # against each other: at one decimal a near miss shows the same number
        # twice and reads as "15.6 does not fit in 15.6".
        need, have = _gib_vs(breakdown.kv_cache, kv_budget)
        return (
            f"Over budget by {_gib(over)}. KV cache is the problem: "
            f"{need} per rank at {req.context_length} tokens "
            f"x {req.max_concurrent_seqs} sequences, against {have} "
            f"left after weights, activations and overhead"
            f"{' on the machine as it is right now' if basis == 'live' else ''}. "
            f"{_options(fixes, capitalize=True)}."
        )

    def _combined_reason(
        self,
        req: FitRequest,
        breakdown: MemoryBreakdown,
        usable: int,
        node: NodeProfile,
        over: int,
        basis: str = "static",
    ) -> str:
        named = [
            ("weights", breakdown.weights),
            ("KV", breakdown.kv_cache),
            ("activations", breakdown.activations),
            ("comm buffers", breakdown.comm_buffers),
            ("replicated", breakdown.replicated),
            ("framework", breakdown.framework_overhead),
        ]
        terms = ", ".join(f"{name} {_gib(size)}" for name, size in named)
        biggest, biggest_size = max(named, key=lambda item: item[1])
        share = biggest_size / breakdown.total if breakdown.total else 0.0

        if share > 0.5:
            lead = (
                f"{biggest} is the biggest term at {_gib(biggest_size)}, "
                f"{share:.0%} of the budget, but the rest still needs "
                f"{_gib(breakdown.total - biggest_size)} against "
                f"{_gib(usable)} {_budget_word(basis)}, so shedding it alone "
                f"will not close "
                f"the gap"
            )
        else:
            lead = "no single term dominates"

        fixes = [self._nodes_phrase(req, node, usable if basis == "live" else None)]
        seqs = self._largest_max_seqs(
            req.shape, req.plan, usable, req.context_length, req.kv_dtype,
            req.max_concurrent_seqs - 1, req.weight_bytes,
        )
        if seqs >= 1:
            fixes.append(f"reduce concurrency to {seqs} sequences")
        suggestion = self._suggest_quant(req, breakdown, usable)
        if suggestion:
            name, size = suggestion
            fixes.append(f"requantize to {name} ({_gib(size)} per rank)")
        return (
            f"Won't fit: {_gib(breakdown.total)} needed per rank against "
            f"{_gib(usable)} {_budget_word(basis)}, over budget by {_gib(over)}. "
            f"Every term: "
            f"{terms}. Here {lead} — it needs {_options(fixes)}."
        )
