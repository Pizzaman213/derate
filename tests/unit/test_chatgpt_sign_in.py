"""Sign in with ChatGPT as a provider credential.

Everything OpenAI would answer is faked behind one ``httpx.MockTransport``:
OpenID discovery, a JWKS built from an RSA key generated here (so the ID
token's signature is checked for real), the token endpoint, revocation,
``/v1/models`` and ``/v1/responses`` as scripted SSE.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
import jwt.algorithms
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from control_plane.providers import (
    AdapterUnsupportedError,
    ProviderService,
    SecretStore,
    SignInError,
    UpstreamError,
)
from control_plane.providers.config import CHATGPT_REDIRECT_URI
from control_plane.providers.responses_adapter import ResponsesToChat, chat_to_responses

ISSUER = "https://auth.openai.com"
JWKS_URI = f"{ISSUER}/.well-known/jwks.json"
REVOKE_URI = f"{ISSUER}/oauth/revoke"
CLIENT_ID = "oaiapp_test"
PLAN_SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"

# Distinctive enough that finding one anywhere is unambiguous.
ACCESS = "chatgpt-access-tokenvalue-0123456789abcdefXYZ"
REFRESH = "chatgpt-refresh-tokenvalue-0123456789abcdefXYZ"
ACCESS_2 = "chatgpt-access-rotated-0123456789abcdefQRS"
REFRESH_2 = "chatgpt-refresh-rotated-0123456789abcdefQRS"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_JWK = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(_KEY.public_key()))
JWKS = {"keys": [{**_JWK, "kid": "test-kid", "alg": "RS256", "use": "sig"}]}


def run(coro):
    return asyncio.run(coro)


class Clock:
    def __init__(self, t: float | None = None) -> None:
        self.t = t if t is not None else time.time()

    def __call__(self) -> float:
        return self.t


def sse(*events: dict) -> bytes:
    return b"".join(
        f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events
    )


def text_stream(*parts: str, usage=(7, 2)) -> bytes:
    events = [{"type": "response.created", "response": {"id": "resp_1"}}]
    events += [{"type": "response.output_text.delta", "delta": p} for p in parts]
    events.append(
        {
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "usage": {"input_tokens": usage[0], "output_tokens": usage[1]},
            },
        }
    )
    return sse(*events)


class OpenAI:
    """auth.openai.com and api.openai.com, as far as derate talks to them."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.claims: dict = {}
        self.token_scope = PLAN_SCOPE
        self.token_responses: list[httpx.Response] = []
        self.revoke_responses: list[httpx.Response] = []
        self.responses: list[httpx.Response] = []

    def id_token(self, **claims) -> str:
        now = int(time.time())
        payload = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": "user-1",
            "email": "op@example.com",
            "iat": now,
            "exp": now + 3600,
            **self.claims,
            **claims,
        }
        return jwt.encode(payload, _KEY, algorithm="RS256", headers={"kid": "test-kid"})

    def form(self, request: httpx.Request) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if host == "auth.openai.com":
            if path == "/.well-known/openid-configuration":
                return httpx.Response(
                    200,
                    json={"issuer": ISSUER, "jwks_uri": JWKS_URI, "revocation_endpoint": REVOKE_URI},
                )
            if path == "/.well-known/jwks.json":
                return httpx.Response(200, json=JWKS)
            if path == "/api/accounts/oauth/token":
                if self.token_responses:
                    return self.token_responses.pop(0)
                form = self.form(request)
                if form["grant_type"] == "refresh_token":
                    return httpx.Response(
                        200,
                        json={
                            "access_token": ACCESS_2,
                            "refresh_token": REFRESH_2,
                            "expires_in": 3600,
                            "scope": PLAN_SCOPE,
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "access_token": ACCESS,
                        "refresh_token": REFRESH,
                        "id_token": self.id_token(),
                        "token_type": "Bearer",
                        "expires_in": 3600,
                        "scope": self.token_scope,
                    },
                )
            if path == "/oauth/revoke":
                if self.revoke_responses:
                    return self.revoke_responses.pop(0)
                return httpx.Response(200)
        if host == "api.openai.com":
            if request.method == "GET" and path == "/v1/models":
                return httpx.Response(
                    200,
                    json={
                        "models": [
                            {"slug": "gpt-x", "display_name": "GPT X", "visibility": "list"},
                            {"slug": "hidden", "display_name": "Hidden", "visibility": "hide"},
                        ]
                    },
                )
            if request.method == "POST" and path == "/v1/responses":
                if self.responses:
                    return self.responses.pop(0)
                return httpx.Response(
                    200, content=text_stream("Hel", "lo"),
                    headers={"content-type": "text/event-stream"},
                )
        return httpx.Response(404, json={"error": {"message": "nope"}})

    def last(self, path: str) -> httpx.Request:
        return [r for r in self.requests if r.url.path == path][-1]

    def count(self, path: str) -> int:
        return sum(1 for r in self.requests if r.url.path == path)


