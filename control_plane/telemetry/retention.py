"""Rollups and retention. One pass, on a timer.

On a timer deliberately. deploy/store.py's purge_expired() is called only from
reconcile(), which runs once per restart, so a process that stays up never
collects. This runs every COMPACT_INTERVAL_S whatever else is happening.

Rollups are what make a year affordable. Raw samples cost about 8 MB per node
per day; the 1-minute rows that summarise them cost about 10 KB, and the
hourly rows about 170 bytes. So the raw window can stay short enough to be
cheap while the answer to "what did this cluster do in March" stays exact
enough to be worth having.

Latency percentiles roll through Hist, whose buckets merge exactly, so an
hourly p99 is the real p99 of the hour rather than a mean of sixty
percentiles. That distinction is the whole reason for the blob columns.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from . import config
from .archive import Archive
from .hist import Hist

log = logging.getLogger(__name__)

STEP_1M = 60
STEP_1H = 3600
STEP_1D = 86400

#: A bucket is only rolled once it is closed and has had a moment to settle,
#: so an in-flight second does not produce a rollup row that is wrong the
#: instant after it is written.
ROLL_LAG_S = 120.0

#: How far back a pass re-rolls even with no late arrivals. Cheap insurance
#: against a batch that straddled a compaction.
REROLL_WINDOW_S = 900.0

#: Backfill is processed a day at a time so a month-long catch-up never holds
#: a month of rows in memory.
CHUNK_S = 86400.0

#: Days one size-enforcement pass will evict before leaving the rest to the
#: next compaction. A backstop, not a policy: the raw horizons are 7 and 30
#: days, so a real eviction never comes close. It exists because the journal's
#: version of the same loop, trusting only its file measure, ran forever.
MAX_EVICT_DAYS = 400

from control_plane.registry.telemetry import CLOCK_THROTTLE_NAMES, CLOCK_THROTTLE_MASK  # noqa: E402  -- constants live below the module docstring's guard


def _bit_union(column: str) -> str:
    """SQL that unions a bitmask across a GROUP BY.

    SQLite has no BIT_OR aggregate, and MAX() is not a stand-in: one sample at
    0x4 (power cap) and one at 0x20 (thermal) would give 0x20 and the power cap
    would simply vanish. The bits are disjoint, so a sum of per-bit maxima
    reconstructs the OR exactly. Generated from CLOCK_THROTTLE_NAMES rather than
    written out, so the mask has one home and this cannot drift from it.
    """
    return " + ".join(f"MAX({column} & {bit})" for bit, _ in CLOCK_THROTTLE_NAMES)


def _weighted_avg(column: str) -> str:
    """A 1m->1h weighted average that a NULL minute cannot dilute.

    The plain form ``SUM(x_avg * n) / SUM(n)`` skips NULL products in the
    numerator while still counting those minutes in the denominator, so an hour
    half of whose minutes had no reading reports half the true average. Every
    column predating this was non-NULL by construction, so it never bit; the
    genuinely optional ones make it bite immediately, and it would do so exactly
    during a fleet upgrade -- when the chart is least trustworthy and most
    likely to be believed.

    Residual and deliberate: a minute is still weighted by its full ``n`` even
    if only three of its sixty samples carried a reading. Fixing that exactly
    needs a per-field count column each. Partial minutes only happen mid-upgrade
    or while a probe is flapping, and every pre-existing column has the same
    property implicitly.
    """
    return (
        f"SUM({column} * n) / "
        f"NULLIF(SUM(CASE WHEN {column} IS NULL THEN 0 ELSE n END), 0)"
    )


_SAMPLE_ROLL_1M = f"""
INSERT OR REPLACE INTO rollup_samples(
  step, node_id, bucket, n,
  power_w_avg, power_w_max, temp_c_avg, temp_c_max,
  util_pct_avg, util_pct_max, memory_used_avg, memory_used_max,
  gpu_memory_used_avg, gpu_memory_used_max,
  host_memory_available_min, swap_used_max,
  throttle_bits_any, throttled_s, throttle_n,
  sm_clock_avg, sm_clock_min, sm_clock_max_mhz,
  swap_in_bps_avg, swap_in_bps_max, swap_out_bps_avg, swap_out_bps_max,
  major_faults_max, memory_pressure_pct_avg, memory_pressure_pct_max)
