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
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException

from . import capacity_api, enroll_api, internal_api, openai_api, ui_api
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


class _UIStatics(StaticFiles):
    """The built UI, with the one cache rule the bundler's naming scheme implies.

    Vite content-hashes every asset and **deletes the previous one** on each
    build. `ui_dir` is served straight off disk, so the moment anyone rebuilds,
    a browser tab still holding the old `index.html` asks for a hash that no
    longer exists, gets a 404, and renders nothing. The failure is silent from
    every angle worth looking at: the server logs a 200 for `/`, the page is
    blank, and no `/api/*` request is ever made to hint that the app never
    started. Starlette sends an ETag and Last-Modified but no `Cache-Control`,
    and absent one a browser is free to reuse `index.html` from cache without
    revalidating -- so this is a rebuild away at any time, not a rare race.

    The document is therefore never stored and the hashed assets are immutable,
    which is what a content hash means and the pair the naming scheme was
    designed for.

    It also answers a deep path with the document, which is what makes a UI URL
    shareable. The UI puts its screen in the path (``/cluster``,
    ``/models/meta-llama/Llama-3.1-8B``; see ui/src/state/routes.ts), and no
    such file exists on disk -- there is one ``index.html`` and the router in
    the page reads the path itself. Without the fallback below, every one of
    those URLs works while you click to it and 404s the moment anyone reloads
    or opens the link you sent them, which is the failure that teaches people
    the links do not work.
    """

    #: Prefixes that must 404 rather than fall back to the document. Every
    #: router is registered above this mount, so nothing here reaches a *live*
    #: API route -- these are the misspelled and the retired ones, and a
    #: ``/api/settngs`` that answers 200 with HTML surfaces to the caller as a
    #: JSON parse error with no hint of the cause. ``/assets`` is here for the
    #: same reason in the other direction: a hashed bundle that no longer
    #: exists must fail as a missing script, not load an HTML document into a
    #: ``<script type="module">``.
    _NO_FALLBACK = frozenset({"api", "v1", "assets", "healthz", "install.sh"})

    async def get_response(self, path, scope):
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404 or not self._is_ui_route(path, scope):
                raise
        # 200, not 404: the path is a real screen in the app, and the document
        # that renders it is the correct answer to a request for it.
        return await super().get_response("index.html", scope)

    def _is_ui_route(self, path: str, scope) -> bool:
        """Whether a missing *path* is a screen in the UI rather than a miss.

        *path* is already relative to the mount and normalised by Starlette, so
        the first component is the whole test for the reserved prefixes.

        The second test is for everything else that 404s: a request for a file
        the deploy forgot must stay a 404, or a broken build looks like a
        working one serving a blank page. A navigation is a request whose last
        component has no extension (``/cluster``) or which says outright that it
        wants a document -- and it has to be both tests, because a model id is
        allowed a dot in it (``/models/meta-llama/Llama-3.1-8B``) and a browser
        asking for that one is only distinguishable by its Accept header.
        """
        first, _, _ = path.replace(os.sep, "/").lstrip("/").partition("/")
        if first in self._NO_FALLBACK:
            return False
        last = path.rsplit("/", 1)[-1]
        if "." not in last:
            return True
        accept = Headers(scope=scope).get("accept", "")
        return "text/html" in accept

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        name = Path(full_path).name
        if name.endswith(".html"):
            response.headers["cache-control"] = "no-store, must-revalidate"
        elif "/assets/" in Path(full_path).as_posix():
            response.headers["cache-control"] = "public, max-age=31536000, immutable"
        return response


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
        title="Derate Gateway",
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

    # Only when an origin was named. Absent (the default), not one header
    # changes and same-origin -- the deployed shape -- is untouched.
    if settings.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.allowed_origins),
            allow_methods=["*"],
            allow_headers=["*"],
            # Without this the header is invisible to the page that asked for
            # it: a cross-origin fetch may only read a handful of headers by
            # default, and the request id is how the UI names the row that
            # recorded a refusal.
            expose_headers=["X-Request-Id"],
            # Deliberately off. Nothing here is cookie-authenticated, and
            # allow_credentials forbids the "*" an operator may reasonably use
            # on a lab network.
            allow_credentials=False,
        )
        log.info(
            "CORS enabled for %s", ", ".join(settings.allowed_origins)
        )

    app.include_router(openai_api.create_router(ctx))
    app.include_router(internal_api.create_router(ctx))
    # Above the StaticFiles mount below, and it must stay there: a Starlette
    # mount at "/" catches every path not matched by an EARLIER route, so a
    # router registered after it never sees a request.
    app.include_router(ui_api.create_router(ctx))
    # Same rule, and it bites harder here: enroll_api owns "/install.sh", a
    # root-level path. Below the mount, `curl | sh` on a new machine would be
    # piped index.html.
    app.include_router(enroll_api.create_router(ctx))
    # Same rule again: /api/memory and /api/capacity are JSON, and below the
    # mount they would answer index.html to a fetch that expects a report.
    app.include_router(capacity_api.create_router(ctx))

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
            app.mount("/", _UIStatics(directory=str(ui_path), html=True), name="ui")
        else:
            log.warning(
                "DERATE_UI_DIR=%s does not exist; not serving the UI",
                settings.ui_dir,
            )

    return app
