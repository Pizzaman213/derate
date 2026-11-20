"""The audit chain head is anchored in ``<logs dir>/audit/HEAD``, so deleting
the newest rows -- which ``walk_chain`` alone cannot see -- breaks the chain."""

from __future__ import annotations

import json
import sqlite3

from control_plane.auditlog import AuditMirror
from control_plane.auth import AuthService
from control_plane.auth import verify as auth_verify
from control_plane.contracts.auth import AuditAction


def _service(tmp_path):
    mirror = AuditMirror(tmp_path / "logs" / "audit")
    return AuthService.open(tmp_path / "auth.db", mirror=mirror), tmp_path / "logs" / "audit"


def _write_five(service):
    for i in range(5):
        service.audit("alice", AuditAction.API_WRITE, "thing", str(i))


def test_deleting_the_newest_rows_breaks_the_chain(tmp_path):
    service, _ = _service(tmp_path)
    try:
        _write_five(service)
        assert service.verify_chain().ok
        conn = sqlite3.connect(tmp_path / "auth.db")
        conn.execute("DELETE FROM audit WHERE id > 3")
        conn.commit()
        conn.close()
        report = service.verify_chain()
    finally:
        service.close()
    assert report.ok is False
    assert report.first_bad_id == 5
    assert report.anchor == "head-file"


def test_head_file_is_private_and_names_the_newest_row(tmp_path):
    service, audit_dir = _service(tmp_path)
    try:
        _write_five(service)
    finally:
        service.close()
    head = json.loads((audit_dir / "HEAD").read_text())
    assert head["id"] == 5 and head["rows"] == 5 and head["row_hash"] and head["ts_iso"]
    assert (audit_dir / "HEAD").stat().st_mode & 0o777 == 0o600


def test_no_head_file_reports_no_anchor(tmp_path):
    service = AuthService.open(tmp_path / "auth.db")
    try:
        _write_five(service)
        report = service.verify_chain()
    finally:
        service.close()
    assert report.ok and report.anchor == "none"


def test_verify_cli_exits_1_naming_the_deleted_row(tmp_path, capsys):
    service, audit_dir = _service(tmp_path)
    try:
        _write_five(service)
    finally:
        service.close()
    conn = sqlite3.connect(tmp_path / "auth.db")
    conn.execute("DELETE FROM audit WHERE id = 5")
    conn.commit()
    conn.close()
    code = auth_verify.main(["--db", str(tmp_path / "auth.db"), "--audit-dir", str(audit_dir)])
    assert code == 1
    assert "row 5" in capsys.readouterr().out


def test_watch_writes_one_broken_row_per_distinct_first_bad_id(tmp_path, caplog):
    from types import SimpleNamespace

    from control_plane.gateway.chainwatch import ChainWatch

    service, _ = _service(tmp_path)
    try:
        _write_five(service)
        watch = ChainWatch(SimpleNamespace(auth=service))
        assert watch.check().ok
        conn = sqlite3.connect(tmp_path / "auth.db")
        conn.execute("UPDATE audit SET target_id='forged' WHERE id = 2")
        conn.commit()
        conn.close()
        with caplog.at_level("CRITICAL", logger="gateway.security"):
            first = watch.check()
            second = watch.check()
    finally:
        service.close()
    assert first.ok is False and first.first_bad_id == 2 and second.first_bad_id == 2
    conn = sqlite3.connect(tmp_path / "auth.db")
    rows = conn.execute(
        "SELECT actor, target_id, after_json FROM audit WHERE action='audit_chain_broken'"
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    actor, target, after = rows[0]
    assert actor == "system" and target == "2"
    assert json.loads(after)["first_bad_id"] == 2
    assert sum("BROKEN at row 2" in r.message for r in caplog.records) == 1


def test_unset_sink_is_warned(caplog):
    from control_plane.gateway.chainwatch import warn_if_unanchored

    with caplog.at_level("WARNING", logger="gateway.security"):
        assert warn_if_unanchored({}) is False
        assert warn_if_unanchored({"DERATE_AUDIT_SINK": "udp://127.0.0.1:5514"}) is True
    assert [r.message for r in caplog.records] == [
        "DERATE_AUDIT_SINK is unset: the audit chain head is anchored only on this box"
    ]


def test_boot_verifies_the_chain_once_and_starts_the_sink(tmp_path, monkeypatch):
    """EV-5 (F3/F9): the lifespan opens the audit store -- starting the
    remote sink from boot rather than on the first request -- and verifies
    the chain once before serving."""
    from fastapi.testclient import TestClient

    from control_plane import auditlog
    from control_plane.gateway import create_app

    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path))
    verified: list[int] = []
    started: list[object] = []
    real_verify = AuthService.verify_chain

    def counting_verify(self, *a, **k):
        verified.append(1)
        return real_verify(self, *a, **k)

    class _Sink:
        def start(self, heartbeat=None):
            started.append(heartbeat)

        def send(self, row):
            pass

        def close(self):
            pass

    monkeypatch.setattr(AuthService, "verify_chain", counting_verify)
    monkeypatch.setattr(auditlog.AuditSink, "from_env", classmethod(lambda cls, *a, **k: _Sink()))
    app = create_app()
    assert verified == [] and started == []  # create_app touches nothing
    with TestClient(app):
        assert verified == [1]
        assert len(started) == 1 and callable(started[0])
