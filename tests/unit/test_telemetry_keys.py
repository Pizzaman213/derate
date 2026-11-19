"""Per-key telemetry: the v2 request record, archive migration, rollups,
usage(), requests_list() and the engine-metrics attribution rules."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import sqlite3
import subprocess
from dataclasses import fields
from pathlib import Path

import httpx
import pytest

from control_plane.contracts import telemetry as contract
from control_plane.contracts.telemetry import USAGE_GROUP_BY, FieldError
from control_plane.telemetry import query, retention
from control_plane.telemetry.archive import Archive
from control_plane.telemetry.engine_metrics import EngineMetricsPoller, SpecSample
from control_plane.telemetry.hist import Hist, merged
from control_plane.telemetry.records import (
    REQUEST_RECORD_FIELDS,
    RequestRecord,
    RequestTrace,
    context_bucket,
)

ROOT = Path(__file__).resolve().parents[2]
SETS = ROOT / ".omp/work/auth-keys/sets/telemetry_fields.json"
DAY = 86400.0
T0 = 1_800_000_000.0 - (1_800_000_000.0 % DAY)  # a UTC midnight


def _rec(**kw) -> RequestRecord:
    return RequestRecord(request_id=kw.pop("request_id", "r"), ts=kw.pop("ts", T0), served_name=kw.pop("served_name", "m"), **kw)


def _ingest(archive: Archive, records: list[RequestRecord], node: str = "n1") -> None:
    # The archive files a row under the node that shipped it, never the
    # node_id a page claims, so each record is shipped by its own node.
    by_node: dict[str, list[dict]] = {}
    for i, r in enumerate(records):
        rows = by_node.setdefault(r.node_id or node, [])
        rows.append({"kind": "request", "seq": i + 1, "ts": r.ts, "body": json.dumps(r.as_dict())})
    for node_id, rows in by_node.items():
        last = rows[-1]["seq"]
        archive.ingest(node_id, {"rows": rows, "next": last, "head": last}, trusted=True)


# -- the record ---------------------------------------------------------------


def test_fields_match_contract_in_order():
    assert REQUEST_RECORD_FIELDS == contract.REQUEST_RECORD_FIELDS


def test_every_new_field_defaults_to_none_never_zero():
    members = json.loads(SETS.read_text())["members"]
    assert tuple(members) == REQUEST_RECORD_FIELDS
    defaults = {f.name: f.default for f in fields(RequestRecord)}
    for name in members[29:]:
        value = defaults[name]
        if name in ("fallback_used", "cancelled"):
            assert value is False, name
        else:
            assert value is None, f"{name} defaults to {value!r}, not None"


def test_trace_record_without_reports_leaves_new_fields_none():
    rec = RequestTrace(request_id="r", served_name="m").record(status=200)
    for name in REQUEST_RECORD_FIELDS[29:]:
        value = getattr(rec, name)
        assert value != 0 or isinstance(value, bool), name
    assert rec.cache_error == "not_reported"
    assert rec.spec_decode_error == "not_reported"
    assert rec.prefix_cache_hit_rate is None
    assert rec.model_served == "m"


@pytest.mark.parametrize(
    "tokens,bucket",
    [
        (1, "0-8k"),
        (8191, "0-8k"),
        (8192, "8-32k"),
        (32767, "8-32k"),
        (32768, "32-128k"),
        (131071, "32-128k"),
        (131072, "128k+"),
        (0, None),
        (None, None),
    ],
)
def test_context_bucket_edges(tokens, bucket):
    assert context_bucket(tokens) == bucket
    if bucket is not None:
        assert bucket in {b.value for b in contract.ContextBucket}


def test_trace_record_derives_hit_rate_identity_and_spec():
    trace = RequestTrace(request_id="r", served_name="m", key_id="k1", org_id="o1")
    rec = trace.record(
        status=200,
        prompt_tokens=10000,
        completion_tokens=50,
        tokens=50,
        cache_read_tokens=2500,
        generation_s=0.5,
        spec=SpecSample(num_drafts=10, draft_tokens=30, accepted_tokens=15, accepted_per_position=[8, 5, 2]),
    )
    assert rec.key_id == "k1" and rec.org_id == "o1"
    assert rec.context_bucket == "8-32k"
    assert rec.prefix_cache_hit_rate == 0.25 and rec.cache_error is None
    assert rec.tokens_per_s == 100.0
    assert rec.spec_acceptance_rate == 0.5
    assert rec.spec_mean_accepted_len == 2.5
    assert rec.spec_accepted_per_position == [8, 5, 2]
    assert rec.spec_decode_error is None
    # A zero cache count is a fact, not an absence.
    zero = trace.record(status=200, prompt_tokens=100, cache_read_tokens=0)
    assert zero.prefix_cache_hit_rate == 0.0 and zero.cache_error is None
    err = trace.record(status=200, spec=FieldError.AMBIGUOUS_CONCURRENCY)
    assert err.spec_decode_error == "ambiguous_concurrency" and err.spec_num_drafts is None


# -- archive ------------------------------------------------------------------


def test_new_columns_round_trip_with_nulls(tmp_path):
    a = Archive(tmp_path / "a.db")
    _ingest(a, [
        _rec(request_id="full", status=200, key_id="k", attempt_chain=[{"target": "t"}],
             spec_accepted_per_position=[1, 2], cache_read_tokens=0),
        _rec(request_id="bare", status=200),
    ])
    rows = {r["request_id"]: dict(r) for r in a.conn.execute("SELECT * FROM requests")}
    assert json.loads(rows["full"]["attempt_chain"]) == [{"target": "t"}]
    assert rows["full"]["cache_read_tokens"] == 0
    assert rows["bare"]["cache_read_tokens"] is None
    assert rows["bare"]["key_id"] is None and rows["bare"]["spec_num_drafts"] is None


def test_reemitted_row_replaces(tmp_path):
    a = Archive(tmp_path / "a.db")
    _ingest(a, [_rec(request_id="x", status=200)])
    _ingest(a, [_rec(request_id="x", status=200, spec_num_drafts=4, spec_draft_tokens=8, spec_accepted_tokens=6)])
    rows = list(a.conn.execute("SELECT spec_num_drafts FROM requests"))
    assert [r[0] for r in rows] == [4]


def _v1_schema() -> str:
    src = subprocess.run(
        ["git", "show", "HEAD:control_plane/telemetry/archive.py"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    start = src.index('_SCHEMA = """') + len('_SCHEMA = """')
    return src[start: src.index('"""', start)]


