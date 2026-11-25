"""Auth-gate hardening: setup Host check (A4), username-scope lockout as a
delay (A3), re-authentication for admin-level changes (A9), and the response
and cookie hardening of SC-09/A8.

Every test is ``raw_http``: it sets up its own admin and sends its own cookie.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from control_plane.contracts.auth import (
    LOGIN_LOCK_THRESHOLD,
    SESSION_COOKIE,
    SESSION_COOKIE_SECURE,
)
from control_plane.gateway import auth_api, create_app

pytestmark = pytest.mark.raw_http

ADMIN = {
    "username": "root-admin",
    "email": "root-admin@derate.test",
    "password": "first-harbor-lamp-123456",
}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path))
    return create_app()


def _client(app, ip="127.0.0.1", **kwargs) -> TestClient:
    return TestClient(app, client=(ip, 50000), **kwargs)


def _setup(app) -> tuple[TestClient, dict]:
    client = _client(app)
    r = client.post("/api/setup/admin", json=ADMIN)
    assert r.status_code == 200, r.text
    cookie = {"Cookie": f"{SESSION_COOKIE}={r.cookies[SESSION_COOKIE]}"}
    client.cookies.clear()
    return client, {"cookie": cookie, "setup": r.json()}


def _audit(client, cookie, action):
    entries = client.get("/api/audit", headers=cookie).json()["entries"]
    return [e for e in entries if e["action"] == action]


# -- 2.1: setup answers whatever Host the browser used -----------------------


def test_setup_answers_a_lan_hostname(app):
    """First-run setup is open to any client, so a browser on the LAN using
    the coordinator's own name gets the form, not a refusal."""
    client = _client(app)
    r = client.post("/api/setup/admin", json=ADMIN, headers={"host": "spark-01.lan:8088"})
    assert r.status_code == 200, r.text


def test_setup_accepts_the_public_origin_host(tmp_path, monkeypatch):
    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DERATE_PUBLIC_ORIGIN", "https://derate.example")
    client = _client(create_app())
    r = client.post("/api/setup/admin", json=ADMIN, headers={"host": "derate.example"})
    assert r.status_code == 200, r.text


# -- 2.3: username-scope hold is a delay -------------------------------------


@pytest.fixture
def slept(monkeypatch):
    """Records the login delay the gateway sleeps instead of sleeping it."""
    calls: list[float] = []
    real = asyncio.sleep

    async def fake(delay, *args, **kwargs):
        if delay >= 1:
            calls.append(delay)
            delay = 0
        return await real(delay, *args, **kwargs)

    monkeypatch.setattr(auth_api.asyncio, "sleep", fake)
    return calls


def _login(app, ip, password):
    return _client(app, ip).post(
        "/api/auth/login", json={"username": ADMIN["username"], "password": password}
    )


def test_spread_failures_delay_the_admin_and_never_refuse(app, slept):
    """A3: four failures each from two addresses used to hold the username
    and refuse the real admin's correct password."""
    _setup(app)
    for ip in ("10.0.0.1", "10.0.0.2"):
        for _ in range(LOGIN_LOCK_THRESHOLD - 1):
            assert _login(app, ip, "wrong-password-xx").status_code == 401
    slept.clear()  # the later failures were already slowed; the admin is next
    r = _login(app, "10.0.0.9", ADMIN["password"])
    assert r.status_code == 200, r.text
    assert slept and slept[-1] > 0


def test_the_pair_lock_still_answers_429(app, slept):
    _setup(app)
    for _ in range(LOGIN_LOCK_THRESHOLD):
        assert _login(app, "10.0.0.1", "wrong-password-xx").status_code == 401
    r = _login(app, "10.0.0.1", ADMIN["password"])
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "login_locked"
    assert _login(app, "10.0.0.2", ADMIN["password"]).status_code == 200


# -- 2.4: re-authentication --------------------------------------------------


NEW_ADMIN = {"username": "second", "email": "second@derate.test", "password": "other-harbor-lamp-1234"}


@pytest.mark.parametrize("current", [None, "not-the-password-1"])
def test_adding_an_admin_without_the_password_is_refused_and_recorded(app, current):
    client, s = _setup(app)
    body = dict(NEW_ADMIN)
    if current is not None:
        body["current_password"] = current
    r = client.post("/api/admins", json=body, headers=s["cookie"])
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_credentials"
    names = [a["username"] for a in client.get("/api/admins", headers=s["cookie"]).json()]
    assert names == [ADMIN["username"]]
    failed = _audit(client, s["cookie"], "login_failed")
    assert len(failed) == 1 and failed[0]["actor"] == ADMIN["username"]


def test_adding_and_removing_an_admin_with_the_password(app):
    client, s = _setup(app)
    body = {**NEW_ADMIN, "current_password": ADMIN["password"]}
    assert client.post("/api/admins", json=body, headers=s["cookie"]).status_code == 200
    r = client.delete("/api/admins/second", headers=s["cookie"])
    assert r.status_code == 401
    r = client.request(
        "DELETE", "/api/admins/second", json={"current_password": "wrong-password-xx"},
        headers=s["cookie"],
    )
    assert r.status_code == 401
    assert len(_audit(client, s["cookie"], "login_failed")) == 2
    r = client.request(
        "DELETE", "/api/admins/second", json={"current_password": ADMIN["password"]},
        headers=s["cookie"],
    )
    assert r.status_code == 200, r.text


