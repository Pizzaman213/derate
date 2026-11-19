"""A ChatGPT sign-in that finishes through the redirect leaves a
``provider_signed_in`` row naming the admin who started it (plan 3.8)."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from control_plane.gateway import create_app
from control_plane.contracts.providers import ProviderKind
from tests.unit.test_gateway import FakeProviders, build_deps, make_provider


class SignInProviders(FakeProviders):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    def start_chatgpt_sign_in(self, provider_id):
        self.attempts += 1
        return {"attempt_id": f"att-{self.attempts}", "authorize_url": "https://auth.example/x",
                "redirect_uri": "http://127.0.0.1:1455/auth/callback", "expires_at": 0}

    async def complete_chatgpt_callback(self, query):
        provider = make_provider("chatgpt-plan")
        provider.kind = ProviderKind.CHATGPT
        self.providers.append(provider)
        return "att-1", provider


def _rows(client, action):
    entries = client.get("/api/audit", params={"page_size": 500}).json()["entries"]
    return [e for e in entries if e["action"] == action]


def test_redirect_completion_is_audited_as_the_starting_admin(monkeypatch):
    providers = SignInProviders()
    app = create_app(deps=build_deps(providers=providers))
    # the redirect page needs nothing beyond the provider id
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        assert client.post("/api/sign-in/chatgpt", json={}).status_code == 200
        landing = client.get("/auth/callback?code=c&state=s")
        assert landing.status_code == 200, landing.text
        rows = _rows(client, "provider_signed_in")
        admin = client.get("/api/auth/me").json().get("username")
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["target_id"] == "chatgpt-plan"
    assert row["actor"] == admin and admin
    after = row["after"] if isinstance(row["after"], dict) else json.loads(row["after"])
    assert after["kind"] == "chatgpt" and after["rebound"] is False
    assert row["ip"] == "127.0.0.1" and row["request_id"]
