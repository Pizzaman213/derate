"""Day-0 stub of ``PlannerPort``.

This returns a fixed PP=2 plan with a fixed reason and reads nothing. The real
planner landed and `node.py` wires it in production behind
`GatewayDeps(strict=True, ...)`, which refuses to start if any port -- this
one included -- is still missing. This file stayed anyway: `__init__.py`
exports `StubPlanner` as this package's public fake, and it's what
`tests/unit/test_planner.py`, `test_setup.py`, `test_gateway_runtime.py`,
`test_gateway_restart.py` and `test_gateway.py` reach for wherever a test
needs a `PlannerPort` without running the real placement logic.

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
        *,
        context_length: int | None = None,
        kv_dtype: str = "auto",
    ) -> ParallelismPlan:
        # The keyword-only extras mirror the real Planner (accepted, unused
        # here): the gateway passes them, and a stub that rejects them would
        # 502 /api/plan the moment it was wired.
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
