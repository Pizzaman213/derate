"""Composition root.

Startup order, per the agent brief:

1. Registry starts, discovery begins.
2. Link store loads from disk. No measurement on startup, it is disruptive.
3. Resolver cache loads.
4. Deployment manager reconciles against what is actually running, then
   starts its own watch loop.
5. Provider service starts its refresh/backoff loop.
6. HTTP server binds.

Every step is bounded and optional. Startup must not block on a slow or
unreachable node: a step that times out or raises is recorded and the gateway
comes up anyway. Degraded startup beats no startup.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import internal_api, openai_api, ui_api
from .admission import AdmissionController
from .breaker import CircuitBreaker
from .budget import RetryBudget
from .deps import GatewayContext, GatewayDeps
from .metrics import MetricsHub
from .parking import ParkingLot
from .proxy import UpstreamProxy
from .router import Router
from .settings import GatewaySettings
from .stats import StatsRegistry

log = logging.getLogger("gateway")


async def _optional_step(
    ctx: GatewayContext, label: str, owner, *names: str
) -> None:
    """Call the first method the port actually has, bounded by a timeout.

    Ports are Protocols that do not declare lifecycle methods, so this is
    deliberately duck-typed: a component that has nothing to do at startup
    simply does not define one. An async method is awaited directly; a sync
    one runs on a worker thread so it cannot stall the event loop -- either
    way the timeout bounds it and a failure degrades rather than aborts.
    """
    fn = next(
        (getattr(owner, n) for n in names if callable(getattr(owner, n, None))), None
    )
    if fn is None:
        return
    try:
        if inspect.iscoroutinefunction(fn):
            await asyncio.wait_for(fn(), timeout=ctx.settings.startup_step_timeout_s)
        else:
            await asyncio.wait_for(
                asyncio.to_thread(fn), timeout=ctx.settings.startup_step_timeout_s
            )
        log.info("startup: %s ok", label)
    except TimeoutError:
        ctx.degraded_startup.append(f"{label}: timed out")
        log.warning("startup: %s timed out, continuing degraded", label)
    except Exception as exc:
        ctx.degraded_startup.append(f"{label}: {type(exc).__name__}")
        log.warning("startup: %s failed (%s), continuing degraded", label, exc)


async def _best_effort_shutdown(label: str, owner, *names: str) -> None:
    """The shutdown-side counterpart to :func:`_optional_step`.

    No timeout and no degraded_startup bookkeeping -- there is no startup
    left to mark degraded -- just: call it if it exists, await it properly if
    it is async, and never let one component's teardown stop the rest of
    shutdown from running.
    """
    fn = next(
        (getattr(owner, n) for n in names if callable(getattr(owner, n, None))), None
    )
    if fn is None:
        return
    try:
        if inspect.iscoroutinefunction(fn):
            await fn()
        else:
            await asyncio.to_thread(fn)
        log.info("shutdown: %s ok", label)
    except Exception as exc:
        log.warning("shutdown: %s failed: %s", label, exc)


# A block reason distinct from admission.BLOCK_MEMORY_CRITICAL. AdmissionController
# .reconcile() derives that reason from its own registry-based view of memory
# pressure and unconditionally unblocks it for every deployment its view does
# not consider pressured -- every 0.5s, as a backstop for anything that
# misses this event stream. If this event path blocked under that same
# reason, the next reconcile tick would erase it the instant the two views
# disagree, which is exactly when this stream exists to matter (audit M-13).
# reconcile() only ever touches BLOCK_MEMORY_CRITICAL and BLOCK_DRAINING, so a
# block held under a reason of our own survives every tick until this stream
# itself clears it.
_EVENT_MEMORY_CRITICAL = "event_memory_critical"


async def _consume_deployment_events(ctx: GatewayContext, events) -> None:
    """React to Agent F's memory events by pausing or resuming admission.

    A memory_critical event means the deployment must stop admitting new
    requests immediately, not at the next 0.5s admission reconcile poll --
    the poll stays as a backstop for anything that misses this stream, not as
    the primary path. See _EVENT_MEMORY_CRITICAL for why this blocks under
    its own reason rather than calling AdmissionController.set_memory_critical
    (whose BLOCK_MEMORY_CRITICAL reason is reconcile's own to set and clear).
    """
    try:
        async for event in events():
            deployment_id = event.get("deployment_id")
            if not deployment_id:
                continue
            event_type = event.get("type")
            if event_type == "memory_critical":
                ctx.admission.block(deployment_id, _EVENT_MEMORY_CRITICAL)
            elif event_type == "memory_cleared":
                ctx.admission.unblock(deployment_id, _EVENT_MEMORY_CRITICAL)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("deployment event stream ended unexpectedly")


def create_app(
    deps: GatewayDeps | None = None,
    *,
    settings: GatewaySettings | None = None,
    http_client: httpx.AsyncClient | None = None,
    telemetry: "object" = None,
) -> FastAPI:
    """Build the gateway. With no arguments every dependency is a stub, which
    is how the day-0 surface and the whole test suite run.

    *telemetry* is a Telemetry bundle (control_plane.telemetry.service). With
    none, one is built from the environment -- and if telemetry is switched
    off, that bundle records nothing and costs a method call per event.
    """
    deps = deps or GatewayDeps(settings=settings or GatewaySettings())
    settings = settings or deps.settings

    if telemetry is None:
        from control_plane.telemetry.service import Telemetry

        telemetry = Telemetry.from_env()

    events = telemetry.gateway_events

    stats = StatsRegistry()
    admission = AdmissionController(
        registry=deps.registry,
        fit=deps.fit,
        deployments=deps.deployments,
        settings=settings,
        events=events,
    )
    breaker = CircuitBreaker(
        failure_threshold=settings.breaker_failure_threshold,
        cooldown_s=settings.breaker_cooldown_s,
        events=events,
    )
    retry_budget = RetryBudget(
        ratio=settings.retry_budget_ratio,
        window_s=settings.retry_budget_window_s,
        floor=settings.retry_budget_floor,
        events=events,
    )
    parking = ParkingLot(settings, events=events)
    router = Router(
        deployments=deps.deployments,
        providers=deps.providers,
        registry=deps.registry,
        stats=stats,
        admission=admission,
        settings=settings,
        breaker=breaker,
        events=events,
    )
    proxy = UpstreamProxy(
        settings, client=http_client, breaker=breaker, sink=telemetry.sink
    )
    metrics = MetricsHub(
        registry=deps.registry,
        deployments=deps.deployments,
        stats=stats,
        settings=settings,
    )

    ctx = GatewayContext(
        deps=deps,
        settings=settings,
        stats=stats,
        admission=admission,
        router=router,
        proxy=proxy,
        metrics=metrics,
        breaker=breaker,
        parking=parking,
        retry_budget=retry_budget,
        sink=telemetry.sink,
        events=events,
    )
    ctx.telemetry = telemetry

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx.started_at = time.time()

        # 1. Registry starts, discovery begins.
        await _optional_step(ctx, "registry", deps.registry, "start", "discover")
        # 2. Link store loads from disk. Deliberately no measure() call here.
        await _optional_step(ctx, "links", deps.links, "load", "start")
        # 3. Resolver cache loads.
        await _optional_step(ctx, "resolver", deps.resolver, "load_cache", "load", "start")
        # 4. Deployment manager reconciles against what is actually running,
        #    then starts its own watch loop. These are two separate steps,
        #    not alternative names for one: reconcile() only self-starts the
        #    watch when it adopted something, so a fresh install with nothing
        #    to adopt would otherwise never start watching at all.
        await _optional_step(ctx, "deployments.reconcile", deps.deployments, "reconcile")
        await _optional_step(ctx, "deployments.start", deps.deployments, "start")
        # 5. Provider service starts its refresh/backoff loop.
        await _optional_step(ctx, "providers", deps.providers, "start", "refresh_loop")

        if settings.coordinator_node_id is None:
            try:
                nodes = deps.registry.list_nodes()
                if nodes:
                    settings.coordinator_node_id = nodes[0].profile.node_id
            except Exception:
                log.warning("could not determine coordinator node")

        await proxy.start()
        await admission.start()
        await router.start()
        await metrics.start()
        # Started last, and given the registry as its source of agent URLs, so
        # it only ever polls nodes the registry already knows are members.
        await telemetry.start(
            registry=deps.registry,
            node_id=settings.coordinator_node_id,
            providers=deps.providers,
        )
        # The deployment manager's bus is where fit_miss lives -- predicted
        # memory against what actually happened, and the most valuable thing
        # this system produces. Duck-typed because a stub has no bus.
        deploy_bus = getattr(deps.deployments, "bus", None)
        if deploy_bus is not None:
            telemetry.watch_deployments(deploy_bus)
        ctx.events.startup_degraded(ctx.degraded_startup)

        # Agent F's memory events, when the port offers them: memory_critical
        # stops admission to that deployment within one event, not at the
        # next 0.5s admission reconcile poll -- the poll stays as a backstop
        # for anything that misses this stream, not as the primary path.
        # Duck-typed like the steps above: a port with no events() has
        # nothing wrong with it, it just has nothing to subscribe to.
        deployment_events_fn = getattr(deps.deployments, "events", None)
        deployment_events_task = (
            asyncio.create_task(
                _consume_deployment_events(ctx, deployment_events_fn)
            )
            if callable(deployment_events_fn)
            else None
        )

        log.info(
            "gateway ready on %s:%s (%s degraded step(s))",
            settings.host,
            settings.port,
            len(ctx.degraded_startup),
        )

        # 6. HTTP server binds (uvicorn does this once lifespan startup returns).
        try:
            yield
        finally:
            if deployment_events_task is not None:
                deployment_events_task.cancel()
                try:
                    await deployment_events_task
                except asyncio.CancelledError:
                    pass
            # Parked requests are released first: they are clients still
            # holding a connection, and shutting down under them would hang
            # both sides rather than answering.
            parking.close()
            await metrics.stop()
            await router.stop()
            await admission.stop()
            await proxy.stop()
            # Best-effort teardown of the ports themselves, in case they hold
            # their own connections, threads, or files open.
            await _best_effort_shutdown("providers", deps.providers, "aclose", "close")
            await _best_effort_shutdown("deployments", deps.deployments, "close")
            await _best_effort_shutdown("registry", deps.registry, "stop")
            # Last, so anything the shutdown path emits is still recorded.
            await telemetry.stop()

    app = FastAPI(
        title="Sparkplane Gateway",
        version="0.1.0",
        lifespan=lifespan,
        # The UI and OpenAI clients both talk to this; the surface is the
        # contract in 00-architecture.md, not a generated schema. /redoc is
        # undocumented surface the spec never mentions, so it stays off.
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.ctx = ctx
    app.include_router(openai_api.create_router(ctx))
    app.include_router(internal_api.create_router(ctx))
    # Above the StaticFiles mount below, and it must stay there: a Starlette
    # mount at "/" catches every path not matched by an EARLIER route, so a
    # router registered after it never sees a request.
    app.include_router(ui_api.create_router(ctx))

    @app.get("/healthz")
    async def healthz():
        return {
            "ok": True,
            "started_at": ctx.started_at,
            "degraded_startup": ctx.degraded_startup,
        }

    # Serving the built UI is optional and last: it mounts at "/", and
    # Starlette matches routes in registration order, so everything above
    # (/v1, /api, /healthz) must already be registered or the mount would
    # shadow it. Absent by default -- every existing test and the day-0 stub
    # gateway run with no UI directory at all.
    if settings.ui_dir:
        ui_path = Path(settings.ui_dir)
        if ui_path.is_dir():
            app.mount("/", StaticFiles(directory=str(ui_path), html=True), name="ui")
        else:
            log.warning(
                "SPARKPLANE_UI_DIR=%s does not exist; not serving the UI",
                settings.ui_dir,
            )

    return app
