"""Every route the gateway answers is gated, read from the sets, never narrowed.

The route lists are ``.omp/work/auth-keys/sets/{v1,api}_routes.json``,
derived from ``control_plane/contracts/routes.json`` (the generator walks the
real app), and ``setup_routes.json`` names the open ones and why. A route added
to the gateway lands in those files on the next regenerate and is covered here
without anyone editing this file -- which is the point: a hand-kept list stops
covering a route the day somebody adds one.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from control_plane.contracts.routes import _collect as collect_routes
from control_plane.gateway import create_app

pytestmark = pytest.mark.raw_http

SETS = Path(__file__).resolve().parents[2] / ".omp" / "work" / "auth-keys" / "sets"
ADMIN = {
    "username": "cover-admin",
    "email": "cover-admin@derate.test",
    "password": "cover-boot-pw-123456",
}
GARBAGE = "derate-zzzzzzzzzz_" + "x" * 43
#: Setup answers a loopback peer only; TestClient's default peer is "testclient".
LOOPBACK = ("127.0.0.1", 50000)


def _members(name: str) -> list[str]:
    return list(json.loads((SETS / name).read_text())["members"])


def _routes(entries) -> list[str]:
    return [e["route"] if isinstance(e, dict) else e for e in entries]


SETUP = json.loads((SETS / "setup_routes.json").read_text())
ALWAYS_OPEN = _routes(SETUP["always_open"])
BEFORE_FIRST_ADMIN = _routes(SETUP["before_first_admin"])
V1_ROUTES = _members("v1_routes.json")
API_ROUTES = _members("api_routes.json")
GATED_API = [r for r in API_ROUTES if r not in ALWAYS_OPEN and r not in BEFORE_FIRST_ADMIN]


def _fill(path: str) -> str:
    """Path parameters filled with a dummy value (``{model:path}`` included)."""
    return re.sub(r"\{[^}]+\}", "x", path)


def _send(client: TestClient, route: str, headers: dict):
    method, path = route.split(" ", 1)
    url = _fill(path)
    if method == "WEBSOCKET":
        raise AssertionError("use _socket for websocket routes")
    kwargs: dict = {"headers": headers}
    if path == "/v1/audio/transcriptions":
        kwargs["files"] = {"file": ("a.wav", b"RIFF0000", "audio/wav")}
        kwargs["data"] = {"model": "x"}
    elif method in ("POST", "PUT", "PATCH"):
        kwargs["json"] = {"model": "x"}
    return client.request(method, url, **kwargs)


def _socket_refusal(client: TestClient, path: str, headers: dict) -> WebSocketDisconnect:
    with pytest.raises(WebSocketDisconnect) as caught:
        with client.websocket_connect(_fill(path) + "?model=x", headers=headers):
            pass
    return caught.value


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """One admin, and a key in each state the /v1 gate distinguishes."""
    data = tmp_path_factory.mktemp("cover")
    mp = pytest.MonkeyPatch()
    mp.setenv("DERATE_DATA_DIR", str(data))
    app = create_app()
    client = TestClient(app, client=LOOPBACK)
    r = client.post("/api/setup/admin", json=ADMIN)
    assert r.status_code == 200, r.text
    cookie = {"Cookie": f"derate_session={r.cookies['derate_session']}"}
    client.cookies.clear()

    def mint(name: str) -> dict:
        m = client.post(
            "/api/keys",
            json={"name": name, "expiry_days": 30, "current_password": ADMIN["password"]},
            headers=cookie,
        )
        assert m.status_code == 200, m.text
        return m.json()

    disabled = mint("disabled")
    assert client.post(
        f"/api/keys/{disabled['key']['key_id']}/disable", headers=cookie
    ).json()["status"] == "disabled"
    expired = mint("expired")
    # The walkthrough's way, since no clock knob ships: edit the stored expiry.
    with sqlite3.connect(data / "keys.db") as db:
        db.execute(
            "UPDATE api_keys SET expires_at=? WHERE key_id=?",
            (time.time() - 60, expired["key"]["key_id"]),
        )
    yield {
        "client": client,
        "cookie": cookie,
        "keys": {
            "missing_api_key": None,
            "invalid_api_key": GARBAGE,
            "api_key_disabled": disabled["secret_once"],
            "api_key_expired": expired["secret_once"],
        },
    }
    mp.undo()


def test_the_sets_are_not_empty_and_name_the_routes_this_suite_relies_on():
    assert "GET /v1/models" in V1_ROUTES and "WEBSOCKET /v1/realtime" in V1_ROUTES
    assert "POST /v1/audio/transcriptions" in V1_ROUTES
    assert "GET /api/keys" in API_ROUTES and "POST /api/setup/admin" in API_ROUTES
    assert BEFORE_FIRST_ADMIN == ["POST /api/setup/admin"]


@pytest.mark.parametrize("route", V1_ROUTES)
@pytest.mark.parametrize(
    "code", ["missing_api_key", "invalid_api_key", "api_key_disabled", "api_key_expired"]
)
def test_every_v1_route_refuses_each_bad_key_with_its_own_code(world, route, code):
    key = world["keys"][code]
    headers = {} if key is None else {"Authorization": f"Bearer {key}"}
    method, path = route.split(" ", 1)
    client = world["client"]
    # Every case here is a refused key from one address; the per-address
    # budget (test_gateway_security_gate.py pins it) would trip mid-walk.
    client.app.state.key_refusals.reset()
    if method == "WEBSOCKET":
        refusal = _socket_refusal(client, path, headers)
        assert refusal.code == 1008
        assert refusal.reason == code
        return
    r = _send(client, route, headers)
    assert r.status_code == 401, (route, r.status_code, r.text)
    assert r.json()["error"]["code"] == code
    assert r.headers["WWW-Authenticate"] == 'Bearer realm="derate"'


@pytest.mark.parametrize("route", GATED_API)
def test_every_api_route_wants_a_session(world, route):
    method, path = route.split(" ", 1)
    client = world["client"]
    if method == "WEBSOCKET":
        refusal = _socket_refusal(client, path, {})
        assert (refusal.code, refusal.reason) == (1008, "session_required")
        return
    # A key is not a session: /api ignores Authorization entirely.
    for headers in ({}, {"Authorization": f"Bearer {GARBAGE}"}):
        r = _send(client, route, headers)
        assert r.status_code == 401, (route, r.status_code, r.text)
        assert r.json()["error"]["code"] == "session_required"


@pytest.mark.parametrize("route", ALWAYS_OPEN)
def test_the_open_routes_never_ask_for_a_credential(world, route):
    client = world["client"]
    method, path = route.split(" ", 1)
    if route == "POST /api/auth/login":
        r = client.post(path, json=ADMIN)
        assert r.status_code == 200
        client.cookies.clear()
        return
    r = _send(client, route, {})
    assert r.status_code != 401, (route, r.text)
    # The default app's stub provider store has no ChatGPT sign-in, and
    # /auth/callback says so with a deliberate 501 -- an answer, not a crash.
    assert r.status_code < 500 or (
        r.status_code == 501 and r.json()["error"]["code"] == "not_implemented"
    ), (route, r.text)


def test_setup_closes_for_good_once_an_admin_exists(world):
    r = world["client"].post(
        "/api/setup/admin",
        json={"username": "late", "email": "late@derate.test", "password": "tardy-pw-1234567890"},
    )
    assert r.status_code == 410
    assert r.json()["error"]["code"] == "setup_closed"
    # Even with a session: setup is not an admin action, it is over.
    r = world["client"].post(
        "/api/setup/admin",
        json={"username": "late", "email": "late@derate.test", "password": "tardy-pw-1234567890"},
        headers=world["cookie"],
    )
    assert r.status_code == 410


def test_setup_is_open_before_the_first_admin(tmp_path, monkeypatch):
    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path))
    client = TestClient(create_app(), client=LOOPBACK)
    state = client.get("/api/auth/state").json()
    assert state["admin_exists"] is False
    assert state["setup_local_only"] is False
    r = client.post("/api/setup/admin", json=ADMIN)
    assert r.status_code == 200
    assert r.json()["secret_once"].startswith("derate-")


def test_setup_from_another_host_is_open_until_the_first_admin(tmp_path, monkeypatch):
    """Docker Desktop's gateway (192.168.65.1) and a LAN browser are not
    loopback, and both are how an operator reaches their own coordinator.
    Setup answers them; once an admin exists it is closed to everyone."""
    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path))
    app = create_app()
    remote = TestClient(app, client=("192.168.65.1", 50000))
    assert remote.get("/api/auth/state").json()["setup_local_only"] is False
    r = remote.post("/api/setup/admin", json=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["secret_once"].startswith("derate-")
    late = TestClient(app, client=("10.0.0.9", 50000))
    assert late.post("/api/setup/admin", json=ADMIN).status_code == 410
    local = TestClient(app, client=LOOPBACK)
    assert local.post("/api/setup/admin", json=ADMIN).status_code == 410


def test_the_node_shell_socket_wants_a_session(tmp_path, monkeypatch):
    """Only routed when DERATE_SHELL is set, so it is in no generated set."""
    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DERATE_SHELL", "1")
    client = TestClient(create_app())
    # Through included routers: newer FastAPI holds an _IncludedRouter on
    # app.routes rather than copying the router's routes onto the app.
    assert any(
        r["path"] == "/api/nodes/{node_id}/shell" for r in collect_routes(client.app)
    ), "DERATE_SHELL=1 did not route the shell socket"
    refusal = _socket_refusal(client, "/api/nodes/{node_id}/shell", {})
    assert (refusal.code, refusal.reason) == (1008, "session_required")


@pytest.mark.parametrize("path", ["/api/keys", "/api/no-such-route", "/v1/models"])
def test_options_is_gated_like_any_method(tmp_path, monkeypatch, path):
    """GA-4: an anonymous OPTIONS used to pass the gate, and Starlette's
    405-with-Allow versus 404 told a stranger which routes exist."""
    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DERATE_ALLOWED_ORIGINS", raising=False)
    r = TestClient(create_app()).options(path)
    assert r.status_code == 401, r.text
    expected = "missing_api_key" if path.startswith("/v1") else "session_required"
    assert r.json()["error"]["code"] == expected


def test_a_real_preflight_passes_when_an_origin_is_allowed(tmp_path, monkeypatch):
    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DERATE_ALLOWED_ORIGINS", "http://dev:5173")
    client = TestClient(create_app())
    r = client.options(
        "/api/keys",
        headers={"Origin": "http://dev:5173", "Access-Control-Request-Method": "GET"},
    )
    assert r.status_code == 200, r.text
    # Without the preflight header it is just an OPTIONS, and gated.
    assert client.options("/api/keys", headers={"Origin": "http://dev:5173"}).status_code == 401


# -- the edge listener (DERATE_EDGE_PORT) ------------------------------------

EDGE_PORT = 18099
#: A documentation address (RFC 5737), the caller Cloudflare would name.
WAN = {"CF-Connecting-IP": "203.0.113.9"}
#: Paths outside both sets that the main port answers without a credential.
EDGE_EXTRA = ["GET /", "GET /healthz", "GET /install.sh", "GET /auth/callback"]


@pytest.fixture(scope="module")
def edge(tmp_path_factory):
    """One app with an edge port, a client on each listener, and a valid key.
    Starlette's TestClient sets ``scope["server"]`` from the base URL's port,
    which is exactly what uvicorn sets from the accepted socket."""
    data = tmp_path_factory.mktemp("edge")
    mp = pytest.MonkeyPatch()
    mp.setenv("DERATE_DATA_DIR", str(data))
    mp.setenv("DERATE_EDGE_PORT", str(EDGE_PORT))
    mp.delenv("DERATE_EDGE_CLIENT_HEADER", raising=False)
    app = create_app()
    main = TestClient(app, client=LOOPBACK)
    r = main.post("/api/setup/admin", json=ADMIN)
    assert r.status_code == 200, r.text
    cookie = {"Cookie": f"derate_session={r.cookies['derate_session']}"}
    main.cookies.clear()
    m = main.post(
        "/api/keys",
        json={"name": "edge", "expiry_days": 30, "current_password": ADMIN["password"]},
        headers=cookie,
    )
    assert m.status_code == 200, m.text
    edge_client = TestClient(app, base_url=f"http://testserver:{EDGE_PORT}", client=LOOPBACK)
    yield {
        "app": app,
        "main": main,
        "edge": edge_client,
        "key": {"Authorization": f"Bearer {m.json()['secret_once']}"},
        "cookie": cookie,
    }
    mp.undo()


def _edge_socket(edge, path: str, headers: dict) -> WebSocketDisconnect:
    # websocket_connect joins onto ws://testserver and drops the base URL's
    # port, so the edge's port has to be in an absolute URL.
    return _socket_refusal(edge["edge"], f"ws://testserver:{EDGE_PORT}{path}", headers)


@pytest.mark.parametrize("route", sorted(set(API_ROUTES) | set(ALWAYS_OPEN) | set(EDGE_EXTRA)))
def test_the_edge_answers_nothing_but_v1(edge, route):
    method, path = route.split(" ", 1)
    edge["app"].state.key_refusals.reset()
    if method == "WEBSOCKET":
        refusal = _edge_socket(edge, path, {**WAN, **edge["cookie"]})
        assert refusal.code == 1008
        return
    # A session cookie and a valid key change nothing: the path decides.
    r = _send(edge["edge"], route, {**WAN, **edge["cookie"], **edge["key"]})
    assert r.status_code == 404, (route, r.status_code, r.text)
    assert r.json()["error"]["code"] == "not_found"
    assert "WWW-Authenticate" not in r.headers
    assert "set-cookie" not in r.headers


@pytest.mark.parametrize("route", V1_ROUTES)
def test_every_v1_route_on_the_edge_answers_as_the_main_port_does(edge, route):
    method, path = route.split(" ", 1)
    edge["app"].state.key_refusals.reset()
    if method == "WEBSOCKET":
        # Without the client header the socket never reaches the key gate...
        assert _edge_socket(edge, path + "?model=x", edge["key"]).code == 1008
        # ...with it, a bad key is refused by the key gate, by name.
        refusal = _edge_socket(edge, path, {**WAN, "Authorization": f"Bearer {GARBAGE}"})
        assert (refusal.code, refusal.reason) == (1008, "invalid_api_key")
        return
    main = _send(edge["main"], route, edge["key"])
    via_edge = _send(edge["edge"], route, {**WAN, **edge["key"]})
    assert via_edge.status_code == main.status_code, (route, via_edge.text, main.text)
    if via_edge.status_code >= 400 and "json" in via_edge.headers.get("content-type", ""):
        assert via_edge.json()["error"]["code"] not in ("not_found", "edge_client_unknown"), route


@pytest.mark.parametrize("client_header", [None, "nonsense", "203.0.113.9, 10.0.0.1", ""])
def test_an_edge_v1_call_without_a_usable_client_address_is_400(edge, client_header):
    headers = dict(edge["key"])
    if client_header is not None:
        headers["CF-Connecting-IP"] = client_header
    r = edge["edge"].get("/v1/models", headers=headers)
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "edge_client_unknown"
    assert "cf-connecting-ip" in r.json()["error"]["message"]
    # The main port never reads the header at all.
    assert edge["main"].get("/v1/models", headers=headers).status_code == 200


def test_the_edge_records_the_client_header_and_ignores_x_forwarded_for(edge):
    edge["app"].state.key_refusals.reset()
    stores = edge["app"].state.ctx.auth_stores
    r = edge["edge"].get(
        "/v1/models",
        headers={"CF-Connecting-IP": "2001:db8::7", "X-Forwarded-For": "198.51.100.1", **edge["key"]},
    )
    assert r.status_code == 200, r.text
    (key,) = [k for k in stores.keys.list_keys() if k.name == "edge"]
    assert key.last_used_ip == "2001:db8::7"


def test_the_edge_is_https_and_the_main_port_is_not(edge):
    via_edge = edge["edge"].get("/v1/models", headers={**WAN, **edge["key"]})
    assert via_edge.headers["strict-transport-security"].startswith("max-age=")
    main = edge["main"].get("/v1/models", headers=edge["key"])
    assert "strict-transport-security" not in main.headers


def test_the_main_port_is_unchanged_with_an_edge_configured(edge):
    main = edge["main"]
    assert main.get("/api/auth/state").status_code == 200
    assert main.get("/api/keys", headers=edge["cookie"]).status_code == 200
    assert main.get("/healthz").status_code == 200
    r = main.get("/v1/models", headers={"Authorization": f"Bearer {GARBAGE}"})
    assert (r.status_code, r.json()["error"]["code"]) == (401, "invalid_api_key")
