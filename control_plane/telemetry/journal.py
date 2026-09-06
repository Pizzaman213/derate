"""L1: the append-only journal every node keeps of its own activity.

One SQLite file in WAL mode, one background writer thread, and a bounded
in-memory buffer in front of it. Nothing in this module may block a caller:
the callers are a 1 Hz sampler, the gateway's request path, an event bus and a
logging handler, and a telemetry write that waits on a disk would turn a slow
volume into a slow gateway.

The overflow policy is drop-oldest with a counter, which is the same policy
deploy/events.py already applies to a subscriber that stops draining, for the
same reason: losing the tail of a burst is recoverable, stalling the producer
is not.

One table for all four kinds. That is deliberate -- collection then needs a
single cursor rather than four, and the coordinator is where rows become
typed. See archive.py.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from . import config
from .records import KIND_EVENT, KIND_LOG, KIND_REQUEST, KIND_SAMPLE, RequestRecord

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

#: Emitted into the journal itself when a size trim discards rows that were
#: never collected. A window with no data must say whether it was quiet or
#: trimmed, so the gap travels the ordinary path and lands in the archive's
#: events table like any other event.
GAP_EVENT = "telemetry_gap"

#: Journals with a running writer, by absolute path. Two Journal objects on one
#: file is not corruption -- WAL handles concurrent writers -- but within a
#: single process it means two writer threads and, if both came from a Telemetry
#: bundle, two collectors advancing the same cursors. That happens when a
#: composition root lets registry.startup.start_node() and gateway.create_app()
#: each build their own bundle instead of sharing one. It is invisible from the
#: outside, so it is said out loud here rather than left to be inferred from a
#: doubled CPU cost.
_LIVE: dict[str, int] = {}
_LIVE_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS journal(
  seq  INTEGER PRIMARY KEY AUTOINCREMENT,
  ts   REAL NOT NULL,
  kind TEXT NOT NULL,
  body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS journal_ts ON journal(ts);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


def _connect(path: Path, *, writer: bool = True) -> sqlite3.Connection:
    """A connection to the journal.

    *writer* is the long-lived one the background thread owns; it is the only
    one that needs the auto-vacuum setting. Short-lived connections still
    write -- reading acknowledges collection, which moves the high-water
    mark -- so this is not a read-only mode, just a lighter setup.
    """
    conn = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    # Telemetry is not worth an fsync per commit. WAL plus NORMAL loses at
    # most the last commits to a machine losing power, and never corrupts.
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    if writer:
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    return conn


class Journal:
    """A node's durable record of what it did. Implements TelemetrySink."""

    enabled = True

    def __init__(
        self,
        path: Path | str,
        node_id: str = "",
        *,
        queue_max: int = config.JOURNAL_QUEUE_MAX,
        batch_rows: int = config.JOURNAL_BATCH_ROWS,
        batch_interval_s: float = config.JOURNAL_BATCH_INTERVAL_S,
        retention_s: float = config.JOURNAL_RETENTION_S,
        max_bytes: int = config.JOURNAL_MAX_BYTES,
        trim_interval_s: float = config.JOURNAL_TRIM_INTERVAL_S,
        clock=time.time,
    ) -> None:
        self.path = Path(path)
        self.node_id = node_id
        self._queue_max = queue_max
        self._batch_rows = batch_rows
        self._batch_interval_s = batch_interval_s
        self._retention_s = retention_s
        self._max_bytes = max_bytes
        self._trim_interval_s = trim_interval_s
        self._clock = clock

        self._buffer: deque[tuple[float, str, str]] = deque(maxlen=queue_max)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._dropped = 0
        self._written = 0
        self._appended = 0
        self._settled = threading.Condition(self._lock)
        self._last_trim = 0.0

        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = _connect(self.path)
        try:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT OR REPLACE INTO meta(k, v) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            if node_id:
                conn.execute(
                    "INSERT OR REPLACE INTO meta(k, v) VALUES('node_id', ?)", (node_id,)
                )
            elif (row := conn.execute("SELECT v FROM meta WHERE k='node_id'").fetchone()):
                self.node_id = row[0]
        finally:
            conn.close()

    # -- producer side, called from any thread ---------------------------

    def append(self, kind: str, body: Any, ts: float | None = None) -> None:
        """Buffer one row. Never blocks, never raises."""
        try:
            encoded = json.dumps(body, default=str, separators=(",", ":"))
        except Exception:  # pragma: no cover - json.dumps with default=str is total
            log.debug("telemetry row could not be encoded", exc_info=True)
            return
        row = (self._clock() if ts is None else ts, kind, encoded)
        with self._lock:
            if len(self._buffer) == self._queue_max:
                # deque drops the oldest for us; count it so the gap is visible.
                self._dropped += 1
            self._buffer.append(row)
            self._appended += 1
            ready = len(self._buffer) >= self._batch_rows
        # Only nudge the writer once a batch has accumulated. Waking it per row
        # defeats the batching entirely: under load it commits a transaction
        # per append, which costs more CPU than everything else here put
        # together. Below the threshold the writer's own interval timer
        # commits, so a quiet node still persists within batch_interval_s.
        if ready:
            self._wake.set()

    # The TelemetrySink surface.

    def sample(self, node_id: str, sample: Any) -> None:
        body = sample.as_dict() if hasattr(sample, "as_dict") else dict(sample)
        ts = body.get("ts")
        self.append(KIND_SAMPLE, body, ts=ts)

    def request(self, record: RequestRecord) -> None:
        body = record.as_dict() if isinstance(record, RequestRecord) else dict(record)
        self.append(KIND_REQUEST, body, ts=body.get("ts"))

    def event(self, source: str, event: dict[str, Any]) -> None:
        body = {"source": source}
        body.update(event)
        self.append(KIND_EVENT, body, ts=body.get("ts"))

    def log(self, entry: dict[str, Any]) -> None:
        self.append(KIND_LOG, entry, ts=entry.get("ts"))

    # -- writer thread ---------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._warn_if_already_live()
        self._running = True
        self._thread = threading.Thread(
            target=self._run, name="telemetry-journal", daemon=True
        )
        self._thread.start()

    def _warn_if_already_live(self) -> None:
        key = str(self.path.resolve())
        with _LIVE_LOCK:
            existing = _LIVE.get(key, 0)
            _LIVE[key] = existing + 1
        if existing:
            log.warning(
                "a second telemetry journal is starting on %s in this process. "
                "Two writer threads, and two collectors if both came from a "
                "Telemetry bundle. A composition root should build one bundle "
                "and pass it to both start_node() and create_app(), e.g. "
                "create_app(deps, telemetry=runtime.telemetry).",
                key,
            )

    def _release(self) -> None:
        key = str(self.path.resolve())
        with _LIVE_LOCK:
            remaining = _LIVE.get(key, 1) - 1
            if remaining > 0:
                _LIVE[key] = remaining
            else:
                _LIVE.pop(key, None)

    def close(self, timeout: float = 5.0) -> None:
        """Stop the writer, flushing whatever is buffered."""
        if self._running:
            self._release()
        self._running = False
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None
        if thread is None or thread.is_alive():
            # Either we were never started or the writer is wedged; either way
            # the buffered rows are still ours to persist.
            self.flush()

    def _run(self) -> None:
        conn = _connect(self.path)
        try:
            while self._running:
                self._wake.wait(self._batch_interval_s)
                self._wake.clear()
                try:
                    self._drain(conn)
                except Exception:
                    log.exception("telemetry journal write failed")
                self._maybe_trim(conn)
            try:
                self._drain(conn)
            except Exception:
                log.exception("telemetry journal final flush failed")
        finally:
            conn.close()

    def _take(self, limit: int) -> list[tuple[float, str, str]]:
        with self._lock:
            if not self._buffer:
                return []
            n = min(limit, len(self._buffer))
            return [self._buffer.popleft() for _ in range(n)]

    def _drain(self, conn: sqlite3.Connection) -> None:
        while True:
            batch = self._take(self._batch_rows)
            if not batch:
                return
            conn.execute("BEGIN")
            try:
                conn.executemany(
                    "INSERT INTO journal(ts, kind, body) VALUES(?, ?, ?)", batch
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                # Put them back at the front rather than losing them: a full
                # disk that recovers should not have cost us the rows written
                # while it was full.
                with self._settled:
                    self._buffer.extendleft(reversed(batch))
                    self._settled.notify_all()
                raise
            with self._settled:
                self._written += len(batch)
                self._settled.notify_all()

    def flush(self) -> None:
        """Persist buffered rows on the calling thread. For tests and shutdown."""
        conn = _connect(self.path)
        try:
            self._drain(conn)
        finally:
            conn.close()

    def sync(self, timeout: float = 5.0) -> bool:
        """Block until everything appended so far is committed.

        Wakes the writer rather than waiting out its interval.

        An empty buffer does not mean a durable write: the writer thread pops a
        batch before it commits it, so a caller watching only the queue length
        can read the database in the window between the two and see nothing.
        Callers that need to read their own writes -- tests, and shutdown --
        need this instead.
        """
        self._wake.set()
        with self._settled:
            target = self._appended
            deadline = time.monotonic() + timeout
            while self._written + self._dropped < target:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                if self._thread is None or not self._thread.is_alive():
                    break
                self._settled.wait(min(remaining, 0.05))
            else:
                return True
        # No writer running, or it died: persist on this thread instead.
        self.flush()
        return True

    # -- retention -------------------------------------------------------

    def _maybe_trim(self, conn: sqlite3.Connection) -> None:
        now = self._clock()
        if now - self._last_trim < self._trim_interval_s:
            return
        self._last_trim = now
        try:
            self.trim(conn)
        except Exception:
            log.exception("telemetry journal trim failed")

    def trim(self, conn: sqlite3.Connection | None = None) -> int:
        """Drop collected and expired rows. Returns how many went.

        Rows that have not been collected are safe until the size cap trips.
        Past it they go oldest-first and the discard is announced as a gap
        event, because a hole nobody knows about is worse than a small file.
        """
        owned = conn is None
        conn = conn or _connect(self.path)
        try:
            shipped = self._meta_int(conn, "shipped_hwm", 0)
            cutoff = self._clock() - self._retention_s
            row = conn.execute(
                "SELECT MAX(seq) FROM journal WHERE ts < ? AND seq <= ?",
                (cutoff, shipped),
            ).fetchone()
            safe_to = row[0] if row and row[0] is not None else 0
            deleted = 0
            if safe_to:
                deleted += conn.execute(
                    "DELETE FROM journal WHERE seq <= ?", (safe_to,)
                ).rowcount
            deleted += self._enforce_size(conn)
            if deleted:
                conn.execute("PRAGMA incremental_vacuum")
            return deleted
        finally:
            if owned:
                conn.close()

    def _enforce_size(self, conn: sqlite3.Connection) -> int:
        deleted = 0
        while self._file_bytes(conn) > self._max_bytes:
            rows = conn.execute(
                "SELECT seq, ts FROM journal ORDER BY seq LIMIT ?",
                (self._batch_rows,),
            ).fetchall()
            if not rows:
                return deleted
            first_ts, last_seq, last_ts = rows[0][1], rows[-1][0], rows[-1][1]
            shipped = self._meta_int(conn, "shipped_hwm", 0)
            deleted += conn.execute(
                "DELETE FROM journal WHERE seq <= ?", (last_seq,)
            ).rowcount
            conn.execute("PRAGMA incremental_vacuum")
            if last_seq > shipped:
                self._note_gap(conn, first_ts, last_ts)
        return deleted

    def _note_gap(self, conn: sqlite3.Connection, from_ts: float, to_ts: float) -> None:
        body = json.dumps(
            {
                "source": "telemetry",
                "type": GAP_EVENT,
                "ts": self._clock(),
                "node_id": self.node_id,
                "from_ts": from_ts,
                "to_ts": to_ts,
                "reason": "journal_size_cap",
            },
            separators=(",", ":"),
        )
        conn.execute(
            "INSERT INTO journal(ts, kind, body) VALUES(?, ?, ?)",
            (self._clock(), KIND_EVENT, body),
        )
        log.warning(
            "telemetry journal hit its %d MiB cap; discarded uncollected rows "
            "from %.0f to %.0f",
            self._max_bytes // (1024 * 1024),
            from_ts,
            to_ts,
        )

    @staticmethod
    def _file_bytes(conn: sqlite3.Connection) -> int:
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        return int(page_count) * int(page_size)

    @staticmethod
    def _meta_int(conn: sqlite3.Connection, key: str, default: int) -> int:
        row = conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return default

    # -- collector side --------------------------------------------------

    def read(
        self,
        since: int = 0,
        limit: int = config.SHIP_MAX_ROWS,
        max_bytes: int = config.SHIP_MAX_BYTES,
    ) -> dict[str, Any]:
        """Rows after *since*, capped by count and by encoded size.

        Asking for rows after N is itself the acknowledgement that everything
        up to N is safe on the coordinator, so this advances the high-water
        mark. That keeps the protocol to one endpoint and makes a replayed
        request harmless.
        """
        conn = _connect(self.path, writer=False)
        try:
            if since > 0:
                conn.execute(
                    "INSERT INTO meta(k, v) VALUES('shipped_hwm', ?) "
                    "ON CONFLICT(k) DO UPDATE SET v=excluded.v "
                    "WHERE CAST(meta.v AS INTEGER) < ?",
                    (str(since), since),
                )
            cursor = conn.execute(
                "SELECT seq, ts, kind, body FROM journal WHERE seq > ? "
                "ORDER BY seq LIMIT ?",
                (since, max(1, limit)),
            )
            rows: list[dict[str, Any]] = []
            budget = max_bytes
            next_seq = since
            for seq, ts, kind, body in cursor:
                budget -= len(body) + 64
                if budget < 0 and rows:
                    break
                rows.append({"seq": seq, "ts": ts, "kind": kind, "body": body})
                next_seq = seq
            head = conn.execute("SELECT MAX(seq) FROM journal").fetchone()[0] or 0
            return {
                "node_id": self.node_id,
                "rows": rows,
                "next": next_seq,
                "head": head,
                "dropped": self._dropped,
                "schema_version": SCHEMA_VERSION,
            }
        finally:
            conn.close()

    def stats(self) -> dict[str, Any]:
        conn = _connect(self.path, writer=False)
        try:
            head = conn.execute("SELECT MAX(seq) FROM journal").fetchone()[0] or 0
            count = conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
            shipped = self._meta_int(conn, "shipped_hwm", 0)
            bytes_on_disk = self._file_bytes(conn)
        finally:
            conn.close()
        with self._lock:
            queued = len(self._buffer)
        return {
            "node_id": self.node_id,
            "path": str(self.path),
            "rows": count,
            "head": head,
            "shipped_hwm": shipped,
            "queued": queued,
            "dropped": self._dropped,
            "written": self._written,
            "bytes": bytes_on_disk,
        }


def open_journal(
    node_id: str = "", root: Path | str | None = None, **kwargs: Any
) -> Journal:
    """Open the node's journal under the data root and start its writer."""
    journal = Journal(config.journal_path(root), node_id=node_id, **kwargs)
    journal.start()
    return journal
