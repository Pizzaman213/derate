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
from collections.abc import Mapping

from control_plane.contracts import (
    BYTES_PER_PARAM,
    COMM_BUFFER_BYTES,
    DEFAULT_GUARDRAIL,
    DEGRADED_TPS_THRESHOLD,
    EP_EXTRA_BUFFER_BYTES,
    FRAMEWORK_OVERHEAD,
    FitRequest,
    FitResult,
    MemoryBreakdown,
    ModelShape,
    NodeProfile,
    ParallelismKind,
    ParallelismPlan,
    Verdict,
)

from .constants import (
    ACTIVATION_CHUNK_TOKENS,
    CONTEXT_ROUNDING,
    DECODE_EFFICIENCY,
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


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------


def _gib(n: float) -> str:
    return f"{n / GIB:.1f} GiB"


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
# exported helpers, called by Agent E
# --------------------------------------------------------------------------


def predict_decode_tps(
    shape: ModelShape, bandwidth_gbps: float, kv_read_bytes: float
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
    """
    if bandwidth_gbps <= 0:
        return 0.0
    weight_bytes_per_token = shape.effective_active_params * shape.bytes_per_param()
    bytes_per_token = weight_bytes_per_token + max(0.0, kv_read_bytes)
    if bytes_per_token <= 0:
        return 0.0
    ceiling = (bandwidth_gbps * 1e9) / bytes_per_token
    return ceiling * DECODE_EFFICIENCY


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
    link bandwidth is Agent E's call, not this function's.

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

    Exported for Agent E.
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
    """
    bpp = shape.bytes_per_param()
    vision = min(max(0, shape.vision_params), shape.total_params)
    vision_bytes = vision * bpp
    if total_weight_bytes is not None and total_weight_bytes > 0:
        shardable_bytes = max(0.0, total_weight_bytes - vision_bytes)
    else:
        shardable_bytes = (shape.total_params - vision) * bpp
    tp = max(1, plan.tensor_parallel)
    return shardable_bytes * stage_fraction(shape, plan.pipeline_parallel) / tp


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
) -> tuple[MemoryBreakdown, list[str]]:
    """Per-rank memory, and everything worth warning about while computing it.

    ``weight_bytes`` is the resolver's measured total, forwarded to
    :func:`weight_bytes_per_rank`; ``None`` (the default) prices weights from
    the dtype formula instead.
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

    kv_total = kv_cache_bytes(shape, context, max_seqs, kv_dtype)
    breakdown = MemoryBreakdown(
        weights=int(weight_bytes_per_rank(shape, plan, weight_bytes)),
        kv_cache=int(kv_total / kv_divisor(shape, plan)),
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

    ``check`` is the blocking gate: Agent F refuses to launch on its verdict,
    Agent G refuses to admit on its numbers. ``FITS_DEGRADED`` is not a
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
        )
        warnings.extend(budget_warnings)
        headroom = usable - breakdown.total

        kv_read = kv_cache_bytes(shape, req.context_length, 1, req.kv_dtype)
        tps = predict_decode_tps(shape, bandwidth, kv_read)
        max_ctx = self._largest_context(
            shape,
            plan,
            usable,
            req.max_concurrent_seqs,
            req.kv_dtype,
            weight_bytes=req.weight_bytes,
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
                    f"x DP {plan.data_parallel}) but only {ranks} GPU"
                    f"{'s' if ranks != 1 else ''} were supplied. Add "
                    f"{plan.world_size - ranks} more, or replan for {ranks}."
                ),
                limiting_term="combined",
                # A plan that cannot be placed on the supplied ranks has no
                # meaningful context suggestion: max_ctx was computed under a
                # sharding that does not exist here and would not round-trip.
                max_context_that_fits=None,
                predicted_decode_tps=tps,
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
                warnings=_dedup(warnings),
                budget_basis=basis,
            )

        if tps < DEGRADED_TPS_THRESHOLD:
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
                f"rank, {_gib(headroom)} headroom. Predicted decode "
                f"{tps:.0f} tok/s. Context could go to {ceiling}"
                f"{max_ctx} tokens at {req.max_concurrent_seqs} sequences."
            ),
            limiting_term="none",
            max_context_that_fits=max_ctx,
            predicted_decode_tps=tps,
            warnings=_dedup(warnings),
            budget_basis=basis,
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
    ) -> int:
        """Largest context that fits this plan on these nodes, on a
        ``CONTEXT_ROUNDING`` grain. Verified, never extrapolated. 0 means not
        even one page of cache fits.

        ``weight_bytes``, when given, prices the weights term from the
        resolver's measured total instead of the dtype formula -- see
        :func:`weight_bytes_per_rank`. Optional and additive: every existing
        caller that omits it gets the formula basis unchanged.
        """
        used, _ = self._participating(plan, nodes)
        _, usable, _, _ = self._budget(used, allocatable)
        return self._largest_context(
            shape, plan, usable, max_seqs, kv_dtype, weight_bytes=weight_bytes
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
    ) -> bool:
        breakdown, _ = memory_breakdown(
            shape, plan, context, max_seqs, kv_dtype, self.chunk_tokens, weight_bytes
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
    ) -> int:
        step = CONTEXT_ROUNDING

        def fits(units: int) -> bool:
            return self._fits_at(
                shape, plan, units * step, max_seqs, kv_dtype, usable, weight_bytes
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
            shape, plan, result, max_seqs, kv_dtype, usable, weight_bytes
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
            return f"more than {MAX_SEARCH_NODES} nodes"
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
        where = (
            f"on {node.node_id}; its {self.guardrail:.0%} static ceiling is "
            f"{_gib(node.usable_memory(self.guardrail))}"
            if live
            else f"{self.guardrail:.0%} of {_gib(node.addressable_memory)} "
            f"addressable on {node.gpu_name}"
        )
        return (
            f"Won't fit: weights alone are {_gib(held)} per rank{replicated_note} "
            f"against {_gib(usable)} {_budget_word(basis)} "
            f"({where}). Over budget by {_gib(over)} in "
            f"total. Context and concurrency cannot fix this at "
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

        return (
            f"Over budget by {_gib(over)}. KV cache is the problem: "
            f"{_gib(breakdown.kv_cache)} per rank at {req.context_length} tokens "
            f"x {req.max_concurrent_seqs} sequences, against {_gib(kv_budget)} "
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
