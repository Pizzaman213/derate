"""3.2: an access row for every HTTP request, durable and queryable.

The gate writes one ``AccessRecord`` per request -- open paths included --
with the address and source port, the credential, the redacted query string
and the status, and the archive keeps it in its own ``access`` table that
``python3 -m control_plane.forensics`` reads.
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from control_plane import forensics
from control_plane.gateway import create_app
from control_plane.telemetry.archive import Archive
from control_plane.telemetry.records import KIND_ACCESS, AccessRecord

pytestmark = pytest.mark.raw_http

ADMIN = {"username": "access-admin", "email": "access@derate.test", "password": "quiet-harbor-lamp-7719"}


class _Recorder:
    def __init__(self) -> None:
        self.rows: list[AccessRecord] = []

    def access(self, record: AccessRecord) -> None:
        self.rows.append(record)

    def sample(self, *a) -> None: ...
    def request(self, *a) -> None: ...
    def event(self, *a) -> None: ...
    def log(self, *a) -> None: ...


@pytest.fixture
def gate(tmp_path, monkeypatch):
    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path))
    app = create_app()

    async def boom():
        raise RuntimeError("handler failure")

    app.router.routes.insert(0, APIRoute("/api/test/boom", boom, methods=["GET"]))
    client = TestClient(app, client=("127.0.0.1", 50123), raise_server_exceptions=False)
    r = client.post("/api/setup/admin", json=ADMIN)
    assert r.status_code == 200, r.text
    cookie = next(v for k, v in r.cookies.items() if k.endswith("derate_session"))
    name = next(k for k in r.cookies.keys() if k.endswith("derate_session"))
    client.cookies.clear()
    recorder = _Recorder()
    app.state.ctx.sink = recorder
    return client, {"Cookie": f"{name}={cookie}"}, recorder


def _one(recorder: _Recorder, rid: str) -> AccessRecord:
    rows = [r for r in recorder.rows if r.request_id == rid]
    assert len(rows) == 1, recorder.rows
    return rows[0]


def test_a_read_leaves_an_access_row_with_query_and_port(gate):
    client, cookie, recorder = gate
    r = client.get("/api/keys?org_id=x", headers={**cookie, "User-Agent": "drill/2"})
    row = _one(recorder, r.headers["x-request-id"])
    assert row.query == "org_id=x"
    assert row.who == f"session:{ADMIN['username']}"
    assert row.client_ip == "127.0.0.1"
    assert row.client_port == 50123
    assert (row.method, row.path, row.status) == ("GET", "/api/keys", r.status_code)
    assert row.user_agent == "drill/2"


def test_an_open_path_leaves_a_row_too(gate):
    client, _, recorder = gate
    r = client.get("/healthz")
    row = _one(recorder, r.headers["x-request-id"])
    assert row.who == "-"
    assert row.path == "/healthz"


def test_a_handler_exception_is_recorded_as_500(gate):
    client, cookie, recorder = gate
    r = client.get("/api/test/boom", headers=cookie)
    assert r.status_code == 500
    rows = [row for row in recorder.rows if row.path == "/api/test/boom"]
    assert len(rows) == 1 and rows[0].status == 500


def test_a_key_in_the_query_is_redacted(gate):
    client, cookie, recorder = gate
    secret = "drt_abcdefghij_" + "s" * 43
    client.get(f"/api/keys?token={secret}", headers=cookie)
    row = [r for r in recorder.rows if r.path == "/api/keys"][-1]
    assert secret not in row.query


def test_the_access_line_carries_port_and_query(gate, caplog):
    client, cookie, _ = gate
    with caplog.at_level(logging.INFO, logger="gateway.access"):
        client.get("/api/keys?org_id=x", headers=cookie)
    line = [rec.getMessage() for rec in caplog.records if rec.name == "gateway.access"][-1]
    assert " port=50123 " in line and " q='org_id=x' " in line


def test_archive_keeps_access_rows_and_forensics_reads_them(tmp_path):
    rec = AccessRecord(
        request_id="r-abc", ts=1_800_000_000.0, client_ip="10.1.2.3", client_port=41000,
        who="session:root", method="GET", path="/api/keys", query="org_id=x", status=200,
        duration_ms=3.0, user_agent="ua",
    )
    page = {"rows": [{"kind": KIND_ACCESS, "seq": 1, "ts": rec.ts, "body": json.dumps(rec.as_dict())}],
            "next": 1, "head": 1}
    (tmp_path / "telemetry").mkdir()
    archive = Archive(tmp_path / "telemetry" / "archive.db")
    archive.ingest("spark-01", page, trusted=True)  # the gateway's own journal
    archive.ingest("spark-02", page, trusted=True)  # same id from another node: ignored
    row = dict(archive.conn.execute("SELECT * FROM access").fetchone())
    assert row["node_id"] == "spark-01" and row["client_port"] == 41000
    archive.close()

    report = forensics.investigate(
        tmp_path, forensics.Filters(since=0.0, ip="10.1.2.3")
    )
    events = [e for e in report.events if e.source == "access"]
    assert len(events) == 1
    assert events[0].actor == "root"
    assert "org_id=x" in events[0].what and "port=41000" in events[0].what
    assert "10.1.2.3" in forensics.summary(report)["ips"]


def test_forensics_names_the_peer_and_escapes_what_a_client_typed(tmp_path):
    """GA-1: a proxied row says which socket it came through. GA-3: a path
    or a username carrying a newline or an escape cannot write a line of its
    own into the text report."""
    rec = AccessRecord(
        request_id="r-via", ts=1_800_000_000.0, client_ip="198.51.100.7", client_port=4242,
        who="session:root\n2026-11-18T10:00:00Z  audit    root", method="GET",
        path="/api/keys\n2026-11-18T10:00:00Z  forged\x1b[2J", status=200,
        peer_ip="10.0.0.5",
    )
    page = {"rows": [{"kind": KIND_ACCESS, "seq": 1, "ts": rec.ts, "body": json.dumps(rec.as_dict())}],
            "next": 1, "head": 1}
    (tmp_path / "telemetry").mkdir()
    archive = Archive(tmp_path / "telemetry" / "archive.db")
    archive.ingest("spark-01", page, trusted=True)
    assert archive.conn.execute("SELECT peer_ip FROM access").fetchone()[0] == "10.0.0.5"
    archive.close()

    report = forensics.investigate(tmp_path, forensics.Filters(since=0.0))
    events = [e for e in report.events if e.source == "access"]
    assert len(events) == 1 and "via 10.0.0.5" in events[0].what
    import io

    out = io.StringIO()
    forensics.render_text(report, out)
    text = out.getvalue()
    assert "forged" in text and "\x1b" not in text
    access_lines = [line for line in text.splitlines() if "r-via" in line]
    assert len(access_lines) == 1
    assert "\\x0a" in access_lines[0] and "\\x1b" in access_lines[0]
    assert not any(line.startswith("2026-11-18T10:00:00Z") for line in text.splitlines())


def test_a_newline_in_the_path_cannot_forge_an_access_line(gate, caplog):
    """GA-3: Starlette decodes %0A into scope["path"]."""
    client, cookie, _ = gate
    with caplog.at_level(logging.INFO, logger="gateway.access"):
        client.get("/api/keys%0A2026-11-18T10:00:00.000Z INFO gateway.access rid=forged", headers=cookie)
    lines = [rec.getMessage() for rec in caplog.records if rec.name == "gateway.access"]
    forged = [line for line in lines if "rid=forged" in line]
    assert len(forged) == 1
    assert "\n" not in forged[0] and "\\x0a" in forged[0]


@pytest.fixture
def keyed(tmp_path, monkeypatch):
    from starlette.routing import WebSocketRoute

    from control_plane.gateway import authz

    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path))
    app = create_app()

    async def socket(websocket):
        if await authz.require_key(websocket, app.state.ctx.auth_stores) is None:
            return
        await websocket.accept()
        await websocket.close()

    app.router.routes.insert(0, WebSocketRoute("/v1/test/socket", socket))
    client = TestClient(app, client=("127.0.0.1", 50124))
    r = client.post("/api/setup/admin", json=ADMIN)
    assert r.status_code == 200, r.text
    client.cookies.clear()
    recorder = _Recorder()
    app.state.ctx.sink = recorder
    return client, r.json(), recorder


def test_an_accepted_socket_leaves_a_ws_row_with_its_key(keyed):
    """K10r: written by AuthGuard when the socket closes."""
    client, setup, recorder = keyed
    with client.websocket_connect(
        "/v1/test/socket", headers={"Authorization": f"Bearer {setup['secret_once']}"}
    ):
        pass
    rows = [r for r in recorder.rows if r.path == "/v1/test/socket"]
    assert len(rows) == 1
    assert rows[0].method == "WS" and rows[0].status == 101
    assert rows[0].who == f"key:drt_{setup['key']['key_id']}"
    assert rows[0].client_port == 50124 and rows[0].peer_ip == "127.0.0.1"


def test_a_refused_realtime_socket_records_its_close_code(keyed):
    from starlette.websockets import WebSocketDisconnect

    client, setup, recorder = keyed
    with pytest.raises(WebSocketDisconnect):
        # Keyed, but no model: the relay refuses before accept.
        with client.websocket_connect(
            "/v1/realtime", headers={"Authorization": f"Bearer {setup['secret_once']}"}
        ) as ws:
            ws.receive_text()
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/v1/realtime?model=x") as ws:
            ws.receive_text()
    rows = [r for r in recorder.rows if r.path == "/v1/realtime"]
    assert [(r.method, r.status) for r in rows] == [("WS", 1008), ("WS", 1008)]
    assert rows[0].who == f"key:drt_{setup['key']['key_id']}"
    assert rows[1].who == "-"