def test_rotating_any_key_needs_the_password(app):
    client, s = _setup(app)
    admin_key = client.get("/api/auth/me", headers=s["cookie"]).json()["admin_key_id"]
    plain = s["setup"]["key"]["key_id"]  # the default org's key, not an admin key
    for key_id in (admin_key, plain):
        r = client.post(f"/api/keys/{key_id}/rotate", headers=s["cookie"])
        assert r.status_code == 401, key_id
        assert r.json()["error"]["code"] == "invalid_credentials"
        assert "secret_once" not in r.text
        r = client.post(
            f"/api/keys/{key_id}/rotate", json={"current_password": ADMIN["password"]},
            headers=s["cookie"],
        )
        assert r.status_code == 200 and r.json()["secret_once"], r.text


def test_creating_a_key_needs_the_password(app):
    client, s = _setup(app)
    body = {"name": "minted", "expiry_days": 30}
    r = client.post("/api/keys", json=body, headers=s["cookie"])
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_credentials"
    assert "secret_once" not in r.text
    r = client.post(
        "/api/keys", json={**body, "current_password": "not-the-password-1"}, headers=s["cookie"]
    )
    assert r.status_code == 401
    assert "secret_once" not in r.text
    failed = _audit(client, s["cookie"], "login_failed")
    assert failed and failed[-1]["actor"] == ADMIN["username"]
    names = [k["name"] for k in client.get("/api/keys", headers=s["cookie"]).json()]
    assert "minted" not in names
    r = client.post(
        "/api/keys", json={**body, "current_password": ADMIN["password"]}, headers=s["cookie"]
    )
    assert r.status_code == 200, r.text
    assert r.json()["secret_once"]


# -- 4.4: headers, cookie, server banner, 503 text ---------------------------


def test_hsts_is_sent_over_plain_http_when_the_public_origin_is_https(tmp_path, monkeypatch):
    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DERATE_PUBLIC_ORIGIN", "https://derate.example")
    r = _client(create_app()).get("/api/auth/state")
    assert r.headers["strict-transport-security"].startswith("max-age=")


def test_every_answer_carries_the_new_policy_headers(app):
    r = _client(app).get("/api/auth/state")
    assert r.headers["permissions-policy"] == "camera=(), microphone=(self), geolocation=()"
    assert r.headers["cross-origin-opener-policy"] == "same-origin"
    assert "strict-transport-security" not in r.headers


def test_the_cookie_is_host_prefixed_when_secure_and_is_read_back(tmp_path, monkeypatch):
    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DERATE_PUBLIC_ORIGIN", "https://localhost")
    client = _client(create_app())
    r = client.post("/api/setup/admin", json=ADMIN)
    assert r.status_code == 200, r.text
    set_cookie = r.headers["set-cookie"]
    assert set_cookie.startswith(f"{SESSION_COOKIE_SECURE}=")
    assert "secure" in set_cookie.lower() and "path=/" in set_cookie.lower()
    assert "domain=" not in set_cookie.lower()
    value = set_cookie.split(";", 1)[0].split("=", 1)[1]
    client.cookies.clear()
    me = client.get("/api/auth/me", headers={"Cookie": f"{SESSION_COOKIE_SECURE}={value}"})
    assert me.status_code == 200 and me.json()["username"] == ADMIN["username"]
    out = client.post("/api/auth/logout", headers={"Cookie": f"{SESSION_COOKIE_SECURE}={value}"})
    assert out.headers["set-cookie"].startswith(f"{SESSION_COOKIE_SECURE}=")


def test_the_plain_cookie_keeps_its_name_over_http(app):
    r = _client(app).post("/api/setup/admin", json=ADMIN)
    assert r.headers["set-cookie"].startswith(f"{SESSION_COOKIE}=")


def test_uvicorn_is_configured_without_a_server_header(monkeypatch):
    from control_plane import node
    from control_plane.gateway import main as gateway_main

    server = node._make_server(object(), host="127.0.0.1", port=18999)
    assert server.config.server_header is False

    seen: dict = {}
    monkeypatch.setattr(gateway_main.uvicorn, "run", lambda app, **kw: seen.update(kw))
    monkeypatch.setattr(gateway_main, "create_app", lambda **kw: object())
    gateway_main.main([])
    assert seen["server_header"] is False


def test_an_unopenable_store_does_not_name_its_path(tmp_path, monkeypatch):
    not_a_dir = tmp_path / "secret-data-dir"
    not_a_dir.write_text("x")
    monkeypatch.setenv("DERATE_DATA_DIR", str(not_a_dir))
    client = _client(create_app())
    for path in ("/api/auth/state", "/api/settings", "/v1/models"):
        r = client.get(path)
        assert r.status_code == 503, path
        assert r.json()["error"]["message"] == "The authentication store is unavailable."
        assert "secret-data-dir" not in r.text
