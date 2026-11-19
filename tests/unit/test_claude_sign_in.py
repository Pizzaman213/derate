"""Claude subscription behavior against a scripted HTTP boundary, never a CLI token.

``FakeAnthropic`` and ``make_service`` are also the isolated gateway/browser
smoke's upstream boundary. The provider, credential store and wire adapters are
real; the fake only answers HTTP requests that would otherwise leave the box.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import stat
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from control_plane.providers import (
    AdapterUnsupportedError,
    ProviderNotAdmittingError,
    ProviderService,
    SecretStore,
    SignInError,
    UnknownProviderError,
    UpstreamError,
)
from control_plane.providers.config import (
    CLAUDE_AUTHORIZE_URL,
    CLAUDE_CLIENT_ID,
    CLAUDE_CODE_VERSION,
    CLAUDE_PROFILE_URL,
    CLAUDE_REDIRECT_URI,
    CLAUDE_SCOPES,
    CLAUDE_TOKEN_URL,
    SIGN_IN_ATTEMPT_TTL_S,
)

ACCESS = "claude-access-token-sentinel-0123456789abcdef"
REFRESH = "claude-refresh-token-sentinel-0123456789abcdef"
ACCESS_2 = "claude-rotated-access-sentinel-0123456789abcdef"
REFRESH_2 = "claude-rotated-refresh-sentinel-0123456789abcdef"
CODE = "claude-authorization-code-sentinel-0123456789"
MODEL = "claude-test-model"
ANSWER = "CLAUDE_CONNECTION_OK"
MESSAGES = [{"role": "user", "content": "Reply exactly: CLAUDE_CONNECTION_OK"}]


def run(coro):
    return asyncio.run(coro)


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def sse(*events: dict) -> bytes:
    return b"".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
        for event in events
    )


def message_start() -> dict:
    return {
        "type": "message_start",
        "message": {
            "id": "msg_claude_test", "type": "message", "role": "assistant",
            "model": MODEL, "content": [], "stop_reason": None,
            "usage": {"input_tokens": 7, "output_tokens": 0,
                      "cache_creation_input_tokens": 3, "cache_read_input_tokens": 5},
        },
    }


def text_stream(*parts: str) -> bytes:
    parts = parts or (ANSWER,)
    return sse(
        message_start(),
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text", "text": ""}},
        *({"type": "content_block_delta", "index": 0,
           "delta": {"type": "text_delta", "text": part}} for part in parts),
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
         "usage": {"output_tokens": 2}},
        {"type": "message_stop"},
    )


def token_payload(*, rotated: bool = False, **overrides) -> dict:
    return {
        "access_token": ACCESS_2 if rotated else ACCESS,
        "refresh_token": REFRESH_2 if rotated else REFRESH,
        "expires_in": 3600,
        "scope": CLAUDE_SCOPES,
        **overrides,
    }


class FakeAnthropic:
    """Only fixed Anthropic endpoints; optional OpenAI fixture for UI regression.

    Response queues accept Response, HTTPError, or a sync/async request callback.
    A callback can hold an individual HTTP request while a test removes or
    reconnects the real provider. Unknown destinations fail closed in the fake.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.token_responses: list = []
        self.profile_responses: list = []
        self.model_responses: list = []
        self.responses: list = []
        self.profile = {"account": {"uuid": "account-test", "email": "operator@example.com"},
                        "organization": {"uuid": "org-test"}}
        self.models = [{"id": MODEL, "type": "model", "display_name": "Claude test model",
                        "max_input_tokens": 200000}]
        self.openai = None

    def _answer(self, queue: list, default, request: httpx.Request):
        answer = queue.pop(0) if queue else default
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            answer = answer(request)
        return answer

    def handler(self, request: httpx.Request):
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if host in {"auth.openai.com", "api.openai.com"} and self.openai is not None:
            return self.openai.handler(request)
        if str(request.url) == CLAUDE_TOKEN_URL and request.method == "POST":
            grant = json.loads(request.content)
            return self._answer(
                self.token_responses,
                httpx.Response(200, json=token_payload(rotated=grant["grant_type"] == "refresh_token")),
                request,
            )
        if str(request.url) == CLAUDE_PROFILE_URL and request.method == "GET":
            return self._answer(self.profile_responses, httpx.Response(200, json=self.profile), request)
        if host == "api.anthropic.com" and path == "/v1/models" and request.method == "GET":
            return self._answer(
                self.model_responses,
                httpx.Response(200, json={"data": self.models, "has_more": False}), request,
            )
        if host == "api.anthropic.com" and path == "/v1/messages" and request.method == "POST":
            return self._answer(
                self.responses,
                httpx.Response(200, content=text_stream(), headers={"content-type": "text/event-stream"}),
                request,
            )
        raise AssertionError(f"Unexpected upstream destination: {request.method} {request.url}")

    def last(self, path: str) -> httpx.Request:
        return [request for request in self.requests if request.url.path == path][-1]

    def count(self, path: str) -> int:
        return sum(request.url.path == path for request in self.requests)

    def grants(self, kind: str) -> list[dict]:
        return [json.loads(request.content) for request in self.requests
                if str(request.url) == CLAUDE_TOKEN_URL
                and json.loads(request.content).get("grant_type") == kind]


def make_service(tmp_path, fake: FakeAnthropic, clock=None) -> ProviderService:
    return ProviderService(
        data_path=tmp_path,
        secrets=SecretStore(tmp_path / "secrets.json", env={}),
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
        now=clock or time.time,
    )


def callback(start: dict, *, code: str = CODE, state: str | None = None) -> str:
    state = state if state is not None else parse_qs(urlsplit(start["authorize_url"]).query)["state"][0]
    return f"{code}#{state}"


async def sign_in(service: ProviderService, fake: FakeAnthropic, *, enable: bool = True):
    start = service.start_claude_sign_in(None)
    provider = await service.complete_claude_sign_in(start["attempt_id"], callback(start))
    if enable:
        service.update(provider.provider_id, {"enabled_models": [MODEL]})
    return provider


async def collect(gen) -> bytes:
    return b"".join([chunk async for chunk in gen])


async def forward(service, pid, *, stream=False, body=None, **kwargs) -> bytes:
    return await collect(service.forward(pid, MODEL, body if body is not None else {"messages": MESSAGES},
                                         stream=stream, **kwargs))


def frames(raw: bytes) -> list:
    return [line[6:] if line[6:] == "[DONE]" else json.loads(line[6:])
            for line in raw.decode().splitlines() if line.startswith("data: ")]


class HttpGate:
    def __init__(self, response: httpx.Response) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.response = response

    async def __call__(self, request):
        self.entered.set()
        await asyncio.wait_for(self.release.wait(), 5)
        return self.response

    async def wait(self):
        await asyncio.wait_for(self.entered.wait(), 5)


# PKCE, callback validation and single consumption.


