"""The parallelism planner.

Decides tensor, pipeline, expert, and data parallel degrees from measured
hardware facts rather than from a user-supplied guess. Nothing else in the
ecosystem does this: vLLM, SGLang, TensorRT-LLM and Ray all take the degrees
from the user and only validate legality at startup; Dynamo sweeps tensor
parallel offline but has no GB10 profile and profiles a single node only;
sparkrun maps ``--tp N`` straight to N hosts and never chooses N.

The output carries a ``reason`` that the UI shows verbatim. That sentence is the
product: it names the measured bandwidth and the concurrency the decision turned
on, so a person can disagree with the planner on the evidence rather than on
faith. ``rejected`` carries every alternative considered and why it lost, which
is what makes the recommendation checkable instead of magic.

Nothing here hardcodes a bandwidth. If a driver update turns GPUDirect RDMA on
and the measured all-reduce doubles, the answer flips on its own.
"""

from __future__ import annotations

from dataclasses import dataclass

from control_plane.contracts import (
    EP_VIABLE_THRESHOLD,
    TP_VIABLE_THRESHOLD,
    LinkMeasurement,
    ModelShape,
    NodeProfile,
    ParallelismKind,
    ParallelismPlan,
)

from . import comm
from .constants import (
    DEFAULT_KV_DTYPE,
    DEFAULT_PLAN_CONTEXT,
    LATENCY_CONCURRENCY_CEILING,
    MAX_NODES_CONSIDERED,
    MIN_NODES_FOR_CROSS_NODE_EP,
    PIPELINE_INFLIGHT_PER_STAGE,
)
from .fit_bridge import FitHelpers, default_fit_helpers
from .legality import (
    Candidate,
    IllegalDegrees,
    check_degrees,
    enumerate_candidates,
    tp_rejection,
    valid_ep_degrees,
    valid_tp_degrees,
)
from .topology import (
    NodeGroup,
    exclusion_note,
    homogeneous_groups,
    pooled_group,
    pooling_note,
)

LATENCY_TARGET = "latency"


def _node_list(ids: list[str], limit: int = 3) -> str:
    """Render node ids the way a person would say them, without a wall of names."""
    if not ids:
        return "no nodes"
    if len(ids) == 1:
        return ids[0]
    if len(ids) <= limit:
        return f"{', '.join(ids[:-1])} and {ids[-1]}"
    return f"{', '.join(ids[:limit])} and {len(ids) - limit} more"


def _plural(n: int, word: str) -> str:
    """'1 exchange', '72 exchanges'. Reason strings are read by people."""
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


