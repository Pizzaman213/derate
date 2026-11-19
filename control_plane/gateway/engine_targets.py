"""What the spec-decode poller scrapes, and how it reaches it.

``telemetry/engine_metrics.EngineMetricsPoller`` wants a ``deployments_fn``
naming every READY local text deployment by id, server root and runtime, and
an HTTP client with an awaitable ``get``. Both come from things the gateway
already owns -- the router's index and the proxy's per-origin clients -- so
the poller opens no connection pool of its own.
"""

from __future__ import annotations

from dataclasses import dataclass

from control_plane.contracts import DeploymentState, Modality

from .deps import GatewayContext


@dataclass(frozen=True)
class EngineRef:
    deployment_id: str
    #: The server root -- where ``/metrics`` lives -- not the OpenAI base URL.
    backend_url: str
    runtime: str


def _server_root(backend_url: str) -> str:
    root = backend_url.rstrip("/")
    return root[: -len("/v1")] if root.endswith("/v1") else root


def engines(ctx: GatewayContext) -> list[EngineRef]:
    """Every READY local text deployment the router can route to now."""
    out: list[EngineRef] = []
    seen: set[str] = set()
    for dep in ctx.router.index().deployments.values():
        if (
            dep is None
            or dep.deployment_id in seen
            or dep.state is not DeploymentState.READY
            or dep.modality is not Modality.TEXT
            or not dep.backend_url
        ):
            continue
        seen.add(dep.deployment_id)
        out.append(EngineRef(dep.deployment_id, _server_root(dep.backend_url), dep.runtime))
    return out


class ProxyHttp:
    """``get(url, timeout=)`` through the proxy's client for that origin."""

    def __init__(self, proxy) -> None:
        self._proxy = proxy

    async def get(self, url: str, timeout: float | None = None):
        return await self._proxy.client_for(url).get(url, timeout=timeout)


def build_poller(ctx: GatewayContext):
    try:
        from control_plane.telemetry.engine_metrics import EngineMetricsPoller
    except ImportError:  # the telemetry branch is not merged: no engine scrape
        return None

    return EngineMetricsPoller(ProxyHttp(ctx.proxy), lambda: engines(ctx))
