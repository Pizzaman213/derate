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

import csv
import io
import json
import time
from typing import Any

from . import config
from .archive import Archive
from .hist import Hist
from .records import REQUEST_RECORD_FIELDS
from .retention import _HISTS, STEP_1D, STEP_1H, STEP_1M, _Group

RAW = "raw"
STEP_NAMES = {RAW: 0, "1m": STEP_1M, "1h": STEP_1H, "1d": STEP_1D}


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


def pick_step(
    step: Any,
    from_ts: float,
    to_ts: float,
    *,
    limit: int | None = None,
    series: int = 1,
) -> str:
    """`auto` unless the caller insists, and never finer than the data.

    ``auto`` is also never finer than the row budget can carry. Node samples
    land at 1 Hz per node, so a six-hour window is ~21,600 rows each against a
    5,000-row cap: choosing RAW there does not return six hours at full
    resolution, it returns the first 40 minutes of it. Stepping up to a
    coarser rollup answers the question that was actually asked.
    """
    if isinstance(step, str) and step.lower() in STEP_NAMES:
        return step.lower()
    span = to_ts - from_ts
    budget = limit if limit and limit > 0 else None
    reach = max(1, series)

    def fits(sample_period_s: float) -> bool:
        if budget is None:
            return True
        return (span / sample_period_s) * reach <= budget

    if span <= config.AUTO_STEP_RAW_MAX_S and fits(1.0):
        return RAW
    if span <= config.AUTO_STEP_1M_MAX_S and fits(60.0):
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


def _node_count(archive: Archive) -> int:
    """How many nodes the archive holds rows for. Cheap: the cursors table has
    one row per node, not one per sample."""
    try:
        with archive.lock:
            row = archive.conn.execute(
                "SELECT COUNT(*) AS n FROM cursors"
            ).fetchone()
        return int(row["n"]) if row else 1
    except Exception:
        return 1


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
    limit = max(1, min(int(limit), config.QUERY_MAX_ROWS))
    args: list[Any] = [frm, to]
    where = ""
    if node_id:
        where = " AND node_id = ?"
        args.append(node_id)

    # Node samples land once per second PER NODE, so the row budget is shared
    # across however many nodes the window covers. Telling pick_step how many
    # series it is choosing a resolution for is what stops `auto` returning
    # the first 40 minutes of a six-hour request and calling it "raw".
    series = 1 if node_id else max(1, _node_count(archive))
    chosen = pick_step(step, frm, to, limit=limit, series=series)

    with archive.lock:
        if chosen == RAW:
            sql = (
                "SELECT node_id, ts, memory_used, memory_total, power_w, temp_c, "
                "util_pct, gpu_memory_used, gpu_process_count, host_memory_total, "
                "host_memory_available, swap_used FROM samples "
                "WHERE ts >= ? AND ts < ?" + where + " ORDER BY ts DESC LIMIT ?"
            )
        else:
            sql = (
                "SELECT node_id, bucket AS ts, n, power_w_avg, power_w_max, "
                "temp_c_avg, temp_c_max, util_pct_avg, util_pct_max, "
                "memory_used_avg, memory_used_max, gpu_memory_used_avg, "
                "gpu_memory_used_max, host_memory_available_min, swap_used_max "
                "FROM rollup_samples WHERE step = ? AND bucket >= ? AND bucket < ?"
                + where
                + " ORDER BY bucket DESC LIMIT ?"
            )
            args = [STEP_NAMES[chosen], *args]
        rows = [dict(r) for r in archive.conn.execute(sql, (*args, limit))]

    # Read back newest-first so the LIMIT drops the far end of the window,
    # then restore chronological order for the caller.
    rows.reverse()
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
            # The v2 rollup is keyed finer (node, provider, key, org, context
            # bucket, status); this answer keeps its v1 shape by summing
            # across those, merging histograms exactly.
            merged: dict[tuple, dict[str, Any]] = {}
            for r in archive.conn.execute(
                "SELECT bucket, served_name, target_id, n, ok, failed, tokens, "
                "prompt_tokens, cost_usd, ttft_hist, duration_hist "
                "FROM rollup_requests WHERE step = ? AND bucket >= ? AND bucket < ?"
                + filters,
                (STEP_NAMES[chosen], frm, to, *args),
            ):
                key = (r["bucket"], r["served_name"], r["target_id"])
                entry = merged.get(key)
                if entry is None:
                    entry = merged[key] = {
                        "bucket": r["bucket"],
                        "served_name": r["served_name"],
                        "target_id": r["target_id"],
                        "n": 0,
                        "ok": 0,
                        "failed": 0,
                        "tokens": 0,
                        "prompt_tokens": 0,
                        "cost_usd": 0.0,
                        "_ttft": Hist(),
                        "_duration": Hist(),
                    }
                for k in ("n", "ok", "failed", "tokens", "prompt_tokens"):
                    entry[k] += int(r[k] or 0)
                entry["cost_usd"] += float(r["cost_usd"] or 0.0)
                entry["_ttft"].merge(Hist.from_blob(r["ttft_hist"]))
                entry["_duration"].merge(Hist.from_blob(r["duration_hist"]))
            ordered = sorted(
                merged.values(),
                key=lambda e: (e["bucket"], e["served_name"], e["target_id"]),
                reverse=True,
            )[:limit]
            rows = []
            for entry in ordered:
                entry["ts"] = entry["bucket"]
                # Percentiles, not the buckets that produced them. The blob is
                # an implementation detail and would dwarf the response.
                entry["ttft"] = entry.pop("_ttft").summary()
                entry["duration"] = entry.pop("_duration").summary()
                rows.append(entry)

    # Both branches read newest-first so the LIMIT drops the far end rather
    # than the recent end; one reverse puts the caller back in time order.
    rows.reverse()
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
    node_id: str = "",
    from_ts: Any = None,
    to_ts: Any = None,
    limit: int = 500,
) -> dict[str, Any]:
    frm, to = resolve_window(from_ts, to_ts)
    limit = max(1, min(int(limit), config.QUERY_MAX_ROWS))
    filters, args = "", []
    # ``node_id`` is stored and selected already; it simply had no filter, so
    # asking "what happened on this machine" meant pulling the whole cluster's
    # events and discarding most of them in the caller. ``logs`` has filtered
    # on it since it shipped -- this is the same clause.
    for column, value in (
        ("type", type),
        ("deployment_id", deployment_id),
        ("source", source),
        ("node_id", node_id),
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
    out = _envelope(archive, RAW, frm, to, node_id)
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
    exclude: tuple[str, ...] | list[str] | None = None,
    from_ts: Any = None,
    to_ts: Any = None,
    limit: int = 500,
) -> dict[str, Any]:
    frm, to = resolve_window(from_ts, to_ts)
    limit = max(1, min(int(limit), config.QUERY_MAX_ROWS))
    filters, args = "", []
    # The only negation on this surface. `logger` is a prefix filter and an
    # inclusive one, so "everything except the access log" was inexpressible --
    # which mattered because the handler now declines to record those loggers,
    # and a window recorded before that change would otherwise still read as
    # a wall of them. Same prefix semantics, opposite sign.
    for prefix in exclude or ():
        if not prefix:
            continue
        filters += " AND logger NOT LIKE ?"
        args.append(prefix + "%")
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


