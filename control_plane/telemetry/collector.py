"""L2: draining journals into the archive.

Pull, not push. The coordinator already pulls health (registry.record_health)
and telemetry (registry.telemetry_round) from every member over the same
AgentClient seam, so this reuses a transport that exists, is bounded by an
explicit timeout, and is testable without a network. It also puts backpressure
where the disk is: a coordinator that cannot keep up simply asks for less.

Asking for rows after N is the acknowledgement that everything through N is
safe here, so there is no second endpoint and no ack message. A replayed
request returns the same rows, and the archive's primary keys absorb them.

The coordinator drains its own journal through this same class, via
LocalSource instead of HttpSource. That is deliberate: the single-node case
exercises the identical ingest path, so it is trustworthy before a second
machine exists.

Every SQLite call goes through asyncio.to_thread. The gateway's event loop
must never wait on a disk -- tests/load/report.py fails a run whose loop-lag
p99 passes 250 ms, and that is exactly the failure a careless commit here
would produce.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Protocol

from . import config
from .archive import Archive
from .journal import Journal
from .retention import compact

log = logging.getLogger(__name__)


class JournalSource(Protocol):
    """One node's journal, however it is reached."""

    node_id: str

    async def read(self, since: int, limit: int, max_bytes: int) -> dict[str, Any]: ...


class LocalSource:
    """This node's own journal, read in a worker thread."""

    def __init__(self, node_id: str, journal: Journal) -> None:
        self.node_id = node_id
        self._journal = journal

    async def read(self, since: int, limit: int, max_bytes: int) -> dict[str, Any]:
        return await asyncio.to_thread(self._journal.read, since, limit, max_bytes)


class HttpSource:
    """Another node's journal, over its agent port."""

    def __init__(self, node_id: str, agent_url: str, client: Any) -> None:
        self.node_id = node_id
        self.agent_url = agent_url.rstrip("/")
        self._client = client

    async def read(self, since: int, limit: int, max_bytes: int) -> dict[str, Any]:
        url = f"{self.agent_url}/agent/journal?since={since}&limit={limit}"
        return await self._client.get_json(url, timeout=config.SHIP_TIMEOUT_S)


class Collector:
    """Drains every known journal into the archive, and compacts on a timer."""

    def __init__(
        self,
        archive: Archive,
        *,
        client: Any = None,
        agents: Callable[[], dict[str, str]] | None = None,
        interval_s: float | None = None,
        compact_interval_s: float = config.COMPACT_INTERVAL_S,
        max_rows: int = config.SHIP_MAX_ROWS,
        max_bytes: int = config.SHIP_MAX_BYTES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.archive = archive
        self._client = client
        self._agents = agents or (lambda: {})
        self._interval_s = (
            config.ship_interval_s() if interval_s is None else interval_s
        )
        self._compact_interval_s = compact_interval_s
        self._max_rows = max_rows
        self._max_bytes = max_bytes
        self._clock = clock
        self._local: dict[str, LocalSource] = {}
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_compact = 0.0

    def add_local(self, node_id: str, journal: Journal) -> None:
        self._local[node_id] = LocalSource(node_id, journal)

    def sources(self) -> list[JournalSource]:
        out: list[JournalSource] = list(self._local.values())
        if self._client is not None:
            try:
                agents = self._agents() or {}
            except Exception:
                log.debug("could not list agent URLs for collection", exc_info=True)
                agents = {}
            for node_id, url in agents.items():
                if node_id in self._local or not url:
                    continue
                out.append(HttpSource(node_id, url, self._client))
        return out

    # -- one round -------------------------------------------------------

    async def poll_once(self) -> dict[str, int]:
        """Drain every source once. A failing node degrades only itself."""
        collected: dict[str, int] = {}
        for source in self.sources():
            try:
                collected[source.node_id] = await self._drain(source)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                log.warning(
                    "telemetry collection from %s failed: %s", source.node_id, detail
                )
                try:
                    await asyncio.to_thread(
                        self.archive.note_ship_failure, source.node_id, detail
                    )
                except Exception:
                    log.debug("could not record ship failure", exc_info=True)
        return collected

    #: Pages per node per round. A bound, so one enormous backlog cannot hog a
    #: round, and with SHIP_MAX_ROWS it sets the sustained catch-up rate.
    MAX_PAGES = 32

    async def _drain(self, source: JournalSource) -> int:
        """Pull from one node until it has nothing more, or the caps say stop."""
        total = 0
        for page in range(self.MAX_PAGES):
            if page:
                # Hand the loop back, and give it long enough to actually
                # drain. Ingest is an uninterrupted stretch of parsing and
                # inserts; paced short ones cost the gateway's tail far less
                # than one long one. See SHIP_PAGE_PAUSE_S.
                await asyncio.sleep(config.SHIP_PAGE_PAUSE_S)
            since = await asyncio.to_thread(self.archive.cursor_for, source.node_id)
            payload = await source.read(since, self._max_rows, self._max_bytes)
            rows = payload.get("rows") or []
            if not rows:
                if payload.get("head") is not None:
                    await asyncio.to_thread(
                        self.archive.ingest, source.node_id, payload
                    )
                return total
            total += await asyncio.to_thread(
                self.archive.ingest, source.node_id, payload
            )
            if len(rows) < self._max_rows:
                return total
        return total

    async def compact_once(self) -> dict[str, Any]:
        return await asyncio.to_thread(compact, self.archive)

    # -- loop ------------------------------------------------------------

    async def _run(self) -> None:
        while self._running:
            try:
                await self.poll_once()
                now = self._clock()
                if now - self._last_compact >= self._compact_interval_s:
                    self._last_compact = now
                    await self.compact_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("telemetry collection round failed")
            await asyncio.sleep(self._interval_s)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._last_compact = self._clock()
        self._task = asyncio.create_task(self._run(), name="telemetry-collector")

    async def stop(self) -> None:
        self._running = False
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