def test_migration_from_v1_keeps_rows_and_adds_columns(tmp_path):
    path = tmp_path / "archive.db"
    conn = sqlite3.connect(path)
    conn.executescript(_v1_schema())
    conn.execute("INSERT INTO meta(k, v) VALUES('schema_version', '1')")
    conn.execute(
        "INSERT INTO requests(request_id, attempt_no, ts, served_name, target_id, status, tokens, prompt_tokens, ttft_ms) "
        "VALUES('old', 0, ?, 'm', 't', 200, 7, 3, 12.5)", (T0,),
    )
    conn.execute(
        "INSERT INTO rollup_requests(step, bucket, served_name, target_id, n, ok, failed, tokens, prompt_tokens) "
        "VALUES(60, ?, 'm', 't', 1, 1, 0, 7, 3)", (T0,),
    )
    conn.commit()
    conn.close()

    a = Archive(path)
    cols = {r["name"] for r in a.conn.execute("PRAGMA table_info(requests)")}
    assert set(REQUEST_RECORD_FIELDS) <= cols
    row = dict(a.conn.execute("SELECT * FROM requests").fetchone())
    assert row["request_id"] == "old" and row["tokens"] == 7 and row["key_id"] is None
    assert a.conn.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()[0] == "2"
    assert a.take_reroll_pending()
    assert a.conn.execute("SELECT COUNT(*) FROM rollup_requests").fetchone()[0] == 0

    report = retention.compact(a, now=T0 + 2 * DAY)
    assert report.get("rerolled")
    assert not a.take_reroll_pending()
    got = a.conn.execute("SELECT step, n, tokens FROM rollup_requests ORDER BY step").fetchall()
    assert [tuple(r) for r in got] == [(60, 1, 7), (3600, 1, 7), (86400, 1, 7)]
    a.close()
    # Reopening a migrated file does not ask for another re-roll.
    assert not Archive(path).take_reroll_pending()