def make_service(tmp_path, fake: OpenAI, clock: Clock | None = None) -> ProviderService:
    return ProviderService(
        data_path=tmp_path,
        secrets=SecretStore(tmp_path / "secrets.json", env={}),
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
        now=clock or time.time,
    )


def callback(start: dict, **overrides) -> str:
    state = parse_qs(urlsplit(start["authorize_url"]).query)["state"][0]
    params = {"code": "auth-code", "state": state, "scope": PLAN_SCOPE, "client_id": CLIENT_ID}
    params.update(overrides)
    params = {k: v for k, v in params.items() if v is not None}
    return f"{CHATGPT_REDIRECT_URI}?{urlencode(params)}"


def nonce_of(start: dict) -> str:
    return parse_qs(urlsplit(start["authorize_url"]).query)["nonce"][0]


async def sign_in(service: ProviderService, fake: OpenAI, *, enable: bool = True):
    start = service.start_chatgpt_sign_in(None)
    fake.claims = {"nonce": nonce_of(start)}
    provider = await service.complete_chatgpt_sign_in(start["attempt_id"], callback(start))
    if enable:
        service.update(provider.provider_id, {"enabled_models": ["gpt-x"]})
    return provider


async def collect(gen) -> bytes:
    return b"".join([chunk async for chunk in gen])


def frames(raw: bytes) -> list:
    out = []
    for line in raw.decode().splitlines():
        if line.startswith("data: "):
            data = line[len("data: "):]
            out.append(data if data == "[DONE]" else json.loads(data))
    return out


# ---------------------------------------------------------------------------
# The sign-in flow
# ---------------------------------------------------------------------------


def test_authorize_url_registers_a_new_client(tmp_path):
    service = make_service(tmp_path, OpenAI())
    start = service.start_chatgpt_sign_in(None)
    parts = urlsplit(start["authorize_url"])
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == (
        "https://auth.openai.com/api/accounts/authorize"
    )
    assert query["client_id"] == "dynamic_agent_client"
    assert query["agent_name_hint"] == "derate"
    assert query["ext_agent_host_id"].startswith("urn:uuid:")
    assert query["resource"] == "https://api.openai.com/v1"
    assert query["code_challenge_method"] == "S256"
    assert query["redirect_uri"] == "http://127.0.0.1:1455/auth/callback"
    assert "chatgpt.tokens.use.direct" in query["scope"].split()
    assert "login_hint" not in query and "id_token_hint" not in query
    assert start["redirect_uri"] == CHATGPT_REDIRECT_URI
    # One host id for the install, not one per attempt.
    again = parse_qs(urlsplit(service.start_chatgpt_sign_in(None)["authorize_url"]).query)
    assert again["ext_agent_host_id"][0] == query["ext_agent_host_id"]


