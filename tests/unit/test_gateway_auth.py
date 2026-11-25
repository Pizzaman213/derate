"""Inbound auth on the gateway, end to end through create_app().

Every test here is ``raw_http``: it signs in and sends its own credentials, so
what the gateway sees is exactly what the test says it sent.
"""

from __future__ import annotations

import csv
import io
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from control_plane.contracts import RoutingPolicy
from control_plane.contracts.auth import SESSION_MAX_S
from control_plane.gateway import create_app
from tests.unit.test_gateway import (
    SECRET_KEY,
    FakeDeployments,
    FakeProviders,
    build_deps,
    make_deployment,
    make_provider,
)

pytestmark = pytest.mark.raw_http

ADMIN = {
    "username": "root-admin",
    "email": "root-admin@derate.test",
    "password": "first-harbor-lamp-123456",
}
LOCAL = "local-m"
CLOUD = "cloud-m"


class Upstream:
    """Both backends behind one MockTransport; records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        body = json.loads(request.content or b"{}")
        payload = {
            "id": "cmpl-1",
            "object": "chat.completion",
            "model": body.get("model"),
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "prompt_tokens_details": {"cached_tokens": 4},
            },
        }
        # A stream, as a real upstream's body is: the proxy reads it raw.
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=httpx.ByteStream(json.dumps(payload).encode()),
        )

    def last_to(self, host: str) -> httpx.Request:
        return [r for r in self.requests if r.url.host == host][-1]


class Rows:
    """A sink that keeps every request row it is handed."""

    def __init__(self) -> None:
        self.rows: list = []

    def request(self, record) -> None:
        self.rows.append(record)

    def __getattr__(self, name):
        return lambda *a, **k: None


@pytest.fixture
def gw():
    upstream = Upstream()
    deps = build_deps(
        deployments=FakeDeployments(
            [make_deployment("d-local", LOCAL, backend_url="http://local-backend/v1")]
        ),
        providers=FakeProviders(
            [make_provider("openrouter", base_url="http://cloud/v1", served_name=CLOUD, upstream_id="up/cloud")]
        ),
    )
    app = create_app(deps, http_client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    rows = Rows()
    ctx = app.state.ctx
    ctx.sink = rows
    ctx.proxy._sink = rows
    client = TestClient(app)
    r = client.post("/api/setup/admin", json=ADMIN)
    assert r.status_code == 200, r.text
    setup = r.json()
    client.cookies.clear()
    cookie = {"Cookie": f"derate_session={r.cookies['derate_session']}"}

    class GW:
        pass

    g = GW()
    g.app, g.client, g.upstream, g.rows, g.setup, g.cookie = app, client, upstream, rows, setup, cookie
    g.key = setup["secret_once"]

    def bearer(key=None):
        return {"Authorization": f"Bearer {key or g.key}"}

    def mint(**fields):
        body = {"name": "k", "expiry_days": 30, "current_password": ADMIN["password"], **fields}
        r = client.post("/api/keys", json=body, headers=cookie)
        assert r.status_code == 200, r.text
        return r.json()

    def chat(key=None, model=LOCAL, **extra):
        body = {"model": model, "messages": [{"role": "user", "content": "hello"}], **extra}
        return client.post("/v1/chat/completions", json=body, headers=bearer(key))

    g.bearer, g.mint, g.chat = bearer, mint, chat
    return g


def test_a_429_names_the_limit_and_carries_rate_limit_headers(gw):
    minted = gw.mint(limits={"requests_cycle": 2})
    key = minted["secret_once"]
    assert gw.chat(key).status_code == 200
    assert gw.chat(key).status_code == 200
    refused = gw.chat(key)
    assert refused.status_code == 429
    error = refused.json()["error"]
    assert error["type"] == "rate_limit_error"
    assert error["code"] == "limit_exceeded"
    assert error["limit_source"] == "key:requests_cycle"
    assert error["used"] == 2 and error["limit"] == 2
    assert error["resets_at"] > time.time()
    assert error["message"]
    assert refused.headers["X-RateLimit-Limit"] == "2"
    assert refused.headers["X-RateLimit-Remaining"] == "0"
    assert int(refused.headers["X-RateLimit-Reset"]) == pytest.approx(error["resets_at"], abs=1)
    # The refusal has a telemetry row of its own, naming the limit.
    refusal_row = gw.rows.rows[-1]
    assert refusal_row.status == 429
    assert refusal_row.limit_source == "key:requests_cycle"
    assert refusal_row.key_id == minted["key"]["key_id"]


def test_per_request_caps_refuse_with_the_cap_named(gw):
    key = gw.mint(
        limits={"max_output_tokens_per_request": 100, "max_prompt_tokens_per_request": 50}
    )["secret_once"]
    too_long = gw.chat(key, max_tokens=99999)
    assert too_long.status_code == 400
    assert too_long.json()["error"]["code"] == "max_output_tokens_exceeded"
    assert "100" in too_long.json()["error"]["message"]

    huge = gw.client.post(
        "/v1/chat/completions",
        json={"model": LOCAL, "messages": [{"role": "user", "content": "word " * 400}]},
        headers=gw.bearer(key),
    )
    assert huge.status_code == 413
    assert huge.json()["error"]["code"] == "max_prompt_tokens_exceeded"


def test_an_output_cap_is_injected_when_the_request_sets_none(gw):
    key = gw.mint(limits={"max_output_tokens_per_request": 100})["secret_once"]
    assert gw.chat(key).status_code == 200
    sent = json.loads(gw.upstream.last_to("local-backend").content)
    assert sent["max_tokens"] == 100


def test_an_exhausted_cloud_pool_falls_back_to_the_local_model(gw):
    minted = gw.mint(limits={"cloud_tokens_cycle": 1}, cloud_fallback_local_model=LOCAL)
    key = minted["secret_once"]
    first = gw.chat(key, model=CLOUD)
    assert first.status_code == 200
    assert gw.upstream.requests[-1].url.host == "cloud"
    # The first request crossed the cloud token limit; the next is served locally.
    second = gw.chat(key, model=CLOUD)
    assert second.status_code == 200
    assert gw.upstream.requests[-1].url.host == "local-backend"
    assert json.loads(gw.upstream.requests[-1].content)["model"] == LOCAL
    row = gw.rows.rows[-1]
    assert row.fallback_used is True
    assert row.fallback_reason == "cloud_pool_exhausted"
    assert row.model_requested == CLOUD
    assert row.model_served == LOCAL


def test_v1_key_describes_the_calling_key_and_nothing_secret(gw):
    r = gw.client.get("/v1/key", headers=gw.bearer())
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"key", "org", "expires_at", "allowed_models", "limit_states", "cycle"}
    assert body["key"]["key_id"] == gw.setup["key"]["key_id"]
    assert "notes" not in body["key"]
    assert "secret_hash" not in json.dumps(body)
    assert gw.key not in r.text
    assert body["org"]["name"] == "default"
    assert set(body["limit_states"]) == {"key", "org"}
    assert sorted(body["allowed_models"]) == sorted([CLOUD, LOCAL])
    assert body["cycle"]["resets_at"] > body["cycle"]["start"]
    for private in ("last_used_ip", "created_by", "admin_username"):
        assert private not in body["key"]


def test_v1_models_lists_only_an_allowlisted_keys_models(gw):
    key = gw.mint(allowed_models=[LOCAL])["secret_once"]
    listed = gw.client.get("/v1/models", headers=gw.bearer(key))
    assert listed.status_code == 200
    assert [m["id"] for m in listed.json()["data"]] == [LOCAL]
    # An unrestricted key still sees the whole catalogue.
    everything = gw.client.get("/v1/models", headers=gw.bearer())
    assert {m["id"] for m in everything.json()["data"]} == {LOCAL, CLOUD}
    # A restricted key gets the same refusal for a name that does not exist
    # as for one outside its allowlist (K-3: no existence oracle), and it
    # does not leak the rest of the catalogue either.
    missing = gw.chat(key, model="no-such-model")
    assert missing.status_code == 403
    assert missing.json()["error"]["code"] == "model_not_allowed"
    assert CLOUD not in missing.json()["error"]["message"]
    outside = gw.chat(key, model=CLOUD)
    assert outside.status_code == 403
    assert outside.json()["error"]["message"].replace(CLOUD, "no-such-model") == missing.json()["error"]["message"]


def test_chat_through_a_session_is_attributed_to_the_admins_own_key(gw):
    me = gw.client.get("/api/auth/me", headers=gw.cookie).json()
    r = gw.client.post(
        "/api/chat/completions",
        json={"model": LOCAL, "messages": [{"role": "user", "content": "hi"}]},
        headers=gw.cookie,
    )
    assert r.status_code == 200, r.text
    row = gw.rows.rows[-1]
    assert row.key_id == me["admin_key_id"]
    keys = {k["key_id"]: k for k in gw.client.get("/api/keys", headers=gw.cookie).json()}
    assert keys[me["admin_key_id"]]["is_admin_key"] is True
    assert keys[me["admin_key_id"]]["admin_username"] == ADMIN["username"]
    sent = gw.upstream.last_to("local-backend")
    assert "cookie" not in sent.headers and "authorization" not in sent.headers


def test_session_mirrors_list_models_and_need_a_session(gw):
    listed = gw.client.get("/api/chat/models", headers=gw.cookie)
    assert listed.status_code == 200
    assert {m["id"] for m in listed.json()["data"]} == {LOCAL, CLOUD}
    assert gw.client.get("/api/chat/models").json()["error"]["code"] == "session_required"
    assert gw.client.get("/api/chat/voices?model=x").status_code == 401
    # A session is not a key: /v1 still wants Authorization.
    assert gw.client.get("/v1/models", headers=gw.cookie).json()["error"]["code"] == "missing_api_key"


def test_the_clients_credentials_never_reach_a_local_backend(gw):
    r = gw.client.post(
        "/v1/chat/completions",
        json={"model": LOCAL, "messages": [{"role": "user", "content": "x"}]},
        headers={**gw.bearer(), "Cookie": "derate_session=abc; other=1"},
    )
    assert r.status_code == 200
    sent = gw.upstream.last_to("local-backend")
    assert "authorization" not in sent.headers
    assert "cookie" not in sent.headers
    assert gw.key not in str(sent.headers)

    assert gw.chat(model=CLOUD).status_code == 200
    remote = gw.upstream.last_to("cloud")
    # A remote target gets the provider's own key, never the client's.
    assert remote.headers["authorization"] == f"Bearer {SECRET_KEY}"


def test_a_local_engine_gets_its_runtime_key_and_only_that(gw):
    """H3: the engine requires its per-deployment key, and the gateway is the
    only holder. The client's own derate- key still never reaches it."""
    router = gw.app.state.ctx.router
    runtime_key = "e" * 64
    router._deployments.runtime_api_key = lambda deployment_id: runtime_key
    router.rebuild(force_scores=True)
    assert gw.chat(model=LOCAL).status_code == 200
    sent = gw.upstream.last_to("local-backend")
    assert sent.headers["authorization"] == f"Bearer {runtime_key}"
    assert gw.key not in str(sent.headers)
    # A deployment launched before keys existed: no header at all.
    router._deployments.runtime_api_key = lambda deployment_id: None
    router.rebuild(force_scores=True)
    assert gw.chat(model=LOCAL).status_code == 200
    assert "authorization" not in gw.upstream.last_to("local-backend").headers


