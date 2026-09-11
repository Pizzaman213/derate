"""Day 0 stub. Callable before the real calculator lands.

FITS below 100 GiB, WONT_FIT above, with a plausible breakdown either way. A
stub that returns invalid contract types is worse than no stub, so this
returns the same shapes the real calculator does.

The real calculator landed and `node.py` wires it in production behind
`GatewayDeps(strict=True, ...)`, which refuses to start if any port -- this
one included -- is still missing. This file stayed anyway, and picked up a
second job: `contracts/routes.py`'s `_gateway_app()` imports `StubFit`
directly to compose a full gateway app with no real dependencies, purely to
ask Starlette which routes it answers for `docs/CONTRACTS.md`. It's also the
`FitPort` fake `__init__.py` exports for `tests/unit/test_fit.py`,
`test_setup.py`, `test_gateway_runtime.py`, `test_gateway_restart.py` and
`test_gateway.py`.
"""

from __future__ import annotations

from collections.abc import Mapping

from control_plane.contracts import (
    COMM_BUFFER_BYTES,
    DEFAULT_GUARDRAIL,
    FRAMEWORK_OVERHEAD,
    FitRequest,
    FitResult,
    MemoryBreakdown,
    ModelShape,
    NodeProfile,
    ParallelismPlan,
    Verdict,
)

GIB = 1024**3
STUB_LIMIT = 100 * GIB


class StubFit:
    """Implements ``FitPort`` with arithmetic simple enough to be obviously
    wrong, so nobody ships it by accident."""

    def check(
        self,
        req: FitRequest,
        nodes: list[NodeProfile],
        *,
        allocatable: Mapping[str, int] | None = None,
    ) -> FitResult:
        # Honour the live budget on the same principle as the None context
        # sentinel below: a stub that skips a branch leaves every consumer
        # tested against it with zero coverage of that branch.
        if allocatable:
            usable = min(
                allocatable.get(n.node_id, n.usable_memory(DEFAULT_GUARDRAIL))
                for n in nodes
            ) if nodes else 100 * GIB
            basis = "live"
        else:
            usable = (
                min(n.usable_memory(DEFAULT_GUARDRAIL) for n in nodes)
                if nodes
                else 100 * GIB
            )
            basis = "static"
        world = max(1, req.plan.world_size)
        weights = int(req.shape.total_params * req.shape.bytes_per_param() / world)
        kv = int(
            req.shape.num_layers
            * 2
            * req.shape.num_kv_heads
            * req.shape.effective_head_dim
            * 2
            * req.context_length
            * req.max_concurrent_seqs
            / world
        )
        breakdown = MemoryBreakdown(
            weights=weights,
            kv_cache=kv,
            activations=int(0.5 * GIB),
            comm_buffers=COMM_BUFFER_BYTES if world > 1 else 0,
            replicated=0,
            framework_overhead=FRAMEWORK_OVERHEAD,
        )
        fits = breakdown.total < STUB_LIMIT
        return FitResult(
            verdict=Verdict.FITS if fits else Verdict.WONT_FIT,
            breakdown=breakdown,
            usable_per_node=usable,
            headroom=usable - breakdown.total,
            reason=(
                "stub fit: under the 100 GiB stub threshold"
                if fits
                else "stub fit: over the 100 GiB stub threshold"
            ),
            limiting_term="none" if fits else "weights",
            # The real calculator's contract sentinel for "no context helps"
            # is None, never 0 or a made-up number -- exercise that branch
            # here too, or every consumer tested against the stub gets zero
            # coverage of it.
            max_context_that_fits=req.context_length if fits else None,
            predicted_decode_tps=25.0,
            warnings=["stub fit calculator: these numbers are not a real gate"],
            budget_basis=basis,
        )

    def max_context(
        self,
        shape: ModelShape,
        plan: ParallelismPlan,
        nodes: list[NodeProfile],
        max_seqs: int,
        kv_dtype: str,
    ) -> int:
        return 8192