# -- rollups ------------------------------------------------------------------


def _spread(n: int) -> list[RequestRecord]:
    out = []
    for i in range(n):
        out.append(_rec(
            request_id=f"r{i}",
            ts=T0 + i * 97.0,  # across many minutes and hours of one day
            status=200 if i % 7 else 500,
            error_class="" if i % 7 else "ReadTimeout",
            ttft_ms=1.0 + (i * 37) % 900,
            duration_ms=50.0 + (i * 13) % 4000,
            tokens=10, prompt_tokens=100, completion_tokens=10,
            key_id="k1" if i % 2 else "k2", org_id="o1",
            spec_num_drafts=2, spec_draft_tokens=6, spec_accepted_tokens=3,
            spec_accepted_per_position=[2, 1] if i % 3 else [1, 1, 1],
        ))
    return out


def test_1d_percentile_equals_raw_merged_percentile(tmp_path):
    a = Archive(tmp_path / "a.db")
    recs = _spread(800)
    _ingest(a, recs)
    a.clear_reroll_pending()
    with a.lock:
        retention._roll(a.conn, a, T0, T0 + DAY, retention.STEP_1M)
        retention._roll(a.conn, a, T0, T0 + DAY, retention.STEP_1H)
        retention._roll(a.conn, a, T0, T0 + DAY, retention.STEP_1D)
    raw = Hist()
    for r in recs:
        raw.add(r.ttft_ms)
    day = merged(
        Hist.from_blob(r[0]) for r in a.conn.execute(
            "SELECT ttft_hist FROM rollup_requests WHERE step = 86400"
        )
    )
    assert day.count == len(recs)
    for q in (0.5, 0.95, 0.99):
        assert day.pct(q) == raw.pct(q)
    res = query.usage(a, start=T0, end=T0 + DAY, bucket="1d")
    assert len(res["rows"]) == 1
    row = res["rows"][0]
    assert row["requests"] == 800
    assert row["ttft_ms"]["p99"] == raw.pct(0.99)
    # Per-position acceptance merges element-wise across rows of different length.
    twos = sum(1 for i in range(800) if i % 3)
    ones = 800 - twos
    assert row["spec_accepted_per_position"] == [2 * twos + ones, twos + ones, ones]
    assert row["spec_acceptance_rate"] == 0.5
    assert row["errors"] == {"ReadTimeout": sum(1 for i in range(800) if i % 7 == 0)}


def test_existing_requests_query_sums_across_new_dimensions(tmp_path):
    a = Archive(tmp_path / "a.db")
    _ingest(a, _spread(120))
    with a.lock:
        retention._roll(a.conn, a, T0, T0 + DAY, retention.STEP_1M)
        retention._roll(a.conn, a, T0, T0 + DAY, retention.STEP_1H)
    res = query.requests(a, from_ts=T0, to_ts=T0 + DAY, step="1h")
    assert sum(r["n"] for r in res["requests"]) == 120
    # One row per (bucket, model, target): key/org/status split is folded away.
    assert len({(r["bucket"], r["served_name"], r["target_id"]) for r in res["requests"]}) == len(res["requests"])
    assert set(res["requests"][0]) >= {"bucket", "served_name", "target_id", "n", "ok", "failed", "tokens", "prompt_tokens", "cost_usd", "ts", "ttft", "duration"}


# -- usage() ------------------------------------------------------------------


