"""Grouping nodes into sets it is sane to serve one model across.

A 3090 desktop and a Spark have different memory sizes, different bandwidth, and
a slower link between them. Putting them in one serving pool makes the slow node
the tail latency for every request that touches it, and the pipeline stage on
the small node becomes the stage everything waits for. So they do not go in one
pool by default. They are separate deployment targets, and the planner says so
out loud rather than silently dropping a node.

An operator can override that default by naming the nodes explicitly. When they
do, :func:`pooled_group` puts every named node in one group and
:func:`pooling_note` states the hazard affirmatively rather than as a reason for
exclusion. Both sentences are built from :data:`POOLING_HAZARD`, so the refusal
and the warning cannot drift apart into two descriptions of one fact.
"""

from __future__ import annotations

from dataclasses import dataclass

from control_plane.contracts import NodeProfile

#: The one description of what pooling unlike hardware costs. ``verb`` is the
#: only thing that changes between the two callers: ``exclusion_note`` speaks
#: hypothetically about a pool it is refusing to build ("would make"), and
#: ``pooling_note`` speaks about one that now exists ("makes"). Keeping a single
#: template is what stops the refusal and the warning from disagreeing about the
#: hazard the moment either sentence is edited.
POOLING_HAZARD = (
    "pooling hardware unlike the {gpu} group {verb} the slowest node the tail "
    "latency for every request"
)


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


def _node_names(groups: list[NodeGroup]) -> str:
    """``node_id (gpu_name)`` for every node in these groups, in order."""
    return ", ".join(f"{n.node_id} ({n.gpu_name})" for g in groups for n in g.nodes)


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
        """The node every per-node figure is charged against.

        The weakest member, not the first. For a homogeneous group this is the
        same node it has always been -- ``_shape_key`` already includes
        addressable memory and rounded bandwidth, so every member ties and
        ``min`` returns the first, which is ``nodes[0]``. It matters for a
        pooled group, where the capacity floor, the step-time estimate and the
        prose must all be charged against the node that will actually bind.
        """
        return min(
            self.nodes, key=lambda n: (n.usable_memory(), n.memory_bandwidth_gbps)
        )

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


def pooled_group(nodes: list[NodeProfile]) -> NodeGroup:
    """Every supplied node in one group, in the order given.

    For when an operator has named the nodes: the set and its order are the
    request, not a suggestion. Order is preserved because the first node is the
    pipeline head that sparkrun will SSH to first, and sorting here would
    silently move it.

    The group's ``exemplar`` is its weakest member, so a pool of unlike hardware
    is sized and timed against the node that will actually bind rather than
    against whichever one happened to be listed first.
    """
    return NodeGroup(key=("pooled",), nodes=list(nodes))


def exclusion_note(chosen: NodeGroup, groups: list[NodeGroup]) -> str:
    """One clause naming every node left out of the plan, and why.

    Empty when nothing was excluded, so callers can append it unconditionally.
    """
    others = [g for g in groups if g.key != chosen.key]
    if not others:
        return ""
    hazard = POOLING_HAZARD.format(gpu=chosen.exemplar.gpu_name, verb="would make")
    return (
        f"; excluded {_node_names(others)} because {hazard}, so it is offered "
        f"as a separate deployment target instead"
    )


def pooling_note(groups: list[NodeGroup]) -> str:
    """One sentence stating that unlike hardware is now pooled, and what it costs.

    The affirmative counterpart of :func:`exclusion_note`: that one explains a
    node being left out, this one explains a node being kept in against the
    default. Empty when the selection is homogeneous, so callers can append it
    unconditionally -- a pooled group of alike machines is not a hazard and must
    not be warned about, or the warning stops meaning anything.

    ``groups`` is the real partition of the selection, not the pooled group, so
    the odd nodes can be named exactly as the exclusion note names them.
    """
    if len(groups) <= 1:
        return ""
    reference = groups[0]
    others = groups[1:]
    names = _node_names(others)
    verb_is = "is" if sum(g.size for g in others) == 1 else "are"
    hazard = POOLING_HAZARD.format(gpu=reference.exemplar.gpu_name, verb="makes")
    return (
        f"Warning: {names} {verb_is} pooled with the "
        f"{reference.exemplar.gpu_name} group at the operator's instruction; "
        f"{hazard}."
    )
