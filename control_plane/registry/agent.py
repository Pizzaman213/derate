"""The node agent. Runs on every container, coordinator or worker.

Reads and an mDNS advertisement. This is the part of the registry that
exists on every machine; a worker is a node agent and nothing else.

``POST /agent/processes/{pid}/kill`` and ``DELETE /agent/models/cache/{folder}``
are the mutating routes on this surface. They carry ``X-Derate-Token`` because
they change the machine, and because every other node runs the same app on the
same LAN. ``GET /agent/journal`` and ``GET /agent/processes`` carry it too: one
is the node's log stream and its read is the trim ack, the other is every GPU
command line. So do the inventory reads (telemetry, storage, the model cache):
none of them is anybody else's business on the LAN. The token is this node's
own agent token (minted at random by the coordinator, ``Registry.agent_token``),
never the cluster token. What the
kill may touch is bounded in ``procs.py``, not here: only PIDs nvidia-smi
currently reports as holding GPU memory, and never our own process tree.

Every gated action and every refusal is journalled as an ``agent`` event, so
the coordinator's archive holds the agent-side fact beside its own audit row.

FastAPI is imported inside ``create_agent_app`` so that importing this module,
and unit-testing NodeAgent, works on a machine without a web framework.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
from pathlib import Path

from control_plane.paths import data_dir
from typing import Callable

from control_plane.contracts import NodeProfile
from control_plane.telemetry import NULL_SINK, TelemetrySink
from control_plane.version import build_id

from . import modelcache, reach, shell_config, storage
from .config import (
    PROFILE_REPROBE_INTERVAL_S,
    ROLE_WORKER,
    TELEMETRY_INTERVAL_S,
)
from .probe import probe_local
from .profiles import profile_supersedes
from .discovery import Advertiser
from .net import same_host
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

try:
    # Only so the route signatures in create_agent_app can name it: with
    # `from __future__ import annotations` FastAPI resolves an annotation
    # against this module's globals, and a `Request` imported inside the
    # factory is invisible there -- the parameter silently becomes a query
    # field and every route answers 422. Optional, like FastAPI itself.
    from starlette.requests import Request
except ImportError:  # pragma: no cover - no web framework installed
    Request = None  # type: ignore[assignment,misc]

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
        # Monotonic time of the last hardware re-probe, anchored in start().
        self._last_reprobe = 0.0
        # Wall-clock time of the last inbound /agent/health request, or None
        # when nobody has ever asked. This is how a node notices it has been
        # forgotten: the coordinator polls every member every 5s, so a member
        # that has not been asked in several intervals is a member whose
        # coordinator has lost it -- restarted with an empty roster, replaced,
        # or dialling an address this node no longer answers on. Recorded from
        # the route rather than from health_payload(), because the fact that
        # matters is that somebody asked over the network.
        self._last_polled: float | None = None
        self._running = False
        self._advertiser = advertiser
        # Defaults to the no-op sink, so a NodeAgent still constructs and
        # tests on a machine with telemetry switched off.
        self._sink = sink
        # This node's agent token (identity.agent_token), and the only thing
        # gating the credentialed routes on this agent. None means no token is
        # configured, which is a refusal rather than a free pass: an agent
        # that cannot check a credential must not act on an uncredentialed
        # request.
        self._token = token or None
        # peer -> (last journalled refusal, refusals since). See note_refused.
        self._refusals: dict[str, tuple[float, int]] = {}
        # (peer, path) -> (last journalled read, reads since). See note_read.
        self._reads: dict[tuple[str, str], tuple[float, int]] = {}
        # The coordinator's address as this node last joined it. A token-gated
        # read from it is routine; from anywhere else it is journalled.
        self._coordinator_host: str | None = None
        # Addresses that presented this agent's token -- the coordinator, in
        # practice. The link probe sink admits them even off a private range.
        self._trusted_peers: set[str] = set()
        # Where this node's data lives, for the storage probe. Defaults to the
        # same env var every other component reads, with the same /data
        # fallback, so an agent constructed without one still reports the real
        # root rather than nothing.
        self._data_root = Path(data_root) if data_root is not None else data_dir()
        # One live shell session per node. See claim_shell below.
        self._shell_open = False

    # ------------------------------------------------------------------
    # Payloads
    # ------------------------------------------------------------------

    @property
    def node_id(self) -> str:
        return self.profile.node_id

    @property
    def uptime_s(self) -> float:
        return self._clock() - self._started_at

    def profile_payload(self, include_build: bool = False) -> dict:
        """This machine's hardware, plus -- for a token holder -- its build.

        ``build`` is not part of NodeProfile and deliberately never will be:
        the profile is hardware, and the software reading it is a different
        fact. It rides along here because "what does this node think it is"
        is not answerable without also knowing what was doing the thinking.
        Only for a caller holding this agent's token, though: the route is
        open (probe-back precedes any token) and a build id is a version
        fingerprint any LAN host could otherwise match against advisories.
        """
        payload = profile_to_dict(self.profile)
        if include_build:
            payload["build"] = build_id()
        return payload

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

    def claim_shell(self) -> bool:
        """Take the single shell slot, or refuse.

        One live session per node, deliberately. Not a resource limit -- a pty
        is cheap -- but an accountability one: two prompts on the same machine
        with no way to tell them apart is how somebody watches a command they
        did not run and cannot find who did. It also bounds what a stolen key
        reaches while a legitimate session is open.
        """
        if self._shell_open:
            return False
        self._shell_open = True
        return True

    def release_shell(self) -> None:
        self._shell_open = False

    def note_shell_opened(
        self, *, host: bool, peer: str = "-", origin: str | None = None
    ) -> None:
        """Record that a root prompt was opened on this machine, from which
        address and which page (``peer``, ``origin``).

        An event, never the output. Terminal output is unbounded and would
        recreate the access-log problem with a payload that can contain
        anything the operator typed, credentials included. What is worth
        keeping is that a session happened, when, and whether it reached the
        host or stopped at this container. ``agent`` and ``shell`` events are
        evidence in the archive: its size cap evicts them only after every
        telemetry row and every anonymous access row has gone, and they are
        journalled even with ``DERATE_TELEMETRY=0``.
        """
        try:
            self._sink.event(
                "shell",
                {
                    "type": "shell_opened",
                    "ts": self._clock(),
                    "node_id": self.profile.node_id,
                    "host": host,
                    "peer": peer,
                    "origin": origin,
                },
            )
        except Exception:
            log.debug("could not record shell event", exc_info=True)

    def set_coordinator(self, host: str | None) -> None:
        """The address of the coordinator this node joined (N-11)."""
        self._coordinator_host = host or None

    def note_read(self, peer: str, method: str, path: str) -> None:
        """A token-gated read answered for somebody other than the coordinator.

        The coordinator reads every member every second; that is not news.
        The same token presented from another address is somebody holding a
        credential they should not, and the read itself is otherwise the one
        thing that leaves no trace. Journalled at most once a minute per peer
        and path, with the count in between, like ``note_refused``.
        """
        if self._coordinator_host and same_host(peer, self._coordinator_host):
            return
        now = self._clock()
        key = (peer, path)
        last, since = self._reads.get(key, (None, 0))
        if last is not None and now - last < 60.0:
            self._reads[key] = (last, since + 1)
            return
        if len(self._reads) >= 1024:
            self._reads.clear()
        self._reads[key] = (now, 0)
        self._event(
            {
                "type": "agent_read",
                "peer": peer,
                "method": method,
                "path": path,
                "suppressed": since,
            }
        )

    def set_token(self, token: str | None) -> None:
        """Adopt this node's agent token, including on a late admission.

        A worker that started as a candidate has no token until a human
        admits it. Without this the agent would keep refusing kills for the
        life of the process, on a node the coordinator already considers a
        member.
        """
        self._token = token or None

    def token_matches(self, presented: str | None) -> bool:
        """Constant-time, on bytes: a header is latin-1 text, and comparing a
        non-ASCII ``str`` raises inside ``compare_digest`` -- a 500 where a
        refusal belongs."""
        if not self._token or not presented:
            return False
        return hmac.compare_digest(
            self._token.encode(), presented.encode("latin-1", "ignore")
        )

    def _event(self, payload: dict) -> None:
        try:
            self._sink.event(
                "agent", {"ts": self._clock(), "node_id": self.node_id, **payload}
            )
        except Exception:
            log.debug("could not record agent event", exc_info=True)

    def note_action(self, kind: str, peer: str, path: str, **detail) -> None:
        """Journal a token-gated action this agent performed.

        The coordinator's audit row says which admin asked; this says what the
        machine itself did and who it did it for, and it is written whether or
        not the request came through the coordinator -- the cluster-token
        holder who talks to the agent directly leaves this and nothing else.
        """
        self._event({"type": kind, "peer": peer, "path": path, **detail})

    def note_trusted(self, peer: str) -> None:
        """Remember an address that presented this agent's token."""
        if peer and peer != "-" and peer not in self._trusted_peers:
            if len(self._trusted_peers) >= 64:
                self._trusted_peers.clear()
            self._trusted_peers.add(peer)

    def trusted_peers(self) -> tuple[str, ...]:
        """Addresses that presented this agent's token (the probe sink's
        allow-list beyond the private ranges)."""
        return tuple(self._trusted_peers)

    def note_refused(self, peer: str, method: str, path: str) -> None:
        """A credentialed route turned somebody away.

        Logged and journalled at most once a minute per peer, the next one
        carrying the count in between, so a LAN host hammering the port can
        fill neither the journal's size cap -- evicting rows the coordinator
        has not collected yet -- nor node.log (N-12).
        """
        now = self._clock()
        last, since = self._refusals.get(peer, (None, 0))
        if last is not None and now - last < 60.0:
            self._refusals[peer] = (last, since + 1)
            return
        if since:
            log.warning(
                "agent refused %s %s from %s (and %d more in the minute before)",
                method, path, peer, since,
            )
        else:
            log.warning("agent refused %s %s from %s", method, path, peer)
        if len(self._refusals) >= 1024:
            self._refusals.clear()
        self._refusals[peer] = (now, 0)
        self._event(
            {
                "type": "agent_refused",
                "peer": peer,
                "method": method,
                "path": path,
                "suppressed": since,
            }
        )

    def health_payload(self, include_build: bool = False) -> dict:
        payload = {
            "status": "ok",
            "node_id": self.node_id,
            "role": self.role,
            "cluster_id": self.cluster_id,
            "uptime_s": round(self.uptime_s, 1),
        }
        if include_build:
            # Rides the heartbeat rather than needing a call of its own: the
            # coordinator already dials this endpoint every 5s, with this
            # node's agent token. Nobody else learns the version.
            payload["build"] = build_id()
        return payload

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

    def note_polled(self) -> None:
        """Somebody just asked this agent for its health over HTTP."""
        self._last_polled = self._clock()

    def seconds_since_poll(self) -> float | None:
        """How long since anyone asked. None when nobody ever has.

        None is not "a long time": on a freshly started node it means the
        coordinator has not had a chance yet, and treating that as abandonment
        would make every boot announce itself twice.
        """
        if self._last_polled is None:
            return None
        return max(0.0, self._clock() - self._last_polled)

    async def reprobe_once(self) -> bool:
        """Re-read this machine's hardware. True when the profile changed.

        A node used to probe exactly once, at construction, so a driver
        installed on a running machine was invisible until somebody restarted
        the container -- and on a Spark that meant a GB10 sitting in the roster
        as unidentified hardware with nothing saying why.

        The probe shells out, so it runs on a thread: this loop also drives
        telemetry and must not stall behind nvidia-smi.

        **A failed probe never overwrites a good profile.** ``probe_local`` is
        total -- it always returns something, and its answer for "I could not
        look" is a fully-formed profile that says UNKNOWN. On a one-shot path
        that is fine, because a join implies the machine just answered. On a
        timer it is not: one wedged nvidia-smi cycle would flip an identified
        GB10 to unidentified, and the roster would flap between the two. So
        UNKNOWN is only ever accepted when we had nothing better already.
        Every other class is a positive identification and lands -- including a
        downgrade like GB10 to CPU, which is what pulling a card actually looks
        like and should be believed.
        """
        try:
            fresh = await asyncio.to_thread(probe_local, self.profile.node_id)
        except Exception as exc:  # probe_local does not raise, but a thread can
            log.debug("re-probe failed: %s", exc)
            return False
        if not profile_supersedes(fresh, self.profile):
            return False
        if fresh == self.profile:
            return False
        log.info(
            "hardware changed on %s: %s -> %s",
            self.profile.node_id,
            self.profile.device_class.value,
            fresh.device_class.value,
        )
        # Publishing is the point, not bookkeeping: /agent/profile reads this
        # attribute, so replacing it IS how the coordinator finds out. A
        # re-probe the node kept to itself would fix nothing.
        self.profile = fresh
        return True

    async def _sample_loop(self, interval: float) -> None:
        while self._running:
            started = asyncio.get_running_loop().time()
            try:
                await self.sample_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("local telemetry sample failed: %s", exc)
            now = asyncio.get_running_loop().time()
            # Hardware on the same loop as telemetry, but two orders of
            # magnitude slower. A second task would be a second thing to
            # cancel; this inherits the one that already exists.
            if now - self._last_reprobe >= PROFILE_REPROBE_INTERVAL_S:
                self._last_reprobe = now
                try:
                    await self.reprobe_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.exception("re-probe failed: %s", exc)
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
        # Anchor the re-probe clock here rather than at construction. The
        # profile we were handed was probed moments ago by start_node, so the
        # first re-probe belongs one full interval away, not on the first tick.
        # It also keeps nvidia-smi out of the short-interval agent tests, which
        # spin this loop for milliseconds and have no business shelling out.
        self._last_reprobe = asyncio.get_running_loop().time()
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

    # No /openapi.json either: the schema would answer whether /agent/shell is
    # installed, which is the one thing the conditional registration below hides.
    app = FastAPI(
        title="derate node agent", docs_url=None, redoc_url=None, openapi_url=None
    )

    def _peer(request: Request) -> str:
        return request.client.host if request.client else "-"

    def _require_token(
        request: Request, presented: str | None, detail: str = "agent token required"
    ) -> None:
        """403 unless *presented* is this node's agent token; every refusal
        is logged with the peer and journalled (``NodeAgent.note_refused``)."""
        if node_agent.token_matches(presented):
            peer = _peer(request)
            node_agent.note_trusted(peer)
            # Reads only: the actions record themselves (note_action).
            if request.method == "GET" or request.url.path == "/agent/reach":
                node_agent.note_read(peer, request.method, request.url.path)
            return
        node_agent.note_refused(_peer(request), request.method, request.url.path)
        raise HTTPException(status_code=403, detail=detail)

    # Open by design: profile and health, because probe-back
    # (registry.probe_remote) and reach call them on nodes that hold no token
    # yet -- and neither says which build is running unless the caller holds
    # the token. Everything else is token-gated: telemetry, storage and the
    # model cache are an inventory of this machine that no other host on the
    # LAN needs, the journal and the process list are worse.

    @app.get("/agent/profile")
    async def get_profile(x_derate_token: str | None = Header(default=None)) -> dict:
        return node_agent.profile_payload(node_agent.token_matches(x_derate_token))

    @app.get("/agent/telemetry")
    async def get_telemetry(
        request: Request, x_derate_token: str | None = Header(default=None)
    ) -> dict:
        _require_token(request, x_derate_token)
        return node_agent.telemetry_payload()

    @app.get("/agent/health")
    async def get_health(
        request: Request, x_derate_token: str | None = Header(default=None)
    ) -> dict:
        node_agent.note_polled()
        trusted = node_agent.token_matches(x_derate_token)
        if trusted:
            node_agent.note_trusted(_peer(request))
        return node_agent.health_payload(trusted)

    @app.get("/agent/journal")
    async def get_journal(
        request: Request,
        since: int = 0,
        limit: int = 2000,
        x_derate_token: str | None = Header(default=None),
    ) -> dict:
        # Token-gated, and before anything else: the rows are this node's
        # whole log stream, and asking for rows after `since` is itself the
        # acknowledgement that everything through `since` reached the
        # coordinator, so the node is free to trim below it. There is no
        # separate ack, which means an uncredentialed read could erase every
        # row the coordinator has not collected yet.
        _require_token(request, x_derate_token)
        payload = await asyncio.to_thread(
            node_agent.journal_payload, since, min(max(limit, 1), 5000)
        )
        if isinstance(payload, dict) and "clamped_from" in payload:
            # An acknowledgement of rows this node never wrote. The journal
            # clamps it (Journal.read) so nothing uncollected is released,
            # but a cursor from the future is either a coordinator that lost
            # its archive or somebody trying to make this node drop its log.
            node_agent.note_action(
                "journal_cursor_clamped", _peer(request), request.url.path,
                since=since,
            )
        return payload

    @app.get("/agent/processes")
    async def get_processes(
        request: Request,
        x_derate_token: str | None = Header(default=None),
    ) -> dict:
        # Token-gated: command lines and PIDs of everything on the GPU.
        _require_token(request, x_derate_token)
        return await node_agent.processes_payload()

    @app.get("/agent/storage")
    async def get_storage(
        request: Request, x_derate_token: str | None = Header(default=None)
    ) -> dict:
        _require_token(request, x_derate_token)
        return await node_agent.storage_payload()

    @app.get("/agent/models/cache")
    async def get_model_cache(
        request: Request, x_derate_token: str | None = Header(default=None)
    ) -> dict:
        _require_token(request, x_derate_token)
        return await node_agent.model_cache_payload()

    @app.delete("/agent/models/cache/{folder}")
    async def delete_model_cache(
        request: Request,
        folder: str,
        x_derate_token: str | None = Header(default=None),
    ) -> dict:
        # Token-gated, like the kill. This deletes hundreds of gigabytes off
        # the machine, and the token is checked before the folder is looked at
        # so an uncredentialled caller cannot learn what is cached by reading
        # which refusal comes back.
        _require_token(request, x_derate_token, "Bad or missing agent token.")
        try:
            result = await node_agent.delete_cached_model(folder)
        except modelcache.DeleteRefused as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        node_agent.note_action(
            "cache_deleted", _peer(request), request.url.path, folder=folder,
            bytes_freed=result.get("bytes_freed") if isinstance(result, dict) else None,
        )
        return result

    @app.post("/agent/reach")
    async def reach_peer(
        request: Request,
        payload: dict,
        x_derate_token: str | None = Header(default=None),
    ) -> dict:
        # Token-gated, and for a different reason from the kill below: this
        # route makes THIS machine dial an address the caller chooses, so an
        # uncredentialed one would be a port scanner that runs inside the
        # operator's network and reports its findings. The token is checked
        # before the body is read.
        _require_token(request, x_derate_token)
        try:
            return await node_agent.reach_payload(payload.get("url"))
        except reach.UnusableTarget as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    @app.post("/agent/processes/{pid}/kill")
    async def kill_process(
        request: Request,
        pid: int,
        x_derate_token: str | None = Header(default=None),
    ) -> dict:
        # The token is checked before anything else, including the PID
        # guards. An uncredentialed caller must not be able to learn which
        # PIDs exist or are protected by reading which refusal comes back.
        _require_token(request, x_derate_token)
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
        body = result.as_dict()
        node_agent.note_action(
            "process_killed", _peer(request), request.url.path, pid=pid,
            name=body.get("name") or "", signal_sent=body.get("signal_sent"),
        )
        return body

    # The shell, and only if it was asked for. Registered conditionally rather
    # than registered-and-refusing: an absent route cannot be probed for, and
    # cannot be switched on by anything arriving over the network.
    if shell_config.enabled():
        # The key is NOT bootstrapped here. This is a factory, and twenty-odd
        # tests call it; generating a secret and creating a data directory is a
        # process-startup side effect, so it lives in startup.py where the node
        # actually boots.
        #
        # Imported here, not at the top: this module must stay importable
        # without FastAPI, and shell_route imports it eagerly.
        from . import shell_route

        shell_route.install(app, node_agent)

    app.state.node_agent = node_agent
    return app
