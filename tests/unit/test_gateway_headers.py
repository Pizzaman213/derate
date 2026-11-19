"""Every gateway answer carries the browser hardening headers; HTML carries CSP.

The session cookie is SameSite=Strict, which is per *site*: a page on another
port of the same host can frame the UI with the cookie attached, and the
clicks it steals are same-origin writes csrf.py lets through. These headers are
what stop that, so they must be on the refusals too -- the 401 from the gate
and the 403 from the CSRF check are sent by middleware, not by a route.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from control_plane.gateway.app import create_app
from control_plane.gateway.headers import CSP_HTML
from control_plane.gateway.settings import GatewaySettings
from tests.unit.test_gateway import build_deps

pytestmark = pytest.mark.raw_http

STATIC = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "same-origin",
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path / "data"))
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<!doctype html><title>derate</title>")
    app = create_app(
        build_deps(), settings=GatewaySettings(cluster_id="c-test", ui_dir=str(ui))
    )
    with TestClient(app) as http:
        yield http


def _assert_static(res) -> None:
    for name, value in STATIC.items():
        assert res.headers.get(name) == value, (res.request.url, name)


@pytest.mark.parametrize("path", ["/", "/login", "/cluster"])
def test_the_ui_document_is_unframeable_and_runs_only_its_own_script(client, path):
    res = client.get(path)
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    _assert_static(res)
    assert res.headers["content-security-policy"] == CSP_HTML
    assert "frame-ancestors 'none'" in CSP_HTML
    assert "script-src 'self';" in CSP_HTML


@pytest.mark.parametrize(
    "method, path, status",
    [
        ("GET", "/api/auth/state", 200),
        ("GET", "/healthz", 200),
        # Refused by AuthGuard, before any route runs.
        ("GET", "/api/settings", 401),
    ],
)
def test_json_answers_and_gate_refusals_carry_the_static_headers(
    client, method, path, status
):
    res = client.request(method, path)
    assert res.status_code == status
    _assert_static(res)
    assert "content-security-policy" not in res.headers


def test_a_cross_site_write_refusal_carries_them_too(client):
    res = client.post(
        "/api/auth/login",
        json={"username": "x", "password": "y"},
        headers={"Origin": "http://evil.example"},
    )
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "cross_origin_write_refused"
    _assert_static(res)
    # EV-5 (A7): the refusal's audit row names the request the client saw.
    from control_plane.contracts.auth import AuditAction

    ctx = client.app.state.ctx
    ctx.secevents.flush()
    rows, _ = ctx.auth_stores.auth.audit_page(action=AuditAction.CROSS_ORIGIN_REFUSED.value)
    assert [r.request_id for r in rows] == [res.headers["x-request-id"]]


def _connect_src(policy: str) -> list[str]:
    directive = next(d.strip() for d in policy.split(";") if d.strip().startswith("connect-src"))
    return directive.split()[1:]


def test_connect_src_is_self_alone_by_default(client):
    """No scheme wildcards: an injected script cannot post what it reads to
    any host it likes."""
    policy = client.get("/").headers["content-security-policy"]
    assert _connect_src(policy) == ["'self'"]
    for wildcard in ("http:", "https:", "ws:", "wss:"):
        assert wildcard not in _connect_src(policy)


def test_connect_src_names_the_configured_origins_and_their_sockets(tmp_path, monkeypatch):
    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path / "data"))
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<!doctype html><title>derate</title>")
    settings = GatewaySettings(
        cluster_id="c-test",
        ui_dir=str(ui),
        public_origin="https://derate.example",
        allowed_origins=(
            "http://10.0.0.5:8080/some/path",
            "https://x.example; script-src *",  # CSP syntax: dropped
            "javascript:alert(1)",  # not an http(s) origin: dropped
        ),
    )
    with TestClient(create_app(build_deps(), settings=settings)) as http:
        policy = http.get("/").headers["content-security-policy"]
    assert _connect_src(policy) == [
        "'self'",
        "https://derate.example",
        "wss://derate.example",
        "http://10.0.0.5:8080",
        "ws://10.0.0.5:8080",
    ]
    assert "script-src 'self';" in policy
