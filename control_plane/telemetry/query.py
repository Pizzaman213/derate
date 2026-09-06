"""Reading the archive back.

Two rules shape everything here.

Pick the resolution, do not let the caller ask for 2.6 million rows. `step`
defaults to `auto`, which answers from raw rows for a short window, 1-minute
rollups for a week, and hourly ones beyond -- so dragging a date picker to
"last year" costs the same as "last hour".

Say when data is missing and why. Every answer carries `resolution` and any
`gaps` overlapping the window, because a flat line has two very different
causes: the cluster was idle, or the rows were trimmed. The product already
makes this distinction for links it has never measured; a history that quietly
implies "nothing happened" would be the same lie in a new place.
"""

from __future__ import annotations

import json
import time
from typing import Any

from . import config
from .archive import Archive
from .hist import Hist
from .retention import STEP_1H, STEP_1M

RAW = "raw"
STEP_NAMES = {RAW: 0, "1m": STEP_1M, "1h": STEP_1H}


def resolve_window(
    from_ts: Any = None, to_ts: Any = None, now: float | None = None
) -> tuple[float, float]:
    """Absolute or relative bounds. A negative or `-1h`-style value is ago."""
    now = time.time() if now is None else now
    to = _stamp(to_ts, now, now)
    frm = _stamp(from_ts, now, to - 3600.0)
    if frm > to:
        frm, to = to, frm
    return frm, to


def _stamp(value: Any, now: float, default: float) -> float:
    if value is None or value == "":
        return default
    if isinstance(value, str):
        value = value.strip()
        if value.endswith(("s", "m", "h", "d")) and value[:-1].lstrip("-").isdigit():
            scale = {"s": 1, "m": 60, "h": 3600, "d": 86400}[value[-1]]
            return now - abs(int(value[:-1])) * scale
        try:
            value = float(value)
        except ValueError:
            return default
    value = float(value)
    # A small or negative number is an offset, not a date in 1970.
    return now + value if value <= 0 else (now - value if value < 1e6 else value)


def pick_step(step: Any, from_ts: float, to_ts: float) -> str:
    """`auto` unless the caller insists, and never finer than the data."""
    if isinstance(step, str) and step.lower() in STEP_NAMES:
        return step.lower()
    span = to_ts - from_ts
    if span <= config.AUTO_STEP_RAW_MAX_S:
        return RAW
    if span <= config.AUTO_STEP_1M_MAX_S:
        return "1m"
    return "1h"


def _envelope(
    archive: Archive, step: str, from_ts: float, to_ts: float, node_id: str = ""
) -> dict[str, Any]:
    return {
        "from": from_ts,
        "to": to_ts,
        "resolution": step,
        # Archive-backed answers survive a restart. The registry's in-RAM ring,
        # which answers /api/history/nodes when telemetry is off, does not --
        # and says so with durable: false. A caller charting both must be able
        # to tell them apart without knowing how the node was configured.
        "durable": True,
        "gaps": archive.gaps(from_ts, to_ts, node_id),
    }


def nodes(
    archive: Archive,
    *,
    node_id: str = "",
    from_ts: Any = None,
    to_ts: Any = None,
    step: Any = "auto",
    limit: int = config.QUERY_MAX_ROWS,
) -> dict[str, Any]:
    """Node metrics over a window, at whatever resolution fits it."""
    frm, to = resolve_window(from_ts, to_ts)
    chosen = pick_step(step, frm, to)
    limit = max(1, min(int(limit), config.QUERY_MAX_ROWS))
    args: list[Any] = [frm, to]
    where = ""
    if node_id:
        where = " AND node_id = ?"
        args.append(node_id)

    with archive.lock:
        if chosen == RAW:
            sql = (
                "SELECT node_id, ts, memory_used, memory_total, power_w, temp_c, "
                "util_pct, gpu_memory_used, gpu_process_count, host_memory_total, "
                "host_memory_available, swap_used FROM samples "
                "WHERE ts >= ? AND ts < ?" + where + " ORDER BY ts LIMIT ?"
            )
        else:
            sql = (
                "SELECT node_id, bucket AS ts, n, power_w_avg, power_w_max, "
                "temp_c_avg, temp_c_max, util_pct_avg, util_pct_max, "
                "memory_used_avg, memory_used_max, gpu_memory_used_avg, "
                "gpu_memory_used_max, host_memory_available_min, swap_used_max "
                "FROM rollup_samples WHERE step = ? AND bucket >= ? AND bucket < ?"
                + where
                + " ORDER BY bucket LIMIT ?"
            )
            args = [STEP_NAMES[chosen], *args]
        rows = [dict(r) for r in archive.conn.execute(sql, (*args, limit))]

    out = _envelope(archive, chosen, frm, to, node_id)
    out["truncated"] = len(rows) >= limit
    out["samples"] = rows
    return out