SELECT
  ?, node_id, CAST(ts / ? AS INTEGER) * ?, COUNT(*),
  AVG(power_w), MAX(power_w), AVG(temp_c), MAX(temp_c),
  AVG(util_pct), MAX(util_pct), AVG(memory_used), MAX(memory_used),
  AVG(gpu_memory_used), MAX(gpu_memory_used),
  MIN(host_memory_available), MAX(swap_used),
  {_bit_union("clock_throttle_bits")},
  -- At 1 Hz this is literally seconds throttled. COUNT(clock_throttle_bits)
  -- rather than COUNT(*) is the denominator that keeps it honest: NULL & mask
  -- is NULL and CASE WHEN NULL falls to ELSE 0, so without it every CPU-only
  -- and every old-agent row would count as "checked, not throttled".
  SUM(CASE WHEN clock_throttle_bits & {CLOCK_THROTTLE_MASK} THEN 1 ELSE 0 END),
  COUNT(clock_throttle_bits),
  AVG(sm_clock_mhz), MIN(sm_clock_mhz), MAX(sm_clock_max_mhz),
  AVG(swap_in_bps), MAX(swap_in_bps), AVG(swap_out_bps), MAX(swap_out_bps),
  MAX(major_faults_per_s), AVG(memory_pressure_pct), MAX(memory_pressure_pct)
FROM samples
WHERE ts >= ? AND ts < ?
GROUP BY node_id, CAST(ts / ? AS INTEGER)
"""

# Averages roll forward weighted by the sample count behind them, so an hour
# containing one busy minute and fifty-nine idle ones reports the truth.
_SAMPLE_ROLL_1H = f"""
INSERT OR REPLACE INTO rollup_samples(
  step, node_id, bucket, n,
  power_w_avg, power_w_max, temp_c_avg, temp_c_max,
  util_pct_avg, util_pct_max, memory_used_avg, memory_used_max,
  gpu_memory_used_avg, gpu_memory_used_max,
  host_memory_available_min, swap_used_max,
  throttle_bits_any, throttled_s, throttle_n,
  sm_clock_avg, sm_clock_min, sm_clock_max_mhz,
  swap_in_bps_avg, swap_in_bps_max, swap_out_bps_avg, swap_out_bps_max,
  major_faults_max, memory_pressure_pct_avg, memory_pressure_pct_max)
SELECT
  ?, node_id, CAST(bucket / ? AS INTEGER) * ?, SUM(n),
  SUM(power_w_avg * n) / SUM(n), MAX(power_w_max),
  SUM(temp_c_avg * n) / SUM(n), MAX(temp_c_max),
  SUM(util_pct_avg * n) / SUM(n), MAX(util_pct_max),
  SUM(memory_used_avg * n) / SUM(n), MAX(memory_used_max),
  SUM(gpu_memory_used_avg * n) / SUM(n), MAX(gpu_memory_used_max),
  MIN(host_memory_available_min), MAX(swap_used_max),
  {_bit_union("throttle_bits_any")},
  SUM(throttled_s), SUM(throttle_n),
  {_weighted_avg("sm_clock_avg")}, MIN(sm_clock_min), MAX(sm_clock_max_mhz),
  {_weighted_avg("swap_in_bps_avg")}, MAX(swap_in_bps_max),
  {_weighted_avg("swap_out_bps_avg")}, MAX(swap_out_bps_max),
  MAX(major_faults_max),
  {_weighted_avg("memory_pressure_pct_avg")}, MAX(memory_pressure_pct_max)
