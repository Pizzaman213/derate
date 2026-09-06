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
            return f"DP={self.dp} attention + EP={self.ep}"
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
