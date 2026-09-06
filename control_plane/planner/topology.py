"""Grouping nodes into sets it is sane to serve one model across.

A 3090 desktop and a Spark have different memory sizes, different bandwidth, and
a slower link between them. Putting them in one serving pool makes the slow node
the tail latency for every request that touches it, and the pipeline stage on
the small node becomes the stage everything waits for. So they do not go in one
pool by default. They are separate deployment targets, and the planner says so
out loud rather than silently dropping a node.
"""

from __future__ import annotations

from dataclasses import dataclass

from control_plane.contracts import NodeProfile


def _shape_key(p: NodeProfile) -> tuple:
    """What makes two nodes interchangeable for sharding.

    Device class and GPU name are not enough on their own: two GB10s with
    different addressable memory cannot hold equal shards. Bandwidth is rounded
    because probes wobble in the last decimal, not because it does not matter.
    """
    return (
        p.device_class,
        p.gpu_name,
        p.gpu_count,
        p.addressable_memory,
        round(p.memory_bandwidth_gbps, 1),
    )


@dataclass(frozen=True)
class NodeGroup:
    """A set of interchangeable nodes, plus the label used in reason strings."""

    key: tuple
    nodes: list[NodeProfile]

    @property
    def size(self) -> int:
        return len(self.nodes)

    @property
    def node_ids(self) -> list[str]:
        return [n.node_id for n in self.nodes]

    @property
    def exemplar(self) -> NodeProfile:
        return self.nodes[0]

    @property
    def aggregate_usable(self) -> int:
        return sum(n.usable_memory() for n in self.nodes)

    @property
    def label(self) -> str:
        return f"{self.exemplar.gpu_name} x{self.size}"


def homogeneous_groups(nodes: list[NodeProfile]) -> list[NodeGroup]:
    """Partition nodes into interchangeable sets, strongest group first.

    Ordering is by total usable memory, then node count, then per-node
    bandwidth. Memory leads because it is what decides whether the model can be
    served at all; the fastest group is useless if the weights do not fit in it.
    """
    buckets: dict[tuple, list[NodeProfile]] = {}
    for node in nodes:
        buckets.setdefault(_shape_key(node), []).append(node)

    groups = [
        NodeGroup(key=key, nodes=sorted(members, key=lambda n: n.node_id))
        for key, members in buckets.items()
    ]
    groups.sort(
        key=lambda g: (
            g.aggregate_usable,
            g.size,
            g.exemplar.memory_bandwidth_gbps,
        ),
        reverse=True,
    )
    return groups


def exclusion_note(chosen: NodeGroup, groups: list[NodeGroup]) -> str:
    """One clause naming every node left out of the plan, and why.

    Empty when nothing was excluded, so callers can append it unconditionally.
    """
    others = [g for g in groups if g.key != chosen.key]
    if not others:
        return ""
    names = ", ".join(
        f"{n.node_id} ({n.gpu_name})" for g in others for n in g.nodes
    )
    return (
        f"; excluded {names} because pooling hardware unlike the "
        f"{chosen.exemplar.gpu_name} group would make the slowest node the tail "
        f"latency for every request, so it is offered as a separate deployment "
        f"target instead"
    )
