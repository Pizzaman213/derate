"""control_plane/gateway/auth.py -- the optional bearer-token gate on /api.

Unlike csrf.py, this is off by default: DERATE_API_TOKEN unset means every
existing no-auth workflow (ui/check.mjs, tests/model_sweep, loadtest.py, this
project's own documented curl workflows) keeps working unchanged.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from control_plane.gateway import GatewayDeps, GatewaySettings, create_app
from control_plane.gateway.auth import is_unauthorized

# ---------------------------------------------------------------------------
# pure logic
# ---------------------------------------------------------------------------


def test_unset_token_is_always_a_noop():
    assert not is_unauthorized(path="/api/providers", authorization=None, token=None)


def test_missing_header_is_refused_once_a_token_is_configured():
    assert is_unauthorized(path="/api/providers", authorization=None, token="secret")


def test_wrong_token_is_refused():
    assert is_unauthorized(
        path="/api/providers", authorization="Bearer nope", token="secret"
    )


def test_matching_bearer_token_is_allowed():
    assert not is_unauthorized(
        path="/api/providers", authorization="Bearer secret", token="secret"
    )


def test_wrong_scheme_is_refused():
    assert is_unauthorized(
        path="/api/providers", authorization="Basic secret", token="secret"
    )


def test_unguarded_path_is_never_blocked():
    assert not is_unauthorized(path="/healthz", authorization=None, token="secret")


def test_v1_is_not_guarded_by_this_check():
    assert not is_unauthorized(
        path="/v1/chat/completions", authorization=None, token="secret"
    )


def test_node_join_stays_reachable_without_the_management_token():
    """A joining node carries its own short-lived enrollment token in the
    body, not this header -- see auth.py's module docstring."""
    assert not is_unauthorized(
        path="/api/nodes/join", authorization=None, token="secret"
    )


# ---------------------------------------------------------------------------
# wired into the app
# ---------------------------------------------------------------------------


def _client(settings: GatewaySettings | None = None) -> TestClient:
    app = create_app(GatewayDeps(), settings=settings or GatewaySettings())
    return TestClient(app)


def test_unset_token_reaches_the_route():
    with _client() as client:
        reply = client.get("/api/providers")
    assert reply.status_code == 200


def test_missing_token_is_refused_before_the_route_runs():
    settings = GatewaySettings(api_token="secret")
    with _client(settings) as client:
        reply = client.get("/api/providers")
    assert reply.status_code == 401
    assert reply.json()["error"]["code"] == "invalid_api_token"


def test_matching_bearer_token_reaches_the_route():
    settings = GatewaySettings(api_token="secret")
    with _client(settings) as client:
        reply = client.get(
            "/api/providers", headers={"Authorization": "Bearer secret"}
        )
    assert reply.status_code == 200
