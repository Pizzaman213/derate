"""Every gateway in this suite is secured; this is how its tests get in.

The gateway refuses ``/api`` without an admin session and ``/v1`` without a
``derate-`` key (``control_plane/gateway/authz.py``), and there is no switch that
turns that off -- the stub app ``create_app()`` builds is secured exactly like
the real one. So the ~150 ``TestClient(create_app(...))`` call sites in this
directory sign in the way a person does.

**This is the real auth path, exercised on every request, not a bypass.** The
first gated request a client makes with no ``Cookie`` / ``Authorization`` of
its own is preceded by ``POST /api/setup/admin`` (or ``POST /api/auth/login``
once setup has closed, plus ``POST /api/keys`` for a key), through the same
app and the same middleware. The session cookie and the minted key are then
attached to that request and every later one: the cookie on ``/api``, the
key as ``Authorization: Bearer`` on ``/v1``. Every guard, store and hash runs.
Credentials are cached per auth store for the length of one test.

A test that asserts on what an unauthenticated caller gets (401, 410, a
socket closed 1008) opts out with ``@pytest.mark.raw_http``: nothing is
attached, and whatever it sends is what the gateway sees. A gateway served
on a real socket gets the same treatment through :func:`with_credentials`,
and :func:`credentials` hands a test the pair to send itself.

Also here: ``DERATE_DATA_DIR`` points at a fresh temporary directory for any
test that has not set it, so no test writes into the developer's real data
directory -- which is where every store, the telemetry archive included,
lands otherwise.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from control_plane.contracts.auth import SESSION_COOKIE, SESSION_COOKIE_SECURE
from control_plane.gateway.authz import OPEN_ROUTES, SETUP_ROUTE

BOOT_USER = "test-admin"
BOOT_EMAIL = "test-admin@derate.test"
BOOT_PASSWORD = "suite-boot-pw-123456"
BOOT_KEY_NAME = "test-bootstrap"

_raw = {"on": False}
#: str(auth.db path) -> (session cookie value, raw derate- key)
_creds: dict[str, tuple[str, str]] = {}


@pytest.fixture(autouse=True)
def _derate_data_dir(tmp_path_factory, monkeypatch):
    # Its own directory, not under tmp_path: a test that asserts what its
    # tmp_path holds must find only what it put there.
    if not os.environ.get("DERATE_DATA_DIR"):
        monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path_factory.mktemp("derate-data")))
    # No test may reach download.db-ip.com: every app the suite builds reads
    # this when create_app constructs its GatewaySettings.
    monkeypatch.setenv("DERATE_GEOIP_DOWNLOAD", "0")
    # Nor ask this machine's tailscaled: a box that runs Tailscale would place
    # every loopback client the suite sends, and differently on every box.
    monkeypatch.setenv("DERATE_GEOIP_TAILSCALE", "0")
    yield


@pytest.fixture(autouse=True)
def _no_port_preflight(monkeypatch):
    # Every DeploymentManager dials each plan node before a launch
    # (manager._preflight_port). The suite's nodes are fixture addresses
    # on somebody's LAN, so the real SYN would time out -- and refuse -- on
    # every launch test. Answered "closed" (free, and reachable) here; a test
    # about the check passes its own `port_reach_fn`, and one about the real
    # function asks for this fixture by name, which yields it.
    import control_plane.deploy.manager as manager

    real = manager.port_reachable
    monkeypatch.setattr(
        manager, "port_reachable", lambda host, port: (manager.PORT_CLOSED, "")
    )
    yield real


@pytest.fixture(autouse=True)
def _derate_auth(request):
    _raw["on"] = request.node.get_closest_marker("raw_http") is not None
    _creds.clear()
    yield
    _raw["on"] = False
    _creds.clear()


# -- deciding ------------------------------------------------------------------


def _stores_of(app):
    """The app's AuthStores, or None for anything that is not a gateway."""
    for _ in range(4):
        ctx = getattr(getattr(app, "state", None), "ctx", None)
        stores = getattr(ctx, "auth_stores", None)
        if stores is not None:
            return stores
        app = getattr(app, "app", None)
        if app is None:
            return None
    return None


def _needs(method: str, path: str) -> str | None:
    """'cookie' for a gated /api route, 'key' for /v1, None for the rest."""
    if path == "/v1" or path.startswith("/v1/"):
        return "key"
    if path == "/api" or path.startswith("/api/"):
        route = ("GET" if method.upper() == "HEAD" else method.upper(), path)
        if route in OPEN_ROUTES or route == SETUP_ROUTE or method.upper() == "OPTIONS":
            return None
        return "cookie"
    return None


