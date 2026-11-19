"""N-6 and N-5: what a member's journal may put in the coordinator's archive.

Only the coordinator's own journal writes ``/v1`` (``requests``) and
``access`` rows: a member shipping them is dropped and counted, never
presented by forensics as the gateway's. And a member's raw ``logs`` and
``events`` are bounded per node per day, so one rogue node cannot push the
whole archive into its size cap.
"""

from __future__ import annotations

import asyncio
import io
import json

from control_plane import forensics
from control_plane.telemetry import config
from control_plane.telemetry.archive import Archive
from control_plane.telemetry.collector import Collector
from control_plane.telemetry.journal import Journal
from control_plane.telemetry.records import (
    KIND_ACCESS,
    KIND_LOG,
    KIND_REQUEST,
    KIND_SAMPLE,
    AccessRecord,
    RequestRecord,
)

NOW = 1_800_000_000.0


def _forged_page() -> dict:
    rows = [
        {"kind": KIND_SAMPLE, "seq": 1, "ts": NOW, "body": json.dumps({"power_w": 3.0})},
        {"kind": KIND_REQUEST, "seq": 2, "ts": NOW, "body": json.dumps({
            "request_id": "r-deadbeef0001", "key_id": "victim",
            "client_ip": "203.0.113.9", "status": 200,
        })},
        {"kind": KIND_ACCESS, "seq": 3, "ts": NOW, "body": json.dumps(
            AccessRecord(request_id="a-forged", ts=NOW, client_ip="203.0.113.9",
                         who="key:drt_victim", method="POST", path="/v1/chat/completions").as_dict()
        )},
    ]
    return {"rows": rows, "next": 3, "head": 3, "dropped": 0}


def test_a_members_journal_cannot_write_request_or_access_rows(tmp_path):
    archive = Archive(tmp_path / "telemetry" / "archive.db", clock=lambda: NOW)
    own = Journal(tmp_path / "own.db", node_id="coord")
    own.start()
    own.request(RequestRecord(request_id="r-own", ts=NOW, served_name="m", key_id="k1"))
    own.access(AccessRecord(request_id="a-own", ts=NOW, who="session:root", path="/api/keys"))
    own.flush()

    class Worker:
        async def get_json(self, url, timeout, headers=None, **_):
            since = int(url.split("since=", 1)[1].split("&", 1)[0])
            return _forged_page() if since == 0 else {"rows": [], "next": 3, "head": 3}

    collector = Collector(
        archive, client=Worker(), agents=lambda: {"worker": "http://10.0.0.13:8081"}
    )
    collector.add_local("coord", own)
    asyncio.run(collector.poll_once())
    own.close()

    requests = [r[0] for r in archive.conn.execute("SELECT request_id FROM requests")]
    access = [r[0] for r in archive.conn.execute("SELECT request_id FROM access")]
    assert requests == ["r-own"]
    assert access == ["a-own"]
    # The worker's telemetry still lands; only the evidence it may not write
    # is refused, and counted where an operator looks.
    assert archive.conn.execute(
        "SELECT COUNT(*) FROM samples WHERE node_id='worker'"
    ).fetchone()[0] == 1
    rejected = archive.conn.execute(
        "SELECT rejected FROM cursors WHERE node_id='worker'"
    ).fetchone()[0]
    assert rejected == 2
    assert archive.cursor_for("worker") == 3
    archive.close()


def test_forensics_names_the_node_of_a_row_the_coordinator_did_not_write(tmp_path):
    archive = Archive(tmp_path / "telemetry" / "archive.db", clock=lambda: NOW)
    own = {"rows": [
        {"kind": KIND_REQUEST, "seq": 1, "ts": NOW,
         "body": json.dumps({"request_id": "r-own", "key_id": "k1", "status": 200})},
        {"kind": KIND_ACCESS, "seq": 2, "ts": NOW,
         "body": json.dumps(AccessRecord(request_id="a-own", ts=NOW, method="GET", path="/x").as_dict())},
    ], "next": 2, "head": 2}
    # Rows written before this fix, by a member, under its own name.
    archive.conn.execute(
        "INSERT INTO requests(request_id, attempt_no, ts, node_id, key_id, status) "
        "VALUES('r-deadbeef0001', 0, ?, 'worker', 'victim', 200)", (NOW,)
    )
    archive.conn.execute(
        "INSERT INTO access(request_id, ts, node_id, who, method, path, status) "
        "VALUES('a-forged', ?, 'worker', 'key:drt_victim', 'POST', '/v1/chat/completions', 200)",
        (NOW,),
    )
    archive.ingest("coord", own, trusted=True)
    archive.close()

    report = forensics.investigate(tmp_path, forensics.Filters(since=0.0))
    by_rid = {e.request_id: e.what for e in report.events if e.request_id}
    assert "node=worker" in by_rid["r-deadbeef0001"]
    assert "node=worker" in by_rid["a-forged"]
    assert "node=" not in by_rid["r-own"]
    assert "node=" not in by_rid["a-own"]
    out = io.StringIO()
    forensics.render_text(report, out)
    assert "node=worker" in out.getvalue()


def _log_page(start: int, n: int, size: int) -> dict:
    rows = [
        {"kind": KIND_LOG, "seq": start + i, "ts": NOW,
         "body": json.dumps({"level": "INFO", "logger": "x", "message": "a" * size})}
        for i in range(n)
    ]
    return {"rows": rows, "next": start + n - 1, "head": start + n - 1}


def test_a_nodes_raw_rows_past_its_daily_quota_are_dropped_with_one_gap(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "NODE_RAW_BYTES_PER_DAY", 1000)
    archive = Archive(tmp_path / "a.db", clock=lambda: NOW)
    archive.ingest("rogue", _log_page(1, 5, 400))
    archive.ingest("rogue", _log_page(6, 5, 400))
    archive.ingest("quiet", _log_page(1, 2, 100))

    kept = dict(archive.conn.execute(
        "SELECT node_id, COUNT(*) FROM logs GROUP BY node_id"
    ).fetchall())
    assert kept == {"rogue": 2, "quiet": 2}
    gaps = archive.conn.execute(
        "SELECT node_id FROM gaps WHERE reason='node_quota'"
    ).fetchall()
    assert [g[0] for g in gaps] == ["rogue"]
    # The cursor still moves past what was dropped: a quota is a refusal,
    # not a reason to re-ask for the same rows forever.
    assert archive.cursor_for("rogue") == 10
    archive.close()
