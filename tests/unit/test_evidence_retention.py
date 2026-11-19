"""3.6: evidence retention is its own knob, and survives telemetry being off.

``requests`` and ``access`` keep ``DERATE_REQUESTS_RETENTION_DAYS`` (default
90) whatever ``DERATE_TELEMETRY_RETENTION_DAYS`` says, the size cap drops
telemetry days before any evidence day, and ``DERATE_TELEMETRY=0`` still
journals request and access rows.
"""

from __future__ import annotations

import asyncio
import json

from control_plane.telemetry import retention
from control_plane.telemetry.archive import Archive
from control_plane.telemetry.records import (
    KIND_ACCESS,
    KIND_EVENT,
    KIND_REQUEST,
    KIND_SAMPLE,
    AccessRecord,
    RecordsOnlySink,
    RequestRecord,
)
from control_plane.telemetry.service import Telemetry

DAY = 86400.0
NOW = 1_800_000_000.0


def _seed(archive: Archive, age_days: float) -> None:
    ts = NOW - age_days * DAY
    rows = [
        {"kind": KIND_SAMPLE, "seq": 1, "ts": ts, "body": json.dumps({"power_w": 1.0})},
        {"kind": KIND_REQUEST, "seq": 2, "ts": ts,
         "body": json.dumps({"request_id": f"r-{age_days}", "served_name": "m"})},
        {"kind": KIND_ACCESS, "seq": 3, "ts": ts,
         "body": json.dumps(AccessRecord(request_id=f"a-{age_days}", ts=ts, path="/x").as_dict())},
    ]
    archive.ingest("n1", {"rows": rows, "next": 3, "head": 3}, trusted=True)


def _count(archive: Archive, table: str) -> int:
    return archive.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_evidence_horizon_is_not_scaled_by_telemetry_retention(tmp_path, monkeypatch):
    monkeypatch.setenv("DERATE_TELEMETRY_RETENTION_DAYS", "1")
    monkeypatch.delenv("DERATE_REQUESTS_RETENTION_DAYS", raising=False)
    archive = Archive(tmp_path / "a.db")
    _seed(archive, 10)
    with archive.lock:
        retention._expire(archive.conn, NOW)
    assert _count(archive, "samples") == 0
    assert _count(archive, "requests") == 1
    assert _count(archive, "access") == 1
    archive.close()


def test_requests_retention_days_governs_requests_and_access(tmp_path, monkeypatch):
    monkeypatch.setenv("DERATE_REQUESTS_RETENTION_DAYS", "5")
    archive = Archive(tmp_path / "a.db")
    _seed(archive, 10)
    with archive.lock:
        retention._expire(archive.conn, NOW)
    assert _count(archive, "requests") == 0
    assert _count(archive, "access") == 0
    archive.close()


def test_size_cap_drops_telemetry_before_evidence(tmp_path, monkeypatch):
    archive = Archive(tmp_path / "a.db")
    _seed(archive, 3)
    _seed(archive, 2)

    # "Too big" for as long as any telemetry row is left, then it fits.
    def file_bytes(conn):
        return 0 if _count(archive, "samples") == 0 else 1 << 60

    monkeypatch.setattr(retention, "_file_bytes", file_bytes)
    with archive.lock:
        retention._enforce_size(archive, NOW)
    assert _count(archive, "samples") == 0
    assert _count(archive, "requests") == 2
    assert _count(archive, "access") == 2
    archive.close()


def test_telemetry_off_still_records_requests_and_access(tmp_path, monkeypatch):
    monkeypatch.setenv("DERATE_TELEMETRY", "0")
    telemetry = Telemetry.from_env(root=tmp_path, node_id="n1")
    try:
        assert isinstance(telemetry.sink, RecordsOnlySink)
        assert telemetry.status()["enabled"] is False
        telemetry.sink.sample("n1", {"ts": NOW, "power_w": 1.0})
        telemetry.sink.event("deploy", {"type": "x"})
        telemetry.sink.request(RequestRecord(request_id="r-1", ts=NOW, served_name="m"))
        telemetry.sink.access(AccessRecord(request_id="a-1", ts=NOW, path="/api/keys"))
        telemetry.journal.flush()
        kinds = sorted(r["kind"] for r in telemetry.journal.read(0, 100, 1 << 20)["rows"])
        assert kinds == [KIND_ACCESS, KIND_REQUEST]
        # The history screens answer "off"; the evidence archive is private.
        assert telemetry.archive is None and not telemetry.enabled
    finally:
        asyncio.run(telemetry.stop())