@dataclass(frozen=True)
class _Facts:
    """Everything the ranking depends on, resolved once per call."""

    shape: ModelShape
    group: NodeGroup
    groups: list[NodeGroup]
    link: LinkMeasurement | None
    target: str
    concurrency: int
    context: int
    kv_dtype: str
    min_nodes: int
    #: True when the caller named the nodes and every one of them is in the
    #: group, unlike hardware included. Suppresses the exclusion clause (nothing
    #: was excluded) and arms the pooling warning instead.
    pooled: bool = False

    @property
    def measured(self) -> bool:
        return self.link is not None

    @property
    def link_gbps(self) -> float:
        """All-reduce bandwidth, or 0.0 when nothing has been measured.

        0.0 is not a bandwidth. It is the sentinel the contract uses for "this
        decision was not based on a measurement", and the UI must never render
        it as a speed.
        """
        return self.link.all_reduce_gbps if self.link else 0.0

    @property
    def gdr(self) -> bool:
        return bool(self.link and self.link.gpudirect_rdma)

    @property
    def latency_override(self) -> bool:
        """Batch-1 decode is the one regime where tensor parallel wins here.

        Measured on GPT-OSS-120B across two Sparks: roughly 40 tok/s under
        tensor parallel against 29 under pipeline at single stream. A 2-stage
        pipeline with no batch to fill it idles half the time, and no amount of
        link bandwidth fixes that. Above single stream the ordering reverses.
        """
        return (
            self.target == LATENCY_TARGET
            and self.concurrency <= LATENCY_CONCURRENCY_CEILING
        )

    @property
    def tp_preferred(self) -> bool:
        """Whether tensor parallel is the family to beat.

        Two independent routes to yes:

        1. The measured all-reduce is at or above ``TP_VIABLE_THRESHOLD``, above
           which tensor parallel is competitive for batched serving again.
        2. The workload is single-stream and latency-targeted, where pipeline's
           bubble costs more than tensor parallel's all-reduces.

        With no measurement at all the answer is no. Conservative mode prefers
        pipeline, because pipeline degrades gracefully on a slow link and tensor
        parallel does not.
        """
        if not self.measured:
            return False
        return self.link_gbps >= TP_VIABLE_THRESHOLD or self.latency_override

    @property
    def capacity_impossible(self) -> bool:
        """True when no node count in the fit calculator's search range works.

        The real fit calculator returns ``-1`` for "impossible at any node
        count up to its ceiling" -- a different fact than "needs more nodes
        than this cluster has" (which returns a positive, merely large,
        ``min_nodes``). Reading ``-1`` as a normal count and clamping it into
        a floor produces a floor of 1, which makes a single-node plan look
        legal and lets its reason claim the model fits when the honest answer
        is that nothing was found to fit it anywhere.
        """
        return self.min_nodes < 1

    @property
    def capacity_floor(self) -> int:
        """The node-count floor a legal candidate must meet.

        Equal to ``min_nodes`` in the ordinary case. When capacity is
        impossible at any count, that sentinel must never clamp down to a
        permissive floor of 1 -- it is replaced with the whole group, which
        forces the widest split available rather than silently degrading to a
        single-node candidate wearing a fits-on-one-node reason it has not
        earned.
        """
        if self.capacity_impossible:
            return self.group.size
        return self.min_nodes

    @property
    def second_replica_possible(self) -> bool:
        """Whether the nodes this plan does not use could host another replica.

        This is what makes "use the fewest nodes" the right rule. Spare capacity
        is worth more as an independent replica behind the gateway than as extra
        ranks paying interconnect cost on every token -- but only if there is
        actually enough of it for a whole second copy. When there is not, the
        spare nodes are just idle, and spreading wider is the better answer.
        Impossible capacity is not "spare": there is no legitimate node count to
        double against, so the wide-split answer applies here too.
        """
        if self.capacity_impossible:
            return False
        return self.min_nodes * 2 <= self.group.size

    @property
    def cross_node_ep_allowed(self) -> bool:
        """Whether cross-node expert parallel may even be considered.

        DeepEP assumes InfiniBand or RoCE with GPUDirect and loses most of its
        overlap benefit on a PCIe-fed path without it. Measured internode
        dispatch on a properly equipped H800 cluster is roughly 43 GB/s; the
        Spark link is four times slower than that already-degraded case. So the
        bandwidth gate and the GDR gate are both hard.

        The node-count gate is the third: at EP=2 the expert weights halve,
        which is exactly what TP=2 and PP=2 also do, while an all-to-all is
        added that neither of those needs. Expert parallel has to be wide to be
        worth its collective.
        """
        return (
            self.shape.is_moe
            and self.measured
            and self.link_gbps >= EP_VIABLE_THRESHOLD
            and self.gdr
            and self.group.size >= MIN_NODES_FOR_CROSS_NODE_EP
        )


@dataclass(frozen=True)
class _Scored:
    candidate: Candidate
    kind: ParallelismKind
    family_rank: int
    step_seconds: float
    prefer_fewer_nodes: bool

    @property
    def sort_key(self) -> tuple:
        # Family first, then world size in whichever direction spare capacity
        # argues for, then estimated step time -- which is what separates the
        # hybrid splits from each other, since even splits are not automatically
        # optimal and only the arithmetic can say which one wins.
        world = self.candidate.world_size
        return (
            self.family_rank,
            world if self.prefer_fewer_nodes else -world,
            self.step_seconds,
        )


def _kind_of(cand: Candidate) -> ParallelismKind:
    if cand.world_size == 1:
        return ParallelismKind.SINGLE_NODE
    if cand.ep > 1:
        return ParallelismKind.EXPERT
    if cand.is_hybrid:
        return ParallelismKind.HYBRID
    if cand.tp > 1:
        return ParallelismKind.TENSOR
    return ParallelismKind.PIPELINE


def _family_rank(kind: ParallelismKind, facts: _Facts) -> int:
    """Lower is preferred. This is the decision, everything else is arithmetic."""
    if kind is ParallelismKind.SINGLE_NODE:
        # Never cross the link for a model that fits on one node. A second node
        # is worth more as a second replica behind the gateway's load balancer
        # than as a second rank paying interconnect cost on every token.
        return 0
    if kind is ParallelismKind.EXPERT:
        return 1
    if facts.tp_preferred:
        return {
            ParallelismKind.TENSOR: 2,
            ParallelismKind.HYBRID: 3,
            ParallelismKind.PIPELINE: 4,
        }[kind]
    # Below the threshold, and not single-stream: pipeline moves one activation
    # handoff per token where tensor parallel moves two all-reduces per layer.
    return {
        ParallelismKind.PIPELINE: 2,
        ParallelismKind.HYBRID: 3,
        ParallelismKind.TENSOR: 4,
    }[kind]


