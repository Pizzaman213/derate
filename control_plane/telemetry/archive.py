"""L3: the coordinator's archive.

Journal rows arrive as opaque JSON and become typed rows here. One SQLite
file, WAL, incremental auto-vacuum so that deleting a month's samples actually
returns the disk.

Ingest is transactional per batch, and the cursor advances inside the same
transaction as the rows it covers. That is what makes collection idempotent:
a coordinator that dies mid-batch re-asks from the last committed cursor and
gets the same rows again, and the primary keys absorb the repeat.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from . import config
from .records import KIND_EVENT, KIND_LOG, KIND_REQUEST, KIND_SAMPLE

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples(
  node_id               TEXT NOT NULL,
  ts                    REAL NOT NULL,
  memory_used           INTEGER,
  memory_total          INTEGER,
  power_w               REAL,
  temp_c                REAL,
  util_pct              REAL,
  gpu_memory_used       INTEGER,
  gpu_process_count     INTEGER,
  host_memory_total     INTEGER,
  host_memory_available INTEGER,
  swap_used             INTEGER,
  PRIMARY KEY(node_id, ts)
) WITHOUT ROWID;

-- One row per attempt, not per client request: a request that failed over
-- produces two, and the pair is the record of the failover having happened.
CREATE TABLE IF NOT EXISTS requests(
  request_id       TEXT NOT NULL,
  attempt_no       INTEGER NOT NULL DEFAULT 0,
  ts               REAL NOT NULL,
  node_id          TEXT,
  served_name      TEXT,
  target_id        TEXT,
  target_kind      TEXT,
  provider_id      TEXT,
  deployment_id    TEXT,
  policy           TEXT,
  strength_source  TEXT,
  attempts         INTEGER,
  retry_reason     TEXT,
  status           INTEGER,
  error_code       TEXT,
  error_class      TEXT,
  prompt_tokens    INTEGER,
  completion_tokens INTEGER,
  tokens           INTEGER,
  tokens_estimated INTEGER,
  cost_usd         REAL,
  ttft_ms          REAL,
  decode_ms        REAL,
  duration_ms      REAL,
  parked_ms        REAL,
  streaming        INTEGER,
  body_bytes       INTEGER,
  admission_code   TEXT,
  kv_bytes         INTEGER,
  PRIMARY KEY(request_id, attempt_no)
);
CREATE INDEX IF NOT EXISTS requests_ts ON requests(ts);
CREATE INDEX IF NOT EXISTS requests_model_ts ON requests(served_name, ts);

CREATE TABLE IF NOT EXISTS events(
  node_id       TEXT NOT NULL,
  seq           INTEGER NOT NULL,
  ts            REAL NOT NULL,
  source        TEXT,
  type          TEXT,
  deployment_id TEXT,
  served_name   TEXT,
  body          TEXT NOT NULL,
  PRIMARY KEY(node_id, seq)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS events_type_ts ON events(type, ts);

CREATE TABLE IF NOT EXISTS logs(
  node_id TEXT NOT NULL,
  seq     INTEGER NOT NULL,
  ts      REAL NOT NULL,
  level   TEXT,
  logger  TEXT,
  message TEXT,
  body    TEXT,
  PRIMARY KEY(node_id, seq)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS logs_ts ON logs(ts);
CREATE INDEX IF NOT EXISTS logs_level_ts ON logs(level, ts);

CREATE TABLE IF NOT EXISTS cursors(
  node_id      TEXT PRIMARY KEY,
  last_seq     INTEGER NOT NULL DEFAULT 0,
  last_ship_ts REAL,
  dropped      INTEGER NOT NULL DEFAULT 0,
  head         INTEGER NOT NULL DEFAULT 0,
  last_error   TEXT
);

CREATE TABLE IF NOT EXISTS gaps(
  node_id TEXT NOT NULL,
  from_ts REAL NOT NULL,
  to_ts   REAL NOT NULL,
  reason  TEXT,
  PRIMARY KEY(node_id, from_ts, reason)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS rollup_samples(
  step        INTEGER NOT NULL,
  node_id     TEXT NOT NULL,
  bucket      INTEGER NOT NULL,
  n           INTEGER NOT NULL,
  power_w_avg REAL, power_w_max REAL,
  temp_c_avg  REAL, temp_c_max  REAL,
  util_pct_avg REAL, util_pct_max REAL,
  memory_used_avg REAL, memory_used_max INTEGER,
  gpu_memory_used_avg REAL, gpu_memory_used_max INTEGER,
  host_memory_available_min INTEGER,
  swap_used_max INTEGER,
  PRIMARY KEY(step, node_id, bucket)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS rollup_requests(
  step        INTEGER NOT NULL,
  bucket      INTEGER NOT NULL,
  served_name TEXT NOT NULL,
  target_id   TEXT NOT NULL,
  n           INTEGER NOT NULL,
  ok          INTEGER NOT NULL,
  failed      INTEGER NOT NULL,
  tokens      INTEGER NOT NULL,
  prompt_tokens INTEGER NOT NULL,
  cost_usd    REAL,
  ttft_hist   BLOB,
  duration_hist BLOB,
  PRIMARY KEY(step, bucket, served_name, target_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""

_SAMPLE_COLS = (
    "memory_used",
    "memory_total",
    "power_w",
    "temp_c",
    "util_pct",
    "gpu_memory_used",
    "gpu_process_count",
    "host_memory_total",
    "host_memory_available",
    "swap_used",
)

_REQUEST_COLS = (
    "request_id",
    "ts",
    "node_id",
    "served_name",
    "target_id",
    "target_kind",
    "provider_id",
    "deployment_id",
    "policy",
    "strength_source",
    "attempt_no",
    "attempts",
    "retry_reason",
    "status",
    "error_code",
    "error_class",
    "prompt_tokens",
    "completion_tokens",
    "tokens",
    "tokens_estimated",
    "cost_usd",
    "ttft_ms",
    "decode_ms",
    "duration_ms",
    "parked_ms",
    "streaming",
    "body_bytes",
    "admission_code",
    "kv_bytes",
)


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread is off because the collector reaches the archive
    # through asyncio.to_thread, which hands work to whichever pool thread
    # is free. Every entry point below takes _lock, so the connection is
    # still only ever used by one thread at a time.
    conn = sqlite3.connect(
        str(path), timeout=30.0, isolation_level=None, check_same_thread=False
    )
    # BEFORE journal_mode, which fixes the page size and makes a later
    # auto_vacuum change a no-op that reports no error. See the same note in
    # journal.py::_connect, where getting this order wrong evicted the whole
    # journal on every trim. An existing archive is converted by
    # retention.py, not here: a VACUUM at the 16 GiB ceiling is ~48s and this
    # runs inside start_node, before the gateway answers anything.
    conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn


class Archive:
    """Typed, queryable, retained. Coordinator only."""

    def __init__(self, path: Path | str, clock=time.time) -> None:
        self.path = Path(path)
        self._clock = clock
        # Re-entrant: ingest() holds the lock across _ingest_row(), which can
        # call note_gap().
        self._lock = threading.RLock()
        self._conn = connect(self.path)
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR REPLACE INTO meta(k, v) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )

    def close(self) -> None:
        """Close the connection, but not out from under a writer.

        Every other path through this class takes :attr:`lock` before touching
        :attr:`conn`; this one did not, and the gap is a segfault rather than
        an exception. `ingest` runs its whole transaction inside the lock on a
        telemetry pool thread, and `TelemetryService.stop` calls this from the
        gateway lifespan -- so shutting down while a poll was mid-INSERT freed
        the sqlite3 connection under the C extension still executing on it.
        That crashed the test suite intermittently, from
        `app.py::lifespan` -> `service.py::stop` -> here, with a worker parked
        in `ingest`.

        Closing a connection a blocked writer already captured is safe on its
        own: sqlite3 raises ProgrammingError on a closed handle. It is closing
        it *during* a statement that is not.
        """
        with self._lock:
            try:
                self._conn.close()
            except Exception:  # pragma: no cover
                pass

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    @property
    def lock(self) -> threading.RLock:
        """Held by anything touching :attr:`conn` directly, retention included."""
        return self._lock

    # -- ingest ----------------------------------------------------------

    def cursor_for(self, node_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_seq FROM cursors WHERE node_id=?", (node_id,)
            ).fetchone()
        return int(row["last_seq"]) if row else 0

    def ingest(self, node_id: str, payload: dict[str, Any]) -> int:
        """Fan one poll's rows into typed tables and advance the cursor.

        Rows and cursor move in one transaction, so a crash anywhere in here
        re-asks from the last committed position rather than losing or
        duplicating a batch.
        """
        rows = payload.get("rows") or []
        next_seq = int(payload.get("next") or 0)
        head = int(payload.get("head") or 0)
        dropped = int(payload.get("dropped") or 0)

        conn = self._conn
        with self._lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if rows:
                    self._ingest_rows(node_id, rows)
                    # A worker unreachable for an hour delivers an hour of
                    # backlog at once, and those rows land in buckets that
                    # compaction already rolled. Remember how far back this
                    # batch reached, so the next pass re-rolls from there
                    # rather than trusting a clock-derived watermark and
                    # silently under-counting them.
                    self._note_dirty(min(float(r.get("ts") or 0.0) for r in rows))
                conn.execute(
                    "INSERT INTO cursors(node_id, last_seq, last_ship_ts, dropped, head) "
                    "VALUES(?, ?, ?, ?, ?) "
                    "ON CONFLICT(node_id) DO UPDATE SET "
                    "  last_seq=MAX(cursors.last_seq, excluded.last_seq), "
                    "  last_ship_ts=excluded.last_ship_ts, "
                    "  dropped=excluded.dropped, "
                    "  head=excluded.head, "
                    "  last_error=NULL",
                    (node_id, next_seq, self._clock(), dropped, head),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return len(rows)

    def note_ship_failure(self, node_id: str, detail: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO cursors(node_id, last_seq, last_error) VALUES(?, 0, ?) "
                "ON CONFLICT(node_id) DO UPDATE SET last_error=excluded.last_error",
                (node_id, detail[:500]),
            )

    def _ingest_rows(self, node_id: str, rows: list[dict[str, Any]]) -> None:
        """Fan a page into typed tables, one executemany per kind.

        Grouping first and inserting in bulk rather than a statement per row is
        what keeps a page's ingest short. The collector runs this in a worker
        thread while the gateway is serving, so how long one uninterrupted
        stretch of it lasts shows up directly in the gateway's p99.
        """
        samples: list[tuple] = []
        requests: list[tuple] = []
        events: list[tuple] = []
        logs: list[tuple] = []
        gaps: list[tuple] = []

        for row in rows:
            kind = row.get("kind")
            seq = int(row.get("seq") or 0)
            ts = float(row.get("ts") or 0.0)
            raw = row.get("body")
            try:
                body = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
            except (ValueError, TypeError):
                log.debug("undecodable journal row from %s seq %s", node_id, seq)
                continue
            if not isinstance(body, dict):
                continue

            if kind == KIND_SAMPLE:
                samples.append((node_id, ts, *(body.get(c) for c in _SAMPLE_COLS)))
            elif kind == KIND_REQUEST:
                if not body.get("request_id"):
                    continue
                body["node_id"] = body.get("node_id") or node_id
                body["ts"] = ts
                requests.append(tuple(_coerce(body.get(c)) for c in _REQUEST_COLS))
            elif kind == KIND_EVENT:
                text = raw if isinstance(raw, str) else json.dumps(body, default=str)
                events.append(
                    (
                        node_id,
                        seq,
                        ts,
                        body.get("source", ""),
                        body.get("type", ""),
                        body.get("deployment_id", ""),
                        body.get("served_name", ""),
                        text,
                    )
                )
                if body.get("type") == "telemetry_gap":
                    gaps.append(
                        (
                            node_id,
                            float(body.get("from_ts") or ts),
                            float(body.get("to_ts") or ts),
                            str(body.get("reason") or "unknown"),
                        )
                    )
            elif kind == KIND_LOG:
                text = raw if isinstance(raw, str) else json.dumps(body, default=str)
                logs.append(
                    (
                        node_id,
                        seq,
                        ts,
                        body.get("level", ""),
                        body.get("logger", ""),
                        body.get("message", ""),
                        text,
                    )
                )

        conn = self._conn
        if samples:
            conn.executemany(
                "INSERT OR REPLACE INTO samples(node_id, ts, "
                + ", ".join(_SAMPLE_COLS)
                + ") VALUES(?, ?, "
                + ", ".join("?" * len(_SAMPLE_COLS))
                + ")",
                samples,
            )
        if requests:
            conn.executemany(
                "INSERT OR REPLACE INTO requests(" + ", ".join(_REQUEST_COLS) + ") "
                "VALUES(" + ", ".join("?" * len(_REQUEST_COLS)) + ")",
                requests,
            )
        if events:
            conn.executemany(
                "INSERT OR REPLACE INTO events(node_id, seq, ts, source, type, "
                "deployment_id, served_name, body) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                events,
            )
        if logs:
            conn.executemany(
                "INSERT OR REPLACE INTO logs(node_id, seq, ts, level, logger, "
                "message, body) VALUES(?, ?, ?, ?, ?, ?, ?)",
                logs,
            )
        if gaps:
            conn.executemany(
                "INSERT OR IGNORE INTO gaps(node_id, from_ts, to_ts, reason) "
                "VALUES(?, ?, ?, ?)",
                gaps,
            )

    def _note_dirty(self, ts: float) -> None:
        if ts <= 0:
            return
        self._conn.execute(
            "INSERT INTO meta(k, v) VALUES('dirty_from', ?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v "
            "WHERE CAST(meta.v AS REAL) > ?",
            (repr(ts), ts),
        )

    def take_dirty_from(self) -> float | None:
        """Oldest timestamp ingested since the last compaction, and clear it."""
        with self._lock:
            row = self._conn.execute(
                "SELECT v FROM meta WHERE k='dirty_from'"
            ).fetchone()
            if not row:
                return None
            self._conn.execute("DELETE FROM meta WHERE k='dirty_from'")
        try:
            return float(row["v"])
        except (TypeError, ValueError):
            return None

    def note_gap(self, node_id: str, from_ts: float, to_ts: float, reason: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO gaps(node_id, from_ts, to_ts, reason) "
            "VALUES(?, ?, ?, ?)",
            (node_id, from_ts, to_ts, reason),
        )

    def gaps(self, from_ts: float, to_ts: float, node_id: str = "") -> list[dict]:
        sql = (
            "SELECT node_id, from_ts, to_ts, reason FROM gaps "
            "WHERE to_ts >= ? AND from_ts <= ?"
        )
        args: list[Any] = [from_ts, to_ts]
        if node_id:
            sql += " AND node_id = ?"
            args.append(node_id)
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql + " ORDER BY from_ts", args)]

    # -- status ----------------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._status()

    def _status(self) -> dict[str, Any]:
        nodes = []
        now = self._clock()
        for row in self._conn.execute(
            "SELECT node_id, last_seq, last_ship_ts, dropped, head, last_error "
            "FROM cursors ORDER BY node_id"
        ):
            entry = dict(row)
            entry["behind"] = max(0, int(row["head"] or 0) - int(row["last_seq"] or 0))
            entry["age_s"] = (
                None if row["last_ship_ts"] is None else round(now - row["last_ship_ts"], 1)
            )
            nodes.append(entry)
        counts = {}
        for table in ("samples", "requests", "events", "logs"):
            counts[table] = self._conn.execute(
                f"SELECT COUNT(*) AS n FROM {table}"
            ).fetchone()["n"]
        page_count = self._conn.execute("PRAGMA page_count").fetchone()[0]
        page_size = self._conn.execute("PRAGMA page_size").fetchone()[0]
        oldest = self._conn.execute("SELECT MIN(ts) AS t FROM samples").fetchone()["t"]
        return {
            "path": str(self.path),
            "bytes": int(page_count) * int(page_size),
            "rows": counts,
            "nodes": nodes,
            "oldest_sample_ts": oldest,
            "gaps": self._conn.execute("SELECT COUNT(*) AS n FROM gaps").fetchone()["n"],
            "schema_version": SCHEMA_VERSION,
        }


def _coerce(value: Any) -> Any:
    """SQLite has no bool; store one as the integer it already is."""
    if isinstance(value, bool):
        return int(value)
    return value


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def open_archive(root: Path | str | None = None) -> Archive:
    return Archive(config.archive_path(root))