# -- usage: the per-key / per-org stats API -----------------------------------

#: USAGE_GROUP_BY member -> rollup column.
_GROUP_COLUMN = {
    "key_id": "key_id",
    "org_id": "org_id",
    "model": "served_name",
    "node": "node_id",
    "provider": "provider_id",
    "status": "status",
    "context_bucket": "context_bucket",
}

#: Histogram -> the name it is reported under.
_LATENCY_NAMES = {
    "ttft": "ttft_ms",
    "duration": "latency_ms",
    "queue": "queue_ms",
    "tpot": "tpot_ms",
    "tps": "tokens_per_s",
}


def usage(
    archive: Archive,
    *,
    start: float,
    end: float,
    bucket: str,
    group_by: list[str] | tuple[str, ...] = (),
    key_ids: list[str] | None = None,
    org_ids: list[str] | None = None,
    models: list[str] | None = None,
) -> dict[str, Any]:
    """Usage per time bucket and group, read from the request rollups.

    ``bucket`` is one of USAGE_BUCKETS; ``group_by`` members come from
    USAGE_GROUP_BY (``model`` is the served name). A bucket with no requests
    is absent, never a zero row. Every optional measure is None when no row
    in the group reported it -- an average or rate is over the rows that did.
    Raw rows younger than the roll lag (about two minutes) are not counted yet.

    A 1h or 1d rollup is written only once its bucket has closed, so the
    current hour or day exists only as 1m rollups. Those are folded into the
    coarse bucket they belong to; without them a key's "last 30 days" read 0
    until midnight UTC.
    """
    if bucket not in STEP_NAMES or bucket == RAW:
        raise ValueError(f"bucket must be one of 1m, 1h, 1d, not {bucket!r}")
    unknown = [g for g in group_by if g not in _GROUP_COLUMN]
    if unknown:
        raise ValueError(f"cannot group by {', '.join(unknown)}")
    step = STEP_NAMES[bucket]
    filters = ""
    filter_args: list[Any] = []
    for column, values in (("key_id", key_ids), ("org_id", org_ids), ("served_name", models)):
        if values is not None:
            if not values:
                filters += " AND 0"
                continue
            filters += f" AND {column} IN ({', '.join('?' * len(values))})"
            filter_args.extend(values)
    sql = "SELECT * FROM rollup_requests WHERE step = ? AND bucket >= ? AND bucket < ?"
    queries = [(sql + filters, [step, _floor_to(start, step), end, *filter_args])]
    fine = STEP_NAMES["1m"]

    groups: dict[tuple, Any] = {}
    with archive.lock:
        if step != fine:
            # Everything past the last closed coarse bucket. Unfiltered on
            # purpose: a rolled bucket is rolled for every key at once.
            last = archive.conn.execute(
                "SELECT MAX(bucket) AS b FROM rollup_requests WHERE step = ?", (step,)
            ).fetchone()["b"]
            tail = _floor_to(start, fine) if last is None else max(last + step, _floor_to(start, fine))
            queries.append((sql + filters, [fine, tail, end, *filter_args]))
        for q, q_args in queries:
            for r in archive.conn.execute(q, q_args):
                key = (_floor_to(r["bucket"], step), *(r[_GROUP_COLUMN[g]] for g in group_by))
                g = groups.get(key)
                if g is None:
                    g = groups[key] = _Group()
                g.add_rollup(r)

    rows = []
    for key in sorted(groups, key=lambda k: tuple(str(x) for x in k)):
        entry: dict[str, Any] = {"bucket": key[0]}
        entry["group"] = {name: key[i + 1] for i, name in enumerate(group_by)}
        entry.update(_usage_measures(groups[key]))
        rows.append(entry)
    out = _envelope(archive, bucket, start, end)
    out["bucket"] = bucket
    out["group_by"] = list(group_by)
    out["rows"] = rows
    return out


