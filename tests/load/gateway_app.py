"""The device under test.

The real ``create_app()`` with the real ``GatewaySettings``. Only the
deployment port is swapped, for one that lists N equal replicas of a single
served_name pointing at the fake runtimes -- everything else on the request
path (router, admission, breaker, proxy, parking, stats) is the shipping code.

    python -m tests.load.gateway_app --port 8099 --backends http://127.0.0.1:9001/v1

``GET /loadprobe`` is the only addition. The harness cannot see the gateway's
event loop from outside, and event-loop lag is the difference between "slower"
and "fell over".
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from collections import deque

from starlette.responses import JSONResponse

from control_plane.contracts import Deployment, DeploymentState, Modality
from control_plane.gateway import GatewayDeps, GatewaySettings, create_app
from control_plane.gateway.stubs import (
    StubFit,
    StubLinks,
    StubPlanner,
    StubRegistry,
    StubResolver,
)
from tests.fixtures import MODEL_SHAPES, fits, single_node_plan

SERVED_NAME = "loadtest"

# Fixture nodes, cycled when there are more replicas than nodes. Which node a
# replica claims only affects strength attribution and the memory-critical
# reconcile; none of the fixture nodes are near the 95% threshold.
_NODES = ("spark-01", "spark-02", "ws-3090")

# Equal for every replica on purpose: an unequal fleet is a routing test, not a
# capacity test, and a skewed split would make each rung mean something else.
_DECODE_TPS = 42.0


class LoadDeployments:
    """A DeploymentPort over a fixed set of fake runtimes."""

    def __init__(
        self,
        backends: list[str],
        *,
        context_length: int = 32768,
        max_seqs: int = 4096,
        model_key: str = "qwen3-30b-a3b",
    ) -> None:
        shape = MODEL_SHAPES[model_key]
        self._deployments: list[Deployment] = []
        for i, url in enumerate(backends):
            fit = fits()
            fit.predicted_decode_tps = _DECODE_TPS
            self._deployments.append(
                Deployment(
                    deployment_id=f"d-{i + 1}",
                    served_name=SERVED_NAME,
                    shape=shape,
                    plan=single_node_plan(_NODES[i % len(_NODES)]),
                    fit=fit,
                    runtime="vllm",
                    state=DeploymentState.READY,
                    backend_url=url,
                    context_length=context_length,
                    max_concurrent_seqs=max_seqs,
                    started_at=time.time(),
                    last_error=None,
                )
            )

    def list(self) -> list[Deployment]:
        return list(self._deployments)

    def get(self, deployment_id: str) -> Deployment | None:
        return next(
            (d for d in self._deployments if d.deployment_id == deployment_id), None
        )

    def launch(
        self, shape, plan, fit, runtime, ctx, max_seqs, *,
        modality=Modality.TEXT, extra_args=(),
    ) -> Deployment:
        raise NotImplementedError("the load harness does not launch deployments")

    def stop(self, deployment_id: str) -> None:
        for dep in self._deployments:
            if dep.deployment_id == deployment_id:
                dep.state = DeploymentState.STOPPING


class NoProviders:
    """No remote targets, so routing stays local and the auto policy is stable.

    With a mixed fleet the auto policy would be LOCAL_FIRST, which is a spill
    decision rather than a load-balancing one and would change what a rung
    measures.
    """

    def list(self):
        return []

    def models(self):
        return []

    def add(self, spec):
        raise NotImplementedError

    def refresh(self, provider_id):
        raise KeyError(provider_id)

    def resolve_key(self, provider_id):
        raise KeyError(provider_id)

    def health(self, provider_id):
        return (False, "no providers in the load harness")


class _LagProbe:
    """Measures the gateway's own event-loop lag by sleeping and timing it.

    A task that asks for 50 ms and gets 300 ms is a loop that cannot keep up.
    That is invisible from outside the process and is the clearest single
    signal that the gateway has stopped coping rather than merely slowed.
    """

    def __init__(self, interval: float = 0.05, window: int = 20000) -> None:
        self.interval = interval
        self.samples: deque[float] = deque(maxlen=window)
        self._task: asyncio.Task | None = None

    def ensure_started(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            started = time.perf_counter()
            await asyncio.sleep(self.interval)
            self.samples.append(
                max(0.0, time.perf_counter() - started - self.interval) * 1000.0
            )

    def drain(self, reset: bool) -> dict:
        values = sorted(self.samples)
        if reset:
            self.samples.clear()
        if not values:
            return {"n": 0}

        def pct(q: float) -> float:
            idx = min(len(values) - 1, int(q * len(values)))
            return round(values[idx], 3)

        return {
            "n": len(values),
            "p50_ms": pct(0.50),
            "p99_ms": pct(0.99),
            "max_ms": round(values[-1], 3),
        }


def _rss_bytes() -> int:
    try:
        with open("/proc/self/statm") as handle:
            return int(handle.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except Exception:
        return 0


def _cpu_seconds() -> float:
    """utime + stime. The gateway is one process on one core, so this over
    wall time is the single number that says whether it is CPU bound."""
    try:
        with open("/proc/self/stat") as handle:
            fields = handle.read().rsplit(") ", 1)[1].split()
        ticks = os.sysconf("SC_CLK_TCK")
        return (int(fields[11]) + int(fields[12])) / ticks
    except Exception:
        return 0.0


def build_app(
    backends: list[str],
    *,
    settings: GatewaySettings | None = None,
    context_length: int = 32768,
    max_seqs: int = 4096,
):
    settings = settings or GatewaySettings()
    deps = GatewayDeps(
        registry=StubRegistry(),
        links=StubLinks(),
        resolver=StubResolver(),
        fit=StubFit(),
        planner=StubPlanner(),
        deployments=LoadDeployments(
            backends, context_length=context_length, max_seqs=max_seqs
        ),
        providers=NoProviders(),
        settings=settings,
    )
    app = create_app(deps, settings=settings)
    probe = _LagProbe()

    @app.get("/loadprobe")
    async def loadprobe(reset: int = 0) -> JSONResponse:
        # Started lazily rather than in a lifespan hook: create_app() already
        # owns the app's lifespan and mixing in on_event would fight it.
        probe.ensure_started()
        ctx = app.state.ctx
        targets = {
            tid: {
                "outstanding": st.outstanding,
                "completed": st.completed,
                "failed": st.failed,
                "tokens": st.total_tokens,
                "decode_tps": round(st.decode_tps, 2) if st.decode_tps else None,
                "ttft_ms": round(st.ttft_ms, 2) if st.ttft_ms else None,
            }
            for tid, st in ctx.stats.all().items()
        }
        return JSONResponse(
            {
                "pid": os.getpid(),
                "rss_bytes": _rss_bytes(),
                "cpu_s": round(_cpu_seconds(), 3),
                "wall_s": round(time.monotonic(), 3),
                "loop_lag": probe.drain(bool(reset)),
                "targets": targets,
                "outstanding_total": sum(t["outstanding"] for t in targets.values()),
                # Admission's KV ledger. A commitment that is never released
                # is invisible in `outstanding` and ends in a permanent 429,
                # so it has to be visible from outside the process.
                "kv_committed": dict(getattr(ctx.admission, "_committed", {})),
                "kv_budget": {
                    d.deployment_id: ctx.admission.kv_budget(d)
                    for d in deps.deployments.list()
                },
                "admission_blocks": {
                    tid: sorted(reasons)
                    for tid, reasons in getattr(ctx.admission, "_blocks", {}).items()
                    if reasons
                },
                "circuits": ctx.breaker.opened_targets() if ctx.breaker else {},
                "parked": ctx.parking.parked() if ctx.parking else 0,
                "degraded_startup": ctx.degraded_startup,
            }
        )

    return app


def main() -> None:
    import faulthandler
    import signal

    import uvicorn

    # SIGUSR1 dumps every thread's stack. A gateway that stops answering while
    # its process is still alive has a blocked event loop, and the only way to
    # say where is to ask it.
    faulthandler.register(signal.SIGUSR1, all_threads=True, chain=False)

    parser = argparse.ArgumentParser(description="gateway under test")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument(
        "--backends", required=True, help="comma-separated fake runtime base URLs"
    )
    parser.add_argument("--context-length", type=int, default=32768)
    parser.add_argument("--max-seqs", type=int, default=4096)
    parser.add_argument("--backlog", type=int, default=4096)
    parser.add_argument(
        "--settings",
        default="{}",
        help="JSON object of GatewaySettings field overrides",
    )
    args = parser.parse_args()

    settings = GatewaySettings(host=args.host, port=args.port)
    for key, value in json.loads(args.settings).items():
        if not hasattr(settings, key):
            raise SystemExit(f"unknown GatewaySettings field: {key}")
        setattr(settings, key, value)

    app = build_app(
        [b for b in args.backends.split(",") if b],
        settings=settings,
        context_length=args.context_length,
        max_seqs=args.max_seqs,
    )
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="error",
        access_log=False,
        backlog=args.backlog,
    )


if __name__ == "__main__":
    main()
