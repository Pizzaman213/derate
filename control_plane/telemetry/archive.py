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

from control_plane.fsutil import private_sqlite

from . import config
from .records import ACCESS_RECORD_FIELDS, KIND_ACCESS, KIND_EVENT, KIND_LOG, KIND_REQUEST, KIND_SAMPLE

log = logging.getLogger(__name__)

#: Documentation, NOT the migration gate. ``__init__`` stamps this into ``meta``
#: unconditionally on every open, before anything else runs -- so the instant
#: this becomes 2, every archive in the field claims to be at 2 whether or not
#: it has the columns. A ladder keyed on it would skip the migration on exactly
#: the files that need it. ``_ensure_schema`` asks ``PRAGMA table_info`` what
#: the database actually holds instead. ``PRAGMA user_version`` is no better: it
#: is 0 on every archive written so far and has never been read.
SCHEMA_VERSION = 2

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
  clock_throttle_bits   INTEGER,
  sm_clock_mhz          INTEGER,
  sm_clock_max_mhz      INTEGER,
  swap_in_bps           REAL,
  swap_out_bps          REAL,
  major_faults_per_s    REAL,
  memory_pressure_pct   REAL,
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
  key_id                 TEXT,
  key_name               TEXT,
  org_id                 TEXT,
  client_ip              TEXT,
  user_agent             TEXT,
  model_requested        TEXT,
  model_served           TEXT,
  attempt_chain          TEXT,
  fallback_used          INTEGER,
  fallback_reason        TEXT,
  finish_reason          TEXT,
  cancelled              INTEGER,
  limit_source           TEXT,
  generation_ms          REAL,
  tokens_per_s           REAL,
  tpot_mean_ms           REAL,
  tpot_p95_ms            REAL,
  tail_ms                REAL,
  reasoning_tokens       INTEGER,
  cache_read_tokens      INTEGER,
  cache_write_tokens     INTEGER,
  max_tokens_requested   INTEGER,
  message_count          INTEGER,
  context_bucket         TEXT,
  prefix_cache_hit_rate  REAL,
  cache_error            TEXT,
  spec_num_drafts        INTEGER,
  spec_draft_tokens      INTEGER,
  spec_accepted_tokens   INTEGER,
  spec_acceptance_rate   REAL,
  spec_mean_accepted_len REAL,
  spec_accepted_per_position TEXT,
  spec_decode_error      TEXT,
  cost_basis             TEXT,
  client_port            INTEGER,
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

-- One row per HTTP request the gateway answered (and per gated socket on
-- close): the read side of the record. request_id is unique per request.
CREATE TABLE IF NOT EXISTS access(
  request_id  TEXT PRIMARY KEY,
  ts          REAL NOT NULL,
  node_id     TEXT,
  client_ip   TEXT,
  client_port INTEGER,
  who         TEXT,
  method      TEXT,
  path        TEXT,
  query       TEXT,
  status      INTEGER,
  duration_ms REAL,
  user_agent  TEXT,
  peer_ip     TEXT
);
CREATE INDEX IF NOT EXISTS access_ip_ts ON access(client_ip, ts);
CREATE INDEX IF NOT EXISTS access_ts ON access(ts);

CREATE TABLE IF NOT EXISTS cursors(
  node_id      TEXT PRIMARY KEY,
  last_seq     INTEGER NOT NULL DEFAULT 0,
  last_ship_ts REAL,
  dropped      INTEGER NOT NULL DEFAULT 0,
  head         INTEGER NOT NULL DEFAULT 0,
  last_error   TEXT,
  rejected     INTEGER NOT NULL DEFAULT 0
);