def test_a_routing_override_on_the_key_reaches_the_router(gw):
    key = gw.mint(routing_policy=RoutingPolicy.ROUND_ROBIN.value)["secret_once"]
    seen = []
    router = gw.app.state.ctx.router
    original = router.select

    def spy(*args, **kwargs):
        seen.append(kwargs.get("policy"))
        return original(*args, **kwargs)

    router.select = spy
    assert gw.chat(key).status_code == 200
    assert seen and seen[0] is RoutingPolicy.ROUND_ROBIN


def test_login_locks_after_repeated_failures_with_retry_after(gw):
    bad = {"username": ADMIN["username"], "password": "wrong-password-123"}
    for _ in range(5):
        r = gw.client.post("/api/auth/login", json=bad)
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "invalid_credentials"
    locked = gw.client.post("/api/auth/login", json=ADMIN)
    assert locked.status_code == 429
    assert locked.json()["error"]["code"] == "login_locked"
    assert int(locked.headers["Retry-After"]) >= 1


def test_login_accepts_the_email_and_signs_in_as_the_username(gw):
    r = gw.client.post(
        "/api/auth/login", json={"username": ADMIN["email"].upper(), "password": ADMIN["password"]}
    )
    assert r.status_code == 200, r.text
    assert r.json()["username"] == "root-admin"


