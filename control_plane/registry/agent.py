"""The node agent. Runs on every container, coordinator or worker.

Reads and an mDNS advertisement. This is the part of the registry that
exists on every machine; a worker is a node agent and nothing else.

``POST /agent/processes/{pid}/kill`` is the one exception and the only
mutating route on this surface. It carries the cluster token because it is
the only thing here that changes the machine, and because every other node
runs the same app on the same LAN. What it may touch is bounded in
``procs.py``, not here: only PIDs nvidia-smi currently reports as holding
GPU memory, and never our own process tree.

FastAPI is imported inside ``create_agent_app`` so that importing this module,
and unit-testing NodeAgent, works on a machine without a web framework.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Callable

from control_plane.contracts import NodeProfile
from control_plane.telemetry import NULL_SINK, TelemetrySink

from . import modelcache, reach, storage
from .config import ROLE_WORKER, TELEMETRY_INTERVAL_S
from .discovery import Advertiser
from .procs import KillRefused, kill_gpu_process
from .serde import (
    processes_to_dict,
    profile_to_dict,
    storage_to_dict,
    telemetry_to_dict,
)
from .telemetry import (
    RingBuffer,
    TelemetrySample,
    read_gpu_processes,
    read_telemetry,
)

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
        sink: TelemetrySink = NULL_SINK,
        token: str | None = None,
        data_root: Path | str | None = None,
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
        # Defaults to the no-op sink, so a NodeAgent still constructs and
        # tests on a machine with telemetry switched off.
        self._sink = sink
        # The cluster token, and the only thing gating the one mutating
        # endpoint on this agent. None means no token is configured, which
        # is a refusal rather than a free pass: an agent that cannot check a
        # credential must not act on an uncredentialed request.
        self._token = token or None
        # Where this node's data lives, for the storage probe. Defaults to the
        # same env var every other component reads, with the same /data
        # fallback, so an agent constructed without one still reports the real
        # root rather than nothing.
        self._data_root = Path(
            data_root
            if data_root is not None
            else os.environ.get("DERATE_DATA_DIR", "/data")
        )

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

    async def processes_payload(self) -> dict:
        """What is holding GPU memory right now, read on demand.

        Not served from the ring: the 5s sample deliberately keeps only a
        sum and a count, so a process list never reaches the durable
        journal. This costs one nvidia-smi call per request, and only while
        an operator has a node sheet open.
        """
        return processes_to_dict(self.node_id, await read_gpu_processes())

    async def storage_payload(self) -> dict:
        """Disk capacity and our share of it, read on demand.

        Same reasoning as ``processes_payload`` above: this is never sampled,
        so the journal carries no disk columns and the archive schema is
        untouched. The walk is a blocking syscall loop, so it goes to a thread
        exactly as ``journal_payload`` does -- the agent's event loop also
        answers /agent/health, and a health check must not queue behind a
        directory walk.
        """
        try:
            payload = await asyncio.to_thread(
                storage.storage_payload, self.node_id, self._data_root
            )
        except Exception:
            log.exception("storage probe failed")
            return storage_to_dict(None, self.node_id)
        return storage_to_dict(payload, self.node_id)

    async def model_cache_payload(self) -> dict:
        """The downloaded weights on this node, read on demand.

        The control plane never downloaded these -- the runtime container did,
        into the host cache it mounts -- so this is the one place the product
        can see them at all. Cheap: only ``blobs/`` is stat'd, and it is flat.
        """
        try:
            payload = await asyncio.to_thread(modelcache.scan)
        except Exception:
            log.exception("model cache scan failed")
            return {
                "available": False,
                "path": None,
                "repos": [],
                "total_bytes": None,
                "reason": "The model cache could not be read on this node.",
            }
        payload["node_id"] = self.node_id
        return payload

    async def delete_cached_model(self, folder: str) -> dict:
        """Remove one cached repository. Raises DeleteRefused on any guard.

        Whether the model is in use is deliberately NOT decided here: the
        agent has no idea what a deployment is. That check belongs to the
        coordinator, exactly as it does for killing a GPU process.
        """
        return await asyncio.to_thread(modelcache.delete, folder)

    def set_token(self, token: str | None) -> None:
        """Adopt the cluster token, including on a late admission.

        A worker that started as a candidate has no token until a human
        admits it. Without this the agent would keep refusing kills for the
        life of the process, on a node the coordinator already considers a
        member.
        """
        self._token = token or None

    def token_matches(self, presented: str | None) -> bool:
        if not self._token or not presented:
            return False
        return secrets.compare_digest(self._token, presented)

    def health_payload(self) -> dict:
        return {
            "status": "ok",
            "node_id": self.node_id,
            "role": self.role,
            "cluster_id": self.cluster_id,
            "uptime_s": round(self.uptime_s, 1),
        }

    async def reach_payload(self, url: object, client=None) -> dict:
        """Dial another node's agent from THIS machine and report what happened.

        The coordinator can already tell whether it can reach each node; what
        it cannot see from where it stands is whether two workers can reach
        each other. This is that leg, run from the only place it can be run
        from. `client` is an injection point for tests; production leaves it
        None and one is built on demand rather than held open, because most
        agents are never asked this.
        """
        if not isinstance(url, str) or not url.strip():
            raise reach.UnusableTarget('Body must be {"url": "http://host:port"}.')
        # Raises UnusableTarget before anything is dialled, so a malformed
        # address comes back as a 400 naming it rather than as a failed probe.
        target = reach.validate_target(url)
        if client is None:
            from .client import HttpAgentClient

            client = HttpAgentClient()
        try:
            leg = await reach.dial(
                client.get_json, self.node_id, "", target, reach.REACH_TIMEOUT_S
            )
        finally:
            aclose = getattr(client, "aclose", None)
            if callable(aclose):
                try:
                    await aclose()
                except Exception:
                    pass
        # The target's node_id is not ours to assert -- we asked for an
        # address, not for a name -- so `target` stays empty here and the
        # coordinator, which knows which node it asked about, fills it in.
        return leg.as_dict()

    def history(self, seconds: float = 60.0) -> list[dict]:
        return [s.as_dict() for s in self._ring.window(seconds, now=self._clock())]

    def journal_payload(self, since: int = 0, limit: int = 2000) -> dict:
        """Journal rows after *since*, for the coordinator's collector.

        Answers an empty payload rather than 404ing when telemetry is off, so
        a coordinator polling a node that has it disabled sees "nothing to
        collect" instead of a node that looks broken.
        """
        read = getattr(self._sink, "read", None)
        if read is None:
            return {"node_id": self.node_id, "rows": [], "next": since, "head": 0}
        return read(since, limit)

    # ------------------------------------------------------------------
    # Sampling
    # ------------------------------------------------------------------

    async def sample_once(self) -> TelemetrySample | None:
        sample = await read_telemetry(self.profile, now=self._clock())
        if sample is not None:
            self._ring.add(sample)
            # The ring is unchanged: it still answers /agent/telemetry and the
            # UI's 60-second graph. This is the copy that outlives the process.
            self._sink.sample(self.node_id, sample)
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
    """A FastAPI app serving the /agent/* surface."""
    from fastapi import FastAPI, Header, HTTPException

    app = FastAPI(title="derate node agent", docs_url=None, redoc_url=None)

    @app.get("/agent/profile")
    async def get_profile() -> dict:
        return node_agent.profile_payload()

    @app.get("/agent/telemetry")
    async def get_telemetry() -> dict:
        return node_agent.telemetry_payload()

    @app.get("/agent/health")
    async def get_health() -> dict:
        return node_agent.health_payload()

    @app.get("/agent/journal")
    async def get_journal(since: int = 0, limit: int = 2000) -> dict:
        # Asking for rows after `since` is itself the acknowledgement that
        # everything through `since` reached the coordinator, so the node is
        # free to trim below it. There is no separate ack.
        return await asyncio.to_thread(
            node_agent.journal_payload, since, min(max(limit, 1), 5000)
        )

    @app.get("/agent/processes")
    async def get_processes() -> dict:
        return await node_agent.processes_payload()

    @app.get("/agent/storage")
    async def get_storage() -> dict:
        # Uncredentialed, like /agent/profile and /agent/telemetry. It reads
        # capacity and the sizes of files this product wrote; the one route on
        # this surface that changes the machine is the kill below.
        return await node_agent.storage_payload()

    @app.get("/agent/models/cache")
    async def get_model_cache() -> dict:
        return await node_agent.model_cache_payload()

    @app.delete("/agent/models/cache/{folder}")
    async def delete_model_cache(
        folder: str,
        x_derate_token: str | None = Header(default=None),
    ) -> dict:
        # Token-gated, like the kill. This deletes hundreds of gigabytes off
        # the machine, and the token is checked before the folder is looked at
        # so an uncredentialled caller cannot learn what is cached by reading
        # which refusal comes back.
        if not node_agent.token_matches(x_derate_token):
            raise HTTPException(status_code=403, detail="Bad or missing cluster token.")
        try:
            return await node_agent.delete_cached_model(folder)
        except modelcache.DeleteRefused as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    @app.post("/agent/reach")
    async def reach_peer(
        payload: dict,
        x_derate_token: str | None = Header(default=None),
    ) -> dict:
        # Token-gated, and for a different reason from the kill below: this
        # route makes THIS machine dial an address the caller chooses, so an
        # uncredentialed one would be a port scanner that runs inside the
        # operator's network and reports its findings. The token is checked
        # before the body is read.
        if not node_agent.token_matches(x_derate_token):
            raise HTTPException(status_code=403, detail="cluster token required")
        try:
            return await node_agent.reach_payload(payload.get("url"))
        except reach.UnusableTarget as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    @app.post("/agent/processes/{pid}/kill")
    async def kill_process(
        pid: int,
        x_derate_token: str | None = Header(default=None),
    ) -> dict:
        # The token is checked before anything else, including the PID
        # guards. An uncredentialed caller must not be able to learn which
        # PIDs exist or are protected by reading which refusal comes back.
        if not node_agent.token_matches(x_derate_token):
            raise HTTPException(status_code=403, detail="cluster token required")
        try:
            result = await kill_gpu_process(pid)
        except KillRefused as exc:
            status = 404 if exc.code == "not_a_gpu_process" else 400
            if exc.code == "kill_not_permitted":
                status = 403
            elif exc.code == "gpu_unreadable":
                status = 503
            raise HTTPException(
                status_code=status, detail={"code": exc.code, "message": exc.message}
            ) from exc
        return result.as_dict()

    app.state.node_agent = node_agent
    return app