-- Raw logs/events bytes each node shipped per UTC day (by the coordinator's
-- clock, never the page's ts): the per-node budget under the size cap (N-5).
CREATE TABLE IF NOT EXISTS quota(
  node_id TEXT NOT NULL,
  day     INTEGER NOT NULL,
  bytes   INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(node_id, day)
) WITHOUT ROWID;

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
  throttle_bits_any INTEGER, throttled_s INTEGER, throttle_n INTEGER,
  sm_clock_avg REAL, sm_clock_min INTEGER, sm_clock_max_mhz INTEGER,
  swap_in_bps_avg REAL, swap_in_bps_max REAL,
  swap_out_bps_avg REAL, swap_out_bps_max REAL,
  major_faults_max REAL,
  memory_pressure_pct_avg REAL, memory_pressure_pct_max REAL,
  PRIMARY KEY(step, node_id, bucket)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS rollup_requests(
  step          INTEGER NOT NULL,
  bucket        INTEGER NOT NULL,
  served_name   TEXT NOT NULL,
  target_id     TEXT NOT NULL,
  node_id       TEXT NOT NULL DEFAULT '',
  provider_id   TEXT NOT NULL DEFAULT '',
  key_id        TEXT NOT NULL DEFAULT '',
  org_id        TEXT NOT NULL DEFAULT '',
  context_bucket TEXT NOT NULL DEFAULT '',
  status        INTEGER NOT NULL DEFAULT 0,
  n             INTEGER NOT NULL,
  ok            INTEGER NOT NULL,
  failed        INTEGER NOT NULL,
  cancelled     INTEGER NOT NULL DEFAULT 0,
  fallback      INTEGER NOT NULL DEFAULT 0,
  tokens        INTEGER NOT NULL,
  prompt_tokens INTEGER NOT NULL,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  reasoning_tokens INTEGER, reasoning_n INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens INTEGER, cache_n INTEGER NOT NULL DEFAULT 0,
  cache_prompt_tokens INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens INTEGER, cache_write_n INTEGER NOT NULL DEFAULT 0,
  cost_usd      REAL,
  metered_usd   REAL,
  estimated_usd REAL,
  unpriced_n    INTEGER NOT NULL DEFAULT 0,
  errors_json   TEXT,
  spec_n        INTEGER NOT NULL DEFAULT 0,
  spec_drafts   INTEGER,
  spec_draft_tokens INTEGER,
  spec_accepted_tokens INTEGER,
  spec_per_position_json TEXT,
  spec_error_json TEXT,
  cache_error_json TEXT,
  ttft_hist     BLOB,
  duration_hist BLOB,
  queue_hist    BLOB,
  tpot_hist     BLOB,
  tps_hist      BLOB,
  PRIMARY KEY(step, bucket, served_name, target_id, node_id, provider_id,
              key_id, org_id, context_bucket, status)
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
    "clock_throttle_bits",
    "sm_clock_mhz",
    "sm_clock_max_mhz",
    "swap_in_bps",
    "swap_out_bps",
    "major_faults_per_s",
    "memory_pressure_pct",
)

#: Columns added after the first release, and the migration that adds them to a
#: file created before they existed. Additive only: a row written before a
#: column existed must read NULL, because "nobody measured this in March" is not
#: "March had zero throttling" -- the null-vs-zero rule this package enforces
#: everywhere else, applied to time instead of to hardware. Never NOT NULL and
#: never a DEFAULT; SQLite would refuse the first on a WITHOUT ROWID table
#: anyway, and the second would fabricate the measurement.
_ADDITIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "samples": (
        ("clock_throttle_bits", "INTEGER"),
        ("sm_clock_mhz", "INTEGER"),
        ("sm_clock_max_mhz", "INTEGER"),
        ("swap_in_bps", "REAL"),
        ("swap_out_bps", "REAL"),
        ("major_faults_per_s", "REAL"),
        ("memory_pressure_pct", "REAL"),
    ),
    "rollup_samples": (
        ("throttle_bits_any", "INTEGER"),
        ("throttled_s", "INTEGER"),
        ("throttle_n", "INTEGER"),
        ("sm_clock_avg", "REAL"),
        ("sm_clock_min", "INTEGER"),
        ("sm_clock_max_mhz", "INTEGER"),
        ("swap_in_bps_avg", "REAL"),
        ("swap_in_bps_max", "REAL"),
        ("swap_out_bps_avg", "REAL"),
        ("swap_out_bps_max", "REAL"),
        ("major_faults_max", "REAL"),
        ("memory_pressure_pct_avg", "REAL"),
        ("memory_pressure_pct_max", "REAL"),
    ),
}

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
    "key_id",
    "key_name",
    "org_id",
    "client_ip",
    "user_agent",
    "model_requested",
    "model_served",
    "attempt_chain",
    "fallback_used",
    "fallback_reason",
    "finish_reason",
    "cancelled",
    "limit_source",
    "generation_ms",
    "tokens_per_s",
    "tpot_mean_ms",
    "tpot_p95_ms",
    "tail_ms",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "max_tokens_requested",
    "message_count",
    "context_bucket",
    "prefix_cache_hit_rate",
    "cache_error",
    "spec_num_drafts",
    "spec_draft_tokens",
    "spec_accepted_tokens",
    "spec_acceptance_rate",
    "spec_mean_accepted_len",
    "spec_accepted_per_position",
    "spec_decode_error",
    "cost_basis",
    "client_port",
)

