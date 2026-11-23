"""Every way a WAN caller might pass for someone else on the edge listener.

The edge (``DERATE_EDGE_PORT``) is the one socket the internet reaches. Its
whole security story is three claims, and each is attacked here the way a
caller would attack it:

* WHICH listener answered is a fact about the socket, never about a header --
  ``Host``, the request target and the rewrite headers cannot move a request
  onto the LAN port's rules;
* WHO is calling is ``CF-Connecting-IP`` and nothing else -- not
  ``X-Forwarded-For`` or any of its cousins, not a list, a port, a repeated
  header, loopback, or a second spelling of the same address;
* WHAT the caller may touch is ``/v1/**`` with a bearer key -- not ``/api``
  dressed as ``/v1``, not a key in a query, cookie or other header, not a
  session cookie, and not first-run setup on a fresh coordinator.

These run in-process. ``tests/edge_probe.py`` repeats the ones that need a
real socket (raw request targets, pipelining, smuggling) against a running
coordinator, and through a real tunnel.
"""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from control_plane.gateway import create_app

pytestmark = pytest.mark.raw_http

EDGE_PORT = 18097
ADMIN = {"username": "edge-admin", "email": "edge@derate.test", "password": "quiet-lantern-harbor-918273"}
WAN = {"CF-Connecting-IP": "203.0.113.9"}
LOOPBACK = ("127.0.0.1", 50000)


def _edge_client(app) -> TestClient:
    return TestClient(app, base_url=f"http://testserver:{EDGE_PORT}", client=LOOPBACK)


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setenv("DERATE_DATA_DIR", str(tmp_path_factory.mktemp("spoof")))
    mp.setenv("DERATE_EDGE_PORT", str(EDGE_PORT))
    mp.setenv("DERATE_SHELL", "1")
    mp.delenv("DERATE_EDGE_CLIENT_HEADER", raising=False)
    app = create_app()
    main = TestClient(app, client=LOOPBACK)
    r = main.post("/api/setup/admin", json=ADMIN)
    assert r.status_code == 200, r.text
    cookie = r.cookies["derate_session"]
    main.cookies.clear()
    m = main.post(
        "/api/keys",
        json={"name": "spoof", "expiry_days": 30, "current_password": ADMIN["password"]},
        headers={"Cookie": f"derate_session={cookie}"},
    )
    assert m.status_code == 200, m.text
    yield {
        "app": app,
        "main": main,
        "edge": _edge_client(app),
        "cookie": cookie,
        "key": m.json()["secret_once"],
        "key_id": m.json()["key"]["key_id"],
    }
    mp.undo()


@pytest.fixture(autouse=True)
def _fresh_budget(world):
    world["app"].state.key_refusals.reset()