def _event_row(seq: int, ts: float, source: str, kind: str = "x") -> dict:
    return {"kind": KIND_EVENT, "seq": seq, "ts": ts,
            "body": json.dumps({"source": source, "type": kind, "peer": "10.0.0.66"})}


def test_size_cap_keeps_agent_and_shell_events_through_the_telemetry_pass(tmp_path, monkeypatch):
    """N-5: the agent's record is evidence, not telemetry."""
    archive = Archive(tmp_path / "a.db")
    old = NOW - 3 * DAY
    archive.ingest("n1", {"rows": [
        {"kind": KIND_SAMPLE, "seq": 1, "ts": old, "body": json.dumps({"power_w": 1.0})},
        _event_row(2, old, "deploy"),
        _event_row(3, old, "agent", "process_killed"),
        _event_row(4, old, "shell", "shell_opened"),
        {"kind": KIND_REQUEST, "seq": 5, "ts": NOW - DAY,
         "body": json.dumps({"request_id": "r-1", "served_name": "m"})},
    ], "next": 5, "head": 5}, trusted=True)

    def file_bytes(conn):
        telemetry = _count(archive, "samples") + archive.conn.execute(
            "SELECT COUNT(*) FROM events WHERE source NOT IN ('agent', 'shell')"
        ).fetchone()[0]
        return 0 if telemetry == 0 else 1 << 60

    monkeypatch.setattr(retention, "_file_bytes", file_bytes)
    with archive.lock:
        retention._enforce_size(archive, NOW)
    sources = sorted(r[0] for r in archive.conn.execute("SELECT source FROM events"))
    assert sources == ["agent", "shell"]
    assert _count(archive, "samples") == 0
    archive.close()


def test_size_cap_drops_anonymous_access_before_attributed_rows(tmp_path, monkeypatch):
    """EV-1: a flood of anonymous requests cannot evict the attributed record."""
    archive = Archive(tmp_path / "a.db")
    old = NOW - 3 * DAY
    rows = [
        {"kind": KIND_ACCESS, "seq": 1, "ts": old, "body": json.dumps(
            AccessRecord(request_id="a-anon", ts=old, path="/api/keys").as_dict())},
        {"kind": KIND_ACCESS, "seq": 2, "ts": old, "body": json.dumps(
            AccessRecord(request_id="a-root", ts=old, who="session:root", path="/api/keys").as_dict())},
        {"kind": KIND_ACCESS, "seq": 3, "ts": NOW - DAY, "body": json.dumps(
            AccessRecord(request_id="a-anon-new", ts=NOW - DAY, path="/healthz").as_dict())},
    ]
    archive.ingest("n1", {"rows": rows, "next": 3, "head": 3}, trusted=True)

    def file_bytes(conn):
        anonymous = archive.conn.execute(
            "SELECT COUNT(*) FROM access WHERE who = '-'"
        ).fetchone()[0]
        return 0 if anonymous == 0 else 1 << 60

    monkeypatch.setattr(retention, "_file_bytes", file_bytes)
    with archive.lock:
        retention._enforce_size(archive, NOW)
    kept = sorted(r[0] for r in archive.conn.execute("SELECT request_id FROM access"))
    assert kept == ["a-root"]
    archive.close()


def test_telemetry_off_still_journals_agent_events(tmp_path, monkeypatch):
    """EV-2: DERATE_TELEMETRY=0 must not switch off the agent's record."""
    from control_plane.contracts import DeviceClass, NodeProfile
    from control_plane.registry.agent import NodeAgent

    monkeypatch.setenv("DERATE_TELEMETRY", "0")
    telemetry = Telemetry.from_env(root=tmp_path, node_id="n1")
    try:
        agent = NodeAgent(
            NodeProfile(
                node_id="n1", hostname="n1", address="127.0.0.1",
                device_class=DeviceClass.GB10, gpu_name="GB10", gpu_count=1,
                total_memory=1 << 30, addressable_memory=1 << 30,
                memory_bandwidth_gbps=1.0, compute_capability="12.1",
                driver_version="1",
            ),
            sink=telemetry.sink,
            token="t",
        )
        agent.note_action("process_killed", "10.0.0.66", "/agent/processes/1/kill", pid=1)
        agent.note_shell_opened(host=False)
        telemetry.sink.event("deploy", {"type": "not_evidence"})
        telemetry.journal.flush()
        rows = telemetry.journal.read(0, 100, 1 << 20)["rows"]
        events = [json.loads(r["body"]) for r in rows if r["kind"] == KIND_EVENT]
        assert sorted(e["source"] for e in events) == ["agent", "shell"]
    finally:
        asyncio.run(telemetry.stop())