def test_authorize_url_uses_fixed_minimal_pkce_without_openai_fields(tmp_path):
    fake, clock = FakeAnthropic(), Clock()
    service = make_service(tmp_path, fake, clock)
    start = service.start_claude_sign_in(None)
    again = service.start_claude_sign_in(None)
    query = parse_qs(urlsplit(start["authorize_url"]).query)
    assert start.keys() == {"attempt_id", "authorize_url", "redirect_uri", "expires_at"}
    assert start["authorize_url"].split("?")[0] == CLAUDE_AUTHORIZE_URL
    assert query["client_id"] == [CLAUDE_CLIENT_ID]
    assert query["scope"] == ["user:profile user:inference"]
    assert query["code"] == ["true"] and query["response_type"] == ["code"]
    assert query["redirect_uri"] == [CLAUDE_REDIRECT_URI]
    assert query["code_challenge_method"] == ["S256"]
    assert not {"nonce", "resource", "agent_name_hint"} & query.keys()
    assert start["expires_at"] == clock.t + SIGN_IN_ATTEMPT_TTL_S
    other = parse_qs(urlsplit(again["authorize_url"]).query)
    assert start["attempt_id"] != again["attempt_id"]
    assert query["state"] != other["state"]
    assert query["code_challenge"] != other["code_challenge"]
    assert fake.requests == []
    provider = run(service.complete_claude_sign_in(start["attempt_id"], callback(start)))
    grant = fake.grants("authorization_code")[0]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(grant["code_verifier"].encode()).digest()).decode().rstrip("=")
    assert query["code_challenge"] == [challenge]
    assert grant == {"grant_type": "authorization_code", "client_id": CLAUDE_CLIENT_ID,
                     "code": CODE, "state": query["state"][0],
                     "redirect_uri": CLAUDE_REDIRECT_URI, "code_verifier": grant["code_verifier"]}
    assert service.claude_sign_in_status(start["attempt_id"]) == {"status": "done", "provider_id": provider.provider_id}


@pytest.mark.parametrize("format", ["display", "url", "query"])
def test_callback_accepts_only_local_parsing_and_never_fetches_submitted_url(tmp_path, format):
    fake = FakeAnthropic()
    service = make_service(tmp_path, fake)
    start = service.start_claude_sign_in(None)
    state = parse_qs(urlsplit(start["authorize_url"]).query)["state"][0]
    value = callback(start)
    if format != "display":
        value = urlencode({"code": CODE, "state": state})
        if format == "url":
            value = "https://untrusted.invalid/callback?" + value
    run(service.complete_claude_sign_in(start["attempt_id"], value))
    assert all(request.url.host in {"platform.claude.com", "api.anthropic.com"} for request in fake.requests)


@pytest.mark.parametrize("value", ["bare-code", "code#state#extra", "#state", "code#",
                                    "code=a&code=b&state=x", "code=a&state=x&state=y",
                                    "code=a&code=a&state=x", "https://example.test/?code=x"])
def test_malformed_callback_keeps_attempt_usable_and_makes_no_network_request(tmp_path, value):
    fake = FakeAnthropic()
    service = make_service(tmp_path, fake)
    start = service.start_claude_sign_in(None)
    with pytest.raises(SignInError) as error:
        run(service.complete_claude_sign_in(start["attempt_id"], value))
    assert error.value.code == "sign_in_bad_callback"
    assert fake.requests == [] and service.list() == []
    assert service.claude_sign_in_status(start["attempt_id"]) == {"status": "pending"}
    run(service.complete_claude_sign_in(start["attempt_id"], callback(start)))


def test_other_attempt_state_is_refused_without_consuming_either_attempt(tmp_path):
    fake = FakeAnthropic()
    service = make_service(tmp_path, fake)
    a, b = service.start_claude_sign_in(None), service.start_claude_sign_in(None)
    with pytest.raises(SignInError) as error:
        run(service.complete_claude_sign_in(a["attempt_id"], callback(b)))
    assert error.value.code == "sign_in_state_mismatch"
    assert fake.requests == [] and service.oauth.token_values() == []
    assert service.claude_sign_in_status(a["attempt_id"]) == {"status": "pending"}
    assert service.claude_sign_in_status(b["attempt_id"]) == {"status": "pending"}
    run(service.complete_claude_sign_in(a["attempt_id"], callback(a)))


def test_completion_consumes_attempt_before_network_await_and_replay_cannot_register_twice(tmp_path):
    async def scenario():
        fake = FakeAnthropic()
        service = make_service(tmp_path, fake)
        start = service.start_claude_sign_in(None)
        gate = HttpGate(httpx.Response(200, json=token_payload()))
        fake.token_responses.append(gate)
        first = asyncio.create_task(service.complete_claude_sign_in(start["attempt_id"], callback(start)))
        await gate.wait()
        try:
            with pytest.raises(SignInError) as error:
                await service.complete_claude_sign_in(start["attempt_id"], callback(start))
            assert error.value.code == "sign_in_attempt_unknown"
        finally:
            gate.release.set()
        provider = await first
        assert len(service.list()) == 1 and len(fake.grants("authorization_code")) == 1
        assert service.claude_sign_in_status(start["attempt_id"])["provider_id"] == provider.provider_id
    run(scenario())


@pytest.mark.parametrize("restart", [False, True])
def test_expired_or_restart_lost_attempt_is_unknown(tmp_path, restart):
    fake, clock = FakeAnthropic(), Clock()
    service = make_service(tmp_path, fake, clock)
    start = service.start_claude_sign_in(None)
    if restart:
        service = make_service(tmp_path, fake, clock)
    else:
        clock.t += SIGN_IN_ATTEMPT_TTL_S
    with pytest.raises(SignInError) as error:
        run(service.complete_claude_sign_in(start["attempt_id"], callback(start)))
    assert error.value.code == "sign_in_attempt_unknown"
    with pytest.raises(SignInError):
        service.claude_sign_in_status(start["attempt_id"])
    assert fake.requests == []


# Token/profile admission and account identity.


@pytest.mark.parametrize("overrides", [
    {"access_token": ""}, {"access_token": 123}, {"refresh_token": ""},
    {"refresh_token": None}, {"expires_in": 0}, {"expires_in": -1},
    {"expires_in": True}, {"expires_in": "3600"}, {"expires_in": None},
    {"scope": "user:profile"}, {"scope": []},
])
def test_malformed_token_success_registers_nothing_and_marks_attempt_failed(tmp_path, overrides):
    fake = FakeAnthropic()
    fake.token_responses.append(httpx.Response(200, json=token_payload(**overrides)))
    service = make_service(tmp_path, fake)
    start = service.start_claude_sign_in(None)
    with pytest.raises(SignInError) as error:
        run(service.complete_claude_sign_in(start["attempt_id"], callback(start)))
    assert error.value.code == "sign_in_exchange_failed"
    assert service.list() == [] and service.oauth.token_values() == []
    assert fake.count(urlsplit(CLAUDE_PROFILE_URL).path) == 0
    assert service.claude_sign_in_status(start["attempt_id"])["status"] == "failed"


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_token_expiry_is_not_fabricated(tmp_path, literal):
    fake = FakeAnthropic()
    body = json.dumps(token_payload()).replace('"expires_in": 3600', f'"expires_in": {literal}')
    fake.token_responses.append(httpx.Response(200, content=body))
    service = make_service(tmp_path, fake)
    with pytest.raises(SignInError) as error:
        run(sign_in(service, fake))
    assert error.value.code == "sign_in_exchange_failed"
    assert service.oauth.token_values() == [] and service.list() == []