def _varied(tmp_path) -> Archive:
    a = Archive(tmp_path / "a.db")
    _ingest(a, [
        _rec(request_id="a", ts=T0 + 10, served_name="m1", node_id="n1", provider_id="", key_id="k1", org_id="o1",
             status=200, prompt_tokens=100, completion_tokens=10, cache_read_tokens=50, cost_usd=0.01, cost_basis="metered"),
        _rec(request_id="b", ts=T0 + 20, served_name="m2", node_id="n2", provider_id="p1", key_id="k2", org_id="o1",
             status=429, prompt_tokens=9000, completion_tokens=0, cost_basis="unpriced"),
        _rec(request_id="c", ts=T0 + 3700, served_name="m1", node_id="n1", provider_id="", key_id="k3", org_id="o2",
             status=200, prompt_tokens=40000, completion_tokens=5, cost_usd=0.5, cost_basis="estimated"),
    ])
    with a.lock:
        retention._roll(a.conn, a, T0, T0 + DAY, retention.STEP_1M)
        retention._roll(a.conn, a, T0, T0 + DAY, retention.STEP_1H)
    return a


EXPECTED_GROUPS = {
    "key_id": {"k1": 1, "k2": 1, "k3": 1},
    "org_id": {"o1": 2, "o2": 1},
    "model": {"m1": 2, "m2": 1},
    "node": {"n1": 2, "n2": 1},
    "provider": {"": 2, "p1": 1},
    "status": {200: 2, 429: 1},
    "context_bucket": {"0-8k": 1, "8-32k": 1, "32-128k": 1},
}


@pytest.mark.parametrize("dim", USAGE_GROUP_BY)
def test_usage_group_by_each_dimension(tmp_path, dim):
    a = _varied(tmp_path)
    res = query.usage(a, start=T0, end=T0 + DAY, bucket="1h", group_by=[dim])
    counts: dict = {}
    for row in res["rows"]:
        counts[row["group"][dim]] = counts.get(row["group"][dim], 0) + row["requests"]
    assert counts == EXPECTED_GROUPS[dim]


def test_usage_missing_buckets_absent_and_nulls_kept(tmp_path):
    a = _varied(tmp_path)
    res = query.usage(a, start=T0, end=T0 + 4 * 3600, bucket="1h", group_by=["key_id"])
    assert sorted(r["bucket"] for r in res["rows"]) == [T0, T0, T0 + 3600]
    by_key = {r["group"]["key_id"]: r for r in res["rows"]}
    assert by_key["k1"]["cache_hit_rate"] == 0.5
    assert by_key["k2"]["cache_hit_rate"] is None  # not reported, not 0
    assert by_key["k2"]["reasoning_tokens"] is None
    assert by_key["k2"]["cost_basis"] == "unpriced" and by_key["k2"]["cost_usd"] is None
    assert by_key["k2"]["unpriced_requests"] == 1
    assert by_key["k3"]["cost_basis"] == "estimated" and by_key["k3"]["cost_usd"] == 0.5
    assert by_key["k1"]["spec_drafts"] is None and by_key["k1"]["spec_acceptance_rate"] is None
    filtered = query.usage(a, start=T0, end=T0 + DAY, bucket="1h", key_ids=["k3"], group_by=["model"])
    assert [r["requests"] for r in filtered["rows"]] == [1]
    assert query.usage(a, start=T0, end=T0 + DAY, bucket="1h", org_ids=[])["rows"] == []
    with pytest.raises(ValueError):
        query.usage(a, start=T0, end=T0 + DAY, bucket="1h", group_by=["colour"])
    text = query.usage_csv(res)
    parsed = list(csv.reader(io.StringIO(text)))
    assert parsed[0][:2] == ["bucket", "key_id"] and len(parsed) == 4


def test_usage_counts_the_open_day_and_hour(tmp_path):
    """A 1d/1h rollup is written only once its bucket closes. Today's traffic
    must still count: a new key's "last 30 days" read 0 until midnight."""
    a = Archive(tmp_path / "archive.db")
    _ingest(a, [_rec(request_id="y", ts=T0 - 3600, key_id="k1"), _rec(request_id="t", ts=T0 + 600, key_id="k1")])
    retention.compact(a, now=T0 + 1200)  # yesterday closed; today and this hour open
    day = query.usage(a, start=T0 - DAY, end=T0 + 1200, bucket="1d", group_by=["key_id"])
    assert {r["bucket"]: r["requests"] for r in day["rows"]} == {T0 - DAY: 1, T0: 1}
    hour = query.usage(a, start=T0, end=T0 + 1200, bucket="1h")
    assert [(r["bucket"], r["requests"]) for r in hour["rows"]] == [(T0, 1)]