def test_completing_creates_a_provider_with_only_listed_models(tmp_path):
    fake = OpenAI()
    service = make_service(tmp_path, fake)
    provider = run(sign_in(service, fake, enable=False))
    assert provider.provider_id == "chatgpt"
    assert provider.api_key_ref == ""
    assert service.key_status("chatgpt") == {"key_state": "set", "key_source": "oauth"}
    assert service.sign_in_account("chatgpt") == "op@example.com"
    assert [m["upstream_id"] for m in service.catalogue("chatgpt")] == ["gpt-x"]
    exchange = fake.form(fake.last("/api/accounts/oauth/token"))
    assert exchange["grant_type"] == "authorization_code"
    assert exchange["client_id"] == CLIENT_ID
    assert exchange["redirect_uri"] == CHATGPT_REDIRECT_URI
    assert exchange["resource"] == "https://api.openai.com/v1"
    assert exchange["code_verifier"]


def test_reauthorization_uses_the_issued_client_and_account_hint(tmp_path):
    fake = OpenAI()
    service = make_service(tmp_path, fake)
    run(sign_in(service, fake))
    start = service.start_chatgpt_sign_in("chatgpt")
    query = {k: v[0] for k, v in parse_qs(urlsplit(start["authorize_url"]).query).items()}
    assert query["client_id"] == CLIENT_ID
    assert query["login_hint"] == "op@example.com"
    assert "agent_name_hint" not in query


@pytest.mark.parametrize(
    "case, code",
    [
        ("wrong_state", "sign_in_state_mismatch"),
        ("declined", "sign_in_declined"),
        ("no_plan_scope", "sign_in_no_plan_usage"),
        ("bad_nonce", "sign_in_bad_id_token"),
        ("no_issued_client", "sign_in_incomplete"),
        ("not_a_callback", "sign_in_bad_callback"),
        ("unknown_attempt", "sign_in_attempt_unknown"),
    ],
)
def test_a_refused_callback_stores_nothing(tmp_path, case, code):
    fake = OpenAI()
    service = make_service(tmp_path, fake)
    start = service.start_chatgpt_sign_in(None)
    fake.claims = {"nonce": nonce_of(start)}
    attempt_id = start["attempt_id"]
    url = callback(start)
    if case == "wrong_state":
        url = callback(start, state="somebody-elses")
    elif case == "declined":
        url = callback(start, code=None, error="access_denied", error_description="User declined")
    elif case == "no_plan_scope":
        fake.token_scope = "openid profile email offline_access"
        url = callback(start, scope=fake.token_scope)
    elif case == "bad_nonce":
        fake.claims = {"nonce": "not-this-attempt"}
    elif case == "no_issued_client":
        url = callback(start, client_id=None)
    elif case == "not_a_callback":
        url = "https://chatgpt.com/"
    elif case == "unknown_attempt":
        attempt_id = "nope"
    before = (tmp_path / "oauth.json").read_bytes()

    with pytest.raises(SignInError) as excinfo:
        run(service.complete_chatgpt_sign_in(attempt_id, url))
    assert excinfo.value.code == code
    assert (tmp_path / "oauth.json").read_bytes() == before
    assert service.list() == []
    if case == "declined":
        assert "User declined" in excinfo.value.message


def test_reauthorization_as_a_different_account_is_refused(tmp_path):
    fake = OpenAI()
    service = make_service(tmp_path, fake)
    run(sign_in(service, fake))
    before = (tmp_path / "oauth.json").read_bytes()
    start = service.start_chatgpt_sign_in("chatgpt")
    fake.claims = {"nonce": nonce_of(start), "sub": "user-2"}
    with pytest.raises(SignInError) as excinfo:
        run(service.complete_chatgpt_sign_in(start["attempt_id"], callback(start, client_id=None)))
    assert excinfo.value.code == "sign_in_wrong_account"
    assert (tmp_path / "oauth.json").read_bytes() == before