FROM rollup_samples
WHERE step = ? AND bucket >= ? AND bucket < ?
GROUP BY node_id, CAST(bucket / ? AS INTEGER)
"""


def compact(archive: Archive, now: float | None = None) -> dict[str, Any]:
    """Roll closed buckets, then enforce every retention horizon."""
    now = time.time() if now is None else now
    report: dict[str, Any] = {}

    dirty = archive.take_dirty_from()
    roll_to = now - ROLL_LAG_S
    roll_from = min(dirty, now - REROLL_WINDOW_S) if dirty else now - REROLL_WINDOW_S
    # A schema migration dropped the request rollups; rebuild them from the
    # whole raw window once. Cleared only after the pass below committed.
    reroll = archive.take_reroll_pending()
    if reroll:
        with archive.lock:
            oldest = archive.conn.execute("SELECT MIN(ts) AS t FROM requests").fetchone()["t"]
        if oldest is not None:
            roll_from = min(roll_from, float(oldest))
    roll_from = _floor(roll_from, STEP_1H)

    # One lock for the whole pass. Compaction rewrites the same tables the
    # collector is inserting into, and interleaving them would deadlock two
    # BEGIN IMMEDIATE transactions against each other rather than merely
    # slowing each down.
    with archive.lock:
        conn = archive.conn
        _ensure_incremental_vacuum(conn, archive.path)
        # An archive nobody has shipped to yet -- a coordinator with no members
        # -- never reaches Archive.ingest, so this is the other half of the
        # migration's coverage, and the retry for one that failed on a full disk.
        archive._ensure_schema()
        if roll_to > roll_from:
            report["rolled_1m"] = _roll(conn, archive, roll_from, roll_to, STEP_1M)
            report["rolled_1h"] = _roll(conn, archive, roll_from, roll_to, STEP_1H)
            report["rolled_1d"] = _roll(conn, archive, roll_from, roll_to, STEP_1D)
        if reroll:
            archive.clear_reroll_pending()
            report["rerolled"] = True

        report["deleted"] = _expire(conn, now)
        report["size_trimmed"] = _enforce_size(archive, now)
        conn.execute("PRAGMA incremental_vacuum").fetchall()
    return report


def _ensure_incremental_vacuum(conn, path) -> None:
    """Convert an archive written before archive.py::connect had its pragmas
    in the right order. Once, on the first compaction rather than at open.

    Here and not in ``Archive.__init__`` because of what it costs: measured,
    a 238 MB archive VACUUMs in 0.72s, which extrapolates to roughly 48
    seconds at ``ARCHIVE_MAX_BYTES``. The archive is opened inside
    ``start_node``, before the gateway answers anything, and a coordinator
    that takes a silent minute to boot is a worse bug than the one being
    fixed. Compaction already runs on a worker thread under this lock.

    Never raises: this is the tidy-up, not the work.
    """
    try:
        if conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2:
            return
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        conn.execute("VACUUM")  # cannot run inside a transaction
        log.info("telemetry archive at %s converted to incremental auto-vacuum", path)
    except Exception as exc:
        log.warning("could not convert %s to incremental auto-vacuum: %s", path, exc)


# -- rollups -----------------------------------------------------------------


def _roll(conn, archive: Archive, from_ts: float, to_ts: float, step: int) -> int:
    """Roll one step's buckets over a window, a chunk at a time."""
    rolled = 0
    start = _floor(from_ts, step)
    while start < to_ts:
        stop = min(start + CHUNK_S, _floor(to_ts, step))
        if stop <= start:
            break
        conn.execute("BEGIN IMMEDIATE")
        try:
            if step == STEP_1M:
                conn.execute(_SAMPLE_ROLL_1M, (step, step, step, start, stop, step))
            elif step == STEP_1H:
                conn.execute(
                    _SAMPLE_ROLL_1H,
                    (step, step, step, STEP_1M, start, stop, step),
                )
            rolled += _roll_requests(conn, start, stop, step)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        start = stop
    return rolled


#: Rollup dimensions, in primary-key order after (step, bucket). Raw NULLs
#: become '' (or 0 for status) because a NULL in a primary key never collides.
_DIMS = (
    "served_name",
    "target_id",
    "node_id",
    "provider_id",
    "key_id",
    "org_id",
    "context_bucket",
    "status",
)

_HISTS = (
    ("ttft", "ttft_hist"),
    ("duration", "duration_hist"),
    ("queue", "queue_hist"),
    ("tpot", "tpot_hist"),
    ("tps", "tps_hist"),
)

#: Raw column feeding each histogram.
_RAW_HIST_COL = {
    "ttft": "ttft_ms",
    "duration": "duration_ms",
    "queue": "parked_ms",
    "tpot": "tpot_mean_ms",
    "tps": "tokens_per_s",
}

