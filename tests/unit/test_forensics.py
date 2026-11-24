"""``python3 -m control_plane.forensics`` against a fixture data dir."""

from __future__ import annotations

import io
import json
import sqlite3

import pytest
from argon2 import PasswordHasher

from control_plane import forensics
from control_plane.auditlog import AuditMirror
from control_plane.auth import AuthService
from control_plane.contracts.auth import AuditAction

PW = "correct-horse-battery"
T0 = 1_800_000_000.0


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def data_dir(tmp_path):
    clock = Clock()
    mirror = AuditMirror(tmp_path / "logs" / "audit", clock=clock)
    svc = AuthService.open(
        tmp_path / "auth.db",
        clock=clock,
        hasher=PasswordHasher(time_cost=1, memory_cost=8, parallelism=1),
        mirror=mirror,
    )
    svc.create_admin("root", PW, created_by=None, ip="127.0.0.1")
    clock.t += 10
    sess, _ = svc.login("root", PW, ip="10.0.0.5", request_id="r-aaaaaaaaaaaa", user_agent="drill/1")
    clock.t += 10
    svc.audit("root", AuditAction.SETTINGS_CHANGED, "settings", "routing_policy", ip="10.0.0.5",
              request_id="r-bbbbbbbbbbbb", user_agent="drill/1", session_id=sess.session_id[:12], status=200)
    clock.t += 10
    svc.audit("derate-badbadbadb", AuditAction.KEY_REFUSED, "security", "", ip="203.0.113.9",
              request_id="r-cccccccccccc", after={"count": 1})
    clock.t += 1
    # Rows written before the prefix changed name the key as ``drt_<id>``
    # (an audit actor) and ``key:drt_<id>`` (an access row's who, below).
    svc.audit("drt_abcdefghij", AuditAction.KEY_REFUSED, "security", "", ip="203.0.113.9",
              request_id="r-ffffffffffff", after={"count": 1})
    svc.close()

    (tmp_path / "telemetry").mkdir()
    archive = sqlite3.connect(tmp_path / "telemetry" / "archive.db")
    archive.execute(
        "CREATE TABLE requests(request_id TEXT, attempt_no INTEGER, ts REAL, served_name TEXT, status INTEGER,"
        " error_code TEXT, error_class TEXT, key_id TEXT, key_name TEXT, client_ip TEXT, user_agent TEXT,"
        " model_requested TEXT)"
    )
    archive.executemany(
        "INSERT INTO requests VALUES(?, 0, ?, 'm', ?, NULL, NULL, ?, 'k', ?, ?, 'm')",
        [
            ("r-dddddddddddd", T0 + 15, 200, "abcdefghij", "10.0.0.7", "drill/2"),
            ("r-eeeeeeeeeeee", T0 + 25, 200, "abcdefghij", "10.0.0.7", "drill/2"),
        ],
    )
    archive.execute(
        "CREATE TABLE access(request_id TEXT, ts REAL, client_ip TEXT, client_port INTEGER,"
        " peer_ip TEXT, who TEXT, method TEXT, path TEXT, query TEXT, status INTEGER,"
        " user_agent TEXT, node_id TEXT)"
    )
    archive.execute(
        "INSERT INTO access VALUES('r-gggggggggggg', ?, '10.0.0.7', 41000, '10.0.0.7',"
        " 'key:drt_abcdefghij', 'GET', '/v1/models', '', 200, 'drill/2', 'n1')",
        (T0 + 26,),
    )
    archive.commit()
    archive.close()
    return tmp_path


def _run(*args) -> tuple[int, str]:
    out = io.StringIO()
    code = forensics.main(list(args), out=out)
    return code, out.getvalue()


def test_the_timeline_is_merged_and_time_ordered(data_dir):
    code, text = _run("--since", str(T0 - 1), "--data-dir", str(data_dir), "--format", "jsonl")
    assert code == 0
    lines = [json.loads(x) for x in text.splitlines()]
    events, summary = lines[:-1], lines[-1]
    assert [e["ts"] for e in events] == sorted(e["ts"] for e in events)
    assert {e["source"] for e in events} >= {"audit", "session", "login", "v1"}
    assert summary["kind"] == "summary" and summary["chain"]["ok"] is True
    assert summary["user_agents"] == ["drill/1", "drill/2"]
    assert set(summary["ips"]) == {"127.0.0.1", "10.0.0.5", "10.0.0.7", "203.0.113.9"}


def test_filters_by_ip_key_actor_and_request_id(data_dir):
    base = ("--since", "2027-01-15T08:00:00Z", "--data-dir", str(data_dir), "--format", "jsonl")

    def events(*extra):
        return [json.loads(x) for x in _run(*base, *extra)[1].splitlines()][:-1]

    assert {e["ip"] for e in events("--ip", "10.0.0.7")} == {"10.0.0.7"}
    assert [e["request_id"] for e in events("--key", "derate-abcdefghij")] == [
        "r-dddddddddddd", "r-eeeeeeeeeeee", "r-gggggggggggg", "r-ffffffffffff",
    ]
    assert [e["source"] for e in events("--key", "derate-badbadbadb")] == ["audit"]
    assert {e["actor"] for e in events("--actor", "root")} == {"root"}
    assert [e["what"] for e in events("--request-id", "r-bbbbbbbbbbbb")] == [
        "settings_changed settings:routing_policy -> 200"
    ]
    assert events("--since", str(T0 + 1000)) == []


def test_text_report_ends_with_the_chain_and_a_tampered_row_fails(data_dir):
    code, text = _run("--since", str(T0 - 1), "--data-dir", str(data_dir))
    assert code == 0 and text.rstrip().splitlines()[-1].startswith("chain: ok (")

    conn = sqlite3.connect(data_dir / "auth.db")
    bad = conn.execute("SELECT id FROM audit WHERE request_id='r-bbbbbbbbbbbb'").fetchone()[0]
    conn.execute("UPDATE audit SET ip='1.2.3.4' WHERE id=?", (bad,))
    conn.commit()
    code, text = _run("--since", str(T0 - 1), "--data-dir", str(data_dir))
    assert code == 1
    assert text.rstrip().splitlines()[-1].startswith(f"chain: BROKEN at audit row {bad}")
    # the mirror still holds the original row and says it differs
    mirrored = [line for line in text.splitlines() if "mirror!" in line]
    assert len(mirrored) == 1 and "10.0.0.5" in mirrored[0] and "differs from auth.db" in mirrored[0]


def test_parse_time_accepts_rfc3339_and_epoch():
    assert forensics.parse_time("1800000000") == T0
    assert forensics.parse_time("2027-01-15T08:00:00Z") == forensics.parse_time("2027-01-15T08:00:00+00:00")
    assert forensics.key_id_of("derate-abcdefghij_secretpart") == "abcdefghij"
    assert forensics.key_id_of("drt_abcdefghij_secretpart") == "abcdefghij"


def test_a_legacy_drt_actor_is_found_by_either_spelling_of_the_key(data_dir):
    """Rows written before 2026-11-22 name ``drt_<id>`` / ``key:drt_<id>``;
    the investigator has a ``derate-`` key or a bare id, and must find them."""
    base = ("--since", str(T0 - 1), "--data-dir", str(data_dir), "--format", "jsonl")
    for key in ("derate-abcdefghij", "abcdefghij", "drt_abcdefghij"):
        rows = [json.loads(x) for x in _run(*base, "--key", key)[1].splitlines()][:-1]
        found = {e["request_id"] for e in rows}
        assert {"r-ffffffffffff", "r-gggggggggggg"} <= found, key