@pytest.mark.parametrize("profile", [None, {}, {"account": {}, "organization": {}},
    {"account": {"uuid": 1}, "organization": {"uuid": "org"}},
    {"account": {"uuid": "account"}, "organization": {"uuid": " "}},
])
def test_invalid_profile_cannot_register_provider(tmp_path, profile):
    fake = FakeAnthropic()
    fake.profile_responses.append(httpx.Response(200, json=profile))
    service = make_service(tmp_path, fake)
    with pytest.raises(SignInError) as error:
        run(sign_in(service, fake))
    assert error.value.code == "sign_in_exchange_failed"
    assert service.oauth.token_values() == [] and service.list() == []


@pytest.mark.parametrize("scope_present", [False, True])
def test_profile_and_tokens_persist_without_id_token_and_reload_securely(tmp_path, scope_present):
    fake, clock = FakeAnthropic(), Clock()
    payload = token_payload()
    if not scope_present:
        del payload["scope"]
    fake.token_responses.append(httpx.Response(200, json=payload))
    service = make_service(tmp_path, fake, clock)
    provider = run(sign_in(service, fake, enable=False))
    record = service.oauth.get(provider.provider_id)
    assert record.subject == "org-test:account-test"
    assert record.email == "operator@example.com" and record.id_token == ""
    assert record.scopes == CLAUDE_SCOPES.split()
    assert record.access_token == ACCESS and record.refresh_token == REFRESH
    assert record.expires_at == clock.t + 3600
    assert stat.S_IMODE((tmp_path / "oauth.json").stat().st_mode) == 0o600
    assert service.key_status(provider.provider_id) == {"key_state": "set", "key_source": "oauth"}
    assert service.sign_in_account(provider.provider_id) == "operator@example.com"
    profile_request = fake.last(urlsplit(CLAUDE_PROFILE_URL).path)
    assert profile_request.headers["authorization"] == f"Bearer {ACCESS}"
    assert "oauth-2025-04-20" in profile_request.headers["anthropic-beta"]
    assert profile_request.headers["anthropic-version"] == "2023-06-01"
    loaded = make_service(tmp_path, fake, clock)
    assert loaded.oauth.get(provider.provider_id) == record
    assert loaded.get(provider.provider_id).kind.value == "claude_code"
    assert loaded.models() == []


@pytest.mark.parametrize("change", ["account", "organization", "profile_failure"])
def test_reconnect_refusal_preserves_previous_credentials_and_settings(tmp_path, change):
    fake = FakeAnthropic()
    service = make_service(tmp_path, fake)
    provider = run(sign_in(service, fake))
    pid = provider.provider_id
    service.update(pid, {"aliases": {MODEL: "friendly-claude"}})
    before = (tmp_path / "oauth.json").read_bytes()
    fake.token_responses.append(httpx.Response(200, json=token_payload(rotated=True)))
    if change == "profile_failure":
        fake.profile_responses.append(httpx.Response(503, json={"error": {"message": "profile unavailable"}}))
    else:
        fake.profile[change]["uuid"] = "different-subject"
    start = service.start_claude_sign_in(pid)
    with pytest.raises(SignInError) as error:
        run(service.complete_claude_sign_in(start["attempt_id"], callback(start)))
    assert error.value.code == ("sign_in_exchange_failed" if change == "profile_failure" else "sign_in_wrong_account")
    assert (tmp_path / "oauth.json").read_bytes() == before
    assert service.models()[0][1].served_name == "friendly-claude"
    assert service.catalogue(pid)[0]["enabled"] is True


def test_same_subject_reconnect_rotates_credentials_without_resetting_allowlist_or_aliases(tmp_path):
    fake = FakeAnthropic()
    service = make_service(tmp_path, fake)
    provider = run(sign_in(service, fake))
    pid = provider.provider_id
    service.update(pid, {"aliases": {MODEL: "friendly-claude"}})
    fake.token_responses.append(httpx.Response(200, json=token_payload(rotated=True)))
    start = service.start_claude_sign_in(pid)
    renewed = run(service.complete_claude_sign_in(start["attempt_id"], callback(start)))
    assert renewed.provider_id == pid and len(service.list()) == 1
    assert service.oauth.get(pid).access_token == ACCESS_2
    assert service.models()[0][1].served_name == "friendly-claude"
    assert service.catalogue(pid)[0]["enabled"] is True


def test_registration_failure_rolls_back_only_new_oauth_record(tmp_path, monkeypatch):
    fake = FakeAnthropic()
    service = make_service(tmp_path, fake)
    existing = run(sign_in(service, fake))
    before = service.oauth.get(existing.provider_id)
    def refuse(*args, **kwargs):
        raise ValueError("registration deliberately refused")
    monkeypatch.setattr(service, "_register", refuse)
    with pytest.raises(ValueError, match="registration deliberately refused"):
        run(sign_in(service, fake))
    assert service.oauth.get(existing.provider_id) == before
    assert len(service.list()) == 1
    records = json.loads((tmp_path / "oauth.json").read_text())["records"]
    assert set(records) == {existing.provider_id}


# Refresh single-flight, rejected-token refresh and credential generation races.


def test_expiring_concurrent_requests_rotate_once_and_use_new_bearer(tmp_path):
    async def scenario():
        fake, clock = FakeAnthropic(), Clock()
        service = make_service(tmp_path, fake, clock)
        pid = (await sign_in(service, fake)).provider_id
        clock.t += 3600
        gate = HttpGate(httpx.Response(200, json=token_payload(rotated=True)))
        fake.token_responses.append(gate)
        requests = [asyncio.create_task(forward(service, pid)) for _ in range(5)]
        await gate.wait()
        gate.release.set()
        answers = await asyncio.gather(*requests)
        assert all(json.loads(raw)["choices"][0]["message"]["content"] == ANSWER for raw in answers)
        assert fake.grants("refresh_token") == [{"grant_type": "refresh_token", "client_id": CLAUDE_CLIENT_ID,
                                                 "refresh_token": REFRESH}]
        assert service.oauth.get(pid).refresh_token == REFRESH_2
        assert all(request.headers["authorization"] == f"Bearer {ACCESS_2}"
                   for request in fake.requests if request.url.path == "/v1/messages")
    run(scenario())