def test_no_token_in_any_output(tmp_path, caplog):
    fake = OpenAI()
    service = make_service(tmp_path, fake)
    # An upstream error that quotes the token back.
    fake.responses.append(
        httpx.Response(400, json={"error": {"message": f"bad token {ACCESS}"}})
    )
    with caplog.at_level(logging.DEBUG):
        provider = run(sign_in(service, fake))
        with pytest.raises(UpstreamError) as excinfo:
            run(collect(service.forward("chatgpt", "gpt-x", {"messages": []})))
    id_token = json.loads((tmp_path / "oauth.json").read_text())["records"]["chatgpt"]["id_token"]
    shapes = {
        "public_list": service.public_list(),
        "list": [p.__dict__ for p in service.list()],
        "provider": provider.__dict__,
        "providers.json": (tmp_path / "providers.json").read_text(),
        "error": [str(excinfo.value), excinfo.value.body],
        "logs": [r.getMessage() for r in caplog.records],
    }
    for name, payload in shapes.items():
        text = json.dumps(payload, default=str)
        for token in (ACCESS, REFRESH, id_token):
            assert token not in text, f"token leaked in {name}"


# ---------------------------------------------------------------------------
# Forwarding through the Responses adapter
# ---------------------------------------------------------------------------


def test_streaming_forward_translates_both_ways(tmp_path):
    fake = OpenAI()
    service = make_service(tmp_path, fake)
    run(sign_in(service, fake))
    body = {
        "messages": [{"role": "system", "content": "S"}, {"role": "user", "content": "hi"}],
        "temperature": 0.2,
    }
    raw = run(collect(service.forward("chatgpt", "gpt-x", body, stream=True)))

    sent = fake.last("/v1/responses")
    payload = json.loads(sent.content)
    assert payload["instructions"] == "S"
    assert payload["store"] is False and payload["stream"] is True
    assert "temperature" not in payload
    assert payload["input"] == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}
    ]
    assert sent.headers["authorization"] == f"Bearer {ACCESS}"

    out = frames(raw)
    assert out[-1] == "[DONE]"
    chunks = [f for f in out[:-1]]
    assert all(c["object"] == "chat.completion.chunk" for c in chunks)
    # The role frame leads, on response.created, before any text: the proxy
    # times TTFT off the first frame, as it does for every other kind.
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant"}
    roles = [c for c in chunks if c["choices"] and "role" in c["choices"][0]["delta"]]
    assert len(roles) == 1
    deltas = [c["choices"][0]["delta"].get("content") for c in chunks if c["choices"]]
    assert [d for d in deltas if d] == ["Hel", "lo"]
    assert [c["choices"][0]["finish_reason"] for c in chunks if c["choices"]][-1] == "stop"
    assert chunks[-1]["choices"] == []
    assert chunks[-1]["usage"] == {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}
    tokens = service.public_dict("chatgpt")["tokens_today"]
    assert tokens == {"input": 7, "output": 2}


def test_the_role_frame_goes_out_when_the_response_is_created():
    adapter = ResponsesToChat(stream=True, model="m", now=0)
    first = adapter.feed(sse({"type": "response.created", "response": {"id": "r"}}))
    assert [json.loads(f.decode().removeprefix("data: "))["choices"][0]["delta"] for f in first] == [
        {"role": "assistant"}
    ]
    assert adapter.feed(sse({"type": "response.in_progress", "response": {"id": "r"}})) == []
    text = adapter.feed(sse({"type": "response.output_text.delta", "delta": "Hi"}))
    assert [json.loads(f.decode().removeprefix("data: "))["choices"][0]["delta"] for f in text] == [
        {"content": "Hi"}
    ]


def test_reasoning_summaries_stream_as_delta_reasoning():
    adapter = ResponsesToChat(stream=True, model="m", now=0)
    adapter.feed(sse({"type": "response.created", "response": {"id": "r"}}))
    deltas = []
    for event in (
        {"type": "response.reasoning_summary_part.added", "summary_index": 0},
        {"type": "response.reasoning_summary_text.delta", "delta": "Plan"},
        {"type": "response.reasoning_summary_part.added", "summary_index": 1},
        {"type": "response.reasoning_summary_text.delta", "delta": "Check"},
        {"type": "response.output_text.delta", "delta": "Hi"},
    ):
        deltas += [json.loads(f.decode().removeprefix("data: "))["choices"][0]["delta"] for f in adapter.feed(sse(event))]
    # The first part adds no separator; the second is a paragraph break.
    assert deltas == [{"reasoning": "Plan"}, {"reasoning": "\n\n"}, {"reasoning": "Check"}, {"content": "Hi"}]
    done = adapter.feed(sse({
        "type": "response.completed",
        "response": {"id": "r", "usage": {"input_tokens": 5, "output_tokens": 9, "output_tokens_details": {"reasoning_tokens": 6}}},
    }))
    usage = json.loads(done[-2].decode().removeprefix("data: "))["usage"]
    assert usage["completion_tokens_details"] == {"reasoning_tokens": 6}