def _floor_to(ts: float, step: int) -> float:
    return float(int(ts // step) * step)


def _ratio(num, den) -> float | None:
    if num is None or not den:
        return None
    return num / den


def _usage_measures(g) -> dict[str, Any]:
    n = g.n
    drafts = g.spec["spec_drafts"]
    positions = g.spec_positions
    latency = {}
    for name, _col in _HISTS:
        h = g.hists[name]
        latency[_LATENCY_NAMES[name]] = (
            None
            if not h.count
            else {
                "n": h.count,
                "avg": h.mean(),
                "p50": h.pct(0.50),
                "p95": h.pct(0.95),
                "p99": h.pct(0.99),
            }
        )
    if n and g.unpriced_n == n:
        basis = "unpriced"
    elif g.estimated_usd is not None:
        basis = "estimated"
    elif g.metered_usd is not None:
        basis = "metered"
    else:
        basis = None
    return {
        "requests": n,
        "ok": g.ok,
        "failed": g.failed,
        "errors": dict(g.errors),
        "error_rate": _ratio(g.failed, n),
        "fallback_rate": _ratio(g.fallback, n),
        "cancel_rate": _ratio(g.cancelled, n),
        "tokens_in": g.prompt_tokens,
        "tokens_out": g.completion_tokens,
        "reasoning_tokens": g.opt["reasoning_tokens"],
        "cache_read_tokens": g.opt["cache_read_tokens"],
        "cache_write_tokens": g.opt["cache_write_tokens"],
        "cache_hit_rate": _ratio(g.opt["cache_read_tokens"], g.cache_prompt_tokens),
        "cache_errors": dict(g.cache_errors),
        "avg_prompt_tokens": _ratio(g.prompt_tokens, n),
        "avg_output_tokens": _ratio(g.completion_tokens, n),
        "spec_drafts": drafts,
        "spec_draft_tokens": g.spec["spec_draft_tokens"],
        "spec_accepted_tokens": g.spec["spec_accepted_tokens"],
        "spec_acceptance_rate": _ratio(
            g.spec["spec_accepted_tokens"], g.spec["spec_draft_tokens"]
        ),
        "spec_accepted_per_position": positions,
        "spec_acceptance_per_position": (
            None if positions is None or not drafts else [p / drafts for p in positions]
        ),
        "spec_errors": dict(g.spec_errors),
        **latency,
        "cost_usd": g.cost_usd if basis not in (None, "unpriced") else None,
        "metered_usd": g.metered_usd,
        "estimated_usd": g.estimated_usd,
        "unpriced_requests": g.unpriced_n,
        "cost_basis": basis,
    }


_JSON_REQUEST_COLS = ("attempt_chain", "spec_accepted_per_position")


def requests_list(
    archive: Archive,
    *,
    start: float,
    end: float,
    request_id: str | None = None,
    key_id: str | None = None,
    model: str | None = None,
    status: int | str | None = None,
    error: str | None = None,
    target_id: str | None = None,
    provider_id: str | None = None,
    retried: bool = False,
    page: int = 1,
    page_size: int = 100,
) -> dict[str, Any]:
    """One row per client request, newest first, paged.

    Each row is the request's final attempt (highest ``attempt_no``) with
    ``attempts`` replaced by the true attempt count. ``model`` matches the
    served name; ``error`` matches error_class or error_code; ``status`` is
    an int, ``"ok"`` (2xx/3xx) or ``"failed"``; ``target_id`` and
    ``provider_id`` match the final attempt's target and provider; ``retried``
    keeps requests that took more than one attempt. Reaches back only as far
    as the raw window.
    """
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), config.QUERY_MAX_ROWS))
    inner = "ts >= ? AND ts < ?"
    args: list[Any] = [start, end]
    if request_id:
        inner += " AND request_id = ?"
        args.append(request_id)
    where = "1=1"
    if key_id:
        where += " AND r.key_id = ?"
        args.append(key_id)
    if model:
        where += " AND r.served_name = ?"
        args.append(model)
    if status == "ok":
        where += " AND r.status BETWEEN 200 AND 399"
    elif status == "failed":
        where += " AND (r.status IS NULL OR r.status < 200 OR r.status >= 400)"
    elif status is not None:
        where += " AND r.status = ?"
        args.append(int(status))
    if error:
        where += " AND (r.error_class = ? OR r.error_code = ?)"
        args.extend((error, error))
    if target_id:
        where += " AND r.target_id = ?"
        args.append(target_id)
    if provider_id:
        where += " AND r.provider_id = ?"
        args.append(provider_id)
    if retried:
        where += " AND final.n_attempts > 1"
    body = (
        "FROM requests r JOIN ("
        "SELECT request_id, MAX(attempt_no) AS attempt_no, COUNT(*) AS n_attempts "
        f"FROM requests WHERE {inner} GROUP BY request_id) final "
        "ON r.request_id = final.request_id AND r.attempt_no = final.attempt_no "
        f"WHERE {where}"
    )
    cols = ", ".join(
        "final.n_attempts AS attempts" if c == "attempts" else f"r.{c}"
        for c in REQUEST_RECORD_FIELDS
    )
    with archive.lock:
        total = archive.conn.execute(f"SELECT COUNT(*) AS n {body}", args).fetchone()["n"]
        rows = [
            dict(r)
            for r in archive.conn.execute(
                f"SELECT {cols} {body} ORDER BY r.ts DESC, r.request_id LIMIT ? OFFSET ?",
                (*args, page_size, (page - 1) * page_size),
            )
        ]
    for row in rows:
        for col in _JSON_REQUEST_COLS:
            if isinstance(row.get(col), str):
                try:
                    row[col] = json.loads(row[col])
                except ValueError:
                    pass
    return {"rows": rows, "total": int(total), "page": page, "page_size": page_size}


