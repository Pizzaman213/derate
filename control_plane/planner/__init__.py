"""Parallelism planner. Agent E.

Decides TP/PP/EP/DP from measured hardware facts and explains the decision in a
sentence the UI shows verbatim.

    from control_plane.planner import Planner

    plan = Planner().plan(shape, nodes, link, target="throughput", concurrency=16)
    print(plan.reason)
"""

from .comm import (
    estimated_step_seconds,
    expert_bytes_per_step,
    pipeline_bubble_fraction,
    pipeline_bytes_per_step,
    pipeline_exchanges_per_step,
    tensor_bytes_per_step,
    tensor_exchanges_per_step,
)
from .constants import (
    DEFAULT_KV_DTYPE,
    DEFAULT_PLAN_CONTEXT,
    LATENCY_CONCURRENCY_CEILING,
    MIN_NODES_FOR_CROSS_NODE_EP,
    PIPELINE_INFLIGHT_PER_STAGE,
)
from .legality import (
    Candidate,
    DegreeRefusal,
    IllegalDegrees,
    check_degrees,
    enumerate_candidates,
    valid_ep_degrees,
    valid_pp_degrees,
    valid_tp_degrees,
)
from .planner import Planner
from .stub import StubPlanner
from .topology import (
    POOLING_HAZARD,
    NodeGroup,
    exclusion_note,
    homogeneous_groups,
    pooled_group,
    pooling_note,
)

__all__ = [
    "valid_ep_degrees",
    "pooling_note",
    "pooled_group",
    "exclusion_note",
    "check_degrees",
    "POOLING_HAZARD",
    "IllegalDegrees",
    "DegreeRefusal",
    "Candidate",
    "DEFAULT_KV_DTYPE",
    "DEFAULT_PLAN_CONTEXT",
    "LATENCY_CONCURRENCY_CEILING",
    "MIN_NODES_FOR_CROSS_NODE_EP",
    "NodeGroup",
    "PIPELINE_INFLIGHT_PER_STAGE",
    "Planner",
    "StubPlanner",
    "enumerate_candidates",
    "estimated_step_seconds",
    "expert_bytes_per_step",
    "homogeneous_groups",
    "pipeline_bubble_fraction",
    "pipeline_bytes_per_step",
    "pipeline_exchanges_per_step",
    "tensor_bytes_per_step",
    "tensor_exchanges_per_step",
    "valid_pp_degrees",
    "valid_tp_degrees",
]
