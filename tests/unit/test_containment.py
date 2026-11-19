"""Containment (plan step 3.5): a session seen from a new address is an event,
any admin can sign any admin out, an org's keys go dark in one call, and an
open shell or realtime socket ends when the credential behind it does."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from control_plane.auth import AuthService
from control_plane.contracts.auth import SESSION_COOKIE, AuditAction, KeyStatus
from control_plane.gateway import create_app, realtime, shell_api
from tests.unit.conftest import BOOT_USER, credentials
from tests.unit.test_gateway import (
    FakeProviders,
    FakeRegistry,
    RunningServer,
    _free_port,
    build_deps,
    make_provider,
)

OPS_PW = "violet-harbour-lantern-42"


def _stores(app):
    return app.state.ctx.auth_stores


def _rows(app, action: AuditAction):
    app.state.ctx.secevents.flush()
    rows, _ = _stores(app).auth.audit_page(action=action.value, page_size=100)
    return rows


def _ops_cookie(app, ip: str = "127.0.0.1") -> str:
    credentials(app)  # the suite's own admin first: setup closes once one exists
    auth = _stores(app).auth
    if not any(a.username == "ops" for a in auth.list_admins()):
        auth.create_admin("ops", OPS_PW, email="ops@derate.test", created_by=BOOT_USER, ip=ip)
    _, cookie = auth.login("ops", OPS_PW, ip=ip, user_agent="testclient")
    return cookie


def _as(cookie: str) -> dict[str, str]:
    return {"Cookie": f"{SESSION_COOKIE}={cookie}"}


# -- address change ------------------------------------------------------------


def test_a_session_used_from_a_new_address_is_one_event_and_moves_ip():
    app = create_app()
    cookie = _ops_cookie(app)
    home = TestClient(app)
    assert home.get("/api/auth/me", headers=_as(cookie)).status_code == 200
    assert _rows(app, AuditAction.SESSION_IP_CHANGED) == []

    away = TestClient(app, client=("10.9.9.9", 41000))
    for _ in range(3):
        assert away.get("/api/auth/me", headers=_as(cookie)).status_code == 200

    rows = _rows(app, AuditAction.SESSION_IP_CHANGED)
    assert len(rows) == 1
    row = rows[0]
    assert row.actor == "ops" and row.ip == "10.9.9.9"
    assert row.after["login_ip"] == "127.0.0.1"
    assert row.after["previous_ip"] == "127.0.0.1"
    (ops,) = [s for s in _stores(app).auth.list_sessions() if s.username == "ops"]
    assert ops.ip == "10.9.9.9"
    assert ops.login_ip == "127.0.0.1"


def test_a_new_user_agent_alone_is_an_event_too():
    app = create_app()
    cookie = _ops_cookie(app)
    client = TestClient(app)
    client.get("/api/auth/me", headers={**_as(cookie), "User-Agent": "curl/8"})
    rows = _rows(app, AuditAction.SESSION_IP_CHANGED)
    assert len(rows) == 1 and rows[0].after["user_agent"] == "curl/8"
    (ops,) = [s for s in _stores(app).auth.list_sessions() if s.username == "ops"]
    assert ops.user_agent == "curl/8" and ops.ip == "127.0.0.1"


# -- schema v4 -----------------------------------------------------------------


def test_a_v3_file_migrates_to_v4_keeping_the_login_address(tmp_path):
    path = tmp_path / "auth.db"
    svc = AuthService.open(path)
    svc.create_admin("root", OPS_PW, email=None, created_by=None, ip=None)
    svc.login("root", OPS_PW, ip="3.3.3.3", user_agent="ua/1")
    svc.close()
    # Make it the file a v3 build wrote: no login_ip column, stamped 3.
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE sessions DROP COLUMN login_ip")
    conn.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
    conn.commit()
    conn.close()

    again = AuthService.open(path)
    (sess,) = again.list_sessions()
    assert sess.login_ip == "3.3.3.3" and sess.ip == "3.3.3.3"
    assert sqlite3.connect(path).execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()[0] == "4"
    assert again.verify_chain().ok


# -- routes --------------------------------------------------------------------


def test_list_sessions_shape():
    app = create_app()
    client = TestClient(app)
    _ops_cookie(app, ip="10.1.1.1")
    r = client.get("/api/auth/sessions")
    assert r.status_code == 200
    rows = r.json()
    assert {row["username"] for row in rows} == {BOOT_USER, "ops"}
    for row in rows:
        assert set(row) == {
            "username", "session_id", "login_ip", "ip", "user_agent",
            "created_at", "last_seen_at", "expires_at", "current",
        }
        assert len(row["session_id"]) == 12
    assert [row["username"] for row in rows if row["current"]] == [BOOT_USER]
    (ops,) = [row for row in rows if row["username"] == "ops"]
    assert ops["login_ip"] == "10.1.1.1"


def test_any_admin_can_sign_another_out_everywhere():
    app = create_app()
    client = TestClient(app)
    cookie = _ops_cookie(app)
    assert client.get("/api/auth/me", headers=_as(cookie)).status_code == 200

    r = client.delete("/api/admins/ops/sessions")
    assert r.status_code == 200 and r.json() == {"username": "ops", "revoked": 1}
    assert client.get("/api/auth/me", headers=_as(cookie)).status_code == 401
    # The caller's own session is untouched.
    assert client.get("/api/auth/me").status_code == 200

    rows = _rows(app, AuditAction.SESSIONS_REVOKED)
    assert len(rows) == 1
    assert rows[0].actor == BOOT_USER and rows[0].target_id == "ops"
    assert rows[0].after["count"] == 1 and rows[0].request_id

    assert client.delete("/api/admins/nobody/sessions").status_code == 404


def test_disable_every_key_of_an_org():
    app = create_app()
    client = TestClient(app)
    client.get("/api/auth/me")  # sign the suite in first
    keys = _stores(app).keys
    org = keys.create_org("acme", actor=BOOT_USER)
    ids = [
        keys.create_key(name=f"k{i}", org_id=org.org_id, created_by=BOOT_USER, expiry_days=30).key.key_id
        for i in range(3)
    ]
    keys.disable_key(ids[0], actor=BOOT_USER, reason="earlier")
    other = keys.create_key(
        name="elsewhere", org_id=keys.default_org_id(), created_by=BOOT_USER, expiry_days=30
    ).key.key_id

    r = client.post(f"/api/orgs/{org.org_id}/keys/disable", json={"reason": "incident 42"})
    assert r.status_code == 200
    assert sorted(r.json()["disabled"]) == sorted(ids[1:])
    for key_id in ids:
        assert keys.get_key(key_id).status is KeyStatus.DISABLED
    assert keys.get_key(other).status is KeyStatus.ACTIVE

    rows = [r for r in _rows(app, AuditAction.KEY_DISABLED) if r.target_id in ids[1:]]
    assert sorted(r.target_id for r in rows) == sorted(ids[1:])
    assert all(r.actor == BOOT_USER and r.after["status"] == "disabled" for r in rows)

    assert client.post("/api/orgs/nope/keys/disable", json={}).status_code == 404
    assert client.post(f"/api/orgs/{org.org_id}/keys/disable", json={"reason": 3}).status_code == 400


# -- relays --------------------------------------------------------------------


class _AgentRegistry(FakeRegistry):
    def __init__(self, agent_port: int):
        super().__init__()
        self._port = agent_port

    def agent_url(self, node_id):
        return f"http://127.0.0.1:{self._port}"


def _wait_for(predicate, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    return predicate()


def test_an_open_shell_closes_when_its_session_is_revoked(monkeypatch):
    import websockets

    monkeypatch.setenv("DERATE_SHELL", "1")
    monkeypatch.setattr(shell_api, "RECHECK_S", 0.2)

    async def agent(conn):
        async for frame in conn:
            await conn.send(frame)

    async def scenario():
        port = _free_port()
        server = await websockets.serve(agent, "127.0.0.1", port)
        app = create_app(build_deps(registry=_AgentRegistry(port)))
        cookie = _ops_cookie(app)
        with RunningServer(app) as gateway:
            url = f"ws://127.0.0.1:{gateway.port}/api/nodes/spark-01/shell"
            async with websockets.connect(url, additional_headers=_as(cookie)) as ws:
                await ws.send("echo hi")
                assert await asyncio.wait_for(ws.recv(), 10) == "echo hi"
                revoked = await asyncio.to_thread(
                    TestClient(app).delete, "/api/admins/ops/sessions"
                )
                assert revoked.status_code == 200
                with pytest.raises(websockets.ConnectionClosed) as closed:
                    await asyncio.wait_for(ws.recv(), 10)
            row = await asyncio.to_thread(
                _wait_for, lambda: _rows(app, AuditAction.SHELL_CLOSED)
            )
        server.close()
        await server.wait_closed()
        return closed.value, row

    closed, rows = asyncio.run(scenario())
    assert closed.rcvd.code == 1008 and closed.rcvd.reason == "session_revoked"
    assert rows and rows[0].after["ended_by"] == "session_revoked"
    assert rows[0].actor == "ops"


def test_an_open_realtime_session_closes_when_its_key_is_disabled(monkeypatch):
    import websockets

    monkeypatch.setattr(realtime, "RECHECK_S", 0.2)

    async def upstream(conn):
        async for frame in conn:
            await conn.send(frame)

    async def scenario():
        port = _free_port()
        server = await websockets.serve(upstream, "127.0.0.1", port)
        provider = make_provider(
            "openai", base_url=f"http://127.0.0.1:{port}/v1",
            served_name="voice", upstream_id="gpt-4o-realtime",
        )
        app = create_app(build_deps(providers=FakeProviders([provider])))
        _, key = credentials(app)
        keys = _stores(app).keys
        with RunningServer(app) as gateway:
            url = f"ws://127.0.0.1:{gateway.port}/v1/realtime?model=voice"
            async with websockets.connect(
                url, additional_headers={"Authorization": f"Bearer {key}"}
            ) as ws:
                await ws.send(json.dumps({"type": "session.update"}))
                await asyncio.wait_for(ws.recv(), 10)
                org_id = keys.default_org_id()
                r = await asyncio.to_thread(
                    TestClient(app).post, f"/api/orgs/{org_id}/keys/disable",
                    json={"reason": "contain"},
                )
                assert r.status_code == 200
                with pytest.raises(websockets.ConnectionClosed) as closed:
                    await asyncio.wait_for(ws.recv(), 10)
        server.close()
        await server.wait_closed()
        return closed.value

    closed = asyncio.run(scenario())
    assert closed.rcvd.code == 1008 and closed.rcvd.reason == "key_disabled"