def _has(headers: httpx.Headers, need: str, jar) -> bool:
    if need == "key":
        return "authorization" in headers
    if "cookie" in headers:
        return True
    return jar is not None and (
        jar.get(SESSION_COOKIE) is not None or jar.get(SESSION_COOKIE_SECURE) is not None
    )


def _with(headers, extra: dict[str, str]) -> httpx.Headers:
    merged = httpx.Headers(headers or {})
    for name, value in extra.items():
        merged[name] = value
    return merged


def _attach(need: str, creds: tuple[str, str]) -> dict[str, str]:
    cookie, key = creds
    if need == "key":
        return {"Authorization": f"Bearer {key}"}
    return {"Cookie": f"{SESSION_COOKIE}={cookie}"}


def _cookie_from(set_cookie_headers: list[str]) -> str | None:
    for header in set_cookie_headers:
        name, _, rest = header.partition("=")
        if name.strip() in (SESSION_COOKIE, SESSION_COOKIE_SECURE):
            value = rest.split(";", 1)[0].strip()
            if value:
                return value
    return None


def _boot_failed(step: str, status: int, text: str) -> AssertionError:
    return AssertionError(
        f"tests/unit/conftest.py could not sign in ({step} answered {status}: {text[:300]}). "
        "A test that creates its own admin or asserts on unauthenticated access "
        "should be marked @pytest.mark.raw_http."
    )


# -- TestClient ----------------------------------------------------------------


def _boot_sync(client: TestClient, stores) -> tuple[str, str]:
    ident = str(stores.auth_path)
    if ident in _creds:
        return _creds[ident]
    post = _orig_request.__get__(client)
    jar_had = client.cookies.get(SESSION_COOKIE)
    body = {"username": BOOT_USER, "email": BOOT_EMAIL, "password": BOOT_PASSWORD}
    # Setup answers only a request naming a loopback host (authz.py: a
    # DNS-rebinding page names its own), and TestClient's is "testserver".
    r = post("POST", "/api/setup/admin", json=body, headers=SETUP_HOST)
    if r.status_code == 200:
        cookie, key = _cookie_from(r.headers.get_list("set-cookie")), r.json()["secret_once"]
    elif r.status_code == 410:
        r = post("POST", "/api/auth/login", json=body)
        if r.status_code != 200:
            raise _boot_failed("POST /api/auth/login", r.status_code, r.text)
        cookie = _cookie_from(r.headers.get_list("set-cookie"))
        k = post(
            "POST",
            "/api/keys",
            json={"name": BOOT_KEY_NAME, "expiry_days": 365, "current_password": BOOT_PASSWORD},
            headers={"Cookie": f"{SESSION_COOKIE}={cookie}"},
        )
        if k.status_code != 200:
            raise _boot_failed("POST /api/keys", k.status_code, k.text)
        key = k.json()["secret_once"]
    else:
        raise _boot_failed("POST /api/setup/admin", r.status_code, r.text)
    # Leave the client's own jar as the test left it: the credential travels
    # as an explicit header, so a test that inspects cookies sees only its own.
    if jar_had is None:
        client.cookies.delete(SESSION_COOKIE)
    _creds[ident] = (cookie, key)
    return _creds[ident]


def credentials(app) -> tuple[str, str]:
    """``(session cookie, derate- key)`` for a gateway app, signed in through it.

    For a test that reaches the gateway over a real socket (uvicorn in a
    thread), where neither patched client is in the path: it attaches these
    itself, exactly as any outside client would.
    """
    stores = _stores_of(app)
    if stores is None:
        raise AssertionError("not a gateway app: it has no auth stores")
    return _boot_sync(TestClient(app), stores)


def with_credentials(app):
    """``app`` as an ASGI callable that attaches this suite's credentials.

    For a gateway run on a real socket (uvicorn in a thread), where neither
    patched client is in the path. The same rule as the patched clients: a
    request that brought its own ``Cookie`` / ``Authorization`` is untouched,
    one that did not gets the session or key minted through the app itself,
    and the gateway checks either exactly as it checks anybody's. Anything
    that is not a gateway is returned as it is.
    """
    stores = _stores_of(app)
    if stores is None:
        return app
    creds = credentials(app)

    async def attach(scope, receive, send):
        if scope["type"] in ("http", "websocket") and not _raw["on"]:
            need = _needs(scope.get("method", "GET"), scope["path"])
            if need is not None:
                raw = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in scope["headers"]]
                if not _has(httpx.Headers(raw), need, None):
                    extra = [
                        (k.lower().encode("latin-1"), v.encode("latin-1"))
                        for k, v in _attach(need, creds).items()
                    ]
                    scope = {**scope, "headers": [*scope["headers"], *extra]}
        await app(scope, receive, send)

    return attach