#: Every request column but the key, for the upsert's UPDATE half.
_REQUEST_UPDATE_COLS = tuple(c for c in _REQUEST_COLS if c not in ("request_id", "attempt_no"))

_ACCESS_COLS = ACCESS_RECORD_FIELDS

#: Columns added by schema v2 (inbound auth), with their SQLite types. A v1
#: archive gains them in place by ALTER TABLE; see Archive._migrate.
_V2_REQUEST_COLS = (
    ("key_id", "TEXT"),
    ("key_name", "TEXT"),
    ("org_id", "TEXT"),
    ("client_ip", "TEXT"),
    ("user_agent", "TEXT"),
    ("model_requested", "TEXT"),
    ("model_served", "TEXT"),
    ("attempt_chain", "TEXT"),
    ("fallback_used", "INTEGER"),
    ("fallback_reason", "TEXT"),
    ("finish_reason", "TEXT"),
    ("cancelled", "INTEGER"),
    ("limit_source", "TEXT"),
    ("generation_ms", "REAL"),
    ("tokens_per_s", "REAL"),
    ("tpot_mean_ms", "REAL"),
    ("tpot_p95_ms", "REAL"),
    ("tail_ms", "REAL"),
    ("reasoning_tokens", "INTEGER"),
    ("cache_read_tokens", "INTEGER"),
    ("cache_write_tokens", "INTEGER"),
    ("max_tokens_requested", "INTEGER"),
    ("message_count", "INTEGER"),
    ("context_bucket", "TEXT"),
    ("prefix_cache_hit_rate", "REAL"),
    ("cache_error", "TEXT"),
    ("spec_num_drafts", "INTEGER"),
    ("spec_draft_tokens", "INTEGER"),
    ("spec_accepted_tokens", "INTEGER"),
    ("spec_acceptance_rate", "REAL"),
    ("spec_mean_accepted_len", "REAL"),
    ("spec_accepted_per_position", "TEXT"),
    ("spec_decode_error", "TEXT"),
    ("cost_basis", "TEXT"),
    # Added with the access record (security audit, F8).
    ("client_port", "INTEGER"),
)

#: Stored as JSON text. Everything else in a request row is a scalar.
_JSON_COLS = frozenset({"attempt_chain", "spec_accepted_per_position"})


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    private_sqlite(path)
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