class Planner:
    """Implements ``PlannerPort``.

    ``plan`` is the head of ``alternatives``, by construction. The recommended
    plan and the ranked override list can therefore never disagree.
    """

    def __init__(self, fit: FitHelpers | None = None) -> None:
        self._fit = fit if fit is not None else default_fit_helpers()

    # ------------------------------------------------------------------ port

    def plan(
        self,
        shape: ModelShape,
        nodes: list[NodeProfile],
        link: LinkMeasurement | None,
        target: str,
        concurrency: int,
        *,
        context_length: int | None = None,
        kv_dtype: str = DEFAULT_KV_DTYPE,
        allow_mixed_hardware: bool = False,
    ) -> ParallelismPlan:
        """The recommended plan.

        ``context_length`` and ``kv_dtype`` are keyword-only extras beyond the
        frozen ``PlannerPort`` signature. Capacity depends on both, and the port
        does not carry them; callers that do not pass them get
        ``DEFAULT_PLAN_CONTEXT``, which is stated in the reason when it changes
        the answer.
        """
        ranked = self.alternatives(
            shape,
            nodes,
            link,
            target,
            concurrency,
            context_length=context_length,
            kv_dtype=kv_dtype,
            allow_mixed_hardware=allow_mixed_hardware,
        )
        return ranked[0]

    # ------------------------------------------------------------- extra api

    def valid_tp_degrees(self, shape: ModelShape, max_nodes: int) -> set[int]:
        return valid_tp_degrees(shape, max_nodes)

    def alternatives(
        self,
        shape: ModelShape,
        nodes: list[NodeProfile],
        link: LinkMeasurement | None,
        target: str,
        concurrency: int,
        *,
        context_length: int | None = None,
        kv_dtype: str = DEFAULT_KV_DTYPE,
        allow_mixed_hardware: bool = False,
    ) -> list[ParallelismPlan]:
        """Every legal plan, ranked, best first.

        The user can always override. This is a recommendation with its
        reasoning attached, not a lock, so the UI gets the whole ordered list
        and each entry carries its own reason and its own rejected list.
        """
        if not nodes:
            raise ValueError("cannot plan a deployment with no nodes")

        facts = self._facts(
            shape,
            nodes,
            link,
            target,
            concurrency,
            context_length,
            kv_dtype,
            allow_mixed_hardware=allow_mixed_hardware,
        )
        return self._render(self._score(facts), facts)

    def _render(self, scored: list[_Scored], facts: _Facts) -> list[ParallelismPlan]:
        """Ranked candidates to plans, each carrying the others as rejections.

        Split out of `alternatives` so `plan_for` can render an operator's shape
        through exactly this path -- same reason text, same rejected list, same
        single-node filtering rule -- rather than assembling a parallel one.
        """
        lines = [self._rejection_line(s, scored[0], facts) for s in scored]
        preamble = self._structural_rejections(facts)

        plans: list[ParallelismPlan] = []
        for index, chosen in enumerate(scored):
            pre = preamble
            if chosen.kind is ParallelismKind.SINGLE_NODE:
                # A plan must not list its own chosen shape in its rejected
                # list; the reason carries the capacity caveat instead.
                pre = [l for l in preamble if not l.startswith("single node: illegal")]
            rejected = pre + [
                line for other, line in enumerate(lines) if other != index
            ]
            plans.append(
                ParallelismPlan(
                    kind=chosen.kind,
                    tensor_parallel=chosen.candidate.tp,
                    pipeline_parallel=chosen.candidate.pp,
                    expert_parallel=chosen.candidate.ep,
                    data_parallel=chosen.candidate.dp,
                    node_ids=self._nodes_for(chosen, facts),
                    reason=self._reason(chosen, facts),
                    measured_link_gbps=facts.link_gbps,
                    rejected=rejected,
                )
            )
        return plans

    def plan_for(
        self,
        shape: ModelShape,
        nodes: list[NodeProfile],
        link: LinkMeasurement | None,
        target: str,
        concurrency: int,
        *,
        tensor_parallel: int = 1,
        pipeline_parallel: int = 1,
        expert_parallel: int = 1,
        data_parallel: int = 1,
        context_length: int | None = None,
        kv_dtype: str = DEFAULT_KV_DTYPE,
    ) -> ParallelismPlan:
        """The plan for degrees an operator chose, with the planner's own words.

        `agents/E-planner.md` specifies this component as "a recommendation with
        reasoning, not a lock". `alternatives` is the ranked recommendation;
        this is the override it exists to allow.

        Always pooled: the caller named the nodes, so the set and its order are
        the request rather than a suggestion, and silently narrowing it to the
        strongest homogeneous group would serve on fewer machines than were
        asked for. `_reason`'s pooling warning still only fires when those nodes
        are genuinely unlike, so naming a homogeneous set warns about nothing.

        Raises `IllegalDegrees` for a shape that cannot load. Does *not* raise
        for a shape that merely will not fit -- that is the fit gate's sentence
        to say, and saying it here would be a second authority on one fact.
        """
        if not nodes:
            raise ValueError("cannot plan a deployment with no nodes")

        cand = Candidate(
            tp=tensor_parallel,
            pp=pipeline_parallel,
            ep=expert_parallel,
            dp=data_parallel,
        )
        refusals = check_degrees(shape, cand)
        if refusals:
            raise IllegalDegrees(refusals)

        facts = self._facts(
            shape,
            nodes,
            link,
            target,
            concurrency,
            context_length,
            kv_dtype,
            allow_mixed_hardware=True,
        )
        scored = self._score(facts)

        # A single-node plan names one host even when its world size is greater
        # than one (`_nodes_for`), and `_score` may upgrade the winning
        # single-node entry to intra-node TP/EP. So the kind has to be matched
        # alongside the degrees: on a 4-node cluster of 4-GPU boxes, a
        # single-node TP=4 and a cross-node TP=4 share a degree tuple and are
        # not the same plan.
        want_kind = (
            ParallelismKind.SINGLE_NODE if len(nodes) == 1 else _kind_of(cand)
        )
        for index, entry in enumerate(scored):
            if entry.candidate == cand and entry.kind is want_kind:
                return self._render(scored, facts)[index]

        # Legal, but filtered out of the ranking -- the capacity floor drops
        # every shape narrower than `min_nodes`. This is the important case:
        # forcing one node for a model that needs two. Render it through the
        # same path so it still gets planner-authored prose; `_justification`
        # already says the honest thing for it ("offered as the closest
        # available shape, not a working plan -- the fit check will refuse it").
        scored = scored + [
            _Scored(
                candidate=cand,
                kind=want_kind,
                family_rank=_family_rank(want_kind, facts),
                prefer_fewer_nodes=facts.second_replica_possible,
                step_seconds=comm.estimated_step_seconds(
                    facts.shape,
                    facts.group.exemplar,
                    facts.link,
                    cand.tp,
                    cand.pp,
                    cand.ep,
                    cand.dp,
                    facts.concurrency,
                ),
            )
        ]
        return self._render(scored, facts)[-1]

    def explain(self, plan: ParallelismPlan) -> str:
        """Multi-line rendering of a plan and the work behind it."""
        head = f"{plan.kind.value} :: {self._degree_label(plan)}"
        nodes = ", ".join(plan.node_ids) or "no nodes"
        lines = [
            head,
            f"  nodes       {nodes} (world size {plan.world_size})",
            f"  link        {self._link_label(plan.measured_link_gbps)}",
            f"  reason      {plan.reason}",
        ]
        if plan.rejected:
            lines.append("  rejected")
            lines.extend(f"    - {r}" for r in plan.rejected)
        else:
            lines.append("  rejected    nothing else was legal on this cluster")
        return "\n".join(lines)

    # -------------------------------------------------------------- internals

    def _facts(
        self,
        shape: ModelShape,
        nodes: list[NodeProfile],
        link: LinkMeasurement | None,
        target: str,
        concurrency: int,
        context_length: int | None,
        kv_dtype: str,
        allow_mixed_hardware: bool = False,
    ) -> _Facts:
        # `groups` stays the real partition either way, so the pooling warning
        # can name the odd nodes exactly as the exclusion note would have.
        groups = homogeneous_groups(nodes)
        pooled = allow_mixed_hardware and len(nodes) > 0
        group = pooled_group(nodes) if pooled else groups[0]
        context = context_length or DEFAULT_PLAN_CONTEXT
        min_nodes = self._fit.min_nodes_required(
            shape, group.exemplar, context, max(1, concurrency), kv_dtype=kv_dtype
        )
        return _Facts(
            shape=shape,
            group=group,
            groups=groups,
            link=link,
            target=target,
            concurrency=max(1, concurrency),
            context=context,
            kv_dtype=kv_dtype,
            min_nodes=min_nodes,
            pooled=pooled,
        )

    def _score(self, facts: _Facts) -> list[_Scored]:
        cands = enumerate_candidates(
            facts.shape,
            facts.group.size,
            facts.capacity_floor,
            facts.cross_node_ep_allowed,
        )
        if not cands:
            # Only reachable when the group has no usable node count at all.
            cands = [Candidate(tp=1, pp=1, ep=1, dp=1)]

        scored = []
        for cand in cands:
            kind = _kind_of(cand)
            scored.append(
                _Scored(
                    candidate=cand,
                    kind=kind,
                    family_rank=_family_rank(kind, facts),
                    prefer_fewer_nodes=facts.second_replica_possible,
                    step_seconds=comm.estimated_step_seconds(
                        facts.shape,
                        facts.group.exemplar,
                        facts.link,
                        cand.tp,
                        cand.pp,
                        cand.ep,
                        cand.dp,
                        facts.concurrency,
                    ),
                )
            )
        scored.sort(key=lambda s: s.sort_key)

        # A single-node plan may use every GPU in the box. Intra-node links are
        # the case the "tensor parallel inside a node" rule was actually written
        # for, and it holds there.
        head = scored[0]
        if head.kind is ParallelismKind.SINGLE_NODE:
            gpus = max(1, facts.group.exemplar.gpu_count)
            if gpus > 1:
                # Route through the same legality rules as every other
                # candidate rather than assuming ``gpus`` itself divides the
                # model: a 6-GPU node with a 64-head/8-KV-head dense model
                # cannot run TP=6 (neither count is divisible by 6) and would
                # fail at load. Use the largest degree that actually divides,
                # and note the idle GPUs when that degree falls short.
                if facts.shape.is_moe:
                    ep = max(valid_ep_degrees(facts.shape, gpus))
                    tp = 1
                else:
                    tp = max(valid_tp_degrees(facts.shape, gpus))
                    ep = 1
                scored[0] = _Scored(
                    candidate=Candidate(tp=tp, pp=1, ep=ep, dp=1),
                    kind=ParallelismKind.SINGLE_NODE,
                    family_rank=head.family_rank,
                    prefer_fewer_nodes=head.prefer_fewer_nodes,
                    step_seconds=head.step_seconds,
                )
        return scored

    def _nodes_for(self, chosen: _Scored, facts: _Facts) -> list[str]:
        """Hosts this plan occupies.

        A single-node plan names one host even when its world size is greater
        than one, because those ranks are GPUs in the same box rather than
        separate machines.
        """
        if chosen.kind is ParallelismKind.SINGLE_NODE:
            return facts.group.node_ids[:1]
        return facts.group.node_ids[: max(1, chosen.candidate.world_size)]

    # ------------------------------------------------------------- narration

    def _degree_label(self, plan: ParallelismPlan) -> str:
        if plan.kind is ParallelismKind.SINGLE_NODE:
            if plan.expert_parallel > 1:
                return f"single node, EP={plan.expert_parallel} within it"
            if plan.tensor_parallel > 1:
                return f"single node, TP={plan.tensor_parallel} within it"
            return "single node"
        if plan.expert_parallel > 1:
            return f"DP={plan.data_parallel} attention + EP={plan.expert_parallel}"
        parts = []
        if plan.tensor_parallel > 1:
            parts.append(f"TP={plan.tensor_parallel}")
        if plan.pipeline_parallel > 1:
            parts.append(f"PP={plan.pipeline_parallel}")
        return "/".join(parts) or "single node"

    def _link_label(self, gbps: float) -> str:
        if gbps <= 0:
            return "not measured"
        return f"{gbps:.1f} GB/s measured all-reduce"

    def _reason(self, chosen: _Scored, facts: _Facts) -> str:
        cand = chosen.candidate
        nodes = _node_list(self._nodes_for(chosen, facts))
        label = cand.label()
        sentence = f"{label} across {nodes}" if cand.world_size > 1 else f"{label} on {nodes}"

        body = self._justification(chosen, facts)
        # Nothing was excluded when the caller named the nodes, so the exclusion
        # clause would be a false statement. The hazard is stated affirmatively
        # in the pooling warning below instead -- it is a full sentence, so it
        # belongs with the other extras rather than in the `; ...` clause slot.
        note = "" if facts.pooled else exclusion_note(facts.group, facts.groups)
        reason = f"{sentence}: {body}{note}."

        for extra in (
            self._bubble_warning(chosen, facts),
            self._capacity_warning(facts),
            self._pooling_warning(facts),
        ):
            if extra:
                reason = f"{reason} {extra}"
        return reason

    def _pooling_warning(self, facts: _Facts) -> str:
        """The cost of a pool of unlike hardware, when one was asked for.

        Empty unless the caller both named the nodes and named nodes that are
        not alike: pooling machines that are interchangeable is not a hazard,
        and warning about it would teach people to ignore the warning.
        """
        if not facts.pooled:
            return ""
        return pooling_note(homogeneous_groups(facts.group.nodes))

    def _justification(self, chosen: _Scored, facts: _Facts) -> str:
        cand = chosen.candidate
        c = facts.concurrency
        bw = f"{facts.link_gbps:.1f} GB/s"
        thresh = f"{TP_VIABLE_THRESHOLD:.0f} GB/s"

        if chosen.kind is ParallelismKind.SINGLE_NODE:
            if facts.capacity_impossible:
                return (
                    f"no node count in the search range fits this model at "
                    f"{facts.context} context and concurrency {c}; this is "
                    f"offered as the closest available shape, not a working "
                    f"plan, and the fit check will refuse it"
                )

            if facts.min_nodes > 1:
                # Chosen only as the best available shape: the model does NOT
                # fit here, and the lead clause must never claim it does
                # (WF-5 finding: "fits within N GiB" on a model needing more
                # nodes than the group has, or with no legal multi-node shape).
                return (
                    f"one node cannot hold this model: capacity needs "
                    f"{facts.min_nodes} nodes at {facts.context} context and "
                    f"concurrency {c}, and no legal shape in this "
                    f"{facts.group.size}-node group covers it; offered as the "
                    f"closest available shape, not a working plan -- the fit "
                    f"check will refuse it until context, concurrency, or "
                    f"quantization changes"
                )

            usable = facts.group.exemplar.usable_memory() / 1024**3
            spare = facts.group.size - 1
            gpus = max(1, facts.group.exemplar.gpu_count)
            degree = max(cand.tp, cand.ep, 1)
            idle_note = ""
            if degree < gpus:
                what = "expert count" if facts.shape.is_moe else "head counts"
                idle_note = (
                    f"; only {degree} of {gpus} GPUs on this node divide the "
                    f"{what} evenly, so {_plural(gpus - degree, 'GPU')} "
                    f"{'sits' if gpus - degree == 1 else 'sit'} idle"
                )
            tail = (
                f", so nothing crosses the interconnect{idle_note}; run a "
                f"second replica on {facts.group.node_ids[1]} and let the "
                f"gateway load balance"
                if spare >= 1
                else f", so nothing crosses an interconnect{idle_note}"
            )
            return (
                f"the model fits within {usable:.1f} GiB of usable memory on one node "
                f"at {facts.context} context and concurrency {c}{tail}"
            )

        if chosen.kind is ParallelismKind.EXPERT:
            return (
                f"measured all-reduce is {bw} with GPUDirect RDMA enabled and the group "
                f"is {facts.group.size} nodes wide, so the MoE all-to-all keeps its "
                f"overlap benefit and sharding {facts.shape.num_experts} experts "
                f"{cand.ep} ways beats replicating them at concurrency {c}"
            )

        if not facts.measured:
            return (
                f"no link measurement is available, so this falls back to pipeline "
                f"parallel, which moves one activation handoff per token instead of "
                f"{comm.tensor_exchanges_per_step(facts.shape, 2)} all-reduces and "
                f"degrades gracefully at unknown bandwidth -- measure the link to get "
                f"a bandwidth-derived plan"
            )

        if chosen.kind is ParallelismKind.PIPELINE:
            tp_bytes = comm.tensor_bytes_per_step(facts.shape, 2, c)
            pp_bytes = comm.pipeline_bytes_per_step(facts.shape, cand.pp, c)
            return (
                f"measured all-reduce is {bw}, below the {thresh} threshold where "
                f"tensor parallel becomes competitive, and at concurrency {c} pipeline "
                f"moves {comm.human_bytes(pp_bytes)} per step over "
                f"{_plural(comm.pipeline_exchanges_per_step(cand.pp), 'exchange')} against "
                f"tensor parallel's {comm.human_bytes(tp_bytes)} over "
                f"{_plural(comm.tensor_exchanges_per_step(facts.shape, 2), 'exchange')}"
            )

        if facts.latency_override and facts.link_gbps < TP_VIABLE_THRESHOLD:
            return (
                f"at concurrency {c} with a latency target there is no batch to fill a "
                f"pipeline and half the stages would idle, so tensor parallel wins "
                f"single-stream decode despite the {bw} link being below the {thresh} "
                f"threshold that governs batched serving"
            )

        if facts.link_gbps < TP_VIABLE_THRESHOLD:
            # Only reachable when a caller asked for tensor parallel at a
            # bandwidth the planner would not have picked it at (`plan_for`).
            # The clause below is the planner's argument *for* tensor parallel
            # and would be a false statement here -- it would report a measured
            # 10.2 GB/s as "at or above the 40 GB/s threshold". State the
            # measurement against the shape instead, and say plainly that the
            # shape was asked for. The recommendation the operator overruled
            # travels back beside this plan, with its own reason intact.
            return (
                f"measured all-reduce is {bw}, below the {thresh} threshold "
                f"where tensor parallel becomes competitive -- at concurrency "
                f"{c} it moves "
                f"{comm.human_bytes(comm.tensor_bytes_per_step(facts.shape, cand.tp, c))} "
                f"per step over "
                f"{_plural(comm.tensor_exchanges_per_step(facts.shape, cand.tp), 'exchange')} "
                f"-- so this shape was asked for rather than chosen; the planner "
                f"does not rank tensor parallel first at this bandwidth"
            )

        return (
            f"measured all-reduce is {bw}, at or above the {thresh} threshold, so "
            f"tensor parallel's "
            f"{comm.tensor_exchanges_per_step(facts.shape, cand.tp)} all-reduces per "
            f"token are affordable and it is the stronger choice for batched serving "
            f"at concurrency {c}"
        )

    def _bubble_warning(self, chosen: _Scored, facts: _Facts) -> str:
        """Fires when concurrency is too low to amortise the pipeline bubble."""
        pp = chosen.candidate.pp
        if pp <= 1:
            return ""
        floor = PIPELINE_INFLIGHT_PER_STAGE * pp
        if facts.concurrency >= floor:
            return ""
        bubble = comm.pipeline_bubble_fraction(pp, facts.concurrency) * 100
        return (
            f"Warning: at {facts.concurrency} in-flight requests against {pp} stages "
            f"the pipeline bubble is roughly {bubble:.0f} percent and grows sharply "
            f"below {floor}; tensor parallel may serve better at this concurrency."
        )

    def _capacity_warning(self, facts: _Facts) -> str:
        if facts.capacity_impossible:
            return (
                f"Warning: no node count in the search range fits this model "
                f"at {facts.context} context and concurrency {facts.concurrency}, "
                f"so no plan on this cluster will pass the fit check no matter "
                f"how many nodes are used; reduce context, concurrency, or "
                f"quantization."
            )
        if facts.min_nodes <= facts.group.size:
            return ""
        if facts.min_nodes >= MAX_NODES_CONSIDERED:
            need = f"more than {MAX_NODES_CONSIDERED}"
        else:
            need = str(facts.min_nodes)
        return (
            f"Warning: capacity needs {need} nodes at {facts.context} context and "
            f"concurrency {facts.concurrency} but the group has {facts.group.size}, "
            f"so this plan is the best available shape and the fit check will refuse "
            f"it until context, concurrency, or quantization changes."
        )

    # -------------------------------------------------------------- rejected

    def _structural_rejections(self, facts: _Facts) -> list[str]:
        """Alternatives ruled out before ranking: illegal degrees and hard gates.

        This is the part that makes the tool trustworthy rather than magic. An
        option nobody can see was rejected looks like an option nobody
        considered.
        """
        out: list[str] = []
        shape = facts.shape

        # The sentence lives in legality.tp_rejection so that a degree the
        # operator names by hand is refused in these exact words. See the note
        # above the refusal helpers there.
        for tp in range(2, facts.group.size + 1):
            line = tp_rejection(shape, tp)
            if line:
                out.append(line)

        if facts.capacity_impossible:
            out.append(
                f"single node: illegal, no node count in the search range fits "
                f"this model at {facts.context} context and concurrency "
                f"{facts.concurrency}"
            )
        elif facts.min_nodes > 1:
            out.append(
                f"single node: illegal, the model needs {facts.min_nodes} nodes at "
                f"{facts.context} context and concurrency {facts.concurrency}, so one "
                f"node cannot hold it"
            )

        if shape.is_moe and not facts.cross_node_ep_allowed and facts.group.size > 1:
            out.append(self._ep_rejection(facts))

        return out

    def _ep_rejection(self, facts: _Facts) -> str:
        ep = facts.group.size
        if not facts.measured:
            why = (
                "no link measurement is available and cross-node all-to-all cannot be "
                "justified on an unmeasured path"
            )
        elif not facts.gdr:
            why = (
                "GPUDirect RDMA is disabled, so the cross-node all-to-all loses its "
                "overlap benefit on a PCIe-fed path"
            )
        elif facts.link_gbps < EP_VIABLE_THRESHOLD:
            why = (
                f"measured all-reduce {facts.link_gbps:.1f} GB/s is below the "
                f"{EP_VIABLE_THRESHOLD:.0f} GB/s threshold; measured internode dispatch "
                f"on a properly equipped H800 cluster is roughly 43 GB/s and this link "
                f"is well under that already-degraded case"
            )
        else:
            why = (
                f"only {facts.group.size} nodes are available and expert parallel needs "
                f"at least {MIN_NODES_FOR_CROSS_NODE_EP} to buy anything tensor or "
                f"pipeline parallel does not already buy, while still adding an "
                f"all-to-all neither of those needs"
            )
        return f"EP={ep}: {why}"

    def _rejection_line(self, cand: _Scored, chosen: _Scored, facts: _Facts) -> str:
        label = cand.candidate.label()
        c = facts.concurrency
        bw = f"{facts.link_gbps:.1f} GB/s"

        # When the model fits on one node, every multi-node option lost for the
        # same reason and it is not a bandwidth argument: crossing the link is
        # pure cost. Saying anything about thresholds here would be nonsense.
        if chosen.kind is ParallelismKind.SINGLE_NODE and cand.kind is not ParallelismKind.SINGLE_NODE:
            exch = max(
                comm.tensor_exchanges_per_step(facts.shape, cand.candidate.tp),
                comm.pipeline_exchanges_per_step(cand.candidate.pp),
                comm.expert_exchanges_per_step(facts.shape, cand.candidate.ep),
            )
            where = f"a {bw} link" if facts.measured else "an unmeasured link"
            return (
                f"{label}: the model already fits on one node at {facts.context} context "
                f"and concurrency {c}, so splitting it across {where} would add "
                f"{_plural(exch, 'cross-node exchange')} per token and buy nothing; a "
                f"second replica on the spare node serves more total throughput"
            )

        if cand.kind is ParallelismKind.TENSOR and chosen.kind is not ParallelismKind.TENSOR:
            tp = cand.candidate.tp
            exch = comm.tensor_exchanges_per_step(facts.shape, tp)
            volume = comm.human_bytes(comm.tensor_bytes_per_step(facts.shape, tp, c))
            if not facts.measured:
                return (
                    f"{label}: no measurement to justify "
                    f"{_plural(exch, 'cross-node all-reduce')} per token; pipeline is "
                    f"the safe default until the link is probed"
                )
            if facts.tp_preferred:
                # The link is fast enough for tensor parallel; it lost to
                # something else. Saying it failed the threshold would be false,
                # and so would claiming the winner moves fewer bytes -- an
                # expert all-to-all usually moves more, and wins anyway because
                # it overlaps with compute where a chain of all-reduces cannot.
                return (
                    f"{label}: legal, and at {bw} the link can carry its "
                    f"{_plural(exch, 'all-reduce')} and {volume} per step, but "
                    f"{_plural(exch, 'serialised collective')} per token leaves no "
                    f"room to overlap communication with compute, so it ranks below "
                    f"{chosen.candidate.label()} at concurrency {c}"
                )
            return (
                f"{label}: measured all-reduce {bw} is below the "
                f"{TP_VIABLE_THRESHOLD:.0f} GB/s threshold; "
                f"{comm.ALLREDUCES_PER_LAYER} all-reduces per layer across "
                f"{facts.shape.num_layers} layers is {_plural(exch, 'cross-node exchange')} "
                f"and {volume} per step, which would dominate at concurrency {c}"
            )

        if cand.kind is ParallelismKind.PIPELINE and chosen.kind is not ParallelismKind.PIPELINE:
            pp = cand.candidate.pp
            bubble = comm.pipeline_bubble_fraction(pp, c) * 100
            if facts.latency_override:
                return (
                    f"{label}: at concurrency {c} there is no batch to fill the pipe, "
                    f"so roughly {bubble:.0f} percent of decode time would be bubble "
                    f"and single-stream latency suffers"
                )
            return (
                f"{label}: cheaper on the wire, but the measured link at {bw} is fast "
                f"enough that tensor parallel's all-reduces are affordable and pipeline "
                f"still pays a {bubble:.0f} percent bubble at concurrency {c}"
            )

        if cand.kind is ParallelismKind.HYBRID:
            tp = cand.candidate.tp
            exch = comm.tensor_exchanges_per_step(facts.shape, tp)
            return (
                f"{label}: legal, but the tensor-parallel half still pays "
                f"{_plural(exch, 'cross-node all-reduce')} per token at {bw}, and "
                f"even splits are not "
                f"automatically optimal -- this one ranks below {chosen.candidate.label()} "
                f"at concurrency {c}"
            )

        if cand.kind is ParallelismKind.EXPERT:
            return (
                f"{label}: legal on this link, but ranked below "
                f"{chosen.candidate.label()} at concurrency {c}"
            )

        if cand.kind is ParallelismKind.SINGLE_NODE:
            return (
                f"{label}: the model fits on one node, which is why it wins; listed so "
                f"the multi-node options above can be compared against it"
            )

        return (
            f"{label}: legal, ranked below {chosen.candidate.label()} at "
            f"concurrency {c}"
        )
