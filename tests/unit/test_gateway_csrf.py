"""control_plane/gateway/csrf.py -- the Origin/Host cross-site write guard.

Closes the gap the gateway's own docs and 00-architecture.md already name:
`/api` has no per-request credential, so nothing but this stood between "a
page you opened" and "a page that rewrote your cluster" for any browser that
could route to the coordinator.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from control_plane.gateway import GatewayDeps, GatewaySettings, create_app
from control_plane.gateway.csrf import is_cross_site_write

# ---------------------------------------------------------------------------
# pure logic
# ---------------------------------------------------------------------------


def test_get_is_never_blocked():
    assert not is_cross_site_write(
        method="GET",
        path="/api/providers",
        origin="https://evil.example",
        request_scheme="http",
        request_netloc="coordinator:8088",
        allowed_origins=(),
    )


def test_missing_origin_is_allowed():
    """A missing Origin means a non-browser caller -- curl, an SDK -- and
    those are not what this check exists to catch."""
    assert not is_cross_site_write(
        method="POST",
        path="/api/providers",
        origin=None,
        request_scheme="http",
        request_netloc="coordinator:8088",
        allowed_origins=(),
    )


def test_matching_origin_is_allowed():
    assert not is_cross_site_write(
        method="POST",
        path="/api/providers",
        origin="http://coordinator:8088",
        request_scheme="http",
        request_netloc="coordinator:8088",
        allowed_origins=(),
    )


def test_mismatched_origin_is_refused():
    assert is_cross_site_write(
        method="POST",
        path="/api/providers",
        origin="https://evil.example",
        request_scheme="http",
        request_netloc="coordinator:8088",
        allowed_origins=(),
    )


def test_allowlisted_origin_is_allowed():
    """The same escape hatch DERATE_ALLOWED_ORIGINS already gives CORS."""
    assert not is_cross_site_write(
        method="PATCH",
        path="/api/providers/openrouter",
        origin="http://dev.example:5173",
        request_scheme="http",
        request_netloc="coordinator:8088",
        allowed_origins=("http://dev.example:5173",),
    )


def test_unguarded_path_is_never_blocked():
    assert not is_cross_site_write(
        method="POST",
        path="/healthz",
        origin="https://evil.example",
        request_scheme="http",
        request_netloc="coordinator:8088",
        allowed_origins=(),
    )


# ---------------------------------------------------------------------------
# wired into the app
# ---------------------------------------------------------------------------


def _client(settings: GatewaySettings | None = None) -> TestClient:
    app = create_app(GatewayDeps(), settings=settings or GatewaySettings())
    return TestClient(app)


def test_cross_origin_post_is_refused_before_the_route_runs():
    with _client() as client:
        reply = client.post(
            "/api/providers/does-not-exist/refresh",
            headers={"Origin": "https://evil.example"},
        )
    assert reply.status_code == 403
    assert reply.json()["error"]["code"] == "cross_origin_write_refused"


def test_request_with_no_origin_reaches_the_route():
    with _client() as client:
        reply = client.post("/api/providers/does-not-exist/refresh")
    # No Origin header at all -- proves the guard did not eat the request,
    # only a genuine cross-origin one. 404 is the route's own answer for an
    # unknown provider id, not the guard's.
    assert reply.status_code == 404
    assert reply.json()["error"]["code"] == "provider_not_found"


def test_allowlisted_cross_origin_reaches_the_route():
    settings = GatewaySettings(allowed_origins=("http://dev.example:5173",))
    with _client(settings) as client:
        reply = client.post(
            "/api/providers/does-not-exist/refresh",
            headers={"Origin": "http://dev.example:5173"},
        )
    assert reply.status_code == 404
    assert reply.json()["error"]["code"] == "provider_not_found"