#: Optional sums, each paired with the count of rows that contributed to it so
#: an average divides by the rows that reported, never by all of them.
_OPT_SUMS = (
    ("reasoning_tokens", "reasoning_n"),
    ("cache_read_tokens", "cache_n"),
    ("cache_write_tokens", "cache_write_n"),
)

_SPEC_SUMS = ("spec_drafts", "spec_draft_tokens", "spec_accepted_tokens")

_ROLLUP_COLS = (
    "step",
    "bucket",
    *_DIMS,
    "n",
    "ok",
    "failed",
    "cancelled",
    "fallback",
    "tokens",
    "prompt_tokens",
    "completion_tokens",
    "reasoning_tokens",
    "reasoning_n",
    "cache_read_tokens",
    "cache_n",
    "cache_prompt_tokens",
    "cache_write_tokens",
    "cache_write_n",
    "cost_usd",
    "metered_usd",
    "estimated_usd",
    "unpriced_n",
    "errors_json",
    "spec_n",
    "spec_drafts",
    "spec_draft_tokens",
    "spec_accepted_tokens",
    "spec_per_position_json",
    "spec_error_json",
    "cache_error_json",
    *(col for _, col in _HISTS),
)

_INSERT_ROLLUP = (
    "INSERT OR REPLACE INTO rollup_requests("
    + ", ".join(_ROLLUP_COLS)
    + ") VALUES("
    + ", ".join("?" * len(_ROLLUP_COLS))
    + ")"
)


