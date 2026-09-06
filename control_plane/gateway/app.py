"""Composition root.

Startup order, per the agent brief:

1. Registry starts, discovery begins.
2. Link store loads from disk. No measurement on startup, it is disruptive.
3. Resolver cache loads.
4. Deployment manager reconciles against what is actually running.
5. HTTP server binds.

Every step is bounded and optional. Startup must not block on a slow or
unreachable node: a step that times out or raises is recorded and the gateway
comes up anyway. Degraded startup beats no startup.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from . import internal_api, openai_api
from .admission import AdmissionController
from .deps import GatewayContext, GatewayDeps
from .metrics import MetricsHub
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
    simply does not define one.
    """
    fn = next(
        (getattr(owner, n) for n in names if callable(getattr(owner, n, None))), None
    )
    if fn is None:
        return
    try:
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


def create_app(
    deps: GatewayDeps | None = None,
    *,
    settings: GatewaySettings | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    """Build the gateway. With no arguments every dependency is a stub, which
    is how the day-0 surface and the whole test suite run."""
    deps = deps or GatewayDeps(settings=settings or GatewaySettings())
    settings = settings or deps.settings

    stats = StatsRegistry()
    admission = AdmissionController(
        registry=deps.registry,
        fit=deps.fit,
        deployments=deps.deployments,
        settings=settings,
    )
    router = Router(
        deployments=deps.deployments,
        providers=deps.providers,
        registry=deps.registry,
        stats=stats,
        admission=admission,
        settings=settings,
    )
    proxy = UpstreamProxy(settings, client=http_client)
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
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx.started_at = time.time()

        # 1. Registry starts, discovery begins.
        await _optional_step(ctx, "registry", deps.registry, "start", "discover")
        # 2. Link store loads from disk. Deliberately no measure() call here.
        await _optional_step(ctx, "links", deps.links, "load", "start")
        # 3. Resolver cache loads.
        await _optional_step(ctx, "resolver", deps.resolver, "load_cache", "load", "start")
        # 4. Deployment manager reconciles against what is actually running.
        await _optional_step(
            ctx, "deployments", deps.deployments, "reconcile", "start"
        )

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
        log.info(
            "gateway ready on %s:%s (%s degraded step(s))",
            settings.host,
            settings.port,
            len(ctx.degraded_startup),
        )

        # 5. HTTP server binds (uvicorn does this once lifespan startup returns).
        try:
            yield
        finally:
            await metrics.stop()
            await router.stop()
            await admission.stop()
            await proxy.stop()

    app = FastAPI(
        title="Sparkplane Gateway",
        version="0.1.0",
        lifespan=lifespan,
        # The UI and OpenAI clients both talk to this; the surface is the
        # contract in 00-architecture.md, not a generated schema.
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.ctx = ctx
    app.include_router(openai_api.create_router(ctx))
    app.include_router(internal_api.create_router(ctx))

    @app.get("/healthz")
    async def healthz():
        return {
            "ok": True,
            "started_at": ctx.started_at,
            "degraded_startup": ctx.degraded_startup,
        }

    return app
