"""The node agent. Runs on every container, coordinator or worker.

Three endpoints and an mDNS advertisement. This is the part of the registry
that exists on every machine; a worker is a node agent and nothing else.

FastAPI is imported inside ``create_agent_app`` so that importing this module,
and unit-testing NodeAgent, works on a machine without a web framework.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from control_plane.contracts import NodeProfile

from .config import ROLE_WORKER, TELEMETRY_INTERVAL_S
from .discovery import Advertiser
from .serde import profile_to_dict, telemetry_to_dict
from .telemetry import RingBuffer, TelemetrySample, read_telemetry

log = logging.getLogger(__name__)


class NodeAgent:
    """Local hardware, local telemetry, and our advertisement of both."""

    def __init__(
        self,
        profile: NodeProfile,
        role: str = ROLE_WORKER,
        cluster_id: str = "",
        port: int = 8081,
        clock: Callable[[], float] = time.time,
        advertiser: Advertiser | None = None,
    ) -> None:
        self.profile = profile
        self.role = role
        self.cluster_id = cluster_id
        self.port = port
        self._clock = clock
        self._started_at = clock()
        self._ring = RingBuffer()
        self._task: asyncio.Task | None = None
        self._running = False
        self._advertiser = advertiser

    # ------------------------------------------------------------------
    # Payloads
    # ------------------------------------------------------------------

    @property
    def node_id(self) -> str:
        return self.profile.node_id

    @property
    def uptime_s(self) -> float:
        return self._clock() - self._started_at

    def profile_payload(self) -> dict:
        return profile_to_dict(self.profile)

    def telemetry_payload(self) -> dict:
        return telemetry_to_dict(self.node_id, self._ring.latest)

    def health_payload(self) -> dict:
        return {
            "status": "ok",
            "node_id": self.node_id,
            "role": self.role,
            "cluster_id": self.cluster_id,
            "uptime_s": round(self.uptime_s, 1),
        }

    def history(self, seconds: float = 60.0) -> list[dict]:
        return [s.as_dict() for s in self._ring.window(seconds, now=self._clock())]

    # ------------------------------------------------------------------
    # Sampling
    # ------------------------------------------------------------------

    async def sample_once(self) -> TelemetrySample | None:
        sample = await read_telemetry(self.profile, now=self._clock())
        if sample is not None:
            self._ring.add(sample)
        return sample

    async def _sample_loop(self, interval: float) -> None:
        while self._running:
            started = asyncio.get_running_loop().time()
            try:
                await self.sample_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("local telemetry sample failed: %s", exc)
            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(0.0, interval - elapsed))

    async def start(self, interval: float = TELEMETRY_INTERVAL_S) -> None:
        if self._running:
            return
        self._running = True
        # Prime, so /agent/telemetry answers with real numbers rather than
        # available=false for the first second of the process's life.
        try:
            await self.sample_once()
        except Exception as exc:
            log.debug("initial telemetry prime failed: %s", exc)
        self._task = asyncio.create_task(self._sample_loop(interval), name="agent-telemetry")
        if self._advertiser is not None:
            self._advertiser.start()

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._advertiser is not None:
            # Withdraw cleanly: a stale advertisement outlives the process and
            # points the next node at a coordinator that is gone.
            self._advertiser.stop()


def create_agent_app(node_agent: NodeAgent):
    """A FastAPI app serving /agent/profile, /agent/telemetry, /agent/health."""
    from fastapi import FastAPI

    app = FastAPI(title="sparkplane node agent", docs_url=None, redoc_url=None)

    @app.get("/agent/profile")
    async def get_profile() -> dict:
        return node_agent.profile_payload()

    @app.get("/agent/telemetry")
    async def get_telemetry() -> dict:
        return node_agent.telemetry_payload()

    @app.get("/agent/health")
    async def get_health() -> dict:
        return node_agent.health_payload()

    app.state.node_agent = node_agent
    return app