def requests(
    archive: Archive,
    *,
    served_name: str = "",
    target_id: str = "",
    from_ts: Any = None,
    to_ts: Any = None,
    step: Any = "auto",
    limit: int = config.QUERY_MAX_ROWS,
) -> dict[str, Any]:
    """Request throughput and latency, raw rows or rolled buckets."""
    frm, to = resolve_window(from_ts, to_ts)
    chosen = pick_step(step, frm, to)
    limit = max(1, min(int(limit), config.QUERY_MAX_ROWS))
    filters, args = "", []
    if served_name:
        filters += " AND served_name = ?"
        args.append(served_name)
    if target_id:
        filters += " AND target_id = ?"
        args.append(target_id)

    with archive.lock:
        if chosen == RAW:
            rows = [
                dict(r)
                for r in archive.conn.execute(
                    "SELECT request_id, attempt_no, ts, node_id, served_name, "
                    "target_id, target_kind, provider_id, deployment_id, policy, "
                    "strength_source, attempts, retry_reason, status, error_code, "
                    "error_class, "
                    "prompt_tokens, completion_tokens, tokens, tokens_estimated, "
                    "ttft_ms, decode_ms, duration_ms, parked_ms, streaming, "
                    "cost_usd FROM requests WHERE ts >= ? AND ts < ?"
                    + filters
                    + " ORDER BY ts DESC LIMIT ?",
                    (frm, to, *args, limit),
                )
            ]
        else:
            rows = []
            for r in archive.conn.execute(
                "SELECT bucket, served_name, target_id, n, ok, failed, tokens, "
                "prompt_tokens, cost_usd, ttft_hist, duration_hist "
                "FROM rollup_requests WHERE step = ? AND bucket >= ? AND bucket < ?"
                + filters
                + " ORDER BY bucket LIMIT ?",
                (STEP_NAMES[chosen], frm, to, *args, limit),
            ):
                entry = {
                    k: r[k]
                    for k in (
                        "bucket",
                        "served_name",
                        "target_id",
                        "n",
                        "ok",
                        "failed",
                        "tokens",
                        "prompt_tokens",
                        "cost_usd",
                    )
                }
                entry["ts"] = r["bucket"]
                # Percentiles, not the buckets that produced them. The blob is
                # an implementation detail and would dwarf the response.
                entry["ttft"] = Hist.from_blob(r["ttft_hist"]).summary()
                entry["duration"] = Hist.from_blob(r["duration_hist"]).summary()
                rows.append(entry)

    out = _envelope(archive, chosen, frm, to)
    out["truncated"] = len(rows) >= limit
    out["requests"] = rows
    return out


def events(
    archive: Archive,
    *,
    type: str = "",
    deployment_id: str = "",
    source: str = "",
    from_ts: Any = None,
    to_ts: Any = None,
    limit: int = 500,
) -> dict[str, Any]:
    frm, to = resolve_window(from_ts, to_ts)
    limit = max(1, min(int(limit), config.QUERY_MAX_ROWS))
    filters, args = "", []
    for column, value in (
        ("type", type),
        ("deployment_id", deployment_id),
        ("source", source),
    ):
        if value:
            filters += f" AND {column} = ?"
            args.append(value)
    with archive.lock:
        rows = archive.conn.execute(
            "SELECT node_id, ts, source, type, deployment_id, served_name, body "
            "FROM events WHERE ts >= ? AND ts < ?" + filters
            + " ORDER BY ts DESC LIMIT ?",
            (frm, to, *args, limit),
        ).fetchall()
    out = _envelope(archive, RAW, frm, to)
    out["events"] = [_expand(dict(r)) for r in rows]
    out["truncated"] = len(rows) >= limit
    return out


def logs(
    archive: Archive,
    *,
    level: str = "",
    logger: str = "",
    q: str = "",
    node_id: str = "",
    from_ts: Any = None,
    to_ts: Any = None,
    limit: int = 500,
) -> dict[str, Any]:
    frm, to = resolve_window(from_ts, to_ts)
    limit = max(1, min(int(limit), config.QUERY_MAX_ROWS))
    filters, args = "", []
    if level:
        # A level filter means "this and worse", which is what someone asking
        # for warnings actually wants.
        wanted = _at_least(level.upper())
        filters += " AND level IN (" + ", ".join("?" * len(wanted)) + ")"
        args.extend(wanted)
    if logger:
        filters += " AND logger LIKE ?"
        args.append(logger + "%")
    if node_id:
        filters += " AND node_id = ?"
        args.append(node_id)
    if q:
        filters += " AND message LIKE ?"
        args.append(f"%{q}%")
    with archive.lock:
        rows = archive.conn.execute(
            "SELECT node_id, ts, level, logger, message, body FROM logs "
            "WHERE ts >= ? AND ts < ?" + filters + " ORDER BY ts DESC LIMIT ?",
            (frm, to, *args, limit),
        ).fetchall()
    out = _envelope(archive, RAW, frm, to, node_id)
    out["logs"] = [_expand(dict(r)) for r in rows]
    out["truncated"] = len(rows) >= limit
    return out


_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def _at_least(level: str) -> list[str]:
    try:
        return list(_LEVELS[_LEVELS.index(level) :])
    except ValueError:
        return list(_LEVELS)


def _expand(row: dict[str, Any]) -> dict[str, Any]:
    """Merge the stored JSON body up into the row, keeping typed columns."""
    body = row.pop("body", None)
    if isinstance(body, str):
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            merged = dict(parsed)
            merged.update({k: v for k, v in row.items() if v not in (None, "")})
            return merged
    return row
