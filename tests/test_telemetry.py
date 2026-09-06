"""Durable telemetry: journal, collection, archive, rollups, retention.

No network and no GPU. The one thing every test here is really guarding is
that a telemetry failure stays a telemetry failure: nothing in this subsystem
may block a caller, break a request, or leak a key.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import threading
import time

import pytest

from control_plane.deploy.events import FIT_MISS, EventBus
from control_plane.telemetry import RequestRecord, RequestTrace
from control_plane.telemetry.archive import Archive
from control_plane.telemetry.events import SOURCE_DEPLOY, GatewayEvents, journal_events
from control_plane.telemetry.hist import Hist, merged
from control_plane.telemetry.journal import Journal
from control_plane.telemetry.loghandler import install, uninstall
from control_plane.telemetry.query import nodes as q_nodes
from control_plane.telemetry.query import pick_step, requests as q_requests
from control_plane.telemetry.query import resolve_window
from control_plane.telemetry.retention import STEP_1H, STEP_1M, compact
from control_plane.telemetry.service import Telemetry


@pytest.fixture
def journal(tmp_path):
    j = Journal(tmp_path / "journal.db", node_id="spark-01")
    j.start()
    yield j
    j.close()


@pytest.fixture
def archive(tmp_path):
    a = Archive(tmp_path / "archive.db")
    yield a
    a.close()


def drain(journal: Journal, timeout: float = 5.0) -> None:
    """Block until everything appended is committed and readable."""
    assert journal.sync(timeout), "the journal writer did not settle"


# ---------------------------------------------------------------------------
# journal
# ---------------------------------------------------------------------------


def test_a_row_survives_the_process_that_wrote_it(tmp_path):
    """The whole point: a restart must not lose what came before it."""
    first = Journal(tmp_path / "j.db", node_id="spark-01")
    first.start()
    first.append("sample", {"ts": 1.0, "power_w": 71.0})
    first.close()

    second = Journal(tmp_path / "j.db")
    assert second.node_id == "spark-01", "node identity is persisted, not re-derived"
    rows = second.read(0, 10)["rows"]
    assert len(rows) == 1
    assert json.loads(rows[0]["body"])["power_w"] == 71.0


def test_appending_never_blocks_the_caller(journal):
    """The request path is a caller. It may not wait on a disk."""
    journal.append("sample", {"ts": 1.0})  # prime any lazy setup
    began = time.monotonic()
    for i in range(5000):
        journal.append("request", {"request_id": f"r-{i}", "ts": float(i)})
    elapsed = time.monotonic() - began
    assert elapsed < 0.5, f"5000 appends took {elapsed:.2f}s"


def test_overflow_drops_the_oldest_and_counts_it(tmp_path):
    """Drop-oldest, like EventBus. A stalled producer is worse than a gap."""
    j = Journal(tmp_path / "j.db", node_id="n", queue_max=100)
    # Deliberately not started: with no writer thread the buffer must overflow
    # rather than grow without bound.
    for i in range(250):
        j.append("sample", {"i": i})
    assert j.stats()["queued"] == 100
    assert j.stats()["dropped"] == 150
    j.flush()
    kept = [json.loads(r["body"])["i"] for r in j.read(0, 500)["rows"]]
    assert kept == list(range(150, 250)), "the newest rows are the ones kept"


def test_reading_acknowledges_and_lets_the_journal_trim(journal):
    for i in range(10):
        journal.append("sample", {"i": i})
    drain(journal)
    assert journal.stats()["shipped_hwm"] == 0
    journal.read(since=4, limit=10)
    assert journal.stats()["shipped_hwm"] == 4, "asking for >4 acknowledges 1..4"


def test_trim_keeps_rows_that_were_never_collected(tmp_path):
    now = [1000.0]
    j = Journal(tmp_path / "j.db", node_id="n", retention_s=10.0, clock=lambda: now[0])
    for i in range(10):
        j.append("sample", {"i": i})
    j.flush()
    now[0] += 3600.0  # every row is now far past the retention horizon
    assert j.trim() == 0, "uncollected rows outlive their retention window"
    j.read(since=5, limit=10)  # acknowledge the first five
    assert j.trim() == 5
    assert [json.loads(r["body"])["i"] for r in j.read(0, 20)["rows"]] == list(range(5, 10))


def test_paging_is_stable_and_covers_every_row(journal):
    for i in range(1000):
        journal.append("sample", {"i": i})
    drain(journal)
    seen, cursor = [], 0
    while True:
        page = journal.read(cursor, limit=100)
        if not page["rows"]:
            break
        seen.extend(json.loads(r["body"])["i"] for r in page["rows"])
        cursor = page["next"]
    assert seen == list(range(1000))


# ---------------------------------------------------------------------------
# archive and collection
# ---------------------------------------------------------------------------


def _rows(n, kind="sample", ts=1000.0, start_seq=1):
    return [
        {
            "seq": start_seq + i,
            "ts": ts + i,
            "kind": kind,
            "body": json.dumps({"ts": ts + i, "power_w": 70.0 + i, "i": i}),
        }
        for i in range(n)
    ]


def test_ingest_is_idempotent_under_replay(archive):
    payload = {"rows": _rows(50), "next": 50, "head": 50, "dropped": 0}
    archive.ingest("spark-01", payload)
    archive.ingest("spark-01", payload)
    archive.ingest("spark-01", payload)
    assert archive.status()["rows"]["samples"] == 50
    assert archive.cursor_for("spark-01") == 50


def test_a_cursor_never_goes_backwards(archive):
    archive.ingest("n", {"rows": _rows(10), "next": 10, "head": 10})
    archive.ingest("n", {"rows": [], "next": 3, "head": 10})
    assert archive.cursor_for("n") == 10


def test_one_unreachable_node_does_not_stop_the_others(tmp_path, archive):
    from control_plane.telemetry.collector import Collector

    good = Journal(tmp_path / "good.db", node_id="good")
    good.start()
    for i in range(20):
        good.append("sample", {"i": i})
    drain(good)

    class Dead:
        async def get_json(self, url, timeout):
            raise ConnectionError("no route to host")

    collector = Collector(
        archive, client=Dead(), agents=lambda: {"dead": "http://10.0.0.9:8081"}
    )
    collector.add_local("good", good)
    collected = asyncio.run(collector.poll_once())
    good.close()

    assert collected["good"] == 20
    by_node = {n["node_id"]: n for n in archive.status()["nodes"]}
    assert by_node["good"]["last_seq"] == 20
    assert "ConnectionError" in by_node["dead"]["last_error"]


def test_a_backlog_drains_over_several_capped_polls(tmp_path, archive):
    from control_plane.telemetry.collector import Collector

    j = Journal(tmp_path / "j.db", node_id="worker")
    j.start()
    for i in range(5000):
        j.append("sample", {"ts": 1000.0 + i, "i": i})
    drain(j)

    calls = []

    class Agent:
        async def get_json(self, url, timeout):
            calls.append(url)
            params = dict(p.split("=") for p in url.split("?", 1)[1].split("&"))
            return j.read(int(params["since"]), int(params["limit"]))

    collector = Collector(
        archive,
        client=Agent(),
        agents=lambda: {"worker": "http://10.0.0.2:8081"},
        max_rows=1000,
    )
    total = asyncio.run(collector.poll_once())
    j.close()
    assert total["worker"] == 5000
    assert len(calls) == 6, "five full pages plus the one that came up short"
    assert archive.status()["rows"]["samples"] == 5000


# ---------------------------------------------------------------------------
# rollups
# ---------------------------------------------------------------------------


def _fill(archive, *, hours=2, seed=3):
    rng = random.Random(seed)
    now = 1_700_000_000.0
    start = now - hours * 3600
    rows, seq = [], 0
    for i in range(hours * 3600):
        seq += 1
        ts = start + i
        rows.append(
            {
                "seq": seq,
                "ts": ts,
                "kind": "sample",
                "body": json.dumps(
                    {
                        "memory_used": 100 + i,
                        "memory_total": 1000,
                        "power_w": 70.0 + (i % 10),
                        "temp_c": 60.0,
                        "util_pct": 50.0,
                        "gpu_memory_used": 50,
                        "host_memory_available": 900,
                        "swap_used": 0,
                    }
                ),
            }
        )
        if i % 4 == 0:
            seq += 1
            rows.append(
                {
                    "seq": seq,
                    "ts": ts,
                    "kind": "request",
                    "body": json.dumps(
                        {
                            "request_id": f"r-{i}",
                            "served_name": "gpt-oss-120b",
                            "target_id": "d-1",
                            "status": 200,
                            "tokens": 10,
                            "prompt_tokens": 5,
                            "duration_ms": rng.lognormvariate(4, 1),
                            "ttft_ms": rng.lognormvariate(3, 1),
                        }
                    ),
                }
            )
    archive.ingest("spark-01", {"rows": rows, "next": seq, "head": seq})
    return now


def test_hourly_averages_are_weighted_by_their_minutes(archive):
    now = _fill(archive)
    compact(archive, now=now)
    for hour in archive.conn.execute(
        "SELECT bucket, n, power_w_avg FROM rollup_samples WHERE step = ?", (STEP_1H,)
    ).fetchall():
        lo, hi = hour["bucket"], hour["bucket"] + STEP_1H
        raw = archive.conn.execute(
            "SELECT COUNT(*) AS n, AVG(power_w) AS a FROM samples WHERE ts >= ? AND ts < ?",
            (lo, hi),
        ).fetchone()
        assert hour["n"] == raw["n"]
        assert hour["power_w_avg"] == pytest.approx(raw["a"])


def test_an_hourly_percentile_is_exact_not_an_average_of_percentiles(archive):
    """The property the histogram blobs exist for."""
    now = _fill(archive)
    compact(archive, now=now)
    hours = archive.conn.execute(
        "SELECT bucket, n, duration_hist FROM rollup_requests WHERE step = ?", (STEP_1H,)
    ).fetchall()
    assert hours, "nothing rolled"
    for hour in hours:
        lo, hi = hour["bucket"], hour["bucket"] + STEP_1H
        truth = Hist()
        for row in archive.conn.execute(
            "SELECT duration_ms FROM requests WHERE ts >= ? AND ts < ?", (lo, hi)
        ):
            truth.add(row["duration_ms"])
        rolled = Hist.from_blob(hour["duration_hist"])
        assert rolled.buckets == truth.buckets
        assert rolled.pct(0.99) == truth.pct(0.99)
        assert hour["n"] == truth.count


def test_histograms_merge_exactly_through_their_storage_form():
    rng = random.Random(11)
    values = [rng.lognormvariate(3, 1) for _ in range(20_000)]
    whole = Hist()
    for v in values:
        whole.add(v)
    parts = []
    for offset in range(60):
        part = Hist()
        for v in values[offset::60]:
            part.add(v)
        parts.append(Hist.from_blob(part.to_blob()))
    assert merged(parts).buckets == whole.buckets


def test_a_late_arriving_backlog_is_rolled_not_missed(archive):
    """A worker unreachable for an hour delivers into already-rolled buckets."""
    now = _fill(archive, hours=1)
    compact(archive, now=now)
    before = archive.conn.execute(
        "SELECT SUM(n) AS n FROM rollup_samples WHERE step = ?", (STEP_1M,)
    ).fetchone()["n"]

    late = [
        {
            "seq": 900_000 + i,
            "ts": now - 3600 + i,
            "kind": "sample",
            "body": json.dumps({"power_w": 1.0, "memory_used": 1}),
        }
        for i in range(30)
    ]
    archive.ingest("spark-02", {"rows": late, "next": 900_030, "head": 900_030})
    compact(archive, now=now)
    after = archive.conn.execute(
        "SELECT SUM(n) AS n FROM rollup_samples WHERE step = ?", (STEP_1M,)
    ).fetchone()["n"]
    assert after == before + 30, "late rows were rolled, not silently dropped"


def test_retention_deletes_past_the_horizon(archive, monkeypatch):
    from control_plane.telemetry import config as tconfig

    monkeypatch.setattr(tconfig, "SAMPLES_RAW_RETENTION_S", 1800.0)
    now = _fill(archive, hours=2)
    compact(archive, now=now)
    oldest = archive.conn.execute("SELECT MIN(ts) AS t FROM samples").fetchone()["t"]
    assert oldest >= now - 1800.0
    # The rollups outlive the raw rows they summarise. That is the trade.
    assert archive.conn.execute(
        "SELECT COUNT(*) AS n FROM rollup_samples WHERE bucket < ?", (now - 1800.0,)
    ).fetchone()["n"] > 0


# ---------------------------------------------------------------------------
# query
# ---------------------------------------------------------------------------


def test_auto_step_never_asks_for_a_million_rows():
    now = time.time()
    assert pick_step("auto", now - 1800, now) == "raw"
    assert pick_step("auto", now - 3 * 86400, now) == "1m"
    assert pick_step("auto", now - 200 * 86400, now) == "1h"
    assert pick_step("raw", now - 200 * 86400, now) == "raw", "an explicit ask wins"


def test_relative_windows():
    now = time.time()
    assert resolve_window("-1h")[0] == pytest.approx(now - 3600, abs=2)
    assert resolve_window("2d")[0] == pytest.approx(now - 172800, abs=2)


def test_a_query_says_which_resolution_answered_it(archive):
    now = _fill(archive)
    compact(archive, now=now)
    raw = q_nodes(archive, from_ts=now - 3600, to_ts=now, step="raw")
    assert raw["resolution"] == "raw"
    rolled = q_requests(archive, from_ts=now - 3600, to_ts=now, step="1m")
    assert rolled["resolution"] == "1m"
    assert "ttft" in rolled["requests"][0], "rollups answer in percentiles"


def test_a_trimmed_window_reads_as_trimmed_not_as_quiet(archive):
    """A flat line has two causes and they must be distinguishable."""
    now = 1_700_000_000.0
    archive.note_gap("spark-01", now - 600, now - 300, "archive_size_cap")
    answer = q_nodes(archive, from_ts=now - 3600, to_ts=now, step="raw")
    assert answer["samples"] == []
    assert answer["gaps"] and answer["gaps"][0]["reason"] == "archive_size_cap"


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


def test_every_event_reaches_the_journal_even_under_burst(journal):
    """A tap, not a subscriber: the subscriber queue drops its oldest."""
    bus = EventBus()
    journal_events(bus, journal, SOURCE_DEPLOY)

    def burst():
        for i in range(500):
            bus.emit(FIT_MISS, deployment_id=f"d-{i}", predicted_total=i)

    threads = [threading.Thread(target=burst) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    drain(journal)
    rows = [r for r in journal.read(0, 5000)["rows"] if r["kind"] == "event"]
    assert len(rows) == 2000


def test_a_broken_tap_cannot_break_event_delivery(journal):
    bus = EventBus()
    bus.add_tap(lambda event: 1 / 0)
    journal_events(bus, journal, SOURCE_DEPLOY)
    bus.emit("state_changed", deployment_id="d-1")
    drain(journal)
    assert len(journal.read(0, 10)["rows"]) == 1


def test_gateway_events_carry_their_source(journal):
    events = GatewayEvents(sink=journal)
    events.breaker_opened("d-1", failures=3, cooldown_s=30.0)
    drain(journal)
    body = json.loads(journal.read(0, 10)["rows"][0]["body"])
    assert body["source"] == "gateway"
    assert body["type"] == "breaker_opened"
    assert body["target_id"] == "d-1"


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------


class _FakeRedactor:
    def __init__(self, secret):
        self.secret = secret

    def scrub(self, text):
        return text.replace(self.secret, "***")


def test_a_key_never_reaches_the_journal(journal):
    """The redaction backstop the gateway loggers have never had."""
    secret = "sk-live-0123456789abcdefghijklmnop"
    handler = install(journal, level="INFO", redactor=_FakeRedactor(secret))
    assert handler is not None
    try:
        logging.getLogger("gateway.proxy").error("auth failed for %s", secret)
        drain(journal)
        blob = json.dumps(journal.read(0, 100)["rows"])
        assert secret not in blob
        assert "***" in blob
    finally:
        uninstall()


def test_debug_stays_local_and_the_journal_never_logs_itself(journal):
    install(journal, level="INFO")
    logging.getLogger().setLevel(logging.DEBUG)
    try:
        logging.getLogger("gateway.proxy").debug("chatty")
        logging.getLogger("gateway.proxy").warning("worth keeping")
        logging.getLogger("control_plane.telemetry.journal").warning("would loop")
        drain(journal)
        kept = [json.loads(r["body"]) for r in journal.read(0, 100)["rows"]]
        assert [r["message"] for r in kept] == ["worth keeping"]
    finally:
        uninstall()


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------


def test_a_trace_becomes_a_record_with_milliseconds():
    trace = RequestTrace(
        request_id="r-1", served_name="m", streaming=True, body_bytes=42
    )
    record = trace.record(
        attempt_no=1, tokens=12, ttft_s=0.142, duration_s=1.5, status=200
    )
    assert isinstance(record, RequestRecord)
    assert record.ttft_ms == pytest.approx(142.0)
    assert record.duration_ms == pytest.approx(1500.0)
    assert record.attempts == 2, "attempts covers the attempt being recorded"


def test_a_record_survives_a_selection_it_does_not_understand():
    """Duck-typed on purpose: a stub selection must not raise."""
    trace = RequestTrace(request_id="r-1", served_name="m")
    record = trace.record(selection=object(), status=200)
    assert record.target_id == ""


# ---------------------------------------------------------------------------
# the switch
# ---------------------------------------------------------------------------


def test_telemetry_is_off_when_there_is_nowhere_to_write(tmp_path):
    missing = tmp_path / "nope"
    telemetry = Telemetry.from_env(root=missing)
    assert not telemetry.enabled
    assert "does not exist" in telemetry.reason
    # And the no-op sink still satisfies every producer.
    telemetry.sink.sample("n", {"ts": 1.0})
    telemetry.sink.request(RequestRecord(request_id="r", ts=1.0, served_name="m"))
    telemetry.sink.event("gateway", {"type": "x"})
    telemetry.sink.log({"level": "INFO"})
    assert telemetry.status() == {"enabled": False, "reason": telemetry.reason}


def test_the_env_switch_turns_it_off(tmp_path, monkeypatch):
    monkeypatch.setenv("SPARKPLANE_TELEMETRY", "0")
    assert not Telemetry.from_env(root=tmp_path).enabled


def test_an_enabled_bundle_records_and_stops_cleanly(tmp_path):
    telemetry = Telemetry.open(tmp_path, node_id="spark-01")
    try:
        assert telemetry.enabled
        telemetry.sink.event("gateway", {"type": "x"})
        drain(telemetry.journal)
        assert telemetry.status()["journal"]["rows"] == 1
    finally:
        asyncio.run(telemetry.stop())


# ---------------------------------------------------------------------------
# cost
# ---------------------------------------------------------------------------


def test_the_writer_batches_instead_of_committing_per_row(tmp_path):
    """The regression the load harness caught.

    append() used to set the writer's wake event on every row, so under load
    the writer committed one transaction per request and burned three times
    the CPU. Waking only on a full batch is what makes batch_rows mean
    anything; the interval timer still covers a quiet node.
    """
    j = Journal(tmp_path / "j.db", node_id="n", batch_rows=100)
    try:
        for _ in range(99):
            j.append("sample", {"ts": 1.0})
        assert not j._wake.is_set(), "a partial batch must not wake the writer"
        j.append("sample", {"ts": 1.0})
        assert j._wake.is_set(), "a full batch must"
    finally:
        j.close()


def test_a_quiet_node_still_persists_without_filling_a_batch(tmp_path):
    j = Journal(tmp_path / "j.db", node_id="n", batch_rows=1000, batch_interval_s=0.05)
    j.start()
    try:
        j.append("sample", {"ts": 1.0})  # nowhere near a full batch
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if j.read(0, 10)["rows"]:
                break
            time.sleep(0.02)
        assert j.read(0, 10)["rows"], "the interval timer must commit a partial batch"
    finally:
        j.close()


def test_rows_are_not_lost_when_a_write_fails(tmp_path, monkeypatch):
    """A batch is taken from the buffer before it is committed. If the commit
    fails, those rows must go back rather than vanish."""
    import sqlite3

    from control_plane.telemetry import journal as journal_module

    j = Journal(tmp_path / "j.db", node_id="n")
    for i in range(10):
        j.append("sample", {"i": i})

    real_connect = journal_module._connect
    state = {"fail": True}

    class FailsOnce:
        """A connection that refuses its first executemany, then behaves."""

        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def executemany(self, *args, **kwargs):
            if state["fail"]:
                state["fail"] = False
                raise sqlite3.OperationalError("disk I/O error")
            return self._conn.executemany(*args, **kwargs)

    monkeypatch.setattr(
        journal_module, "_connect", lambda *a, **kw: FailsOnce(real_connect(*a, **kw))
    )
    with pytest.raises(sqlite3.OperationalError):
        j.flush()
    monkeypatch.undo()

    j.flush()
    assert [json.loads(r["body"])["i"] for r in j.read(0, 50)["rows"]] == list(range(10))
    j.close()


def test_recording_a_request_is_cheap_enough_for_the_request_path(tmp_path):
    """A budget, not a benchmark: this runs inside proxy.settle()."""
    j = Journal(tmp_path / "j.db", node_id="n")
    j.start()
    try:
        trace = RequestTrace(request_id="r-1", served_name="m")
        record = trace.record(tokens=42, ttft_s=0.014, duration_s=0.12, status=200)
        j.request(record)  # warm any lazy import
        began = time.perf_counter()
        for _ in range(2000):
            j.request(record)
        per_call_us = (time.perf_counter() - began) / 2000 * 1e6
        assert per_call_us < 100, f"{per_call_us:.1f} us per recorded request"
    finally:
        j.close()


def test_a_worker_journal_reaches_the_coordinator_over_the_agent_api(tmp_path, archive):
    """The shipping leg, through the real /agent/journal route.

    A worker records while nobody is collecting, and the coordinator drains the
    backlog when it arrives. That is the case the journal exists for: there is
    no coordinator failover, so whatever a worker does not keep is lost.
    """
    from fastapi.testclient import TestClient

    from control_plane.registry.agent import NodeAgent, create_agent_app
    from control_plane.telemetry.collector import Collector
    import tests.fixtures as fixtures

    worker_profile = fixtures.NODE_PROFILES["spark-02"]

    worker_journal = Journal(tmp_path / "worker.db", node_id=worker_profile.node_id)
    worker_journal.start()
    agent = NodeAgent(worker_profile, sink=worker_journal)

    # The worker records with no coordinator listening.
    for i in range(120):
        worker_journal.append("sample", {"ts": 1000.0 + i, "power_w": 70.0 + i % 5})
    drain(worker_journal)

    with TestClient(create_agent_app(agent)) as http:
        direct = http.get("/agent/journal", params={"since": 0, "limit": 10})
        assert direct.status_code == 200
        assert len(direct.json()["rows"]) == 10

        class ViaHttp:
            """The coordinator's AgentClient, backed by the real route."""

            async def get_json(self, url, timeout):
                path, _, query = url.partition("?")
                params = dict(p.split("=") for p in query.split("&"))
                reply = http.get(path, params=params)
                reply.raise_for_status()
                return reply.json()

        collector = Collector(
            archive,
            client=ViaHttp(),
            agents=lambda: {worker_profile.node_id: "http://worker:8081"},
            max_rows=25,
        )
        collected = asyncio.run(collector.poll_once())

    worker_journal.close()
    assert collected[worker_profile.node_id] == 120
    assert archive.status()["rows"]["samples"] == 120
    node = archive.status()["nodes"][0]
    assert node["behind"] == 0, "the coordinator caught up with the worker's head"
