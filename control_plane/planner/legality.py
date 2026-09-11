"""Which parallelism degrees are legal at all, before any preference applies.

Legality is not a matter of taste. A tensor-parallel degree that does not divide
the head counts will fail at model load, and the planner must never emit one.
"""

from __future__ import annotations

from dataclasses import dataclass

from control_plane.contracts import ModelShape

from .constants import MIN_NODES_FOR_CROSS_NODE_EP


def valid_tp_degrees(shape: ModelShape, max_nodes: int) -> set[int]:
    """Tensor-parallel degrees this model can legally run at, up to ``max_nodes``.

    Both constraints are hard:

      ``num_attention_heads % tp == 0`` -- query heads are split across ranks.
      ``num_kv_heads % tp == 0``        -- KV heads are split too, and under GQA
                                          there are far fewer of them. This is
                                          the binding constraint in practice: a
                                          model with 64 query heads and 8 KV
                                          heads caps at TP=8, not TP=64.

    1 is always present: not sharding is always legal.
    """
    degrees = {1}
    if max_nodes < 2:
        return degrees
    for tp in range(2, max_nodes + 1):
        if shape.num_attention_heads % tp == 0 and shape.num_kv_heads % tp == 0:
            degrees.add(tp)
    return degrees


def valid_ep_degrees(shape: ModelShape, max_nodes: int) -> set[int]:
    """Expert-parallel degrees this MoE model can legally run at, up to ``max_nodes``.

    ``num_experts % ep == 0`` -- an uneven split leaves one rank holding an
    extra expert, and every all-to-all waits on the slowest rank. 1 is always
    present: not sharding experts is always legal, including for a dense model
    (``num_experts == 0``), for which no wider degree ever divides evenly.
    """
    degrees = {1}
    if max_nodes < 2 or not shape.num_experts:
        return degrees
    for ep in range(2, max_nodes + 1):
        if shape.num_experts % ep == 0:
            degrees.add(ep)
    return degrees


def valid_pp_degrees(shape: ModelShape, max_nodes: int) -> set[int]:
    """Pipeline-parallel degrees this model can legally run at.

    No head constraint, and uneven layer splits are tolerated: an 80-layer model
    over 3 stages is 27/27/26 and runs fine. The only real bound is that a stage
    must own at least one layer.
    """
    ceiling = min(max_nodes, shape.num_layers)
    return set(range(1, max(1, ceiling) + 1))


@dataclass(frozen=True)
class Candidate:
    """One legal parallelism configuration, before ranking."""

    tp: int
    pp: int
    ep: int
    dp: int

    @property
    def world_size(self) -> int:
        return self.tp * self.pp * self.dp

    @property
    def is_hybrid(self) -> bool:
        return self.tp > 1 and self.pp > 1

    def label(self) -> str:
        if self.world_size == 1:
            return "single node"
        if self.ep > 1:
            # Which axis carries attention is the whole difference between the
            # two legal EP shapes, and the label has to say which one this is:
            # `tp=1, dp=ep` is data-parallel attention across machines, while
            # `tp=ep, dp=1` is the same expert split inside one box with
            # attention sharded by the very ranks the experts ride on.
            if self.dp > 1:
                return f"DP={self.dp} attention + EP={self.ep}"
            return f"TP={self.tp} attention + EP={self.ep}"
        parts = []
        if self.tp > 1:
            parts.append(f"TP={self.tp}")
        if self.pp > 1:
            parts.append(f"PP={self.pp}")
        return "/".join(parts) if parts else "single node"


def enumerate_candidates(
    shape: ModelShape, available_nodes: int, min_nodes: int, cross_node_ep_allowed: bool
) -> list[Candidate]:
    """Every legal configuration on this many nodes that meets the capacity floor.

    Hybrid TP x PP splits are enumerated rather than assumed away. Published
    sweeps found TP2/PP8 beating TP4/PP4 for one model and TP4/PP4 beating both
    alternatives for another, so symmetry is not automatically optimal and the
    ranking has to actually look at each combination.
    """
    if available_nodes < 1:
        return []

    floor = max(1, min(min_nodes, available_nodes))
    tps = valid_tp_degrees(shape, available_nodes)
    pps = valid_pp_degrees(shape, available_nodes)

    seen: set[tuple[int, int, int, int]] = set()
    out: list[Candidate] = []

    for tp in sorted(tps):
        for pp in sorted(pps):
            world = tp * pp
            if world < floor or world > available_nodes:
                continue
            cand = Candidate(tp=tp, pp=pp, ep=1, dp=1)
            if (tp, pp, 1, 1) not in seen:
                seen.add((tp, pp, 1, 1))
                out.append(cand)

    # Data-parallel attention with expert parallel, for MoE only. The attention
    # ranks each serve a slice of the batch while the experts shard across all
    # of them, which is what EP=DPxTP means in vLLM.
    if shape.is_moe and cross_node_ep_allowed:
        for world in range(max(floor, MIN_NODES_FOR_CROSS_NODE_EP), available_nodes + 1):
            # Experts must divide evenly across ranks. An uneven split leaves one
            # rank holding an extra expert, and since every all-to-all waits on
            # the slowest rank, that rank sets the pace for the whole step.
            if world > shape.num_experts or shape.num_experts % world:
                continue
            key = (1, 1, world, world)
            if key not in seen:
                seen.add(key)
                out.append(Candidate(tp=1, pp=1, ep=world, dp=world))

    return out