# -- requests_list() ------------------------------------------------------------


def test_requests_list_pagination_and_filters(tmp_path):
    a = Archive(tmp_path / "a.db")
    recs = [
        _rec(request_id=f"r{i:02d}", ts=T0 + i, key_id="k1", served_name="m1" if i % 2 else "m2",
             status=200 if i % 5 else 502, error_class="" if i % 5 else "ConnectError",
             attempt_chain=[{"attempt": 0}])
        for i in range(25)
    ] + [_rec(request_id="other", ts=T0 + 3, key_id="k2", status=200)]
    _ingest(a, recs)

    def page(p=1, **kw):
        return query.requests_list(a, key_id="k1", start=T0, end=T0 + 100, page=p, page_size=10, **kw)

    p1, p3 = page(1), page(3)
    # Every fixture is a single-attempt request, so one row each.
    assert p1["total"] == 25 and len(p1["rows"]) == 10 and len(p3["rows"]) == 5
    assert p1["rows"][0]["request_id"] == "r24"  # newest first
    assert p1["rows"][0]["attempt_chain"] == [{"attempt": 0}]
    assert p1["rows"][0]["attempts"] == 1
    ids = {r["request_id"] for p in (1, 2, 3) for r in page(p)["rows"]}
    assert len(ids) == 25 and "other" not in ids
    assert page(status=502)["total"] == 5
    assert page(model="m1")["total"] == 12
    assert page(error="ConnectError")["total"] == 5
    assert query.requests_list(a, start=T0, end=T0 + 100)["total"] == 26
    text = query.requests_csv(p1["rows"])
    header = next(csv.reader(io.StringIO(text)))
    assert tuple(header) == REQUEST_RECORD_FIELDS


def _provider_history_fixture(a: Archive) -> None:
    records = [
        ("limited", "claude_code", "claude_code:sonnet", 429),
        ("sibling", "claude_code", "claude_code:opus", 429),
        ("other", "other", "other:sonnet", 429),
        ("healthy", "claude_code", "claude_code:sonnet", 200),
        ("prefix", "claude_code_extra", "claude_code_extra:sonnet", 429),
    ]
    _ingest(a, [
        _rec(request_id=rid, ts=T0 + i, served_name="shared", provider_id=provider,
             target_id=target, status=status)
        for i, (rid, provider, target, status) in enumerate(records)
    ] + [
        _rec(request_id="rerouted", ts=T0 + 10, served_name="shared",
             provider_id="claude_code", target_id="claude_code:sonnet",
             attempt_no=0, status=429),
        _rec(request_id="rerouted", ts=T0 + 11, served_name="shared",
             provider_id="other", target_id="other:sonnet",
             attempt_no=1, attempts=2, status=429),
    ])


def test_requests_list_provider_filter_matches_final_attempt(tmp_path):
    a = Archive(tmp_path / "a.db")
    _provider_history_fixture(a)

    def rows(**kw):
        return query.requests_list(a, start=T0, end=T0 + 100, **kw)

    filters = {"provider_id": "claude_code", "status": 429, "model": "shared"}
    result = rows(**filters)
    assert result["total"] == 2
    assert [r["request_id"] for r in result["rows"]] == ["sibling", "limited"]
    assert rows(**filters, target_id="claude_code:sonnet")["total"] == 1
    assert rows(**filters, retried=True)["total"] == 0
    assert rows(**filters, request_id="rerouted")["total"] == 0
    assert rows(provider_id="other", status=429, retried=True)["rows"][0]["attempts"] == 2
    assert rows(provider_id="claude_code")["total"] == 3
    assert rows(provider_id="claude_code' OR 1=1 --")["total"] == 0
    page = rows(**filters, page=2, page_size=1)
    assert page["total"] == 2 and page["rows"][0]["request_id"] == "limited"
    assert query.requests_list(
        a, start=T0 + 1, end=T0 + 100, **filters,
    )["total"] == 1


