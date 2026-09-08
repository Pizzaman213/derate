"""The 1 Hz metrics stream.

One producer, many subscribers. If a source is unavailable the event still
goes out with that field null: the UI should degrade a panel, not freeze.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from . import serialize
from .settings import GatewaySettings
from .stats import StatsRegistry

log = logging.getLogger("gateway.metrics")


class MetricsHub:
    def __init__(
        self,
        *,
        registry,
        deployments,
        stats: StatsRegistry,
        settings: GatewaySettings,
        providers=None,
    ) -> None:
        self._registry = registry
        self._deployments = deployments
        self._stats = stats
        self._settings = settings
        self._providers = providers
        self._subscribers: set[asyncio.Queue] = set()
        self._task: asyncio.Task | None = None
        self._latest: dict[str, Any] | None = None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        self._task = asyncio.create_task(self._produce_loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _produce_loop(self) -> None:
        while True:
            try:
                event = self.snapshot()
                self._latest = event
                self._publish(event)
                await asyncio.sleep(self._settings.metrics_interval_s)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A broken source must not stop the stream.
                log.exception("metrics snapshot failed")
                await asyncio.sleep(self._settings.metrics_interval_s)

    def _publish(self, event: dict) -> None:
        for queue in list(self._subscribers):
            if queue.full():
                # A slow subscriber loses the oldest event, never the newest.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    # -- subscription ------------------------------------------------------

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._settings.metrics_queue_depth)
        if self._latest is not None:
            queue.put_nowait(self._latest)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    # -- snapshot ----------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        window = self._settings.metrics_rate_window_s
        now = time.time()

        nodes_payload: list[dict] | None
        total_power: float | None
        try:
            nodes = self._registry.list_nodes()
            nodes_payload = []
            total_power = 0.0
            for node in nodes:
                # Against physical memory, matching the architecture doc's
                # topology payload. See serialize.node_payload.
                total = node.profile.total_memory or node.memory_total or 0
                used_pct = (
                    round(node.memory_used / total * 100.0, 1) if total else None
                )
                nodes_payload.append(
                    {
                        "node_id": node.profile.node_id,
                        # Through serialize, not straight off the state: a node
                        # with no GPU reports 0.0 W, and this frame is what the
                        # UI prefers while it is fresh. Sending the raw figure
                        # here put a measured-looking 0 W beside the null that
                        # /api/nodes sends for the same machine.
                        "power_w": serialize.power_reading(node),
                        "temp_c": serialize.temp_reading(node),
                        "memory_used_pct": used_pct,
                        "util_pct": node.utilization_pct,
                        # When these four were measured. The UI greys a node
                        # whose sample has aged out rather than passing a
                        # frozen reading off as current.
                        "sample_ts": node.sample_ts or None,
                    }
                )
                total_power += serialize.power_reading(node) or 0.0
        except Exception:
            log.exception("registry unavailable for metrics")
            nodes_payload = None
            total_power = None

        deployments_payload: list[dict] | None
        try:
            deployments_payload = []
            for dep in self._deployments.list():
                st = self._stats.peek(dep.deployment_id)
                deployments_payload.append(
                    {
                        "deployment_id": dep.deployment_id,
                        "state": dep.state.value,
                        "tokens_per_sec": round(st.tokens_per_sec(window, now), 1)
                        if st
                        else 0.0,
                        "ttft_ms": round(st.ttft_ms, 1) if st and st.ttft_ms else None,
                        "queue_depth": st.outstanding if st else 0,
                    }
                )
        except Exception:
            log.exception("deployment list unavailable for metrics")
            deployments_payload = None

        # Models a provider serves, counted exactly as a deployment is one
        # block up: same registry, same window, same rounding. The cluster
        # screen draws a remote-served name with the same band as a local one
        # and reads its throughput from here, so the two figures have to come
        # from one place or the band above and the band below tick at
        # different rates for no reason a viewer could work out.
        #
        # Only targets the registry has actually seen. Every model of an
        # un-allowlisted OpenRouter key is several hundred rows, and this
        # payload goes out once a second -- a target nobody has routed to has
        # no counter to report and the UI reads its absence as the zero it is.
        remotes_payload: list[dict] | None
        try:
            if self._providers is None:
                remotes_payload = None
            else:
                remotes_payload = []
                servable = getattr(self._providers, "servable", self._providers.list)
                for provider in servable():
                    for model in provider.models:
                        # The same id gateway/targets.py builds a remote
                        # RouteTarget from, which is what the stats are keyed
                        # by and what the UI's band carries.
                        target_id = f"{provider.provider_id}:{model.upstream_id}"
                        st = self._stats.peek(target_id)
                        if st is None:
                            continue
                        remotes_payload.append(
                            {
                                "target_id": target_id,
                                "provider_id": provider.provider_id,
                                "served_name": model.served_name,
                                "state": "healthy" if provider.healthy else "unhealthy",
                                "tokens_per_sec": round(st.tokens_per_sec(window, now), 1),
                                "ttft_ms": round(st.ttft_ms, 1) if st.ttft_ms else None,
                                "queue_depth": st.outstanding,
                            }
                        )
        except Exception:
            log.exception("provider list unavailable for metrics")
            remotes_payload = None

        return {
            "ts": now,
            "cluster": {
                "tokens_per_sec": round(self._stats.total_tokens_per_sec(window, now), 1),
                "total_power_w": round(total_power, 1) if total_power is not None else None,
                # No backend exposes a prefix-cache hit rate to us yet. Null is
                # honest; a fabricated number would be worse than a blank panel.
                "cache_hit_pct": None,
            },
            "nodes": nodes_payload,
            "deployments": deployments_payload,
            "remotes": remotes_payload,
        }