def test_simultaneous_401s_reuse_token_rotated_since_rejected_request(tmp_path):
    async def scenario():
        fake = FakeAnthropic()
        service = make_service(tmp_path, fake)
        pid = (await sign_in(service, fake)).provider_id
        gates = [HttpGate(httpx.Response(401, json={"error": {"type": "authentication_error", "message": "expired"}}))
                 for _ in range(2)]
        fake.responses.extend(gates)
        a = asyncio.create_task(forward(service, pid))
        await gates[0].wait()
        b = asyncio.create_task(forward(service, pid))
        await gates[1].wait()
        gates[0].release.set()
        await a
        gates[1].release.set()
        await b
        assert len(fake.grants("refresh_token")) == 1
        assert fake.count("/v1/messages") == 4
    run(scenario())


@pytest.mark.parametrize("omit_refresh", [False, True])
def test_refresh_rotates_atomically_and_can_retain_omitted_refresh_token(tmp_path, omit_refresh):
    fake, clock = FakeAnthropic(), Clock()
    service = make_service(tmp_path, fake, clock)
    pid = run(sign_in(service, fake)).provider_id
    payload = token_payload(rotated=True)
    if omit_refresh:
        del payload["refresh_token"]
    fake.token_responses.append(httpx.Response(200, json=payload))
    clock.t += 3600
    run(forward(service, pid))
    record = service.oauth.get(pid)
    assert record.access_token == ACCESS_2
    assert record.refresh_token == (REFRESH if omit_refresh else REFRESH_2)
    assert record.expires_at == clock.t + 3600
    loaded = make_service(tmp_path, fake, clock)
    assert loaded.oauth.get(pid) == record


@pytest.mark.parametrize("status,payload", [
    (400, {"error": "invalid_grant"}), (400, {"error": "invalid_token"}),
    (401, {"error": "unauthorized"}), (403, {"error": "forbidden"}),
])
def test_definitive_refresh_refusal_clears_credentials_and_admission(tmp_path, status, payload):
    fake, clock = FakeAnthropic(), Clock()
    service = make_service(tmp_path, fake, clock)
    pid = run(sign_in(service, fake)).provider_id
    clock.t += 3600
    fake.token_responses.append(httpx.Response(status, json=payload))
    with pytest.raises(UpstreamError) as error:
        run(forward(service, pid))
    assert error.value.status_code == 401
    assert service.key_status(pid) == {"key_state": "reauth", "key_source": "oauth"}
    assert service.get(pid).enabled is False
    assert service.models() == [] and service.route_targets() == []
    record = service.oauth.get(pid)
    assert record.needs_reauth and not record.access_token and not record.refresh_token
    assert ACCESS not in (tmp_path / "oauth.json").read_text()
    assert REFRESH not in (tmp_path / "oauth.json").read_text()


@pytest.mark.parametrize("response", [
    httpx.Response(429, json={"error": {"message": "slow down"}}),
    httpx.Response(503, json={"error": {"message": "unavailable"}}),
    httpx.ConnectError("transport unavailable"),
    httpx.Response(200, json=token_payload(rotated=True, expires_in=None)),
])
def test_transient_or_malformed_refresh_does_not_destroy_last_credentials(tmp_path, response):
    fake, clock = FakeAnthropic(), Clock()
    service = make_service(tmp_path, fake, clock)
    pid = run(sign_in(service, fake)).provider_id
    before = (tmp_path / "oauth.json").read_bytes()
    clock.t += 3600
    fake.token_responses.append(response)
    with pytest.raises(UpstreamError) as error:
        run(forward(service, pid))
    assert error.value.status_code == 502
    assert (tmp_path / "oauth.json").read_bytes() == before
    assert service.oauth.get(pid).needs_reauth is False
    assert len(fake.grants("refresh_token")) == 1


@pytest.mark.parametrize("operation", ["refresh", "reconnect"])
def test_removing_during_credential_request_cannot_resurrect_provider_or_token(tmp_path, operation):
    async def scenario():
        fake, clock = FakeAnthropic(), Clock()
        service = make_service(tmp_path, fake, clock)
        pid = (await sign_in(service, fake)).provider_id
        gate = HttpGate(httpx.Response(200, json=token_payload(rotated=True)))
        fake.token_responses.append(gate)
        if operation == "refresh":
            clock.t += 3600
            pending = asyncio.create_task(forward(service, pid))
        else:
            start = service.start_claude_sign_in(pid)
            pending = asyncio.create_task(service.complete_claude_sign_in(start["attempt_id"], callback(start)))
        await gate.wait()
        service.remove(pid)
        gate.release.set()
        with pytest.raises((SignInError, UpstreamError, UnknownProviderError)):
            await pending
        assert service.list() == [] and service.oauth.get(pid) is None
        assert pid not in json.loads((tmp_path / "oauth.json").read_text())["records"]
    run(scenario())


def test_refresh_losing_to_reconnect_uses_newer_credential_and_never_overwrites_it(tmp_path):
    async def scenario():
        fake, clock = FakeAnthropic(), Clock()
        service = make_service(tmp_path, fake, clock)
        pid = (await sign_in(service, fake)).provider_id
        clock.t += 3600
        stale_access = "claude-stale-refresh-result-0123456789abcdef"
        gate = HttpGate(httpx.Response(200, json=token_payload(rotated=True, access_token=stale_access)))
        fake.token_responses.append(gate)
        refresh = asyncio.create_task(forward(service, pid))
        await gate.wait()
        start = service.start_claude_sign_in(pid)
        fake.token_responses.append(httpx.Response(200, json=token_payload(rotated=True)))
        # The reconnect commits before its catalogue refresh; allow that refresh
        # to wait on the already-held single-flight lock without deadlocking us.
        profile_gate = HttpGate(httpx.Response(200, json=fake.profile))
        fake.profile_responses.append(profile_gate)
        reconnect = asyncio.create_task(service.complete_claude_sign_in(start["attempt_id"], callback(start)))
        await profile_gate.wait()
        profile_gate.release.set()
        # Wait for the credential commit, not for reconnect's blocked catalogue.
        for _ in range(100):
            if service.oauth.get(pid).access_token == ACCESS_2:
                break
            await asyncio.sleep(0)
        assert service.oauth.get(pid).access_token == ACCESS_2
        gate.release.set()
        await asyncio.gather(refresh, reconnect)
        assert service.oauth.get(pid).access_token == ACCESS_2
        assert fake.last("/v1/messages").headers["authorization"] == f"Bearer {ACCESS_2}"
    run(scenario())