def _roll_requests(conn, from_ts: float, to_ts: float, step: int) -> int:
    """Request rollups, in Python because the histograms are not SQL.

    1m reads raw rows; 1h merges 1m rows and 1d merges 1h rows, so each tier
    survives the one below it expiring. Histograms are merged bucket-wise
    (exact), never averaged; counters and the JSON count maps are summed.
    """
    groups: dict[tuple, _Group] = {}

    if step == STEP_1M:
        # Final attempts only: a request retried onto another target counts
        # once, with the outcome the client received.
        rows = conn.execute(
            "SELECT r.* FROM requests r JOIN ("
            "SELECT request_id, MAX(attempt_no) AS a FROM requests "
            "WHERE ts >= ? AND ts < ? GROUP BY request_id) f "
            "ON r.request_id = f.request_id AND r.attempt_no = f.a "
            "WHERE r.ts >= ? AND r.ts < ?",
            (from_ts, to_ts, from_ts, to_ts),
        )
        for r in rows:
            key = (int(r["ts"] // step) * step, *_dims_of(r))
            g = groups.get(key)
            if g is None:
                g = groups[key] = _Group()
            g.add_raw(r)
    else:
        source = STEP_1M if step == STEP_1H else STEP_1H
        rows = conn.execute(
            "SELECT * FROM rollup_requests WHERE step = ? AND bucket >= ? AND bucket < ?",
            (source, from_ts, to_ts),
        )
        for r in rows:
            key = (int(r["bucket"] // step) * step, *(r[d] for d in _DIMS))
            g = groups.get(key)
            if g is None:
                g = groups[key] = _Group()
            g.add_rollup(r)

    if not groups:
        return 0
    conn.executemany(
        _INSERT_ROLLUP,
        [(step, *key, *g.measures()) for key, g in groups.items()],
    )
    return len(groups)


def _dims_of(r) -> tuple:
    status = r["status"]
    return (
        r["served_name"] or "",
        r["target_id"] or "",
        r["node_id"] or "",
        r["provider_id"] or "",
        r["key_id"] or "",
        r["org_id"] or "",
        r["context_bucket"] or "",
        int(status) if status is not None else 0,
    )


def _bump(counts: dict[str, int], key: str | None, n: int = 1) -> None:
    if key:
        counts[key] = counts.get(key, 0) + n


def _counts(text: str | None) -> dict[str, int]:
    if not text:
        return {}
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _add_positions(into: list[int] | None, more) -> list[int] | None:
    """Element-wise sum of two per-position lists of possibly different length."""
    if more is None:
        return into
    out = list(into or [])
    for i, v in enumerate(more):
        if i < len(out):
            out[i] += int(v)
        else:
            out.append(int(v))
    return out


def _opt_add(a, b):
    if b is None:
        return a
    return b if a is None else a + b


class _Group:
    """One rollup row being accumulated."""

    def __init__(self) -> None:
        self.n = self.ok = self.failed = self.cancelled = self.fallback = 0
        self.tokens = self.prompt_tokens = self.completion_tokens = 0
        self.opt: dict[str, Any] = {name: None for name, _ in _OPT_SUMS}
        self.opt_n: dict[str, int] = {cnt: 0 for _, cnt in _OPT_SUMS}
        self.cache_prompt_tokens = 0
        self.cost_usd = 0.0
        self.metered_usd: float | None = None
        self.estimated_usd: float | None = None
        self.unpriced_n = 0
        self.errors: dict[str, int] = {}
        self.spec_n = 0
        self.spec: dict[str, Any] = {name: None for name in _SPEC_SUMS}
        self.spec_positions: list[int] | None = None
        self.spec_errors: dict[str, int] = {}
        self.cache_errors: dict[str, int] = {}
        self.hists = {name: Hist() for name, _ in _HISTS}

    def add_raw(self, r) -> None:
        status = r["status"]
        self.n += 1
        if status is not None and 200 <= int(status) < 400:
            self.ok += 1
        else:
            self.failed += 1
        self.cancelled += 1 if r["cancelled"] else 0
        self.fallback += 1 if r["fallback_used"] else 0
        self.tokens += int(r["tokens"] or 0)
        self.prompt_tokens += int(r["prompt_tokens"] or 0)
        self.completion_tokens += int(r["completion_tokens"] or 0)
        for name, cnt in _OPT_SUMS:
            if r[name] is not None:
                self.opt[name] = (self.opt[name] or 0) + int(r[name])
                self.opt_n[cnt] += 1
        if r["cache_read_tokens"] is not None:
            self.cache_prompt_tokens += int(r["prompt_tokens"] or 0)
        cost = r["cost_usd"]
        basis = r["cost_basis"]
        self.cost_usd += float(cost or 0.0)
        if basis == "metered" and cost is not None:
            self.metered_usd = _opt_add(self.metered_usd, float(cost))
        elif basis == "estimated" and cost is not None:
            self.estimated_usd = _opt_add(self.estimated_usd, float(cost))
        elif basis == "unpriced":
            self.unpriced_n += 1
        _bump(self.errors, r["error_class"] or r["error_code"])
        if r["spec_num_drafts"] is not None:
            self.spec_n += 1
            self.spec["spec_drafts"] = _opt_add(self.spec["spec_drafts"], int(r["spec_num_drafts"]))
            self.spec["spec_draft_tokens"] = _opt_add(
                self.spec["spec_draft_tokens"], int(r["spec_draft_tokens"] or 0)
            )
            self.spec["spec_accepted_tokens"] = _opt_add(
                self.spec["spec_accepted_tokens"], int(r["spec_accepted_tokens"] or 0)
            )
            per_pos = r["spec_accepted_per_position"]
            if per_pos:
                try:
                    self.spec_positions = _add_positions(self.spec_positions, json.loads(per_pos))
                except ValueError:
                    pass
        _bump(self.spec_errors, r["spec_decode_error"])
        _bump(self.cache_errors, r["cache_error"])
        for name, col in _RAW_HIST_COL.items():
            if r[col] is not None:
                self.hists[name].add(float(r[col]))

    def add_rollup(self, r) -> None:
        for attr in ("n", "ok", "failed", "cancelled", "fallback", "tokens",
                     "prompt_tokens", "completion_tokens", "cache_prompt_tokens",
                     "unpriced_n", "spec_n"):
            setattr(self, attr, getattr(self, attr) + int(r[attr] or 0))
        for name, cnt in _OPT_SUMS:
            self.opt[name] = _opt_add(self.opt[name], r[name])
            self.opt_n[cnt] += int(r[cnt] or 0)
        self.cost_usd += float(r["cost_usd"] or 0.0)
        self.metered_usd = _opt_add(self.metered_usd, r["metered_usd"])
        self.estimated_usd = _opt_add(self.estimated_usd, r["estimated_usd"])
        for name in _SPEC_SUMS:
            self.spec[name] = _opt_add(self.spec[name], r[name])
        if r["spec_per_position_json"]:
            try:
                self.spec_positions = _add_positions(
                    self.spec_positions, json.loads(r["spec_per_position_json"])
                )
            except ValueError:
                pass
        for target, col in (
            (self.errors, "errors_json"),
            (self.spec_errors, "spec_error_json"),
            (self.cache_errors, "cache_error_json"),
        ):
            for k, v in _counts(r[col]).items():
                _bump(target, k, int(v))
        for name, col in _HISTS:
            self.hists[name].merge(Hist.from_blob(r[col]))

    def measures(self) -> tuple:
        def js(value) -> str | None:
            return json.dumps(value, sort_keys=True, separators=(",", ":")) if value else None

        return (
            self.n,
            self.ok,
            self.failed,
            self.cancelled,
            self.fallback,
            self.tokens,
            self.prompt_tokens,
            self.completion_tokens,
            self.opt["reasoning_tokens"],
            self.opt_n["reasoning_n"],
            self.opt["cache_read_tokens"],
            self.opt_n["cache_n"],
            self.cache_prompt_tokens,
            self.opt["cache_write_tokens"],
            self.opt_n["cache_write_n"],
            self.cost_usd,
            self.metered_usd,
            self.estimated_usd,
            self.unpriced_n,
            js(self.errors),
            self.spec_n,
            self.spec["spec_drafts"],
            self.spec["spec_draft_tokens"],
            self.spec["spec_accepted_tokens"],
            js(self.spec_positions),
            js(self.spec_errors),
            js(self.cache_errors),
            *(self.hists[name].to_blob() for name, _ in _HISTS),
        )


# -- retention ---------------------------------------------------------------


def _expire(conn, now: float) -> dict[str, int]:
    # DERATE_TELEMETRY_RETENTION_DAYS moves every raw *telemetry* horizon
    # together rather than only the samples one, so halving it halves the log
    # window too and the ratios between them are preserved. Unset, this is
    # exactly 1.0. The evidence tables -- `requests` and `access`, who asked
    # for what from where -- are not telemetry and are not scaled: they keep
    # DERATE_REQUESTS_RETENTION_DAYS (F10).
    scale = config.retention_days() * config.DAY_S / config.SAMPLES_RAW_RETENTION_S
    evidence = config.requests_retention_days() * config.DAY_S
    horizons = (
        ("samples", "ts", config.SAMPLES_RAW_RETENTION_S * scale),
        ("requests", "ts", evidence),
        ("access", "ts", evidence),
        ("events", "ts", config.EVENTS_RETENTION_S * scale),
        ("logs", "ts", config.LOGS_RETENTION_S * scale),
        # `gaps` had no horizon at all -- the one table that grew forever.
        # It is not a detail: _envelope() returns every gap overlapping a
        # window on *every* history query, so an accumulation of them is
        # payload on every poll of every screen. Aged out with `events`,
        # whose window it is describing holes in.
        ("gaps", "to_ts", config.EVENTS_RETENTION_S * scale),
    )
    deleted: dict[str, int] = {}
    for table, column, window in horizons:
        cutoff = now - window
        n = conn.execute(f"DELETE FROM {table} WHERE {column} < ?", (cutoff,)).rowcount
        if n:
            deleted[table] = n
    # Zero-width gaps, which are not a fact about anything. The journal used
    # to emit one per evicted batch and, once its table was empty, one per lap
    # around a loop re-discarding its own marker -- 772 of the 773 rows in the
    # live archive. The journal no longer writes them; this clears what it
    # already wrote, and the row renders as "0 min missing" until it goes.
    n = conn.execute("DELETE FROM gaps WHERE to_ts <= from_ts").rowcount
    if n:
        deleted["gaps_degenerate"] = n
    # The per-node daily budget only ever reads today's row.
    n = conn.execute(
        "DELETE FROM quota WHERE day < ?", (int(now // config.DAY_S) - 1,)
    ).rowcount
    if n:
        deleted["quota"] = n
    # Rollup horizons are deliberately not scaled. They cost almost nothing,
    # and someone shortening the raw window to save disk wants a shorter raw
    # window, not a shorter memory.
    for step, window in (
        (STEP_1M, config.ROLLUP_1M_RETENTION_S),
        (STEP_1H, config.ROLLUP_1H_RETENTION_S),
        (STEP_1D, config.ROLLUP_1D_RETENTION_S),
    ):
        cutoff = now - window
        for table in ("rollup_samples", "rollup_requests"):
            n = conn.execute(
                f"DELETE FROM {table} WHERE step = ? AND bucket < ?", (step, cutoff)
            ).rowcount
            if n:
                deleted[f"{table}_{step}"] = deleted.get(f"{table}_{step}", 0) + n
    return deleted


#: An agent's or a shell's own record of what it did and refused: evidence.
_AGENT_EVENTS = "source IN ('agent', 'shell')"

#: Evicted first under the size cap: telemetry, not evidence. Each entry is a
#: table and the rows of it the tier owns (an empty clause is every row).
_TELEMETRY_TABLES = (
    ("samples", ""),
    ("logs", ""),
    ("events", f"source IS NULL OR NOT ({_AGENT_EVENTS})"),
)
#: Next: requests nobody was authenticated for. An anonymous flood writes one
#: access row per request, and it must cost its own rows before it costs the
#: attributed record (EV-1).
_ANONYMOUS_TABLES = (("access", "who IS NULL OR who = '-'"),)
#: Evicted only once every row above is gone: the attribution record, and the
#: agents' own (N-5).
_EVIDENCE_TABLES = (("requests", ""), ("access", ""), ("events", _AGENT_EVENTS))

_TIERS = (_TELEMETRY_TABLES, _ANONYMOUS_TABLES, _EVIDENCE_TABLES)


def _scoped(clause: str) -> str:
    return f" AND ({clause})" if clause else ""


def _oldest(conn, tables: tuple[tuple[str, str], ...]) -> float | None:
    found = [
        conn.execute(
            f"SELECT MIN(ts) AS t FROM {table} WHERE 1{_scoped(clause)}"
        ).fetchone()["t"]
        for table, clause in tables
    ]
    found = [t for t in found if t is not None]
    return min(found) if found else None


def _enforce_size(archive: Archive, now: float) -> int:
    """Drop the oldest raw day until the file fits, recording each as a gap.

    Samples, logs and telemetry events go first, a day at a time; then
    anonymous `access` rows; and only then `requests`, attributed `access`
    and the agents' and shells' own events -- the record of who asked for
    what from where, and of what a node did for whom. A disk ceiling costs
    detail before it costs evidence, and an anonymous flood costs its own
    rows before anybody else's. The rollups for evicted days stay. Losing
    per-second detail to a disk ceiling is a trade; losing the fact that the
    day happened is not.
    """
    conn = archive.conn
    dropped = 0
    for _ in range(MAX_EVICT_DAYS):
        if _file_bytes(conn) <= config.ARCHIVE_MAX_BYTES:
            break
        tables: tuple[tuple[str, str], ...] = ()
        oldest = None
        for tables in _TIERS:
            oldest = _oldest(conn, tables)
            if oldest is not None:
                break
        if oldest is None:
            return dropped
        stop = _floor(oldest, int(config.DAY_S)) + config.DAY_S
        before = dropped
        conn.execute("BEGIN IMMEDIATE")
        try:
            for table, clause in tables:
                dropped += conn.execute(
                    f"DELETE FROM {table} WHERE ts < ?{_scoped(clause)}", (stop,)
                ).rowcount
            for node in conn.execute(
                "SELECT DISTINCT node_id FROM cursors"
            ).fetchall():
                archive.note_gap(node["node_id"], oldest, stop, "archive_size_cap")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("PRAGMA incremental_vacuum").fetchall()
        if dropped == before:
            # That day held no raw rows, so evicting the next one will not
            # shrink the file either. Stop on progress rather than on the
            # file measure -- the journal's version of this loop spun forever
            # because it trusted the measure alone.
            break
        log.warning(
            "telemetry archive hit its size cap; dropped raw rows before %.0f "
            "(rollups kept)",
            stop,
        )
    return dropped


def _file_bytes(conn) -> int:
    page_count = conn.execute("PRAGMA page_count").fetchone()[0]
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    return int(page_count) * int(page_size)


def _floor(ts: float, step: int) -> float:
    return float(int(ts // step) * step)
