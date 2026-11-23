"""What the gate records and the headers every gated answer carries.

Refusals become rate-limited security-event rows, an unaudited /api write
becomes a generic ``api_write`` row, every gated request leaves one
``gateway.access`` line, sockets refuse a foreign ``Origin``, and responses
carry the cache, SVG and cookie protections from headers.py / auth_api.py.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.websockets import WebSocketDisconnect

from control_plane.contracts.auth import AuditAction
from control_plane.gateway import authz, create_app

pytestmark = pytest.mark.raw_http

ADMIN = {"username": "gate-admin", "email": "gate@derate.test", "password": "quiet-harbor-lamp-7719"}


def _route(app, path: str, endpoint, methods: list[str]) -> None:
    """A test-only route, ahead of any static mount."""
    app.router.routes.insert(0, APIRoute(path, endpoint, methods=methods))


@pytest.fixture
def gate(tmp_path, monkeypatch):
    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path))
    app = create_app()
    client = TestClient(app, client=("127.0.0.1", 50000))
    r = client.post("/api/setup/admin", json=ADMIN)
    assert r.status_code == 200, r.text
    cookie = r.cookies["derate_session"]
    client.cookies.clear()
    return app, client, {"Cookie": f"derate_session={cookie}"}


def _rows(app, action: AuditAction):
    app.state.ctx.secevents.flush()
    entries, _ = app.state.ctx.auth_stores.auth.audit_page(action=action.value)
    return list(entries)


def test_key_refusals_are_one_row_per_minute_per_source_with_a_count(gate):
    app, client, _ = gate
    bad = "derate-abcdefghij_" + "x" * 43
    for _ in range(3):
        r = client.get("/v1/models", headers={"Authorization": f"Bearer {bad}"})
        assert r.status_code == 401
    rows = _rows(app, AuditAction.KEY_REFUSED)
    assert len(rows) == 1
    assert rows[0].actor == "-"
    assert rows[0].after["presented"] == "derate-abcdefghij"
    assert rows[0].ip == "127.0.0.1"
    assert "x" * 43 not in str(rows[0].after)
    # The next row for the same source carries what was folded in since.
    app.state.ctx.secevents._window = 0.0
    client.get("/v1/models", headers={"Authorization": f"Bearer {bad}"})
    rows = _rows(app, AuditAction.KEY_REFUSED)
    assert len(rows) == 2
    assert sorted(r.after["count"] for r in rows) == [1, 3]


def test_a_fresh_fake_key_id_per_request_is_still_one_row_per_address(gate):
    app, client, _ = gate
    for i in range(200):
        bad = f"derate-{i:010d}_" + "x" * 43
        # 401 until the address's refusal budget is spent, 429 after; both
        # are KEY_REFUSED events from one address and fold into one row.
        status = client.get("/v1/models", headers={"Authorization": f"Bearer {bad}"}).status_code
        assert status in (401, 429)
    rows = _rows(app, AuditAction.KEY_REFUSED)
    assert len(rows) == 1 and rows[0].after["count"] == 1
    app.state.ctx.secevents._window = 0.0
    client.get("/v1/models", headers={"Authorization": "Bearer derate-zzzzzzzzzz_" + "x" * 43})
    rows = _rows(app, AuditAction.KEY_REFUSED)
    assert sorted(r.after["count"] for r in rows) == [1, 200]


def test_refused_keys_from_one_address_are_throttled_without_a_lookup(gate, monkeypatch):
    """Past KEY_REFUSALS_PER_ADDRESS_PER_MIN bad keys inside a minute an
    address is answered 429 before the key store is read -- a valid key
    included, since the budget is the address's -- while another address is
    still 401, and the budget comes back once the window drains."""
    from control_plane.contracts.auth import KEY_REFUSALS_PER_ADDRESS_PER_MIN
    from control_plane.keys.service import KeyService

    app, client, cookie = gate
    now = [1000.0]
    app.state.key_refusals._clock = lambda: now[0]
    lookups: list[str] = []
    real = KeyService.authenticate

    def counting(self, token, **kw):
        lookups.append(token)
        return real(self, token, **kw)

    monkeypatch.setattr(KeyService, "authenticate", counting)
    minted = client.post(
        "/api/keys",
        json={"name": "throttle", "expiry_days": 30, "current_password": ADMIN["password"]},
        headers=cookie,
    )
    assert minted.status_code == 200, minted.text
    good = {"Authorization": f"Bearer {minted.json()['secret_once']}"}
    assert client.get("/v1/models", headers=good).status_code == 200

    def bad(i: int) -> dict:
        return {"Authorization": f"Bearer derate-{i:010d}_" + "x" * 43}

    for i in range(KEY_REFUSALS_PER_ADDRESS_PER_MIN):
        assert client.get("/v1/models", headers=bad(i)).status_code == 401
    seen = len(lookups)
    r = client.get("/v1/models", headers=bad(99))
    assert r.status_code == 429, r.text
    assert r.json()["error"]["code"] == "rate_limited"
    assert r.headers["retry-after"] == "60"
    assert "WWW-Authenticate" not in r.headers
    assert len(lookups) == seen  # no store read for the throttled answer
    assert client.get("/v1/models", headers=good).status_code == 429
    other = TestClient(app, client=("10.0.0.7", 50000))
    assert other.get("/v1/models", headers=bad(5)).status_code == 401
    assert other.get("/v1/models", headers=good).status_code == 200
    # The socket gate shares the window.
    with pytest.raises(WebSocketDisconnect) as caught:
        with client.websocket_connect("/v1/realtime", headers=good):
            pass
    assert (caught.value.code, caught.value.reason) == (1008, "rate_limited")
    now[0] += 61.0
    assert client.get("/v1/models", headers=bad(98)).status_code == 401
    assert client.get("/v1/models", headers=good).status_code == 200


def test_a_refusal_returns_before_its_row_is_written():
    import threading

    from control_plane.gateway.secevents import SecurityEvents

    release = threading.Event()
    written: list[dict] = []

    def slow_audit(*args, **kwargs):
        release.wait(5)
        written.append(kwargs["after"])

    events = SecurityEvents(slow_audit, queue_max=1)
    events.note(AuditAction.KEY_REFUSED, actor="derate-aaaaaaaaaa", ip="10.0.0.1")
    assert written == []  # note() did not wait for the slow write
    # A full queue drops and counts; the next written row says how many.
    events.note(AuditAction.KEY_REFUSED, actor="-", ip="10.0.0.2")
    events.note(AuditAction.KEY_REFUSED, actor="-", ip="10.0.0.3")
    release.set()
    assert events.flush()
    events.note(AuditAction.KEY_REFUSED, actor="-", ip="10.0.0.4")
    assert events.flush()
    assert written[-1].get("dropped", 0) >= 1


def test_missing_session_is_a_session_refused_row(gate):
    app, client, _ = gate
    assert client.get("/api/keys").status_code == 401
    assert client.get("/api/keys").status_code == 401
    rows = _rows(app, AuditAction.SESSION_REFUSED)
    assert len(rows) == 1 and rows[0].actor == "-"


def test_an_unaudited_write_gets_the_generic_row_and_an_audited_one_does_not(gate):
    app, client, cookie = gate

    async def plain() -> JSONResponse:
        return JSONResponse({"ok": True})

    async def audited(request: Request) -> JSONResponse:
        authz.audit_request(request, AuditAction.SETTINGS_CHANGED, "settings", "x")
        return JSONResponse({"ok": True})

    _route(app, "/api/test/plain", plain, ["PUT"])
    _route(app, "/api/test/audited", audited, ["POST"])
    r = client.put("/api/test/plain?x=1", headers=cookie)
    assert r.status_code == 200
    r2 = client.post("/api/test/audited", headers=cookie)
    assert r2.status_code == 200
    rows = _rows(app, AuditAction.API_WRITE)
    assert [(row.target_kind, row.target_id) for row in rows] == [("route", "PUT /api/test/plain")]
    assert rows[0].actor == ADMIN["username"]
    assert rows[0].after == {"status": 200, "query": "x=1"}
    assert len(_rows(app, AuditAction.SETTINGS_CHANGED)) == 1


def test_a_dry_run_plan_is_not_an_admin_change(gate):
    app, client, cookie = gate
    client.post("/api/plan", json={"model_id": "Qwen/Qwen2.5-0.5B-Instruct"}, headers=cookie)
    assert _rows(app, AuditAction.API_WRITE) == []


def test_every_gated_request_leaves_one_access_line(gate, caplog):
    _, client, cookie = gate
    with caplog.at_level(logging.INFO, logger="gateway.access"):
        r = client.get("/api/keys", headers={**cookie, "User-Agent": "drill/1"})
    rid = r.headers["x-request-id"]
    lines = [rec.getMessage() for rec in caplog.records if rec.name == "gateway.access"]
    assert len(lines) == 1
    line = lines[0]
    assert line.startswith(
        f"rid={rid} ip=127.0.0.1 port=50000 peer=127.0.0.1 who=session:{ADMIN['username']} GET /api/keys q='' 200 "
    )
    assert line.endswith("ua='drill/1'")


def test_a_socket_from_another_origin_is_closed_before_its_cookie_is_read(tmp_path, monkeypatch):
    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DERATE_SHELL", "1")
    app = create_app()
    client = TestClient(app, client=("127.0.0.1", 50000))
    path = "/api/nodes/x/shell"

    def refusal(headers: dict) -> WebSocketDisconnect:
        with pytest.raises(WebSocketDisconnect) as caught:
            with client.websocket_connect(path, headers=headers):
                pass
        return caught.value

    foreign = refusal({"Origin": "http://evil.example"})
    assert (foreign.code, foreign.reason) == (1008, "cross_origin_refused")
    assert len(_rows(app, AuditAction.CROSS_ORIGIN_REFUSED)) == 1
    # Same origin and no origin get past the origin check (to the session one).
    for headers in ({"Origin": "http://testserver"}, {}):  
        assert refusal(headers).reason == "session_required"


def test_svg_is_sandboxed_and_api_json_is_not_cached(gate):
    app, client, cookie = gate

    async def svg() -> Response:
        return Response(b"<svg/>", media_type="image/svg+xml")

    async def public() -> JSONResponse:
        return JSONResponse({}, headers={"cache-control": "public, max-age=60"})

    _route(app, "/api/test/logo", svg, ["GET"])
    _route(app, "/api/test/public", public, ["GET"])
    r = client.get("/api/test/logo", headers=cookie)
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "sandbox" in csp
    assert r.headers["content-disposition"] == "inline"
    assert client.get("/api/auth/state").headers["cache-control"] == "no-store"
    assert client.get("/api/test/public", headers=cookie).headers["cache-control"] == "public, max-age=60"
    assert "strict-transport-security" not in r.headers


def test_hsts_over_https(gate):
    _, client, _ = gate
    r = client.get("https://testserver/api/auth/state")
    assert r.headers["strict-transport-security"].startswith("max-age=")


def test_public_https_origin_makes_the_cookie_secure_over_plaintext(tmp_path, monkeypatch):
    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DERATE_PUBLIC_ORIGIN", "https://x")
    client = TestClient(create_app(), client=("127.0.0.1", 50000))
    r = client.post("/api/setup/admin", json=ADMIN)
    assert r.status_code == 200
    assert "secure" in r.headers["set-cookie"].lower()
    # The public origin is a same-site write, not a cross-origin one.
    r = client.post("/api/auth/logout", headers={"Origin": "https://x"})
    assert r.status_code == 200


def test_logout_without_a_session_is_200(gate):
    _, client, _ = gate
    r = client.post("/api/auth/logout")
    assert r.status_code == 200
    assert r.json() == {"logged_out": True}


def test_a_typed_newline_cannot_forge_a_security_line(caplog):
    """GA-3: actor and detail are what the caller typed; the WARNING line
    escapes them, the row keeps them raw."""
    from control_plane.gateway.secevents import SecurityEvents

    rows: list[tuple] = []
    events = SecurityEvents(lambda *a, **k: rows.append((a, k)))
    forged = "root\n2026-11-18T10:00:00Z WARNING gateway.security forged"
    with caplog.at_level(logging.WARNING, logger="gateway.security"):
        events.note(AuditAction.SESSION_REFUSED, actor=forged, ip="10.0.0.1",
                    detail={"path": "/api/x\x1b[2J"})
    assert events.flush()
    (line,) = [r.getMessage() for r in caplog.records if r.name == "gateway.security"]
    assert "\n" not in line and "\x1b" not in line
    assert "\\x0a" in line and "\\x1b" in line
    assert rows[0][0][0] == forged  # the structured row is not rewritten


def test_stale_folded_counts_are_flushed_as_rows_then_pruned(monkeypatch):
    """EV-3: a triple idle for ten windows that still holds folded events
    used to stay in ``_seen`` forever and its count was never written."""
    from control_plane.gateway import secevents

    monkeypatch.setattr(secevents, "_PRUNE_AT", 8)
    now = [0.0]
    rows: list[tuple] = []
    events = secevents.SecurityEvents(lambda *a, **k: rows.append((a, k)), clock=lambda: now[0])
    for i in range(9):
        for _ in range(3):  # one row, then two folded into a pending count
            events.note(AuditAction.SESSION_REFUSED, actor="-", ip=f"10.0.1.{i}")
    assert events.flush()
    assert len(rows) == 9
    now[0] = 11 * secevents.WINDOW_S
    events.note(AuditAction.SESSION_REFUSED, actor="-", ip="10.9.9.9")
    assert events.flush()
    assert len(events._seen) == 1
    flushed = sorted(
        (k["ip"], k["after"]["count"]) for a, k in rows[9:] if k["ip"].startswith("10.0.1.")
    )
    assert flushed == [(f"10.0.1.{i}", 2) for i in range(9)]
    assert all(a[1] is AuditAction.SESSION_REFUSED for a, _ in rows[9:])


def test_password_checks_in_flight_are_capped_per_address_and_in_total(tmp_path, monkeypatch):
    """GA-2: each login is an argon2id hash on the shared thread pool; past
    LOGIN_INFLIGHT_PER_IP from one address, or VERIFY_INFLIGHT_MAX in all,
    the gateway answers 429 without verifying."""
    import asyncio
    import threading

    import httpx

    from control_plane.auth.service import AuthService
    from control_plane.contracts.auth import LOGIN_INFLIGHT_PER_IP, VERIFY_INFLIGHT_MAX

    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path))
    app = create_app()
    release = threading.Event()
    calls: list[str] = []

    def slow_login(self, username, password, **context):
        calls.append(username)
        release.wait(10)
        from control_plane.contracts.auth import InvalidCredentials

        raise InvalidCredentials()

    monkeypatch.setattr(AuthService, "login", slow_login)

    def client(ip: str) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=app, client=(ip, 40000))
        return httpx.AsyncClient(transport=transport, base_url="http://localhost")

    async def login(ip: str, n: int):
        async with client(ip) as c:
            return await c.post(
                "/api/auth/login", json={"username": f"u{n}", "password": "x" * 12}
            )

    async def wait_for(count: int) -> None:
        for _ in range(500):
            if len(calls) >= count:
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"only {len(calls)} logins reached the verifier")

    async def scenario():
        held = [asyncio.create_task(login("10.0.0.1", n)) for n in range(LOGIN_INFLIGHT_PER_IP)]
        await wait_for(LOGIN_INFLIGHT_PER_IP)
        same_ip = await login("10.0.0.1", 99)
        others = [
            asyncio.create_task(login(f"10.0.0.{2 + n}", n))
            for n in range(VERIFY_INFLIGHT_MAX - LOGIN_INFLIGHT_PER_IP)
        ]
        await wait_for(VERIFY_INFLIGHT_MAX)
        over_total = await login("10.0.0.200", 98)
        release.set()
        finished = await asyncio.gather(*held, *others)
        after = await login("10.0.0.1", 97)
        return same_ip, over_total, finished, after

    try:
        same_ip, over_total, finished, after = asyncio.run(scenario())
    finally:
        release.set()
    for refused in (same_ip, over_total):
        assert refused.status_code == 429, refused.text
        assert refused.headers["retry-after"] == "1"
        assert refused.json()["error"]["code"] == "login_locked"
    assert [r.status_code for r in finished] == [401] * VERIFY_INFLIGHT_MAX
    assert after.status_code == 401  # the slots were given back
    assert "u99" not in calls and "u98" not in calls


def test_the_refusal_window_is_bounded_and_drains_without_scanning(monkeypatch):
    """over() runs on every /v1 request, valid ones included. It used to copy
    and scan every tracked address each time, so a spread-out flood made every
    caller's request slower. Now a read creates nothing, the map has a hard
    bound, and expired buckets leave from the front."""
    from control_plane.gateway.authz import RefusalWindow

    now = [0.0]
    window = RefusalWindow(20, clock=lambda: now[0])
    monkeypatch.setattr(RefusalWindow, "MAX_BUCKETS", 100)
    for i in range(150):
        window.note(f"198.18.{i // 250}.{i % 250 + 1}")
    assert len(window._window) == 100
    assert window.over("203.0.113.1") is False
    assert "203.0.113.1" not in window._window  # a read creates no bucket
    now[0] = 61.0
    window.over("203.0.113.1")
    assert len(window._window) == 0


@pytest.mark.parametrize("action", [AuditAction.KEY_REFUSED, AuditAction.CROSS_ORIGIN_REFUSED, AuditAction.SESSION_REFUSED])
def test_security_events_fold_per_64_and_cut_caller_strings(action):
    """Every action folds per /64 and every caller-chosen string is cut: a
    cross-origin refusal folded per full address and kept its Origin uncut,
    so rotating the low bits wrote a permanent row per request as large as
    the header."""
    from control_plane.gateway.secevents import _DETAIL_MAX, SecurityEvents

    rows: list[tuple] = []
    events = SecurityEvents(lambda *a, **k: rows.append((a, k)), clock=lambda: 0.0)
    for i in range(500):
        events.note(action, actor="-", ip=f"2001:db8:1:2::{i + 1:x}",
                    detail={"path": "/v1/" + "a" * 20_000, "origin": "https://" + "b" * 20_000})
    assert events.flush()
    assert len(rows) == 1
    args, kwargs = rows[0]
    assert len(kwargs["after"]["path"]) <= _DETAIL_MAX and len(args[3]) <= _DETAIL_MAX
    assert len(kwargs["after"]["origin"]) <= _DETAIL_MAX


def test_a_folded_count_row_names_a_real_address():
    """The count row a prune writes for a /64 used to carry the CIDR as its
    ip, which no search by address ever matches."""
    import ipaddress

    from control_plane.gateway import secevents

    now = [0.0]
    rows: list[tuple] = []
    events = secevents.SecurityEvents(lambda *a, **k: rows.append((a, k)), clock=lambda: now[0])
    events.note(AuditAction.KEY_REFUSED, actor="-", ip="2001:db8:5:6::1")
    events.note(AuditAction.KEY_REFUSED, actor="-", ip="2001:db8:5:6::2")  # folded
    events._pruned_at = -1e9
    now[0] = 11 * secevents.WINDOW_S
    for i in range(secevents._PRUNE_AT + 1):
        events.note(AuditAction.KEY_REFUSED, actor="-", ip=f"10.1.{i // 256}.{i % 256}")
    assert events.flush()
    pending = [k for _, k in rows if k["after"].get("bucket") == "2001:db8:5:6::/64"]
    assert len(pending) == 1 and pending[0]["after"]["count"] == 1
    assert ipaddress.ip_address(pending[0]["ip"]) == ipaddress.ip_address("2001:db8:5:6::2")


def test_closing_writes_the_counts_no_row_has_reported():
    """Folded repeats were reported only by a prune or an eviction, and the
    writer is a daemon thread: a flood that stopped before a restart lost its
    last minute's count."""
    from control_plane.gateway.secevents import SecurityEvents

    rows: list[tuple] = []
    events = SecurityEvents(lambda *a, **k: rows.append((a, k)), clock=lambda: 0.0)
    for _ in range(3):
        events.note(AuditAction.SESSION_REFUSED, actor="-", ip="10.0.0.1")
    assert events.close()
    assert [k["after"]["count"] for _, k in rows] == [1, 2]


def test_tracked_triples_are_capped_and_a_pending_count_survives_eviction(monkeypatch):
    from control_plane.gateway import secevents

    monkeypatch.setattr(secevents, "_SEEN_MAX", 10)
    rows: list[tuple] = []
    events = secevents.SecurityEvents(lambda *a, **k: rows.append((a, k)), clock=lambda: 0.0)
    for i in range(10):
        events.note(AuditAction.SESSION_REFUSED, actor="-", ip=f"10.0.0.{i}")
        events.note(AuditAction.SESSION_REFUSED, actor="-", ip=f"10.0.0.{i}")  # folded
    events.note(AuditAction.SESSION_REFUSED, actor="-", ip="10.0.1.1")  # evicts 10.0.0.0
    assert events.flush()
    assert len(events._seen) == 10
    first = [k["after"]["count"] for _, k in rows if k["ip"] == "10.0.0.0"]
    assert first == [1, 1]  # its row, then the folded repeat reported on eviction