def test_reasoning_effort_asks_for_a_summary_unless_reasoning_is_off():
    msgs = [{"role": "user", "content": "hi"}]
    assert chat_to_responses({"messages": msgs, "reasoning_effort": "high"}, "m")["reasoning"] == {
        "effort": "high",
        "summary": "auto",
    }
    assert chat_to_responses({"messages": msgs, "reasoning_effort": "none"}, "m")["reasoning"] == {"effort": "none"}
    assert "reasoning" not in chat_to_responses({"messages": msgs}, "m")


def test_non_streaming_forward_returns_one_completion(tmp_path):
    fake = OpenAI()
    service = make_service(tmp_path, fake)
    run(sign_in(service, fake))
    raw = run(collect(service.forward("chatgpt", "gpt-x", {"messages": [{"role": "user", "content": "hi"}]})))
    body = json.loads(raw)
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "Hello"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["prompt_tokens"] == 7


def test_in_stream_usage_limit_is_a_429_and_backs_off(tmp_path):
    fake = OpenAI()
    service = make_service(tmp_path, fake)
    run(sign_in(service, fake))
    fake.responses.append(
        httpx.Response(
            200,
            content=sse(
                {"type": "response.created", "response": {"id": "r"}},
                {
                    "type": "response.failed",
                    "response": {
                        "id": "r",
                        "error": {
                            "code": "subscription_sharing_usage_limit_exceeded",
                            "message": "You have reached your plan's limit.",
                        },
                    },
                },
            ),
        )
    )
    with pytest.raises(UpstreamError) as excinfo:
        run(collect(service.forward("chatgpt", "gpt-x", {"messages": [{"role": "user", "content": "hi"}]})))
    assert excinfo.value.status_code == 429
    assert json.loads(excinfo.value.body)["error"]["code"] == (
        "subscription_sharing_usage_limit_exceeded"
    )
    assert not service.admitting("chatgpt", "gpt-x")
    assert service.admitting("chatgpt")


def test_tool_calls_round_trip(tmp_path):
    fake = OpenAI()
    service = make_service(tmp_path, fake)
    run(sign_in(service, fake))
    fake.responses.append(
        httpx.Response(
            200,
            content=sse(
                {"type": "response.created", "response": {"id": "r2"}},
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {"type": "function_call", "id": "fc_1", "call_id": "call_9", "name": "lookup"},
                },
                {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": '{"q":'},
                {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": '"x"}'},
                {"type": "response.completed", "response": {"id": "r2"}},
            ),
        )
    )
    body = {
        "messages": [
            {"role": "user", "content": "find x"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "nothing"},
        ],
        "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
    }
    raw = run(collect(service.forward("chatgpt", "gpt-x", body, stream=True)))
    sent = json.loads(fake.last("/v1/responses").content)
    assert sent["input"][1] == {
        "type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": "{}"
    }
    assert sent["input"][2] == {"type": "function_call_output", "call_id": "call_1", "output": "nothing"}
    assert sent["tools"] == [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}]

    chunks = [f for f in frames(raw) if f != "[DONE]" and f["choices"]]
    calls = [c["choices"][0]["delta"]["tool_calls"][0] for c in chunks if "tool_calls" in c["choices"][0]["delta"]]
    assert calls[0]["id"] == "call_9" and calls[0]["function"]["name"] == "lookup"
    assert "".join(c["function"]["arguments"] for c in calls) == '{"q":"x"}'
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"


# ---------------------------------------------------------------------------
# Token lifecycle
# ---------------------------------------------------------------------------