_orig_request = TestClient.request
_orig_websocket_connect = TestClient.websocket_connect
_orig_init = TestClient.__init__

#: The first admin can only be created from a loopback peer (authz.py), and
#: TestClient's own default peer is the string "testclient", which is not an
#: address. Every TestClient here is the coordinator's own host unless a test
#: says otherwise with ``client=``.
LOOPBACK_CLIENT = ("127.0.0.1", 50000)
#: ...and its default host is "testserver", which setup refuses (a rebinding
#: page names a host of its own), so every TestClient here names localhost
#: unless a test passes ``base_url``; the boot's setup call names it either way.
LOOPBACK_BASE_URL = "http://localhost"
SETUP_HOST = {"host": "localhost"}


def _init(self, app, *args, **kwargs):
    kwargs.setdefault("client", LOOPBACK_CLIENT)
    if not args:
        kwargs.setdefault("base_url", LOOPBACK_BASE_URL)
    _orig_init(self, app, *args, **kwargs)


def _request(self, method, url, *args, **kwargs):
    if not _raw["on"]:
        need = _needs(method, urlsplit(str(url)).path)
        if need is not None:
            headers = httpx.Headers(self.headers)
            headers.update(httpx.Headers(kwargs.get("headers") or {}))
            if not _has(headers, need, self.cookies):
                stores = _stores_of(self.app)
                if stores is not None:
                    creds = _boot_sync(self, stores)
                    kwargs["headers"] = _with(kwargs.get("headers"), _attach(need, creds))
    return _orig_request(self, method, url, *args, **kwargs)


def _websocket_connect(self, url, subprotocols=None, **kwargs):
    if not _raw["on"]:
        need = _needs("GET", urlsplit(str(url)).path)
        if need is not None:
            headers = httpx.Headers(kwargs.get("headers") or {})
            if not _has(headers, need, self.cookies):
                stores = _stores_of(self.app)
                if stores is not None:
                    creds = _boot_sync(self, stores)
                    kwargs["headers"] = dict(_with(kwargs.get("headers"), _attach(need, creds)))
    return _orig_websocket_connect(self, url, subprotocols, **kwargs)


TestClient.__init__ = _init
TestClient.request = _request
TestClient.websocket_connect = _websocket_connect


# -- httpx.ASGITransport ---------------------------------------------------------


_orig_handle = httpx.ASGITransport.handle_async_request


async def _boot_async(transport, stores, base: httpx.URL) -> tuple[str, str]:
    ident = str(stores.auth_path)
    if ident in _creds:
        return _creds[ident]

    async def post(path: str, body: dict, headers: dict | None = None) -> httpx.Response:
        request = httpx.Request("POST", base.join(path), json=body, headers=headers)
        response = await _orig_handle(transport, request)
        await response.aread()
        return response

    body = {"username": BOOT_USER, "email": BOOT_EMAIL, "password": BOOT_PASSWORD}
    r = await post("/api/setup/admin", body, SETUP_HOST)
    if r.status_code == 200:
        cookie, key = _cookie_from(r.headers.get_list("set-cookie")), r.json()["secret_once"]
    elif r.status_code == 410:
        r = await post("/api/auth/login", body)
        if r.status_code != 200:
            raise _boot_failed("POST /api/auth/login", r.status_code, r.text)
        cookie = _cookie_from(r.headers.get_list("set-cookie"))
        k = await post(
            "/api/keys",
            {"name": BOOT_KEY_NAME, "expiry_days": 365, "current_password": BOOT_PASSWORD},
            {"Cookie": f"{SESSION_COOKIE}={cookie}"},
        )
        if k.status_code != 200:
            raise _boot_failed("POST /api/keys", k.status_code, k.text)
        key = k.json()["secret_once"]
    else:
        raise _boot_failed("POST /api/setup/admin", r.status_code, r.text)
    _creds[ident] = (cookie, key)
    return _creds[ident]


async def _handle_async_request(self, request: httpx.Request) -> httpx.Response:
    if not _raw["on"]:
        need = _needs(request.method, request.url.path)
        if need is not None and not _has(request.headers, need, None):
            stores = _stores_of(self.app)
            if stores is not None:
                creds = await _boot_async(self, stores, request.url.copy_with(path="/", query=None))
                for name, value in _attach(need, creds).items():
                    request.headers[name] = value
    return await _orig_handle(self, request)


httpx.ASGITransport.handle_async_request = _handle_async_request
