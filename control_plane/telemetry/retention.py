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

import logging
import time
from typing import Any

from . import config
from .archive import Archive
from .hist import Hist

log = logging.getLogger(__name__)

STEP_1M = 60
STEP_1H = 3600

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

from control_plane.registry.telemetry import CLOCK_THROTTLE_NAMES, CLOCK_THROTTLE_MASK


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
            else:
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


def _roll_requests(conn, from_ts: float, to_ts: float, step: int) -> int:
    """Request rollups, in Python because the histograms are not SQL.

    The 1-minute step reads raw rows; the hourly step merges the 1-minute
    blobs rather than re-reading raw, so hours survive the raw rows expiring.
    """
    groups: dict[tuple, dict[str, Any]] = {}

    if step == STEP_1M:
        rows = conn.execute(
            "SELECT ts, served_name, target_id, status, tokens, prompt_tokens, "
            "cost_usd, ttft_ms, duration_ms FROM requests "
            "WHERE ts >= ? AND ts < ?",
            (from_ts, to_ts),
        )
        for r in rows:
            key = (int(r["ts"] // step) * step, r["served_name"] or "", r["target_id"] or "")
            g = groups.get(key)
            if g is None:
                g = groups[key] = _empty_group()
            status = r["status"]
            g["n"] += 1
            if status is not None and 200 <= int(status) < 400:
                g["ok"] += 1
            else:
                g["failed"] += 1
            g["tokens"] += int(r["tokens"] or 0)
            g["prompt_tokens"] += int(r["prompt_tokens"] or 0)
            g["cost_usd"] += float(r["cost_usd"] or 0.0)
            if r["ttft_ms"] is not None:
                g["ttft"].add(float(r["ttft_ms"]))
            if r["duration_ms"] is not None:
                g["duration"].add(float(r["duration_ms"]))
    else:
        rows = conn.execute(
            "SELECT bucket, served_name, target_id, n, ok, failed, tokens, "
            "prompt_tokens, cost_usd, ttft_hist, duration_hist FROM rollup_requests "
            "WHERE step = ? AND bucket >= ? AND bucket < ?",
            (STEP_1M, from_ts, to_ts),
        )
        for r in rows:
            key = (int(r["bucket"] // step) * step, r["served_name"] or "", r["target_id"] or "")
            g = groups.get(key)
            if g is None:
                g = groups[key] = _empty_group()
            g["n"] += int(r["n"] or 0)
            g["ok"] += int(r["ok"] or 0)
            g["failed"] += int(r["failed"] or 0)
            g["tokens"] += int(r["tokens"] or 0)
            g["prompt_tokens"] += int(r["prompt_tokens"] or 0)
            g["cost_usd"] += float(r["cost_usd"] or 0.0)
            g["ttft"].merge(Hist.from_blob(r["ttft_hist"]))
            g["duration"].merge(Hist.from_blob(r["duration_hist"]))

    if not groups:
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO rollup_requests(step, bucket, served_name, "
        "target_id, n, ok, failed, tokens, prompt_tokens, cost_usd, ttft_hist, "
        "duration_hist) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                step,
                bucket,
                served,
                target,
                g["n"],
                g["ok"],
                g["failed"],
                g["tokens"],
                g["prompt_tokens"],
                g["cost_usd"],
                g["ttft"].to_blob(),
                g["duration"].to_blob(),
            )
            for (bucket, served, target), g in groups.items()
        ],
    )
    return len(groups)


def _empty_group() -> dict[str, Any]:
    return {
        "n": 0,
        "ok": 0,
        "failed": 0,
        "tokens": 0,
        "prompt_tokens": 0,
        "cost_usd": 0.0,
        "ttft": Hist(),
        "duration": Hist(),
    }


# -- retention ---------------------------------------------------------------


def _expire(conn, now: float) -> dict[str, int]:
    # DERATE_TELEMETRY_RETENTION_DAYS moves every raw horizon together
    # rather than only the samples one, so halving it halves the request and
    # log windows too and the ratios between them are preserved. Unset, this
    # is exactly 1.0.
    scale = config.retention_days() * config.DAY_S / config.SAMPLES_RAW_RETENTION_S
    horizons = (
        ("samples", "ts", config.SAMPLES_RAW_RETENTION_S * scale),
        ("requests", "ts", config.REQUESTS_RAW_RETENTION_S * scale),
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
    # Rollup horizons are deliberately not scaled. They cost almost nothing,
    # and someone shortening the raw window to save disk wants a shorter raw
    # window, not a shorter memory.
    for step, window in (
        (STEP_1M, config.ROLLUP_1M_RETENTION_S),
        (STEP_1H, config.ROLLUP_1H_RETENTION_S),
    ):
        cutoff = now - window
        for table in ("rollup_samples", "rollup_requests"):
            n = conn.execute(
                f"DELETE FROM {table} WHERE step = ? AND bucket < ?", (step, cutoff)
            ).rowcount
            if n:
                deleted[f"{table}_{step}"] = deleted.get(f"{table}_{step}", 0) + n
    return deleted


def _enforce_size(archive: Archive, now: float) -> int:
    """Drop the oldest raw day until the file fits, recording each as a gap.

    The rollups for those days stay. Losing per-second detail to a disk
    ceiling is a trade; losing the fact that the day happened is not.
    """
    conn = archive.conn
    dropped = 0
    for _ in range(MAX_EVICT_DAYS):
        if _file_bytes(conn) <= config.ARCHIVE_MAX_BYTES:
            break
        row = conn.execute("SELECT MIN(ts) AS t FROM samples").fetchone()
        oldest = row["t"] if row else None
        if oldest is None:
            return dropped
        stop = _floor(oldest, int(config.DAY_S)) + config.DAY_S
        before = dropped
        conn.execute("BEGIN IMMEDIATE")
        try:
            for table in ("samples", "requests", "logs"):
                dropped += conn.execute(
                    f"DELETE FROM {table} WHERE ts < ?", (stop,)
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