# ---------------------------------------------------------------- refusals
#
# The sentences below are the planner's own. They were lifted verbatim out of
# `Planner._structural_rejections`, which now calls into them, so a degree the
# operator asked for by hand is refused in exactly the words the planner uses
# when it rules the same degree out on its own. Two spellings of one refusal is
# how a vocabulary drifts; one function is how it cannot.


def tp_rejection(shape: ModelShape, tp: int) -> str | None:
    """Why this tensor-parallel degree is illegal, or None when it is legal."""
    if tp < 1:
        return f"TP={tp}: illegal, a parallelism degree is at least 1"
    bad = []
    if shape.num_attention_heads % tp:
        bad.append(f"num_attention_heads={shape.num_attention_heads}")
    if shape.num_kv_heads % tp:
        bad.append(f"num_kv_heads={shape.num_kv_heads}")
    if not bad:
        return None
    return (
        f"TP={tp}: illegal, "
        + " and ".join(f"{b} is not divisible by {tp}" for b in bad)
        + " for this model"
    )


def pp_rejection(shape: ModelShape, pp: int) -> str | None:
    """Why this pipeline-parallel degree is illegal, or None when it is legal.

    The only structural bound is that a stage must own at least one layer;
    uneven splits are fine (an 80-layer model over 3 stages is 27/27/26).
    """
    if pp < 1:
        return f"PP={pp}: illegal, a parallelism degree is at least 1"
    if pp > shape.num_layers:
        return (
            f"PP={pp}: illegal, the model has {shape.num_layers} layers and a "
            f"stage must own at least one"
        )
    return None


def ep_rejection(shape: ModelShape, ep: int, tp: int, dp: int) -> str | None:
    """Why this expert-parallel degree is illegal, or None when it is legal.

    The pairing rule is vLLM's own and not a house preference: expert parallel
    has no rank group of its own, it re-uses the ones tensor and data parallel
    already built, so the expert-parallel size IS ``dp * tp``. Exactly two
    shapes satisfy that and both are real -- ``tp=1, dp=ep`` is the cross-node
    plan `enumerate_candidates` emits, and ``tp=ep, dp=1`` is the same idea
    inside one multi-GPU box. Anything else asks the runtime for one degree and
    gets another, silently: nothing in vLLM reads an expert-parallel number,
    only the boolean flag.
    """
    if ep < 1:
        return f"EP={ep}: illegal, a parallelism degree is at least 1"
    if ep == 1:
        return None
    if not shape.num_experts:
        return f"EP={ep}: illegal, this is a dense model and has no experts to shard"
    if shape.num_experts % ep:
        return (
            f"EP={ep}: illegal, num_experts={shape.num_experts} is not divisible "
            f"by {ep}; an uneven split leaves one rank holding an extra expert, "
            f"and every all-to-all waits on the slowest rank"
        )
    if ep != tp * dp:
        return (
            f"EP={ep} requires DP x TP = {ep}, and this plan is DP={dp} x TP={tp} "
            f"= {tp * dp}: vLLM builds no rank group for expert parallel, it "
            f"shards the experts across the ranks data and tensor parallel "
            f"already made, so the only shapes that mean EP={ep} are DP={ep} "
            f"attention ranks with TP=1, or TP={ep} inside one box with DP=1"
        )
    return None


def dp_rejection(shape: ModelShape, dp: int) -> str | None:
    """Why this data-parallel degree is illegal, or None when it is legal."""
    if dp < 1:
        return f"DP={dp}: illegal, a parallelism degree is at least 1"
    return None


@dataclass(frozen=True)
class DegreeRefusal:
    """One illegal degree, named by the wire field that carried it.

    ``axis`` is the request field (``tensor_parallel``, ...) rather than the
    short label, so a 400 can point at the exact key the operator sent while
    ``message`` still speaks the planner's own vocabulary.
    """

    axis: str
    degree: int
    message: str


class IllegalDegrees(ValueError):
    """Degrees that cannot run at all, with the planner's reason for each.

    Raised rather than returned because there is no plan to hand back: a shape
    that fails head divisibility does not load, so there is nothing for the fit
    gate to have an opinion about.
    """

    def __init__(self, refusals: list[DegreeRefusal]) -> None:
        self.refusals = refusals
        super().__init__("; ".join(r.message for r in refusals))


def check_degrees(shape: ModelShape, cand: Candidate) -> list[DegreeRefusal]:
    """Every reason this configuration cannot run, in axis order.

    Deliberately does NOT check that the world size fits the supplied ranks.
    ``fit.calculator.check`` already refuses that with a better sentence -- it
    knows how many GPUs were actually offered and says how many more are needed
    -- and two authorities on one fact is worse than one. Legality here means
    "this shape cannot load", not "this shape does not fit".
    """
    checks = (
        ("tensor_parallel", cand.tp, tp_rejection(shape, cand.tp)),
        ("pipeline_parallel", cand.pp, pp_rejection(shape, cand.pp)),
        ("expert_parallel", cand.ep, ep_rejection(shape, cand.ep, cand.tp, cand.dp)),
        ("data_parallel", cand.dp, dp_rejection(shape, cand.dp)),
    )
    return [
        DegreeRefusal(axis=axis, degree=degree, message=message)
        for axis, degree, message in checks
        if message is not None
    ]