def test_requests_csv_neutralises_client_supplied_formulas():
    """`model_requested` and `user_agent` are whatever the client sent; a
    spreadsheet would run them."""
    row = {"model_requested": "=cmd()", "user_agent": "@SUM(A1)", "status": -1}
    out = dict(zip(REQUEST_RECORD_FIELDS, list(csv.reader(io.StringIO(query.requests_csv([row]))))[1]))
    assert out["model_requested"] == "'=cmd()"
    assert out["user_agent"] == "'@SUM(A1)"
    assert out["status"] == "-1"  # a number is not a formula


def _retried_fixture(a: Archive) -> None:
    _ingest(a, [
        _rec(request_id="r-1", ts=T0 + 10, key_id="k1", attempt_no=0, status=None,
             error_class="ConnectError", target_id="a"),
        _rec(request_id="r-1", ts=T0 + 10, key_id="k1", attempt_no=1, attempts=2,
             status=200, target_id="b"),
        _rec(request_id="r-2", ts=T0 + 20, key_id="k1", status=200, target_id="a"),
    ])


def test_requests_list_is_one_row_per_request_with_the_true_attempt_count(tmp_path):
    a = Archive(tmp_path / "a.db")
    _retried_fixture(a)

    def ids(**kw):
        res = query.requests_list(a, start=T0, end=T0 + 100, **kw)
        return [r["request_id"] for r in res["rows"]], res

    got, res = ids()
    assert res["total"] == 2 and sorted(got) == ["r-1", "r-2"]
    r1 = next(r for r in res["rows"] if r["request_id"] == "r-1")
    assert r1["attempt_no"] == 1 and r1["attempts"] == 2
    assert r1["status"] == 200 and r1["target_id"] == "b"
    assert tuple(r1) == REQUEST_RECORD_FIELDS
    assert ids(retried=True)[0] == ["r-1"]
    assert ids(request_id="r-2")[0] == ["r-2"]
    assert ids(status="failed")[0] == []
    assert sorted(ids(status="ok")[0]) == ["r-1", "r-2"]
    assert ids(target_id="a")[0] == ["r-2"]


def test_rollup_counts_final_attempts_only(tmp_path):
    a = Archive(tmp_path / "a.db")
    _retried_fixture(a)
    a.clear_reroll_pending()
    with a.lock:
        retention._roll(a.conn, a, T0, T0 + DAY, retention.STEP_1M)
    res = query.usage(a, start=T0, end=T0 + 3600, bucket="1m")
    assert sum(r["requests"] for r in res["rows"]) == 2
    assert sum(r["failed"] for r in res["rows"]) == 0
    assert all(r["errors"] == {} for r in res["rows"])
    by_status = query.usage(a, start=T0, end=T0 + 3600, bucket="1m", group_by=["status"])
    assert {r["group"]["status"] for r in by_status["rows"]} == {200}


def test_trace_records_the_serving_node_of_a_local_target():
    from types import SimpleNamespace as NS

    local = NS(target=NS(target_id="d1", kind="local"),
               deployment=NS(deployment_id="d1", plan=NS(node_ids=["spark-02", "spark-03"])))
    remote = NS(target=NS(target_id="p:m", kind="remote"), provider=NS(provider_id="p"))
    trace = RequestTrace(request_id="r", served_name="m", node_id="coord")
    assert trace.record(selection=local, status=200).node_id == "spark-02"
    assert trace.record(selection=remote, status=200).node_id == "coord"
    empty = NS(target=NS(target_id="d2", kind="local"), deployment=NS(deployment_id="d2", plan=NS(node_ids=[])))
    assert trace.record(selection=empty, status=200).node_id == "coord"


# -- engine metrics -----------------------------------------------------------

_METRICS = """# HELP vllm:spec_decode_num_drafts Number of spec decoding drafts.
# TYPE vllm:spec_decode_num_drafts counter
vllm:spec_decode_num_drafts_total{{engine="0",model_name="m"}} {d}
vllm:spec_decode_num_draft_tokens_total{{engine="0",model_name="m"}} {dt}
vllm:spec_decode_num_accepted_tokens_total{{engine="0",model_name="m"}} {a}
vllm:spec_decode_num_accepted_tokens_per_pos_total{{engine="0",model_name="m",position="0"}} {p0}
vllm:spec_decode_num_accepted_tokens_per_pos_total{{engine="0",model_name="m",position="1"}} {p1}
"""

