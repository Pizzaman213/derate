"""Day 0 stub. Callable by F and G before the real calculator lands.

FITS below 100 GiB, WONT_FIT above, with a plausible breakdown either way.
Delete at integration. A stub that returns invalid contract types is worse
than no stub, so this returns the same shapes the real calculator does.
"""

from __future__ import annotations

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

    def check(self, req: FitRequest, nodes: list[NodeProfile]) -> FitResult:
        usable = (
            min(n.usable_memory(DEFAULT_GUARDRAIL) for n in nodes)
            if nodes
            else 100 * GIB
        )
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