def test_the_session_cookie_is_httponly_strict_and_secure_only_on_https(tmp_path, monkeypatch):
    plain = TestClient(create_app())
    r = plain.post("/api/setup/admin", json=ADMIN)
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("derate_session=")
    for part in ("HttpOnly", "Path=/", "SameSite=strict", f"Max-Age={SESSION_MAX_S}"):
        assert part in cookie
    assert "Secure" not in cookie

    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path / "https"))
    secure = TestClient(create_app(), base_url="https://localhost")
    cookie = secure.post("/api/setup/admin", json=ADMIN).headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie

    out = secure.post("/api/auth/logout")
    assert "derate_session=" in out.headers["set-cookie"]
    assert 'Max-Age=0' in out.headers["set-cookie"] or "expires=" in out.headers["set-cookie"].lower()


def test_a_secret_is_revealed_once_and_rotation_kills_the_old_one(gw):
    key_id = gw.setup["key"]["key_id"]
    secret = gw.key.split("_", 1)[1]  # derate-<id>_<secret>; the secret may hold "_"
    for path in ("/api/keys", f"/api/keys/{key_id}", "/api/audit", f"/api/keys/{key_id}/requests"):
        r = gw.client.get(path, headers=gw.cookie)
        assert gw.key not in r.text and secret not in r.text, path
        assert "secret_once" not in r.text, path
    rotated = gw.client.post(
        f"/api/keys/{key_id}/rotate", json={"current_password": ADMIN["password"]}, headers=gw.cookie
    )
    assert rotated.status_code == 200
    new = rotated.json()["secret_once"]
    assert new != gw.key and new.startswith(f"derate-{key_id}_")
    assert gw.client.get("/v1/models", headers=gw.bearer()).json()["error"]["code"] == "invalid_api_key"
    assert gw.client.get("/v1/models", headers=gw.bearer(new)).status_code == 200
    assert new not in gw.client.get(f"/api/keys/{key_id}", headers=gw.cookie).text