_NO_SPEC = "# HELP vllm:num_requests_running x\nvllm:num_requests_running{engine=\"0\"} 0.0\n"


class _Dep:
    def __init__(self, dep_id, url, runtime="vllm"):
        self.deployment_id, self.backend_url, self.runtime = dep_id, url, runtime


def _poller(bodies: dict[str, list[str]], deps):
    clock = {"t": 100.0}

    def handler(request: httpx.Request) -> httpx.Response:
        queue = bodies[request.url.host]
        return httpx.Response(200, text=queue.pop(0) if len(queue) > 1 else queue[0])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    poller = EngineMetricsPoller(client, lambda: deps, interval_s=1.0, clock=lambda: clock["t"])
    return poller, clock, client


def test_poller_attribution_matrix():
    spec_bodies = [
        _METRICS.format(d=10.0, dt=30.0, a=15.0, p0=10.0, p1=5.0),
        _METRICS.format(d=14.0, dt=42.0, a=21.0, p0=14.0, p1=7.0),
    ]
    deps = [_Dep("spec", "http://spec:8000"), _Dep("plain", "http://plain:8000"), _Dep("sg", "http://sg:30000", "sglang")]
    poller, clock, client = _poller({"spec": spec_bodies, "plain": [_NO_SPEC], "sg": [_NO_SPEC]}, deps)

    async def go():
        await poller.poll_once()
        clock["t"] = 102.0
        await poller.poll_once()
        await client.aclose()

    asyncio.run(go())
    assert poller.attribute("spec", 100.5, 101.5, alone=True) == SpecSample(4, 12, 6, [4, 2])
    assert poller.attribute("spec", 100.5, 101.5, alone=False) == FieldError.AMBIGUOUS_CONCURRENCY
    assert poller.attribute("spec", 99.0, 101.5, alone=True) == FieldError.NOT_REPORTED  # nothing before start
    assert poller.attribute("spec", 100.5, 103.0, alone=True) == FieldError.NOT_REPORTED  # nothing after end
    assert poller.attribute("plain", 100.5, 101.5, alone=True) == FieldError.NOT_ENABLED
    assert poller.attribute("sg", 100.5, 101.5, alone=True) == FieldError.NOT_REPORTED
    assert poller.attribute("unknown", 100.5, 101.5, alone=True) == FieldError.NOT_REPORTED


def test_poller_never_raises_on_transport_error():
    def boom(request):
        raise httpx.ConnectError("down")

    async def go():
        client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
        p = EngineMetricsPoller(client, lambda: [_Dep("d", "http://d:1")])
        await p.poll_once()
        await client.aclose()
        return p.attribute("d", 0, 1, alone=True)

    assert asyncio.run(go()) == FieldError.NOT_REPORTED


def test_a_hostile_position_label_is_bounded():
    """One line from a worker used to allocate a list as long as its label."""
    import time

    from control_plane.telemetry.engine_metrics import MAX_SPEC_POSITIONS, parse_spec

    text = (
        'vllm:spec_decode_num_accepted_tokens_per_pos_total{position="0"} 3\n'
        'vllm:spec_decode_num_accepted_tokens_per_pos_total{position="4000000000"} 1\n'
    )
    started = time.monotonic()
    sample = parse_spec(text)
    assert time.monotonic() - started < 1.0
    assert sample is not None
    assert len(sample.accepted_per_position) <= MAX_SPEC_POSITIONS


def test_an_oversized_metrics_body_is_ignored():
    body = _METRICS.format(d=10.0, dt=30.0, a=15.0, p0=10.0, p1=5.0) + "#" * (5 << 20)
    deps = [_Dep("big", "http://big:8000")]
    poller, _clock, client = _poller({"big": [body]}, deps)

    async def go():
        await poller.poll_once()
        await client.aclose()

    asyncio.run(go())
    assert not poller._tracks["big"].samples
