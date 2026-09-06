"""Day-0 stub of ``PlannerPort``.

Agents F and H need something callable before the real planner is wired. This
returns a fixed PP=2 plan with a fixed reason and reads nothing. It is deleted
at integration; if it is still imported anywhere after the planner is wired,
that is the bug.

A stub that returns invalid contract types is worse than no stub, so this one
returns a real ``ParallelismPlan`` with a non-empty reason and a non-empty
rejected list, exactly like the real thing.
"""

from __future__ import annotations

from control_plane.contracts import (
    LinkMeasurement,
    ModelShape,
    NodeProfile,
    ParallelismKind,
    ParallelismPlan,
)

STUB_REASON = (
    "PP=2 across two nodes: stub planner, this decision was not derived from any "
    "measurement and must not be shown to a user as if it were."
)


class StubPlanner:
    """Fixed PP=2. Implements ``PlannerPort``."""

    def plan(
        self,
        shape: ModelShape,
        nodes: list[NodeProfile],
        link: LinkMeasurement | None,
        target: str,
        concurrency: int,
    ) -> ParallelismPlan:
        node_ids = [n.node_id for n in nodes[:2]] or ["node-1", "node-2"]
        return ParallelismPlan(
            kind=ParallelismKind.PIPELINE,
            tensor_parallel=1,
            pipeline_parallel=2,
            expert_parallel=1,
            data_parallel=1,
            node_ids=node_ids,
            reason=STUB_REASON,
            measured_link_gbps=0.0,
            rejected=["TP=2: stub planner did not evaluate tensor parallel"],
        )