def _sample_insert(cols: tuple[str, ...]) -> str:
    return (
        "INSERT OR REPLACE INTO samples(node_id, ts, "
        + ", ".join(cols)
        + ") VALUES(?, ?, "
        + ", ".join("?" * len(cols))
        + ")"
    )


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
        self._migrate()
        self._conn.execute(
            "INSERT OR REPLACE INTO meta(k, v) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        # Optimistic: a database this constructor just created from _SCHEMA has
        # every column, and _ensure_schema narrows these if it turns out not to.
        # Seeded here rather than left unset so an Archive is usable before the
        # first ingest, which is what the tests construct.
        self._migrated = False
        self._sample_cols: tuple[str, ...] = _SAMPLE_COLS
        self._sample_sql = _sample_insert(_SAMPLE_COLS)

    def _ensure_schema(self) -> None:
        """Add any column this build wants that the file does not have.

        Gated on ``PRAGMA table_info`` -- what the database actually holds --
        rather than on a recorded version number. Three properties follow, and
        each is the reason a version ladder was rejected:

        *Idempotent by construction.* It asks rather than remembers, so a file
        created by ``_SCHEMA`` (which already carries every column) finds
        nothing to do and a migrated one is never re-migrated.

        *Self-healing.* Each ``ALTER TABLE`` is its own implicit transaction, so
        a process killed between two of them leaves a half-migrated file. The
        next pass converges on it. A ladder would have stamped "done" and never
        looked again.

        *Cheap.* ``ADD COLUMN`` rewrites the schema row, not the table -- O(1)
        even on the multi-gigabyte archive this runs against in production.

        A failure here must not escalate. If the volume is read-only or full the
        ALTERs fail, and then ``_SAMPLE_COLS`` would name columns that do not
        exist and every ``executemany`` would raise ``no such column`` -- turning
        "seven fields missing" into "no telemetry ingested at all" for as long as
        the disk stays full. So the effective column list is intersected with
        what is really there, one warning is logged, and ``_migrated`` stays
        False so the next pass retries.
        """
        if self._migrated:
            return
        conn = self._conn
        try:
            for table, additions in _ADDITIONS.items():
                present = {
                    row[1] for row in conn.execute(f"PRAGMA table_info({table})")
                }
                for name, decl in additions:
                    if name not in present:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            self._migrated = True
        except Exception:
            log.warning("telemetry archive migration failed", exc_info=True)
        # Whether or not the ALTERs worked, write down what the file can take.
        try:
            present = {row[1] for row in conn.execute("PRAGMA table_info(samples)")}
        except Exception:
            return
        cols = tuple(c for c in _SAMPLE_COLS if c in present)
        if cols != self._sample_cols:
            missing = [c for c in _SAMPLE_COLS if c not in present]
            if missing:
                log.warning(
                    "telemetry archive is missing %d sample column(s); "
                    "ingesting the rest: %s",
                    len(missing),
                    ", ".join(missing),
                )
            self._sample_cols = cols
            self._sample_sql = _sample_insert(cols)

    def _migrate(self) -> None:
        """Bring a v1 file up to v2 in place. Idempotent; cheap when current.

        Raw request rows are kept and gain the v2 columns as NULL (ALTER TABLE
        ADD COLUMN rewrites nothing). ``rollup_requests`` cannot be widened in
        place -- its primary key grows -- so it is dropped, recreated, and
        ``reroll_pending`` asks the next compaction to re-roll the whole raw
        window once. Hourly rollups older than the raw window are lost.
        """
        conn = self._conn
        have = {r["name"] for r in conn.execute("PRAGMA table_info(requests)")}
        for name, sqltype in _V2_REQUEST_COLS:
            if name not in have:
                conn.execute(f"ALTER TABLE requests ADD COLUMN {name} {sqltype}")
        # The socket peer beside the resolved client (security audit GA-1).
        access_cols = {r["name"] for r in conn.execute("PRAGMA table_info(access)")}
        if "peer_ip" not in access_cols:
            conn.execute("ALTER TABLE access ADD COLUMN peer_ip TEXT")
        # Request/access rows a member shipped and may not write (N-6).
        cursor_cols = {r["name"] for r in conn.execute("PRAGMA table_info(cursors)")}
        if "rejected" not in cursor_cols:
            conn.execute(
                "ALTER TABLE cursors ADD COLUMN rejected INTEGER NOT NULL DEFAULT 0"
            )
        rollup = {r["name"] for r in conn.execute("PRAGMA table_info(rollup_requests)")}
        if "key_id" not in rollup:
            conn.execute("DROP TABLE IF EXISTS rollup_requests")
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT OR REPLACE INTO meta(k, v) VALUES('reroll_pending', '1')"
            )
            log.info("telemetry archive at %s migrated to schema v2; re-roll pending", self.path)
        conn.execute("CREATE INDEX IF NOT EXISTS requests_key_ts ON requests(key_id, ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS requests_org_ts ON requests(org_id, ts)")

    def take_reroll_pending(self) -> bool:
        """Whether a migration asked for a full re-roll. Does not clear it:
        retention clears it only after the re-roll committed."""
        with self._lock:
            row = self._conn.execute(
                "SELECT v FROM meta WHERE k='reroll_pending'"
            ).fetchone()
        return bool(row and row["v"] == "1")

    def clear_reroll_pending(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM meta WHERE k='reroll_pending'")

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

    def ingest(
        self, node_id: str, payload: dict[str, Any], *, trusted: bool = False
    ) -> int:
        """Fan one poll's rows into typed tables and advance the cursor.

        Rows and cursor move in one transaction, so a crash anywhere in here
        re-asks from the last committed position rather than losing or
        duplicating a batch.

        ``trusted`` is true only for this coordinator's own journal, the one
        writer of ``requests`` and ``access`` rows: the gateway answers every
        request, so a member shipping either is fabricating evidence (N-6).
        Those rows are dropped and counted in ``cursors.rejected``.
        """
        rows = payload.get("rows") or []
        next_seq = int(payload.get("next") or 0)
        head = int(payload.get("head") or 0)
        dropped = int(payload.get("dropped") or 0)
        rejected = 0

        conn = self._conn
        with self._lock:
            # Before BEGIN, deliberately: an ALTER inside the batch transaction
            # could roll the batch back, and this has to have run before the
            # first INSERT that names a new column -- which is why it lives here
            # and not only on the compaction timer sixty seconds later.
            self._ensure_schema()
            conn.execute("BEGIN IMMEDIATE")
            try:
                if rows:
                    rejected = self._ingest_rows(node_id, rows, trusted)
                    # A worker unreachable for an hour delivers an hour of
                    # backlog at once, and those rows land in buckets that
                    # compaction already rolled. Remember how far back this
                    # batch reached, so the next pass re-rolls from there
                    # rather than trusting a clock-derived watermark and
                    # silently under-counting them.
                    self._note_dirty(min(float(r.get("ts") or 0.0) for r in rows))
                if trusted:
                    # Which node's rows are the gateway's own: forensics names
                    # the node of any evidence row that is not (N-6).
                    conn.execute(
                        "INSERT OR REPLACE INTO meta(k, v) VALUES('local_node', ?)",
                        (node_id,),
                    )
                conn.execute(
                    "INSERT INTO cursors(node_id, last_seq, last_ship_ts, dropped, head, rejected) "
                    "VALUES(?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(node_id) DO UPDATE SET "
                    "  last_seq=MAX(cursors.last_seq, excluded.last_seq), "
                    "  last_ship_ts=excluded.last_ship_ts, "
                    "  dropped=excluded.dropped, "
                    "  head=excluded.head, "
                    "  rejected=cursors.rejected + excluded.rejected, "
                    "  last_error=NULL",
                    (node_id, next_seq, self._clock(), dropped, head, rejected),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        if rejected:
            log.warning(
                "%s shipped %d request/access rows it may not write; dropped",
                node_id,
                rejected,
            )
        return len(rows)

    def note_ship_failure(self, node_id: str, detail: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO cursors(node_id, last_seq, last_error) VALUES(?, 0, ?) "
                "ON CONFLICT(node_id) DO UPDATE SET last_error=excluded.last_error",
                (node_id, detail[:500]),
            )

    def _ingest_rows(
        self, node_id: str, rows: list[dict[str, Any]], trusted: bool = False
    ) -> int:
        """Fan a page into typed tables, one executemany per kind.

        Grouping first and inserting in bulk rather than a statement per row is
        what keeps a page's ingest short. The collector runs this in a worker
        thread while the gateway is serving, so how long one uninterrupted
        stretch of it lasts shows up directly in the gateway's p99.

        Returns how many request/access rows an untrusted journal shipped and
        were dropped. Raw ``logs``/``events`` bytes past the node's daily
        budget (``config.NODE_RAW_BYTES_PER_DAY``, by the coordinator's clock)
        are dropped too, with one ``node_quota`` gap per node-day: one rogue
        member must not be able to drive the archive into its size cap and
        evict everyone else's record (N-5).
        """
        samples: list[tuple] = []
        requests: list[tuple] = []
        events: list[tuple] = []
        logs: list[tuple] = []
        access: list[tuple] = []
        gaps: list[tuple] = []
        rejected = 0

        conn = self._conn
        day = int(self._clock() // config.DAY_S)
        row = conn.execute(
            "SELECT bytes FROM quota WHERE node_id=? AND day=?", (node_id, day)
        ).fetchone()
        used = start_used = int(row["bytes"]) if row else 0
        budget = config.NODE_RAW_BYTES_PER_DAY
        over = 0

        for row in rows:
            kind = row.get("kind")
            seq = int(row.get("seq") or 0)
            ts = float(row.get("ts") or 0.0)
            raw = row.get("body")
            if kind in (KIND_REQUEST, KIND_ACCESS) and not trusted:
                rejected += 1
                continue
            try:
                body = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
            except (ValueError, TypeError):
                log.debug("undecodable journal row from %s seq %s", node_id, seq)
                continue
            if not isinstance(body, dict):
                continue
            if kind in (KIND_EVENT, KIND_LOG):
                size = len(raw) if isinstance(raw, str) else len(json.dumps(body, default=str))
                if used + size > budget:
                    over += 1
                    continue
                used += size
            if kind == KIND_SAMPLE:
                samples.append(
                    (node_id, ts, *(body.get(c) for c in self._sample_cols))
                )
            elif kind == KIND_REQUEST:
                if not body.get("request_id"):
                    continue
                # The poller's identity, never the page's: a node may only
                # write rows under its own name (F1).
                body["node_id"] = node_id
                body["ts"] = ts
                if body.get("attempt_no") is None:
                    body["attempt_no"] = 0
                requests.append(
                    tuple(
                        _json_col(body.get(c)) if c in _JSON_COLS else _coerce(body.get(c))
                        for c in _REQUEST_COLS
                    )
                )
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
            elif kind == KIND_ACCESS:
                if not body.get("request_id"):
                    continue
                body["node_id"] = node_id
                body["ts"] = ts
                access.append(tuple(_coerce(body.get(c)) for c in _ACCESS_COLS))

        if samples:
            conn.executemany(self._sample_sql, samples)
        if requests:
            # An upsert that only the row's own node may update: a page from
            # another node carrying the same (request_id, attempt_no) changes
            # nothing, where INSERT OR REPLACE let it rewrite key and address.
            conn.executemany(
                "INSERT INTO requests(" + ", ".join(_REQUEST_COLS) + ") "
                "VALUES(" + ", ".join("?" * len(_REQUEST_COLS)) + ") "
                "ON CONFLICT(request_id, attempt_no) DO UPDATE SET "
                + ", ".join(f"{c}=excluded.{c}" for c in _REQUEST_UPDATE_COLS)
                + " WHERE requests.node_id = excluded.node_id",
                requests,
            )
        if access:
            conn.executemany(
                "INSERT INTO access(" + ", ".join(_ACCESS_COLS) + ") "
                "VALUES(" + ", ".join("?" * len(_ACCESS_COLS)) + ") "
                "ON CONFLICT(request_id) DO NOTHING",
                access,
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
        if used != start_used:
            conn.execute(
                "INSERT INTO quota(node_id, day, bytes) VALUES(?, ?, ?) "
                "ON CONFLICT(node_id, day) DO UPDATE SET bytes=excluded.bytes",
                (node_id, day, used),
            )
        if over:
            first = conn.execute(
                "INSERT OR IGNORE INTO gaps(node_id, from_ts, to_ts, reason) "
                "VALUES(?, ?, ?, 'node_quota')",
                (node_id, day * config.DAY_S, (day + 1) * config.DAY_S),
            ).rowcount
            if first:
                log.warning(
                    "%s shipped more than %d bytes of logs/events today; "
                    "dropping the rest of its raw rows until tomorrow (UTC)",
                    node_id,
                    budget,
                )
        return rejected

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
            "SELECT node_id, last_seq, last_ship_ts, dropped, head, last_error, rejected "
            "FROM cursors ORDER BY node_id"
        ):
            entry = dict(row)
            entry["behind"] = max(0, int(row["head"] or 0) - int(row["last_seq"] or 0))
            entry["age_s"] = (
                None if row["last_ship_ts"] is None else round(now - row["last_ship_ts"], 1)
            )
            nodes.append(entry)
        counts = {}
        for table in ("samples", "requests", "events", "logs", "access"):
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


def _json_col(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):  # already serialised by a producer
        return value
    return json.dumps(value, separators=(",", ":"), default=str)


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def open_archive(root: Path | str | None = None) -> Archive:
    return Archive(config.archive_path(root))