def test_a_token_near_expiry_is_refreshed_and_rotated(tmp_path):
    fake = OpenAI()
    clock = Clock()
    service = make_service(tmp_path, fake, clock)
    run(sign_in(service, fake))
    clock.t += 3600 - 299
    run(collect(service.forward("chatgpt", "gpt-x", {"messages": [{"role": "user", "content": "hi"}]})))
    refresh = fake.form(fake.last("/api/accounts/oauth/token"))
    assert refresh == {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "refresh_token": REFRESH,
        "resource": "https://api.openai.com/v1",
    }
    assert fake.last("/v1/responses").headers["authorization"] == f"Bearer {ACCESS_2}"
    assert service.oauth.get("chatgpt").refresh_token == REFRESH_2


def test_a_refused_refresh_needs_the_user(tmp_path):
    fake = OpenAI()
    clock = Clock()
    service = make_service(tmp_path, fake, clock)
    run(sign_in(service, fake))
    clock.t += 3600
    fake.token_responses.append(httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(UpstreamError) as excinfo:
        run(collect(service.forward("chatgpt", "gpt-x", {"messages": [{"role": "user", "content": "hi"}]})))
    assert excinfo.value.status_code == 401
    assert "sign in again" in excinfo.value.message
    assert service.key_status("chatgpt") == {"key_state": "reauth", "key_source": "oauth"}
    assert service.get("chatgpt").enabled is False
    assert REFRESH not in (tmp_path / "oauth.json").read_text()


def test_signing_in_again_restores_a_lapsed_provider(tmp_path):
    fake = OpenAI()
    clock = Clock()
    service = make_service(tmp_path, fake, clock)
    run(sign_in(service, fake))
    clock.t += 3600
    fake.token_responses.append(httpx.Response(400, json={"error": "refresh_token_expired"}))
    run(service.refresh_async("chatgpt"))
    assert service.get("chatgpt").enabled is False

    start = service.start_chatgpt_sign_in("chatgpt")
    fake.claims = {"nonce": nonce_of(start)}
    provider = run(service.complete_chatgpt_sign_in(start["attempt_id"], callback(start)))
    assert provider.enabled is True
    assert service.key_status("chatgpt") == {"key_state": "set", "key_source": "oauth"}


def test_a_401_forces_one_refresh_then_retries(tmp_path):
    fake = OpenAI()
    service = make_service(tmp_path, fake)
    run(sign_in(service, fake))
    fake.responses.append(httpx.Response(401, json={"error": {"message": "token revoked"}}))
    raw = run(collect(service.forward("chatgpt", "gpt-x", {"messages": [{"role": "user", "content": "hi"}]})))
    assert json.loads(raw)["choices"][0]["message"]["content"] == "Hello"
    assert fake.form(fake.last("/api/accounts/oauth/token"))["grant_type"] == "refresh_token"
    assert fake.last("/v1/responses").headers["authorization"] == f"Bearer {ACCESS_2}"
    assert fake.count("/v1/responses") == 2


# ---------------------------------------------------------------------------
# What a sign-in kind refuses
# ---------------------------------------------------------------------------


def test_only_chat_completions_is_served(tmp_path):
    fake = OpenAI()
    service = make_service(tmp_path, fake)
    run(sign_in(service, fake))
    with pytest.raises(AdapterUnsupportedError):
        run(collect(service.forward("chatgpt", "gpt-x", {"input": "x"}, endpoint="embeddings")))
    with pytest.raises(AdapterUnsupportedError):
        service.resolve_key("chatgpt")


def test_the_add_route_cannot_create_one(tmp_path):
    service = make_service(tmp_path, OpenAI())
    with pytest.raises(ValueError, match="POST /api/sign-in/chatgpt"):
        service.add({"kind": "chatgpt", "api_key": "sk-anything-0123456789"})


def test_sign_out_revokes_the_refresh_token(tmp_path):
    fake = OpenAI()
    service = make_service(tmp_path, fake)
    run(sign_in(service, fake))
    assert run(service.sign_out_async("chatgpt")) is True
    assert fake.form(fake.last("/oauth/revoke")) == {
        "token": REFRESH,
        "token_type_hint": "refresh_token",
        "client_id": CLIENT_ID,
    }
    service.remove("chatgpt")
    assert service.oauth.get("chatgpt") is None


def test_sign_out_reports_an_unconfirmed_revocation(tmp_path):
    fake = OpenAI()
    service = make_service(tmp_path, fake)
    run(sign_in(service, fake))
    fake.revoke_responses = [httpx.Response(500) for _ in range(3)]
    assert run(service.sign_out_async("chatgpt")) is False
    assert fake.count("/oauth/revoke") == 3


# ---------------------------------------------------------------------------
# Getting the redirect back
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "shape",
    ["address_bar", "location_header", "curl", "bare_query"],
)
def test_the_callback_is_read_from_wherever_it_was_copied(tmp_path, shape):
    """A browser that hangs on the redirect still shows it in the network panel."""
    fake = OpenAI()
    service = make_service(tmp_path, fake)
    start = service.start_chatgpt_sign_in(None)
    fake.claims = {"nonce": nonce_of(start)}
    url = callback(start)
    pasted = {
        "address_bar": url,
        "location_header": f"location: {url}",
        "curl": f"curl '{url}' -H 'Accept: text/html' --compressed",
        "bare_query": url.split("?", 1)[1],
    }[shape]
    provider = run(service.complete_chatgpt_sign_in(start["attempt_id"], pasted))
    assert provider.provider_id == "chatgpt"