def test_removal_invalidates_pending_reconnect_and_has_no_remote_revocation(tmp_path):
    fake = FakeAnthropic()
    service = make_service(tmp_path, fake)
    pid = run(sign_in(service, fake)).provider_id
    start = service.start_claude_sign_in(pid)
    count = len(fake.requests)
    assert run(service.sign_out_async(pid)) is False
    assert len(fake.requests) == count
    service.remove(pid)
    assert service.oauth.get(pid) is None and service.list() == []
    with pytest.raises(SignInError) as error:
        run(service.complete_claude_sign_in(start["attempt_id"], callback(start)))
    assert error.value.code == "sign_in_attempt_unknown"
    assert len(fake.requests) == count


# Complete paginated discovery, honest metadata and explicit serving.


def test_catalogue_paginates_with_oauth_and_no_models_auto_enable(tmp_path):
    fake = FakeAnthropic()
    fake.model_responses.extend([
        httpx.Response(200, json={"data": fake.models, "has_more": True, "last_id": MODEL}),
        httpx.Response(200, json={"data": [{"id": "claude-second-model"}], "has_more": False}),
    ])
    service = make_service(tmp_path, fake)
    pid = run(sign_in(service, fake, enable=False)).provider_id
    assert {row["upstream_id"] for row in service.catalogue(pid)} == {MODEL, "claude-second-model"}
    calls = [request for request in fake.requests if request.url.path == "/v1/models"]
    assert calls[0].url.params["limit"] == "1000"
    assert calls[1].url.params["after_id"] == MODEL
    assert all(request.headers["authorization"] == f"Bearer {ACCESS}" for request in calls)
    assert all("oauth-2025-04-20" in request.headers["anthropic-beta"] for request in calls)
    models = {model.upstream_id: model for model in service.models_for(pid)}
    assert models[MODEL].context_length == 200000
    assert models["claude-second-model"].context_length == 0
    assert models[MODEL].input_cost_per_mtok is None and models[MODEL].output_cost_per_mtok is None
    assert service.models() == [] and service.route_targets() == []
    assert service.servable()[0].models == []
    assert all(row["enabled"] is False for row in service.catalogue(pid))
    service.update(pid, {"enabled_models": [MODEL]})
    assert [model.upstream_id for _, model in service.models()] == [MODEL]
    assert [model.upstream_id for model in service.servable()[0].models] == [MODEL]
    run(forward(service, pid))
    service.update(pid, {"enabled_models": []})
    assert service.models() == [] and service.servable()[0].models == []
    assert service.route_targets() == []
    sent = fake.count("/v1/messages")
    with pytest.raises(ProviderNotAdmittingError):
        run(forward(service, pid))
    assert fake.count("/v1/messages") == sent


@pytest.mark.parametrize("second", [
    {"data": [{"id": "partial-only"}], "has_more": True},
    {"data": [{"id": "partial-only"}], "has_more": True, "last_id": MODEL},
    {"not_data": [], "has_more": False},
    {"data": {}, "has_more": False},
])
def test_malformed_or_nonadvancing_second_page_keeps_entire_previous_catalogue(tmp_path, second):
    fake = FakeAnthropic()
    service = make_service(tmp_path, fake)
    pid = run(sign_in(service, fake)).provider_id
    before = service.public_models(pid)
    fake.model_responses.extend([
        httpx.Response(200, json={"data": [{"id": "new-first-page"}], "has_more": True, "last_id": MODEL}),
        httpx.Response(200, json=second),
    ])
    run(service.refresh_async(pid))
    assert service.public_models(pid) == before
    assert service.catalogue(pid)[0]["enabled"] is True
    assert service.health(pid)[1]
    assert fake.count("/v1/models") == 3


def test_failed_second_page_retains_cache_but_honest_empty_catalogue_clears_it(tmp_path):
    fake = FakeAnthropic()
    service = make_service(tmp_path, fake)
    pid = run(sign_in(service, fake)).provider_id
    before = service.public_models(pid)
    fake.model_responses.extend([
        httpx.Response(200, json={"data": [{"id": "new-first-page"}], "has_more": True, "last_id": "new-first-page"}),
        httpx.Response(503, json={"error": {"type": "overloaded_error", "message": "later page failed"}}),
    ])
    run(service.refresh_async(pid))
    assert service.public_models(pid) == before
    fake.model_responses.append(httpx.Response(200, json={"data": [], "has_more": False}))
    run(service.refresh_async(pid))
    assert service.public_models(pid) == [] and service.models() == []
    assert service.health(pid) == (True, None)


def test_catalogue_401_refreshes_once_with_new_bearer(tmp_path):
    fake = FakeAnthropic()
    service = make_service(tmp_path, fake)
    pid = run(sign_in(service, fake)).provider_id
    fake.model_responses.append(httpx.Response(401, json={"error": {"type": "authentication_error", "message": "expired"}}))
    run(service.refresh_async(pid))
    assert len(fake.grants("refresh_token")) == 1
    assert fake.last("/v1/models").headers["authorization"] == f"Bearer {ACCESS_2}"
    assert service.health(pid) == (True, None)


# Real service -> Messages converter -> real usage/error/cancellation lifecycle.


def test_streaming_and_buffered_forward_agree_and_preserve_usage_once(tmp_path):
    fake = FakeAnthropic()
    service = make_service(tmp_path, fake)
    pid = run(sign_in(service, fake)).provider_id
    streamed = frames(run(forward(service, pid, stream=True)))
    buffered = json.loads(run(forward(service, pid)))
    assert streamed[-1] == "[DONE]" and streamed.count("[DONE]") == 1
    chunks = streamed[:-1]
    deltas = [chunk["choices"][0]["delta"] for chunk in chunks if chunk.get("choices")]
    assert [delta for delta in deltas if "role" in delta] == [{"role": "assistant"}]
    assert "".join(delta.get("content", "") for delta in deltas) == ANSWER
    assert buffered["choices"][0]["message"]["content"] == ANSWER
    assert buffered["choices"][0]["finish_reason"] == "stop"
    usage = {"prompt_tokens": 15, "completion_tokens": 2, "total_tokens": 17,
             "prompt_tokens_details": {"cached_tokens": 5}}
    assert chunks[-1]["usage"] == buffered["usage"] == usage
    assert service.public_dict(pid)["tokens_today"] == {"input": 30, "output": 4}
    assert service.spend_today(pid) == 0
    sent = fake.last("/v1/messages")
    body = json.loads(sent.content)
    assert sent.url.params["beta"] == "true"
    assert sent.headers["authorization"] == f"Bearer {ACCESS}"
    assert sent.headers["anthropic-version"] == "2023-06-01"
    assert "oauth-2025-04-20" in sent.headers["anthropic-beta"]
    assert "interleaved-thinking-2025-05-14" in sent.headers["anthropic-beta"]
    assert sent.headers["user-agent"] == f"claude-cli/{CLAUDE_CODE_VERSION} (external, cli)"
    assert "x-api-key" not in sent.headers
    assert body["stream"] is True and body["model"] == MODEL and body["max_tokens"] == 4096
    assert body["system"][0]["text"].startswith("x-anthropic-billing-header: ")
    assert body["system"][1]["text"] == "You are a Claude agent, built on Anthropic's Claude Agent SDK."