def _bearer(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _last_used_ip(world) -> str | None:
    row = world["main"].get(
        f"/api/keys/{world['key_id']}", headers={"Cookie": f"derate_session={world['cookie']}"}
    )
    return row.json()["last_used_ip"]


# -- which listener ----------------------------------------------------------


@pytest.mark.parametrize("host", ["testserver", "testserver:80", "localhost", "127.0.0.1:8088"])
def test_the_host_header_cannot_move_a_request_off_the_edge(world, host):
    r = world["edge"].get("/api/auth/state", headers={**WAN, "Host": host})
    assert (r.status_code, r.json()["error"]["code"]) == (404, "not_found")


def test_the_host_header_cannot_move_a_request_onto_the_edge(world):
    r = world["main"].get("/api/auth/state", headers={"Host": f"testserver:{EDGE_PORT}"})
    assert r.status_code == 200 and "admin_exists" in r.json()


@pytest.mark.parametrize(
    "path",
    [
        "/v1/%2e%2e/api/keys",
        "/v1/%2E%2E/api/keys",
        "/v1/..%2fapi/keys",
        "/v1/models/%2e%2e/%2e%2e/api/keys",
        "//api/keys",
        "/V1/models",
        "/v1x/models",
        "/%76%31/../api/keys",
        "/api/keys%00/v1",
    ],
)
def test_api_dressed_as_v1_reaches_no_api_handler(world, path):
    headers = {**WAN, **_bearer(world["key"]), "Cookie": f"derate_session={world['cookie']}"}
    r = world["edge"].get(path, headers=headers)
    assert r.status_code in (400, 401, 404), (path, r.status_code, r.text[:200])
    assert world["key_id"] not in r.text and "admin_exists" not in r.text, path


@pytest.mark.parametrize(
    "header",
    ["X-Original-URL", "X-Rewrite-URL", "X-Forwarded-Prefix", "X-Forwarded-Path", "X-Override-URL"],
)
def test_rewrite_headers_do_not_reroute(world, header):
    r = world["edge"].get("/v1/models", headers={**WAN, **_bearer(world["key"]), header: "/api/keys"})
    assert r.status_code == 200 and r.json()["object"] == "list"


def test_a_method_override_header_is_not_a_method(world):
    r = world["edge"].get(
        "/v1/models", headers={**WAN, **_bearer(world["key"]), "X-HTTP-Method-Override": "DELETE"}
    )
    assert r.status_code == 200 and r.json()["object"] == "list"


def test_the_scheme_cannot_be_downgraded_on_the_edge(world):
    r = world["edge"].get(
        "/v1/models",
        headers={**WAN, **_bearer(world["key"]), "X-Forwarded-Proto": "http", "X-Forwarded-Scheme": "http"},
    )
    assert r.headers["strict-transport-security"].startswith("max-age=")


# -- who is calling ----------------------------------------------------------


@pytest.mark.parametrize(
    "forged",
    [
        {"X-Forwarded-For": "198.51.100.1"},
        {"X-Real-IP": "198.51.100.2"},
        {"True-Client-IP": "198.51.100.3"},
        {"X-Client-IP": "198.51.100.4"},
        {"Forwarded": "for=198.51.100.5;proto=https"},
        {"Fastly-Client-IP": "198.51.100.6"},
        {"X-Cluster-Client-IP": "198.51.100.7"},
    ],
)
def test_no_other_forwarding_header_names_the_caller(world, forged):
    r = world["edge"].get(
        "/v1/models", headers={"CF-Connecting-IP": "203.0.113.44", **forged, **_bearer(world["key"])}
    )
    assert r.status_code == 200
    assert _last_used_ip(world) == "203.0.113.44"


@pytest.mark.parametrize(
    "value",
    [
        "203.0.113.9, 198.51.100.1",
        "203.0.113.9:443",
        "[2001:db8::1]",
        "2001:db8::1%eth0",
        "127.0.0.1",
        "::1",
        "::ffff:127.0.0.1",
        "0.0.0.0",
        "::",
        "unknown",
        "",
        " ",
        "999.1.1.1",
        "203.0.113.009",
        "0x7f.0.0.1",
    ],
)
def test_the_client_header_must_be_one_plain_routable_address(world, value):
    r = world["edge"].get("/v1/models", headers={"CF-Connecting-IP": value, **_bearer(world["key"])})
    assert (r.status_code, r.json()["error"]["code"]) == (400, "edge_client_unknown"), value


def test_a_repeated_client_header_is_refused(world):
    """The edge writes one value; a second copy is the caller's own, and the
    first one in the list is exactly the one a naive reader would take."""
    headers = [
        ("CF-Connecting-IP", "198.51.100.9"),
        ("CF-Connecting-IP", "203.0.113.9"),
        ("Authorization", f"Bearer {world['key']}"),
    ]
    r = world["edge"].get("/v1/models", headers=headers)
    assert (r.status_code, r.json()["error"]["code"]) == (400, "edge_client_unknown")


def test_a_mapped_address_is_recorded_as_the_address_it_maps(world):
    r = world["edge"].get(
        "/v1/models", headers={"CF-Connecting-IP": "::ffff:203.0.113.20", **_bearer(world["key"])}
    )
    assert r.status_code == 200
    assert _last_used_ip(world) == "203.0.113.20"


def test_the_main_port_never_reads_the_client_header(world):
    """A LAN caller cannot borrow the edge's header to pass for a WAN one."""
    world["main"].get("/v1/models", headers={"CF-Connecting-IP": "203.0.113.55", **_bearer(world["key"])})
    assert _last_used_ip(world) == "127.0.0.1"


# -- the refusal budget ------------------------------------------------------


def _spend_budget(edge, address: str) -> None:
    from control_plane.contracts.auth import KEY_REFUSALS_PER_ADDRESS_PER_MIN

    for i in range(KEY_REFUSALS_PER_ADDRESS_PER_MIN):
        r = edge.get("/v1/models", headers={"CF-Connecting-IP": address, **_bearer(f"derate-{i:010d}_" + "x" * 43)})
        assert r.status_code == 401


def test_two_spellings_of_one_address_share_one_budget(world):
    _spend_budget(world["edge"], "203.0.113.60")
    r = world["edge"].get("/v1/models", headers={"CF-Connecting-IP": "::ffff:203.0.113.60", **_bearer(world["key"])})
    assert r.status_code == 429


def test_rotating_inside_an_ipv6_64_does_not_buy_a_new_budget(world):
    from control_plane.contracts.auth import KEY_REFUSALS_PER_ADDRESS_PER_MIN

    edge = world["edge"]
    for i in range(KEY_REFUSALS_PER_ADDRESS_PER_MIN):
        r = edge.get(
            "/v1/models",
            headers={"CF-Connecting-IP": f"2001:db8:1:2::{i + 1:x}", **_bearer("derate-aaaaaaaaaa_" + "x" * 43)},
        )
        assert r.status_code == 401
    rotated = edge.get(
        "/v1/models", headers={"CF-Connecting-IP": "2001:db8:1:2:ffff:ffff:ffff:fffe", **_bearer(world["key"])}
    )
    assert rotated.status_code == 429
    next_64 = edge.get("/v1/models", headers={"CF-Connecting-IP": "2001:db8:1:3::1", **_bearer(world["key"])})
    assert next_64.status_code == 200


# -- what may be touched -----------------------------------------------------


def _refused_401(r) -> bool:
    return r.status_code == 401 and r.json()["error"]["code"] in ("missing_api_key", "invalid_api_key")


def test_a_key_anywhere_but_the_bearer_header_is_no_key(world):
    key = world["key"]
    edge = world["edge"]
    attempts = [
        edge.get(f"/v1/models?api_key={key}", headers=WAN),
        edge.get(f"/v1/models?key={key}", headers=WAN),
        edge.get("/v1/models", headers={**WAN, "X-API-Key": key}),
        edge.get("/v1/models", headers={**WAN, "Api-Key": key}),
        edge.get("/v1/models", headers={**WAN, "Cookie": f"api_key={key}"}),
        edge.get("/v1/models", headers={**WAN, "Cookie": f"derate_session={world['cookie']}"}),
        edge.get(
            "/v1/models",
            headers={**WAN, "Authorization": "Basic " + base64.b64encode(f"{key}:".encode()).decode()},
        ),
    ]
    assert all(_refused_401(r) for r in attempts), [r.status_code for r in attempts]


@pytest.mark.parametrize(
    "mangle",
    [
        lambda k: "drt_" + k.removeprefix("derate-"),
        lambda k: k.upper(),
        lambda k: "DERATE-" + k.removeprefix("derate-"),
        lambda k: k.replace("e", "\u0435", 1),  # Cyrillic e
        lambda k: k + "\u200b",  # zero-width space
        lambda k: k[:-1],
        lambda k: k.replace("_", "-", 1),
    ],
    ids=["legacy-prefix", "upper", "upper-prefix", "homoglyph", "zero-width", "truncated", "separator"],
)
def test_a_lookalike_key_is_refused(world, mangle):
    raw = mangle(world["key"])
    try:
        raw.encode("latin-1")
    except UnicodeEncodeError:
        # Not sendable as an HTTP header value at all; the client library
        # refuses it before the wire, which is the same outcome.
        with pytest.raises(UnicodeEncodeError):
            world["edge"].get("/v1/models", headers={**WAN, **_bearer(raw)})
        return
    assert _refused_401(world["edge"].get("/v1/models", headers={**WAN, **_bearer(raw)}))


def test_the_shell_relay_is_not_reachable_through_the_edge(world):
    with pytest.raises(WebSocketDisconnect) as caught:
        with world["edge"].websocket_connect(
            f"ws://testserver:{EDGE_PORT}/api/nodes/x/shell",
            headers={**WAN, "Cookie": f"derate_session={world['cookie']}"},
        ):
            pass
    assert caught.value.code == 1008


def test_first_run_setup_cannot_be_claimed_through_the_edge(tmp_path, monkeypatch):
    """A fresh coordinator answers setup to anyone -- on the LAN. Through the
    tunnel that would hand the whole cluster to the first stranger."""
    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DERATE_EDGE_PORT", str(EDGE_PORT))
    app = create_app()
    edge = _edge_client(app)
    r = edge.post("/api/setup/admin", json=ADMIN, headers=WAN)
    assert (r.status_code, r.json()["error"]["code"]) == (404, "not_found")
    r = edge.post("/v1/../api/setup/admin", json=ADMIN, headers=WAN)
    assert r.status_code == 404
    state = TestClient(app, client=LOOPBACK).get("/api/auth/state").json()
    assert state["admin_exists"] is False


# -- found by the blue-team review (2026-11-23) -------------------------------


@pytest.mark.parametrize("host", ["testserver/v1", "x#", "x?", "[", "x/v1#"])
def test_a_host_that_would_move_the_path_is_refused_on_the_edge(world, host):
    """Starlette rebuilds ``request.url`` from ``Host``. The gate used to
    decide on that rebuilt path while the router used ``scope["path"]``, so
    ``Host: testserver/v1`` put ``/api/keys`` under the /v1 key gate and a
    key holder got the key list."""
    r = world["edge"].get("/api/keys", headers={**WAN, **_bearer(world["key"]), "Host": host})
    assert (r.status_code, r.json()["error"]["code"]) == (400, "invalid_host"), r.text[:200]
    assert world["key_id"] not in r.text


def test_a_cross_site_write_to_api_is_the_edge_404_not_a_403(world):
    """The cross-site check ran first and answered 403 naming
    DERATE_ALLOWED_ORIGINS: the edge admitted /api exists behind it."""
    r = world["edge"].post(
        "/api/auth/login", json={}, headers={**WAN, "Origin": "https://evil.example"}
    )
    assert (r.status_code, r.json()["error"]["code"]) == (404, "not_found")
    assert "DERATE_ALLOWED_ORIGINS" not in r.text


def _edge_app_with_ui(tmp_path, monkeypatch):
    ui = tmp_path / "ui"
    (ui / "assets").mkdir(parents=True)
    (ui / "index.html").write_text("<!doctype html><html>admin ui</html>")
    (ui / "assets" / "a.js").write_text("console.log('bundle')")
    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DERATE_EDGE_PORT", str(EDGE_PORT))
    monkeypatch.setenv("DERATE_UI_DIR", str(ui))
    app = create_app()
    main = TestClient(app, client=LOOPBACK)
    key = main.post("/api/setup/admin", json=ADMIN).json()["secret_once"]
    return app, key


# Encoded, because an HTTP client resolves a literal "/v1/./x" itself before
# sending; tests/edge_probe.py sends the literal forms over a raw socket.
@pytest.mark.parametrize(
    "path", ["/v1/..%2Fcluster", "/v1/..%2F", "/v1/..%2Fassets/a.js", "/v1/%2E/models", "/v1/%2E%2E/index.html"]
)
def test_a_dot_segment_on_the_edge_never_reaches_the_ui(tmp_path, monkeypatch, path):
    """uvicorn decodes %2F, so /v1/..%2Fcluster arrives as /v1/../cluster:
    under /v1 by prefix, and resolved by the static mount to the admin UI."""
    app, key = _edge_app_with_ui(tmp_path, monkeypatch)
    r = _edge_client(app).get(path, headers={**WAN, **_bearer(key)})
    assert (r.status_code, r.json()["error"]["code"]) == (404, "not_found"), (path, r.text[:120])
    assert "admin ui" not in r.text and "bundle" not in r.text


def test_a_flood_stops_being_written_down_but_is_still_counted(world, caplog):
    """Past an address's budget, throttled 429s and edge 404s are answered
    but write no access line each: a flood would otherwise rotate node.log's
    history away. The folded security row still counts every one."""
    import logging

    from control_plane.contracts.auth import KEY_REFUSALS_PER_ADDRESS_PER_MIN
    from control_plane.gateway.authz import EDGE_MISSES_PER_ADDRESS_PER_MIN

    caller = {"CF-Connecting-IP": "203.0.113.120"}
    bad = _bearer("derate-bbbbbbbbbb_" + "x" * 43)
    with caplog.at_level(logging.INFO):
        codes = [world["edge"].get("/v1/models", headers={**caller, **bad}).status_code for _ in range(60)]
        misses = [world["edge"].get("/nope", headers=caller).status_code for _ in range(EDGE_MISSES_PER_ADDRESS_PER_MIN + 40)]
    assert codes.count(401) == KEY_REFUSALS_PER_ADDRESS_PER_MIN and codes.count(429) == 60 - KEY_REFUSALS_PER_ADDRESS_PER_MIN
    assert set(misses) == {404}
    access = [r for r in caplog.records if r.name == "gateway.access" and "203.0.113.120" in r.getMessage()]
    assert len(access) == KEY_REFUSALS_PER_ADDRESS_PER_MIN + EDGE_MISSES_PER_ADDRESS_PER_MIN
    warnings = [r for r in caplog.records if r.name == "gateway.security" and "203.0.113.120" in r.getMessage()]
    assert len(warnings) == 1, [w.getMessage() for w in warnings]


def test_rotating_inside_a_64_opens_one_security_row(world):
    """The security-event fold was per full address while the throttle was
    per /64, so rotating the low bits wrote a permanent audit row (two
    fsyncs) per request."""
    from control_plane.contracts.auth import AuditAction

    bad = _bearer("derate-cccccccccc_" + "x" * 43)
    for i in range(50):
        world["edge"].get("/v1/models", headers={"CF-Connecting-IP": f"2001:db8:77:1::{i + 1:x}", **bad})
    ctx = world["app"].state.ctx
    ctx.secevents.flush()
    entries, _ = ctx.auth_stores.auth.audit_page(action=AuditAction.KEY_REFUSED.value)
    rows = [e for e in entries if (e.ip or "").startswith("2001:db8:77:1:")]
    assert len(rows) == 1, [(e.ip, e.after) for e in rows]