def test_a_redirect_that_reaches_the_coordinator_finishes_the_attempt(tmp_path):
    fake = OpenAI()
    service = make_service(tmp_path, fake)
    start = service.start_chatgpt_sign_in(None)
    fake.claims = {"nonce": nonce_of(start)}
    assert service.chatgpt_sign_in_status(start["attempt_id"]) == {"status": "pending"}

    attempt_id, provider = run(service.complete_chatgpt_callback(callback(start).split("?", 1)[1]))
    assert attempt_id == start["attempt_id"]
    assert service.chatgpt_sign_in_status(attempt_id) == {
        "status": "done", "provider_id": provider.provider_id
    }
    # The UI's paste, arriving after the redirect already finished it, is refused.
    with pytest.raises(SignInError) as excinfo:
        run(service.complete_chatgpt_sign_in(attempt_id, callback(start)))
    assert excinfo.value.code == "sign_in_attempt_unknown"


def test_a_redirect_with_a_foreign_state_finishes_nothing(tmp_path):
    fake = OpenAI()
    service = make_service(tmp_path, fake)
    start = service.start_chatgpt_sign_in(None)
    with pytest.raises(SignInError) as excinfo:
        run(service.complete_chatgpt_callback("code=x&state=not-ours&client_id=oaiapp_x"))
    assert excinfo.value.code == "sign_in_attempt_unknown"
    assert service.chatgpt_sign_in_status(start["attempt_id"]) == {"status": "pending"}


def test_a_refusal_after_the_state_check_is_reported_to_the_poll(tmp_path):
    fake = OpenAI()
    service = make_service(tmp_path, fake)
    start = service.start_chatgpt_sign_in(None)
    url = callback(start, code=None, error="access_denied", error_description="User declined")
    with pytest.raises(SignInError):
        run(service.complete_chatgpt_callback(url))
    status = service.chatgpt_sign_in_status(start["attempt_id"])
    assert status["status"] == "failed" and status["code"] == "sign_in_declined"


@pytest.mark.parametrize(
    "body",
    [
        {"messages": [{"role": "assistant", "content": "x", "tool_calls": ["x"]}]},
        {"messages": [{"role": "assistant", "content": "x",
                       "tool_calls": [{"id": "c", "function": "f"}]}]},
        {"messages": [{"role": "user", "content": "x"}],
         "tools": [{"type": "function", "function": "f"}]},
    ],
)
def test_a_malformed_tool_shape_is_a_value_error_not_a_500(body):
    from control_plane.providers.responses_adapter import chat_to_responses

    with pytest.raises(ValueError):
        chat_to_responses(body, "m")