def test_multiturn_images_and_case_distinct_tool_names_round_trip_through_service(tmp_path):
    fake = FakeAnthropic()
    service = make_service(tmp_path, fake)
    pid = run(sign_in(service, fake)).provider_id
    fake.responses.append(httpx.Response(200, content=sse(
        message_start(),
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "tool_use", "id": "call-new", "name": "mcp_Tool_1", "input": {}}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": '{"city":'}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": '"Paris"}'}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 4}},
        {"type": "message_stop"},
    )))
    body = {
        "model": "caller-must-not-select-this",
        "messages": [
            {"role": "system", "content": "Keep this EXACTLY."},
            {"role": "developer", "content": "And this too."},
            {"role": "user", "content": [{"type": "text", "text": "Look"},
                {"type": "image_url", "image_url": {"url": "https://images.invalid/pic.png"}}]},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call-old", "type": "function", "function": {"name": "Lookup", "arguments": '{"city":"Rome"}'}}]},
            {"role": "tool", "tool_call_id": "call-old", "content": "Rome result"},
            {"role": "user", "content": "Now Paris"},
        ],
        "tools": [{"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}
                  for name in ("Lookup", "lookup")],
        "tool_choice": {"type": "function", "function": {"name": "lookup"}},
    }
    output = json.loads(run(forward(service, pid, body=body)))
    call = output["choices"][0]["message"]["tool_calls"][0]
    assert call["id"] == "call-new" and call["function"]["name"] == "lookup"
    assert json.loads(call["function"]["arguments"]) == {"city": "Paris"}
    assert output["choices"][0]["finish_reason"] == "tool_calls"
    sent = json.loads(fake.last("/v1/messages").content)
    assert sent["model"] == MODEL
    assert [block["text"] for block in sent["system"]][-2:] == ["Keep this EXACTLY.", "And this too."]
    assert [message["role"] for message in sent["messages"]] == ["user", "assistant", "user"]
    assert sent["messages"][0]["content"][1] == {"type": "image", "source": {"type": "url", "url": "https://images.invalid/pic.png"}}
    assert sent["messages"][1]["content"][0]["id"] == "call-old"
    assert sent["messages"][2]["content"][0]["tool_use_id"] == "call-old"
    assert sent["tool_choice"]["name"] == "mcp_Tool_1"
    assert not any(request.url.host == "images.invalid" for request in fake.requests)
    # The client, not derate, executes tools and posts their result next turn.
    next_body = {"messages": [{"role": "user", "content": "Paris?"},
                  {"role": "assistant", "content": None, "tool_calls": [call]},
                  {"role": "tool", "tool_call_id": call["id"], "content": "sunny"}],
                 "tools": body["tools"]}
    run(forward(service, pid, body=next_body))
    assert json.loads(fake.last("/v1/messages").content)["messages"][-1]["content"][0]["tool_use_id"] == "call-new"


@pytest.mark.parametrize("field,value", [("n", 2), ("seed", 42), ("presence_penalty", 1),
    ("response_format", {"type": "json_object"}), ("reasoning_effort", "minimal"),
    ("logprobs", True)])
def test_unsupported_semantics_fail_400_before_inference_http(tmp_path, field, value):
    fake = FakeAnthropic()
    service = make_service(tmp_path, fake)
    pid = run(sign_in(service, fake)).provider_id
    with pytest.raises(UpstreamError) as error:
        run(forward(service, pid, body={"messages": MESSAGES, field: value}))
    assert error.value.status_code == 400 and field in error.value.message
    assert fake.count("/v1/messages") == 0


@pytest.mark.parametrize("endpoint", ["completions", "embeddings", "audio/speech", "audio/transcriptions", "realtime"])
def test_other_endpoints_never_receive_subscription_bearer(tmp_path, endpoint):
    fake = FakeAnthropic()
    service = make_service(tmp_path, fake)
    pid = run(sign_in(service, fake)).provider_id
    before = len(fake.requests)
    with pytest.raises(AdapterUnsupportedError, match="Claude subscription providers serve /v1/chat/completions only"):
        run(forward(service, pid, endpoint=endpoint))
    assert len(fake.requests) == before
    with pytest.raises(AdapterUnsupportedError):
        service.resolve_key(pid)


def test_raw_multipart_is_refused_before_any_upstream_request(tmp_path):
    async def scenario():
        fake = FakeAnthropic()
        service = make_service(tmp_path, fake)
        pid = (await sign_in(service, fake)).provider_id
        count = len(fake.requests)
        with pytest.raises(AdapterUnsupportedError):
            async with service.open_upstream(pid, MODEL, None, endpoint="audio/transcriptions",
                content=b"private multipart upload", content_type="multipart/form-data; boundary=private"):
                pytest.fail("raw upload was admitted")
        assert len(fake.requests) == count
    run(scenario())


@pytest.mark.parametrize("native,status", [("rate_limit_error", 429), ("overloaded_error", 503)])
@pytest.mark.parametrize("stream", [False, True])
def test_native_inband_error_is_never_a_successful_completion(tmp_path, native, status, stream):
    fake = FakeAnthropic()
    service = make_service(tmp_path, fake)
    pid = run(sign_in(service, fake)).provider_id
    fake.responses.append(httpx.Response(200, content=sse(message_start(),
        {"type": "error", "error": {"type": native, "message": f"refused {ACCESS}"}})))
    if stream:
        output = frames(run(forward(service, pid, stream=True)))
        assert output[-1] == "[DONE]"
        errors = [frame["error"] for frame in output[:-1] if "error" in frame]
        assert len(errors) == 1
        assert not any(frame.get("choices") and frame["choices"][0].get("finish_reason")
                       for frame in output[:-1])
        assert ACCESS not in json.dumps(errors)
    else:
        with pytest.raises(UpstreamError) as error:
            run(forward(service, pid))
        assert error.value.status_code == status
        assert ACCESS not in error.value.body
    assert fake.count("/v1/messages") == 1
    if status == 429:
        model = next(m for m in service.public_dict(pid)["models"] if m["upstream_id"] == MODEL)
        assert model["admitting"] is False
        assert "refused" in model["last_error"] and ACCESS not in model["last_error"]
        assert service.health(pid) == (True, None)
    else:
        assert service.health(pid)[1]


@pytest.mark.parametrize("wire", [sse(message_start()), b"event: message_start\ndata: {invalid}\n\n",
                                    b"event: content_block_delta\ndata: {"])
@pytest.mark.parametrize("stream", [False, True])
def test_truncated_or_malformed_messages_stream_is_an_error_not_stop(tmp_path, wire, stream):
    fake = FakeAnthropic()
    service = make_service(tmp_path, fake)
    pid = run(sign_in(service, fake)).provider_id
    fake.responses.append(httpx.Response(200, content=wire))
    if stream:
        output = frames(run(forward(service, pid, stream=True)))
        assert output[-1] == "[DONE]" and any("error" in frame for frame in output[:-1])
        assert not any(frame.get("choices") and frame["choices"][0].get("finish_reason")
                       for frame in output[:-1])
    else:
        with pytest.raises(UpstreamError) as error:
            run(forward(service, pid))
        assert error.value.status_code == 502
    assert fake.count("/v1/messages") == 1


def test_native_http_error_is_normalized_redacted_and_preserves_retry_after(tmp_path):
    fake = FakeAnthropic()
    service = make_service(tmp_path, fake)
    pid = run(sign_in(service, fake)).provider_id
    fake.responses.append(httpx.Response(429, headers={"retry-after": "17"},
        json={"type": "error", "error": {"type": "rate_limit_error", "message": f"quota {ACCESS}"}}))
    with pytest.raises(UpstreamError) as error:
        run(forward(service, pid))
    assert error.value.status_code == 429 and error.value.retry_after_s == 17
    payload = json.loads(error.value.body)
    assert "error" in payload and "quota" in payload["error"]["message"]
    assert ACCESS not in error.value.body
    assert service.admitting(pid, MODEL) is False
    assert service.admitting(pid) is True


class BlockingStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = False
        self.waiting = asyncio.Event()

    async def __aiter__(self):
        yield sse(message_start())
        self.waiting.set()
        await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("stream", [False, True])
def test_cancellation_closes_upstream_and_releases_outstanding_counter(tmp_path, stream):
    async def scenario():
        fake = FakeAnthropic()
        service = make_service(tmp_path, fake)
        pid = (await sign_in(service, fake)).provider_id
        upstream = BlockingStream()
        fake.responses.append(httpx.Response(200, stream=upstream))
        task = asyncio.create_task(forward(service, pid, stream=stream))
        await asyncio.wait_for(upstream.waiting.wait(), 5)
        assert service.route_targets()[0].outstanding == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert upstream.closed is True
        assert service.route_targets()[0].outstanding == 0
        assert fake.count("/v1/messages") == 1
    run(scenario())


def test_closing_on_translated_rate_limit_still_applies_provider_backoff(tmp_path):
    async def scenario():
        fake = FakeAnthropic()
        service = make_service(tmp_path, fake)
        pid = (await sign_in(service, fake)).provider_id
        fake.responses.append(httpx.Response(200, content=sse({
            "type": "error", "error": {
                "type": "rate_limit_error", "message": "subscription quota exhausted",
            },
        })))
        response = service.forward(pid, MODEL, {"messages": MESSAGES}, stream=True)
        first = await anext(response)
        assert json.loads(first.removeprefix(b"data: ").strip())["error"]["code"] == "rate_limit_error"
        await response.aclose()
        assert service._entry(pid).runtime.rate_limited(service._now(), MODEL)
        assert service._entry(pid).runtime.outstanding.get(MODEL, 0) == 0
    run(scenario())


# No credential exfiltration, fallback origin, export or output leak.


@pytest.mark.parametrize("patch", [{"api_key": "test-key-must-not-be-used"},
    {"api_key_ref": "ANTHROPIC_API_KEY"}, {"base_url": "https://attacker.invalid/v1"}])
def test_create_patch_and_dry_run_refuse_keys_and_untrusted_destination(tmp_path, patch):
    fake = FakeAnthropic()
    service = make_service(tmp_path, fake)
    with pytest.raises(ValueError):
        service.add({"kind": "claude_code", **patch})
    pid = run(sign_in(service, fake)).provider_id
    before = (tmp_path / "providers.json").read_bytes()
    for apply in (service.update, service.validate_patch):
        with pytest.raises(ValueError):
            apply(pid, patch)
    assert (tmp_path / "providers.json").read_bytes() == before
    assert all(request.url.host in {"api.anthropic.com", "platform.claude.com"} for request in fake.requests)


def test_ambient_api_key_never_substitutes_for_subscription_or_reauth(tmp_path, monkeypatch):
    fallback = "anthropic-api-key-must-not-be-used-0123456789abcdef"
    monkeypatch.setenv("ANTHROPIC_API_KEY", fallback)
    fake, clock = FakeAnthropic(), Clock()
    service = make_service(tmp_path, fake, clock)
    pid = run(sign_in(service, fake)).provider_id
    clock.t += 3600
    fake.token_responses.append(httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(UpstreamError):
        run(forward(service, pid))
    for request in fake.requests:
        assert fallback not in str(request.headers) and fallback.encode() not in request.content
        assert "x-api-key" not in request.headers
        assert request.url.host not in {"auth.openai.com", "api.openai.com"}


def test_tokens_code_and_full_profile_never_appear_in_public_outputs_or_logs(tmp_path, caplog):
    fake = FakeAnthropic()
    fake.profile["private_profile_sentinel"] = "never-export-entire-profile"
    service = make_service(tmp_path, fake)
    start = service.start_claude_sign_in(None)
    with caplog.at_level(logging.DEBUG):
        provider = run(service.complete_claude_sign_in(start["attempt_id"], callback(start)))
        pid = provider.provider_id
        service.update(pid, {"enabled_models": [MODEL]})
        fake.responses.append(httpx.Response(400, json={"type": "error", "error": {
            "type": "invalid_request_error", "message": f"quoted {ACCESS} {REFRESH} {CODE}"}}))
        with pytest.raises(UpstreamError) as error:
            run(forward(service, pid))
        shapes = [service.public_list(), service.public_dict(pid), service.catalogue(pid),
                  service.public_models(pid), service.sign_in_account(pid),
                  service.claude_sign_in_status(start["attempt_id"]),
                  [row.__dict__ for row in service.list()], str(error.value), error.value.body,
                  (tmp_path / "providers.json").read_text(), [record.getMessage() for record in caplog.records]]
    exported = json.dumps(shapes, default=str)
    for sentinel in (ACCESS, REFRESH, CODE, "never-export-entire-profile"):
        assert sentinel not in exported
    assert "operator@example.com" in exported


def test_failed_exchange_scrubs_returned_credentials_and_submitted_code_from_status(tmp_path):
    fake = FakeAnthropic()
    fake.token_responses.append(httpx.Response(400, json={
        **token_payload(), "error": {"message": f"bad code {CODE}, token {ACCESS}, refresh {REFRESH}"},
    }))
    service = make_service(tmp_path, fake)
    start = service.start_claude_sign_in(None)
    with pytest.raises(SignInError) as error:
        run(service.complete_claude_sign_in(start["attempt_id"], callback(start)))
    output = json.dumps([str(error.value), service.claude_sign_in_status(start["attempt_id"])])
    assert all(secret not in output for secret in (CODE, ACCESS, REFRESH))
    assert service.list() == [] and service.oauth.token_values() == []


@pytest.mark.parametrize("text,suffix,cch", [
    ("", "bc0", "e3b0c"),
    ("abc", "bc0", "ba781"),
    ("😀" * 11, "9e4", "8aee6"),
    ("abcd\ud800xx", "43e", "d144b"),
])
def test_billing_prefix_matches_independent_javascript_utf16_vectors(text, suffix, cch):
    from control_plane.providers.claude_sign_in import billing_prefix

    assert billing_prefix(text) == (
        f"x-anthropic-billing-header: cc_version={CLAUDE_CODE_VERSION}.{suffix}; "
        f"cc_entrypoint=sdk-cli; cch={cch};"
    )


def test_refresh_cannot_overwrite_a_credential_invalidated_while_http_was_pending(tmp_path):
    from dataclasses import replace

    async def scenario():
        fake, clock = FakeAnthropic(), Clock()
        service = make_service(tmp_path, fake, clock)
        pid = (await sign_in(service, fake)).provider_id
        clock.t += 3600
        gate = HttpGate(httpx.Response(200, json=token_payload(rotated=True)))
        fake.token_responses.append(gate)
        pending = asyncio.create_task(forward(service, pid))
        await gate.wait()
        old = service.oauth.get(pid)
        invalidated = replace(old, access_token="", refresh_token="", needs_reauth=True,
                              saved_at=clock.t)
        service.oauth.put(invalidated)
        gate.release.set()
        with pytest.raises(UpstreamError) as error:
            await pending
        assert error.value.status_code == 401
        assert service.oauth.get(pid) == invalidated
        assert fake.count("/v1/messages") == 0
    run(scenario())


def test_stale_refresh_does_not_replace_new_provider_reusing_removed_id(tmp_path):
    async def scenario():
        fake, clock = FakeAnthropic(), Clock()
        service = make_service(tmp_path, fake, clock)
        pid = (await sign_in(service, fake)).provider_id
        clock.t += 3600
        gate = HttpGate(httpx.Response(200, json=token_payload(
            rotated=True, access_token="stale-old-provider-token-0123456789abcdef")))
        fake.token_responses.append(gate)
        pending = asyncio.create_task(forward(service, pid))
        await gate.wait()
        service.remove(pid)
        fake.token_responses.append(httpx.Response(200, json=token_payload(rotated=True)))
        replacement_task = asyncio.create_task(sign_in(service, fake))
        for _ in range(100):
            current = service.oauth.get(pid)
            if current is not None and current.access_token == ACCESS_2:
                break
            await asyncio.sleep(0)
        assert service.oauth.get(pid).access_token == ACCESS_2
        before = service.oauth.get(pid)
        gate.release.set()
        with pytest.raises(UpstreamError):
            await pending
        replacement = await replacement_task
        assert replacement.provider_id == pid
        assert service.oauth.get(pid) == before
        assert service.get(pid).enabled
    run(scenario())


def test_reconnect_losing_to_newer_reconnect_cannot_overwrite_new_tokens(tmp_path):
    async def scenario():
        fake = FakeAnthropic()
        service = make_service(tmp_path, fake)
        pid = (await sign_in(service, fake)).provider_id
        older = service.start_claude_sign_in(pid)
        newer = service.start_claude_sign_in(pid)
        gate = HttpGate(httpx.Response(200, json=fake.profile))
        fake.profile_responses.append(gate)
        fake.token_responses.append(httpx.Response(200, json=token_payload(
            access_token="superseded-reconnect-token-0123456789abcdef")))
        pending = asyncio.create_task(service.complete_claude_sign_in(
            older["attempt_id"], callback(older)))
        await gate.wait()
        fake.token_responses.append(httpx.Response(200, json=token_payload(rotated=True)))
        await service.complete_claude_sign_in(newer["attempt_id"], callback(newer))
        before = service.oauth.get(pid)
        gate.release.set()
        with pytest.raises(SignInError) as error:
            await pending
        assert error.value.code == "sign_in_attempt_unknown"
        assert service.oauth.get(pid) == before
        assert service.oauth.get(pid).access_token == ACCESS_2
    run(scenario())


class ByteChunks(httpx.AsyncByteStream):
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.closed = False

    async def __aiter__(self):
        for index in range(len(self.content)):
            yield self.content[index:index + 1]

    async def aclose(self):
        self.closed = True


def test_service_preserves_split_utf8_and_crlf_through_real_stream_converter(tmp_path):
    fake = FakeAnthropic()
    service = make_service(tmp_path, fake)
    pid = run(sign_in(service, fake)).provider_id
    # json.dumps normally escapes Unicode; use literal UTF-8 to actually split it.
    wire = text_stream("🙂 café").replace(b"\\ud83d\\ude42", "🙂".encode())
    wire = wire.replace(b"\\u00e9", "é".encode()).replace(b"\n", b"\r\n")
    upstream = ByteChunks(wire)
    fake.responses.append(httpx.Response(200, stream=upstream))
    output = frames(run(forward(service, pid, stream=True)))
    assert "".join(frame["choices"][0]["delta"].get("content", "")
                   for frame in output[:-1] if frame.get("choices")) == "🙂 café"
    assert output[-1] == "[DONE]" and upstream.closed


def test_second_inference_401_is_final_and_does_not_refresh_forever(tmp_path):
    fake = FakeAnthropic()
    service = make_service(tmp_path, fake)
    pid = run(sign_in(service, fake)).provider_id
    fake.responses.extend(httpx.Response(401, json={"type": "error", "error": {
        "type": "authentication_error", "message": "still unauthorized"}}) for _ in range(2))
    with pytest.raises(UpstreamError) as error:
        run(forward(service, pid))
    assert error.value.status_code == 401
    assert len(fake.grants("refresh_token")) == 1
    assert fake.count("/v1/messages") == 2
    assert service.admitting(pid) is False


def test_token_redirect_is_not_followed_and_cannot_leak_code_or_tokens(tmp_path):
    fake = FakeAnthropic()
    fake.token_responses.append(httpx.Response(307, headers={"location": "https://attacker.invalid/token"}))
    service = make_service(tmp_path, fake)
    with pytest.raises(SignInError) as error:
        run(sign_in(service, fake))
    assert error.value.code == "sign_in_exchange_failed"
    assert len(fake.requests) == 1
    assert fake.requests[0].url.host == "platform.claude.com"
    assert service.list() == [] and service.oauth.token_values() == []


def test_profile_denial_cannot_create_row_even_when_minimal_scope_is_omitted(tmp_path):
    fake = FakeAnthropic()
    payload = token_payload()
    del payload["scope"]
    fake.token_responses.append(httpx.Response(200, json=payload))
    fake.profile_responses.append(httpx.Response(403, json={"error": {"message": "profile forbidden"}}))
    service = make_service(tmp_path, fake)
    with pytest.raises(SignInError) as error:
        run(sign_in(service, fake))
    assert error.value.code == "sign_in_exchange_failed"
    assert service.list() == [] and service.oauth.token_values() == []