#: A spreadsheet runs a cell that starts with one of these as a formula, and
#: `model_requested` and `user_agent` are whatever the client sent.
_FORMULA_LEADS = ("=", "+", "-", "@", "\t", "\r")


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"))
    if isinstance(value, str) and value.startswith(_FORMULA_LEADS):
        return "'" + value
    return value


def requests_csv(rows: list[dict[str, Any]]) -> str:
    """Raw request rows as CSV, columns in REQUEST_RECORD_FIELDS order."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(REQUEST_RECORD_FIELDS)
    for row in rows:
        writer.writerow([_cell(row.get(c)) for c in REQUEST_RECORD_FIELDS])
    return buf.getvalue()


def usage_csv(result: dict[str, Any]) -> str:
    """A usage() answer as CSV: one line per bucket and group.

    Group columns come first, then every scalar measure; latency objects are
    flattened to ``<name>_<stat>``; maps and lists are JSON cells.
    """
    rows = result.get("rows") or []
    group_by = list(result.get("group_by") or [])
    flat_rows = []
    for row in rows:
        flat: dict[str, Any] = {"bucket": row["bucket"]}
        for g in group_by:
            flat[g] = row["group"].get(g)
        for k, v in row.items():
            if k in ("bucket", "group"):
                continue
            if k in _LATENCY_NAMES.values():
                for stat in ("n", "avg", "p50", "p95", "p99"):
                    flat[f"{k}_{stat}"] = None if v is None else v[stat]
            else:
                flat[k] = v
        flat_rows.append(flat)
    header: list[str] = ["bucket", *group_by]
    if flat_rows:
        header += [k for k in flat_rows[0] if k not in header]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for flat in flat_rows:
        writer.writerow([_cell(flat.get(c)) for c in header])
    return buf.getvalue()
