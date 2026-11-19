"""Claude management routes use real credentials and attribute completion to its initiator."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from control_plane.gateway import create_app
from control_plane.providers import SignInError, UpstreamError
from tests.unit.test_claude_sign_in import ACCESS, CODE, REFRESH, FakeAnthropic, make_service
from tests.unit.test_gateway import build_deps


ADMIN = {
    "username": "claude-initiator",
    "email": "claude-initiator@derate.test",
    "password": "claude-test-harbor-lamp-1234",
}
SECOND = {
    "username": "claude-finisher",
    "email": "claude-finisher@derate.test",
    "password": "claude-other-harbor-lamp-1234",
}


def _client(tmp_path):
    fake = FakeAnthropic()
    service = make_service(tmp_path, fake)
    app = create_app(build_deps(providers=service))
    return TestClient(app, client=("127.0.0.1", 50000)), service, fake


def _setup(client):
    response = client.post("/api/setup/admin", json=ADMIN)
    assert response.status_code == 200, response.text
    login = client.post("/api/auth/login", json=ADMIN)
    assert login.status_code == 200, login.text


def _start(client, provider_id=None):
    response = client.post(
        "/api/sign-in/claude", json={} if provider_id is None else {"provider_id": provider_id}
    )
    assert response.status_code == 200, response.text
    started = response.json()
    assert set(started) == {"attempt_id", "authorize_url", "redirect_uri", "expires_at"}
    state = parse_qs(urlsplit(started["authorize_url"]).query)["state"][0]
    return started["attempt_id"], f"{CODE}#{state}"


def _complete(client, attempt_id, callback):
    return client.post(
        f"/api/sign-in/claude/{attempt_id}/complete", json={"callback_url": callback}
    )


def _audit(client):
    response = client.get("/api/audit", params={"page_size": 500})
    assert response.status_code == 200, response.text
    return response.json()["entries"]


@pytest.mark.raw_http
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/sign-in/claude", {}),
        ("GET", "/api/sign-in/claude/unknown", None),
        ("POST", "/api/sign-in/claude/unknown/complete", {"callback_url": "code#state"}),
    ],
)
def test_claude_management_routes_require_admin_session(tmp_path, method, path, body):
    client, service, fake = _client(tmp_path)
    with client:
        _setup(client)
        client.cookies.clear()
        response = client.request(method, path, json=body)
        assert response.status_code == 401, response.text
        assert not fake.requests


@pytest.mark.raw_http
def test_completion_audits_starting_admin_and_reconnect(tmp_path):
    client, service, fake = _client(tmp_path)
    with client:
        _setup(client)
        attempt_id, callback = _start(client)
        pending = client.get(f"/api/sign-in/claude/{attempt_id}")
        assert pending.status_code == 200
        assert pending.json()["status"] == "pending"
        added = client.post(
            "/api/admins", json={**SECOND, "current_password": ADMIN["password"]}
        )
        assert added.status_code == 200, added.text
        client.cookies.clear()
        login = client.post("/api/auth/login", json=SECOND)
        assert login.status_code == 200, login.text
        completed = _complete(client, attempt_id, callback)
        assert completed.status_code == 201, completed.text
        provider_id = completed.json()["provider_id"]
        assert completed.json()["kind"] == "claude_code"
        assert service.get(provider_id).provider_id == provider_id
        done = client.get(f"/api/sign-in/claude/{attempt_id}")
        assert done.status_code == 200
        assert done.json()["status"] == "done"
        assert done.json()["provider_id"] == provider_id
        rows = _audit(client)
        signed_in = [row for row in rows if row["action"] == "provider_signed_in"]
        assert len(signed_in) == 1
        row = signed_in[0]
        assert row["actor"] == ADMIN["username"]
        assert row["target_id"] == provider_id
        assert row["after"] == {
            "kind": "claude_code", "rebound": False, "attempt_id": attempt_id,
        }
        assert row["request_id"] and row["ip"] == "127.0.0.1"
        assert not [
            entry for entry in rows
            if entry["action"] == "api_write" and entry["request_id"] == row["request_id"]
        ]

        reconnect_id, reconnect_callback = _start(client, provider_id)
        reconnected = _complete(client, reconnect_id, reconnect_callback)
        assert reconnected.status_code == 200, reconnected.text
        assert reconnected.json()["provider_id"] == provider_id
        assert len(service.list()) == 1
        reconnect_status = client.get(f"/api/sign-in/claude/{reconnect_id}")
        assert reconnect_status.status_code == 200
        assert reconnect_status.json()["status"] == "done"
        assert reconnect_status.json()["provider_id"] == provider_id
        signed_in = [row for row in _audit(client) if row["action"] == "provider_signed_in"]
        rebound = [row for row in signed_in if row["after"]["attempt_id"] == reconnect_id]
        assert len(rebound) == 1
        assert rebound[0]["actor"] == SECOND["username"]
        assert rebound[0]["after"]["kind"] == "claude_code"
        assert rebound[0]["after"]["rebound"] is True
        assert rebound[0]["target_id"] == provider_id

        public = json.dumps({
            "provider": completed.json(), "status": done.json(),
            "reconnect": reconnected.json(), "audit": _audit(client),
        })
        for secret in (ACCESS, REFRESH, CODE):
            assert secret not in public


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/sign-in/claude", []),
        ("/api/sign-in/claude", {"provider_id": 1}),
        ("/api/sign-in/claude/unknown/complete", {}),
        ("/api/sign-in/claude/unknown/complete", {"callback_url": 1}),
    ],
)
def test_claude_management_rejects_malformed_bodies(tmp_path, path, body):
    client, service, fake = _client(tmp_path)
    with client:
        response = client.post(path, json=body)
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "invalid_json"
        assert not fake.requests


def test_claude_unknown_attempt_and_provider_are_explicit(tmp_path):
    client, service, fake = _client(tmp_path)
    with client:
        status = client.get("/api/sign-in/claude/unknown")
        assert status.status_code == 404
        assert status.json()["error"]["code"] == "sign_in_attempt_unknown"
        completion = _complete(client, "unknown", "code#state")
        assert completion.status_code == 400
        assert completion.json()["error"]["code"] == "sign_in_attempt_unknown"
        start = client.post("/api/sign-in/claude", json={"provider_id": "missing"})
        assert start.status_code == 404
        assert start.json()["error"]["code"] == "provider_not_found"
        assert not fake.requests


@pytest.mark.parametrize(
    ("exception", "status", "code"),
    [
        (SignInError("sign_in_state_mismatch", "State did not match."), 400, "sign_in_state_mismatch"),
        (UpstreamError("claude", 503, "Unavailable."), 502, "sign_in_failed"),
    ],
)
def test_claude_completion_preserves_error_contract(tmp_path, monkeypatch, exception, status, code):
    client, service, fake = _client(tmp_path)

    async def fail(attempt_id, callback_url):
        raise exception

    monkeypatch.setattr(service, "complete_claude_sign_in", fail)
    with client:
        response = _complete(client, "attempt", "code#state")
        assert response.status_code == status, response.text
        assert response.json()["error"]["code"] == code
        assert not [row for row in _audit(client) if row["action"] == "provider_signed_in"]