def test_a_request_row_carries_identity_cache_and_cost(gw):
    gw.client.put(
        f"/api/pricing/{LOCAL}",
        json={"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 2.0},
        headers=gw.cookie,
    )
    assert gw.chat().status_code == 200
    row = gw.rows.rows[-1]
    assert row.key_id == gw.setup["key"]["key_id"]
    assert row.key_name == "default"
    assert row.org_id == gw.setup["org"]["org_id"]
    assert row.client_ip == "127.0.0.1"
    assert row.context_bucket == "0-8k"
    assert row.cost_basis == "estimated"
    assert row.cost_usd == pytest.approx((10 * 1.0 + 5 * 2.0) / 1e6)
    assert row.cache_read_tokens == 4
    assert row.prefix_cache_hit_rate == pytest.approx(0.4)
    assert row.finish_reason == "stop"
    assert row.message_count == 1
    assert row.model_requested == LOCAL
    assert row.attempt_chain == [
        {"target_id": "d-local", "status": 200, "error_class": None, "retry_reason": None, "ms": row.duration_ms}
    ]
    assert row.spec_decode_error == "not_reported"


def test_an_unpriced_local_request_says_so(gw):
    assert gw.chat().status_code == 200
    assert gw.rows.rows[-1].cost_basis == "unpriced"
    assert gw.rows.rows[-1].cost_usd is None


def test_a_key_limit_above_the_orgs_is_refused_naming_the_field(gw):
    org_id = gw.setup["org"]["org_id"]
    r = gw.client.patch(f"/api/orgs/{org_id}", json={"limits": {"rpm": 10}}, headers=gw.cookie)
    assert r.status_code == 200, r.text
    pw = {"current_password": ADMIN["password"]}
    r = gw.client.post(
        "/api/keys", json={"name": "big", "expiry_days": 30, "limits": {"rpm": 50}, **pw},
        headers=gw.cookie,
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "limit_exceeds_org"
    assert "rpm" in r.json()["error"]["message"]
    r = gw.client.post(
        "/api/keys",
        json={"name": "wide", "expiry_days": 30, "allowed_models": ["nope"], **pw},
        headers=gw.cookie,
    )
    assert r.json()["error"]["code"] == "allowlist_exceeds_org"


def test_a_model_off_the_allowlist_is_refused(gw):
    key = gw.mint(allowed_models=[LOCAL])["secret_once"]
    r = gw.chat(key, model=CLOUD)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "model_not_allowed"


def test_raise_and_reset_act_on_this_cycle_and_are_audited(gw):
    minted = gw.mint(limits={"requests_cycle": 1})
    key, key_id = minted["secret_once"], minted["key"]["key_id"]
    assert gw.chat(key).status_code == 200
    assert gw.chat(key).status_code == 429
    raised = gw.client.post(
        f"/api/keys/{key_id}/raise", json={"limit_type": "requests_cycle", "value": 2}, headers=gw.cookie
    )
    assert raised.status_code == 200 and raised.json()["effective_limit"] == 2
    assert gw.chat(key).status_code == 200
    reset = gw.client.post(f"/api/keys/{key_id}/reset-cycle", headers=gw.cookie)
    assert reset.status_code == 200
    assert reset.json()["limit_states"][0]["used"] == 0
    actions = [e["action"] for e in gw.client.get("/api/audit", headers=gw.cookie).json()["entries"]]
    for action in ("key_created", "limit_raised", "cycle_reset"):
        assert action in actions


def test_disable_and_delete_are_audited_and_refuse_the_key(gw):
    minted = gw.mint()
    key, key_id = minted["secret_once"], minted["key"]["key_id"]
    r = gw.client.post(f"/api/keys/{key_id}/disable", json={"reason": "leaked"}, headers=gw.cookie)
    assert r.json()["status"] == "disabled" and r.json()["disabled_reason"] == "leaked"
    assert gw.chat(key).json()["error"]["code"] == "api_key_disabled"
    # Enabling makes a leaked key a credential again: a session alone is not enough.
    refused = gw.client.post(f"/api/keys/{key_id}/enable", headers=gw.cookie)
    assert (refused.status_code, refused.json()["error"]["code"]) == (401, "invalid_credentials")
    assert gw.chat(key).json()["error"]["code"] == "api_key_disabled"
    enabled = gw.client.post(
        f"/api/keys/{key_id}/enable", json={"current_password": ADMIN["password"]}, headers=gw.cookie
    )
    assert enabled.json()["status"] == "active"
    assert gw.chat(key).status_code == 200
    assert gw.client.delete(f"/api/keys/{key_id}", headers=gw.cookie).status_code == 200
    assert gw.chat(key).json()["error"]["code"] == "invalid_api_key"
    # Deleted, and still readable for its stats.
    assert gw.client.get(f"/api/keys/{key_id}", headers=gw.cookie).json()["deleted_at"] is not None
    entries = gw.client.get("/api/audit", headers=gw.cookie).json()["entries"]
    disabled = next(e for e in entries if e["action"] == "key_disabled")
    assert disabled["before"]["status"] == "active" and disabled["after"]["status"] == "disabled"
    assert disabled["actor"] == ADMIN["username"]


def test_a_stale_key_does_not_lock_the_other_keys_on_its_address_out(gw):
    """The refused-key throttle counted every refusal against the address, so
    one worker still holding a disabled or rotated key locked every other
    service behind the same NAT or /64 out with 429s. It is the stale key that
    is throttled now, not its neighbours."""
    from control_plane.contracts.auth import KEY_REFUSALS_PER_ADDRESS_PER_MIN

    a, b = gw.mint(name="stale"), gw.mint(name="neighbour")
    r = gw.client.post(f"/api/keys/{a['key']['key_id']}/disable", json={"reason": "rotated"}, headers=gw.cookie)
    assert r.status_code == 200, r.text
    codes = [gw.chat(a["secret_once"]).json()["error"]["code"] for _ in range(KEY_REFUSALS_PER_ADDRESS_PER_MIN + 5)]
    assert codes == ["api_key_disabled"] * KEY_REFUSALS_PER_ADDRESS_PER_MIN + ["rate_limited"] * 5
    assert gw.chat(b["secret_once"]).status_code == 200
    assert gw.chat().status_code == 200


def test_adding_an_admin_mints_their_key_and_removing_one_disables_it(gw):
    r = gw.client.post(
        "/api/admins",
        json={
            "username": "second", "email": "second@derate.test",
            "password": "other-harbor-lamp-1234", "current_password": ADMIN["password"],
        },
        headers=gw.cookie,
    )
    assert r.status_code == 200, r.text
    admin_key_id = r.json()["admin_key_id"]
    key = gw.client.get(f"/api/keys/{admin_key_id}", headers=gw.cookie).json()
    assert key["is_admin_key"] and key["admin_username"] == "second" and key["expires_at"] is None
    again = {"current_password": ADMIN["password"]}
    removed = gw.client.request("DELETE", "/api/admins/second", json=again, headers=gw.cookie)
    assert removed.status_code == 200
    key = gw.client.get(f"/api/keys/{admin_key_id}", headers=gw.cookie).json()
    assert key["status"] == "disabled" and key["disabled_reason"] == "admin removed"
    last = gw.client.request("DELETE", f"/api/admins/{ADMIN['username']}", json=again, headers=gw.cookie)
    assert last.status_code == 400


def test_changing_the_password_ends_the_session(gw):
    r = gw.client.post(
        "/api/auth/password",
        json={"old": ADMIN["password"], "new": "a-brand-new-password-1"},
        headers=gw.cookie,
    )
    assert r.status_code == 200
    assert gw.client.get("/api/auth/me", headers=gw.cookie).json()["error"]["code"] == "session_required"


def test_auth_state_is_open_and_reports_the_session(gw):
    assert gw.client.get("/api/auth/state").json() == {
        "admin_exists": True,
        "authenticated": False,
        "username": None,
        "setup_local_only": False,
    }
    state = gw.client.get("/api/auth/state", headers=gw.cookie).json()
    assert state["authenticated"] is True and state["username"] == ADMIN["username"]


def test_an_unopenable_store_fails_closed(tmp_path, monkeypatch):
    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("x")
    monkeypatch.setenv("DERATE_DATA_DIR", str(not_a_dir))
    client = TestClient(create_app())
    for path in ("/v1/models", "/api/settings", "/api/auth/state"):
        r = client.get(path, headers={"Authorization": "Bearer derate-aaaaaaaaaa_" + "b" * 43})
        assert r.status_code == 503, path
        assert r.json()["error"]["code"] == "auth_unavailable"
    assert client.get("/healthz").status_code == 200


def test_usage_and_requests_answer_csv(gw):
    key_id = gw.setup["key"]["key_id"]
    if gw.app.state.ctx.telemetry.archive is None:
        r = gw.client.get("/api/usage", headers=gw.cookie)
        assert r.status_code == 503
        assert gw.client.get("/api/requests", headers=gw.cookie).status_code == 503
        return
    usage = gw.client.get("/api/usage?bucket=1h&group_by=key_id&format=csv", headers=gw.cookie)
    assert usage.status_code == 200 and usage.headers["content-type"].startswith("text/csv")
    reqs = gw.client.get(f"/api/keys/{key_id}/requests?format=csv", headers=gw.cookie)
    assert reqs.status_code == 200 and reqs.text.splitlines()[0].startswith("request_id,")
    every = gw.client.get("/api/requests?format=csv", headers=gw.cookie)
    assert every.status_code == 200 and every.headers["content-type"].startswith("text/csv")
    assert every.text.splitlines()[0].startswith("request_id,")


def test_requests_provider_filter_applies_to_json_and_csv(gw, tmp_path):
    from control_plane.telemetry.archive import Archive
    from control_plane.telemetry.records import REQUEST_RECORD_FIELDS
    from tests.unit.test_telemetry_keys import T0, _provider_history_fixture

    arc = Archive(tmp_path / "provider-history.db")
    _provider_history_fixture(arc)
    gw.app.state.ctx.telemetry.archive = arc
    params = {
        "start": str(T0), "end": str(T0 + 100),
        "provider_id": "claude_code", "status": "429", "model": "shared",
    }
    response = gw.client.get("/api/requests", params=params, headers=gw.cookie)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    assert [r["request_id"] for r in body["rows"]] == ["sibling", "limited"]
    for target, expected in (
        (None, ["sibling", "limited"]),
        ("claude_code:sonnet", ["limited"]),
    ):
        filtered = {**params, **({"target_id": target} if target else {})}
        response = gw.client.get("/api/requests", params=filtered, headers=gw.cookie)
        assert response.status_code == 200, response.text
        assert response.json()["total"] == len(expected)
        assert [r["request_id"] for r in response.json()["rows"]] == expected
        response = gw.client.get(
            "/api/requests", params={**filtered, "format": "csv"}, headers=gw.cookie,
        )
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/csv")
        reader = csv.DictReader(io.StringIO(response.text))
        assert tuple(reader.fieldnames) == REQUEST_RECORD_FIELDS
        rows = list(reader)
        assert [r["request_id"] for r in rows] == expected
        assert all(r["provider_id"] == "claude_code" and r["status"] == "429" for r in rows)


def _origins_archive(gw, tmp_path):
    from control_plane.telemetry.archive import Archive
    from tests.unit.test_telemetry_keys import T0, _ingest, _rec

    arc = Archive(tmp_path / "origins.db")
    seeded = (
        [("8.8.8.8", "k1")] * 3
        + [("8.8.4.4", "k2")]
        + [("127.0.0.1", "k2")] * 2
        + [(None, "k2")]
    )
    _ingest(arc, [
        _rec(request_id=f"r{i}", ts=T0 + i, client_ip=ip, key_id=key, status=200)
        for i, (ip, key) in enumerate(seeded)
    ])
    gw.app.state.ctx.telemetry.archive = arc
    return {"start": str(T0), "end": str(T0 + 100)}


def test_request_origins_group_addresses_by_place_and_count_the_rest(gw, tmp_path, monkeypatch):
    from control_plane.telemetry.geoip import Location

    window = _origins_archive(gw, tmp_path)
    here = Location(lat=37.4, lon=-122.1, city="Mountain View", region="California", country="US")
    places = {"8.8.8.8": here, "8.8.4.4": here}
    monkeypatch.setattr(gw.app.state.ctx.geoip, "lookup", lambda ip: places.get(ip))

    r = gw.client.get("/api/requests/origins", params=window, headers=gw.cookie)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [(p["city"], p["requests"], p["ips"]) for p in body["points"]] == [
        ("Mountain View", 4, 2)
    ]
    assert (body["located"], body["local"], body["unknown"], body["unlocated"]) == (4, 2, 1, 0)
    assert body["database"]["state"] == "missing"  # nothing downloaded under test
    assert body["attribution"]["text"] == "IP Geolocation by DB-IP"

    narrowed = gw.client.get(
        "/api/requests/origins", params={**window, "key_id": "k1"}, headers=gw.cookie
    ).json()
    assert [(p["requests"], p["ips"]) for p in narrowed["points"]] == [(3, 1)]
    assert (narrowed["local"], narrowed["unknown"]) == (0, 0)


def test_request_origins_without_an_archive_is_history_unavailable(gw):
    gw.app.state.ctx.telemetry.archive = None
    r = gw.client.get("/api/requests/origins", headers=gw.cookie)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "history_unavailable"


def test_geoip_classifies_one_address_and_requires_one(gw):
    r = gw.client.get("/api/geoip", params={"ip": "127.0.0.1"}, headers=gw.cookie)
    assert r.status_code == 200, r.text
    assert (r.json()["kind"], r.json()["location"]) == ("local", None)
    missing = gw.client.get("/api/geoip", params={"ip": ""}, headers=gw.cookie)
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "missing_ip"


def test_a_known_place_beats_the_database_and_places_the_local_network(gw, tmp_path):
    from control_plane.telemetry.places import KnownPlaces

    # Its own file, so no other test sees the table.
    gw.app.state.ctx.geoip_places = KnownPlaces(tmp_path / "places.json")
    window = _origins_archive(gw, tmp_path)
    lab = {"cidr": "127.0.0.0/8", "label": "Lab", "lat": 45.5, "lon": -73.6}

    put = gw.client.put("/api/geoip/places", json={"places": [lab]}, headers=gw.cookie)
    assert put.status_code == 200, put.text
    assert (tmp_path / "places.json").exists()
    listed = gw.client.get("/api/geoip/places", headers=gw.cookie).json()
    assert [p["cidr"] for p in listed["places"]] == ["127.0.0.0/8"]
    assert listed["error"] is None

    one = gw.client.get("/api/geoip", params={"ip": "127.0.0.1"}, headers=gw.cookie).json()
    assert one["kind"] == "local"
    assert (one["location"]["label"], one["location"]["source"]) == ("Lab", "place")

    body = gw.client.get("/api/requests/origins", params=window, headers=gw.cookie).json()
    assert [(p["label"], p["requests"], p["ips"]) for p in body["points"]] == [("Lab", 2, 1)]
    assert (body["local"], body["unknown"]) == (0, 1)

    bad = gw.client.put(
        "/api/geoip/places",
        json={"places": [{"cidr": "nope", "label": "x", "lat": 0, "lon": 0}]},
        headers=gw.cookie,
    )
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "invalid_place"
    assert bad.json()["error"]["message"].startswith("places[0].cidr:")
    north = gw.client.put(
        "/api/geoip/places", json={"places": [{**lab, "lat": 91}]}, headers=gw.cookie
    )
    assert north.status_code == 400
    assert north.json()["error"]["message"] == "places[0].lat: latitude must be between -90 and 90"
    # A refused table leaves the stored one alone.
    assert gw.client.get("/api/geoip/places", headers=gw.cookie).json()["places"] == [lab]

    entries = gw.client.get("/api/audit", headers=gw.cookie).json()["entries"]
    changed = [e for e in entries if e["action"] == "settings_changed" and e["target_kind"] == "geoip_places"]
    assert len(changed) == 1
    assert (changed[0]["before"], changed[0]["after"]) == ({"places": 0}, {"places": 1})


def test_the_route_names_every_server_a_request_passed_and_places_each(gw, tmp_path, monkeypatch):
    from control_plane.gateway import request_route
    from control_plane.telemetry.archive import Archive
    from control_plane.telemetry.geoip import Location
    from control_plane.telemetry.places import KnownPlaces, parse_places
    from tests.unit.test_telemetry_keys import _ingest, _rec

    arc = Archive(tmp_path / "route.db")
    _ingest(arc, [_rec(
        request_id="r-route", ts=time.time(), node_id="spark-01", client_ip="8.8.8.8",
        target_id="openrouter:up/cloud", status=200,
        attempt_chain=[
            {"target_id": "d-local", "status": 503, "ms": 5.0},
            {"target_id": "openrouter:up/cloud", "status": 200, "ms": 40.0},
        ],
    )])
    ctx = gw.app.state.ctx
    ctx.telemetry.archive = arc
    ctx.geoip_places = KnownPlaces(tmp_path / "places.json")
    ctx.geoip_places.save(parse_places([{"cidr": "192.168.11.0/24", "label": "Lab", "lat": 45.5, "lon": -73.6}]))
    database = {
        "8.8.8.8": Location(37.4, -122.1, "Mountain View", "California", "US"),
        "203.0.113.9": Location(37.77, -122.42, "San Francisco", "California", "US"),
    }
    monkeypatch.setattr(ctx.geoip, "lookup", database.get)
    # The provider's base_url is http://cloud/v1; no test may reach real DNS.
    monkeypatch.setattr(request_route, "resolve_host", {"cloud": "203.0.113.9"}.get)

    r = gw.client.get("/api/requests/route", params={"request_id": "r-route"}, headers=gw.cookie)
    assert r.status_code == 200, r.text
    hops = r.json()["hops"]
    assert [(h["role"], h["name"], h["attempt"], h["status"]) for h in hops] == [
        ("client", "8.8.8.8", None, None),
        ("gateway", "spark-01", None, None),
        ("node", "spark-01", 1, 503),
        ("provider", "openrouter", 2, 200),
    ]
    assert hops[0]["location"]["city"] == "Mountain View"
    assert (hops[1]["address"], hops[1]["location"]["label"]) == ("192.168.11.13", "Lab")
    assert (hops[3]["host"], hops[3]["address"]) == ("cloud", "203.0.113.9")
    assert hops[3]["location"]["city"] == "San Francisco"
    assert hops[3]["note"] == request_route.PROVIDER_NOTE

    missing = gw.client.get("/api/requests/route", params={"request_id": ""}, headers=gw.cookie)
    assert missing.status_code == 400 and missing.json()["error"]["code"] == "missing_request_id"
    unknown = gw.client.get("/api/requests/route", params={"request_id": "r-nope"}, headers=gw.cookie)
    assert unknown.status_code == 404



def test_spec_decode_counters_are_attributed_after_settle(gw):
    from control_plane.telemetry.engine_metrics import SpecSample

    class Poller:
        async def attribute_when_ready(self, deployment_id, start, end, alone):
            assert deployment_id == "d-local" and alone is True
            return SpecSample(num_drafts=2, draft_tokens=6, accepted_tokens=3, accepted_per_position=[2, 1, 0])

        async def start(self):
            pass

        async def stop(self):
            pass

    gw.app.state.ctx.engine_metrics = Poller()
    with TestClient(gw.app) as client:
        assert client.post(
            "/v1/chat/completions",
            json={"model": LOCAL, "messages": [{"role": "user", "content": "x"}]},
            headers=gw.bearer(),
        ).status_code == 200
        deadline = time.time() + 5
        while time.time() < deadline and not any(r.spec_num_drafts for r in gw.rows.rows):
            time.sleep(0.02)
    first, again = [r for r in gw.rows.rows if r.served_name == LOCAL][-2:]
    assert (first.request_id, first.attempt_no) == (again.request_id, again.attempt_no)
    assert first.spec_decode_error == "not_reported"
    assert again.spec_num_drafts == 2 and again.spec_accepted_tokens == 3
    assert again.spec_decode_error is None


def _login(client, ua="drill/1"):
    r = client.post(
        "/api/auth/login",
        json={"username": ADMIN["username"], "password": ADMIN["password"]},
        headers={"User-Agent": ua},
    )
    assert r.status_code == 200, r.text
    client.cookies.clear()
    return {"Cookie": f"derate_session={r.cookies['derate_session']}"}


def test_sign_out_everywhere_ends_every_session_and_is_audited_once(gw):
    a, b = _login(gw.client), _login(gw.client)
    # Same browser: a different User-Agent would move the session's last-seen
    # user_agent (and raise session_ip_changed), which is not what this tests.
    me = gw.client.get("/api/auth/me", headers={**a, "User-Agent": "drill/1"})
    assert me.status_code == 200 and me.json()["session"]["user_agent"] == "drill/1"
    r = gw.client.delete("/api/auth/sessions", headers=a)
    assert r.status_code == 200 and r.json()["revoked"] == 3  # setup's session too
    for cookie in (a, b, gw.cookie):
        assert gw.client.get("/api/auth/me", headers=cookie).status_code == 401
    auth = gw.app.state.ctx.auth_stores.auth
    entries, _ = auth.audit_page(action="sessions_revoked")
    assert len(entries) == 1 and entries[0].after["count"] == 3
    assert entries[0].request_id and entries[0].request_id.startswith("r-")
    assert auth.audit_page(action="api_write")[1] == 0


def test_login_rows_carry_the_request_context(gw):
    _login(gw.client, ua="drill/7")
    auth = gw.app.state.ctx.auth_stores.auth
    login = auth.audit_page(action="login")[0][0]
    assert login.user_agent == "drill/7" and login.request_id.startswith("r-")
    assert login.session_id and len(login.session_id) == 12


def test_an_oversized_username_is_a_401_with_no_row(gw):
    auth = gw.app.state.ctx.auth_stores.auth
    before = auth.audit_page()[1]
    r = gw.client.post("/api/auth/login", json={"username": "u" * 5000, "password": "whatever-password"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials"
    assert auth.audit_page()[1] == before


def test_failures_from_one_address_slow_its_logins_down(gw, monkeypatch):
    from control_plane.gateway import auth_api

    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(auth_api.asyncio, "sleep", fake_sleep)
    for i in range(5):
        gw.client.post("/api/auth/login", json={"username": f"guess{i}", "password": "wrong-password-123"})
    assert slept == []
    r = gw.client.post("/api/auth/login", json={"username": ADMIN["username"], "password": ADMIN["password"]})
    assert r.status_code == 200  # a delay, never a lock
    assert slept == [1.0]


def test_the_audit_chain_is_reported_intact(gw):
    r = gw.client.get("/api/audit/integrity", headers=gw.cookie)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["rows"] > 0 and body["first_bad_id"] is None
    assert body["head_hash"] == gw.app.state.ctx.auth_stores.auth.head_hash()[0]


def test_setup_still_answers_the_secret_when_its_own_sign_in_is_locked(monkeypatch):
    from control_plane.auth import AuthService
    from control_plane.contracts.auth import LoginLocked

    def locked(self, *args, **kwargs):
        raise LoginLocked(30)

    monkeypatch.setattr(AuthService, "login", locked)
    client = TestClient(create_app(), client=("127.0.0.1", 50000))
    r = client.post("/api/setup/admin", json=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["secret_once"].startswith("derate-") and body["signed_in"] is False
    assert "set-cookie" not in r.headers
    assert client.get("/api/auth/state").json()["admin_exists"] is True
