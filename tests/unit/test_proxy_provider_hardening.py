"""Security audit slice K: the /v1 proxy, providers, keys, limits, redaction.

One test (or a small group) per finding. Each asserts the observable contract
the plan names -- a status, a code, a header on the wire -- so a regression is
a failure here rather than a quiet behaviour change.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from control_plane.contracts import Modality, TargetKind
from control_plane.contracts.providers import ProviderKind
from control_plane.gateway import create_app
from control_plane.gateway.proxy import UpstreamProxy
from control_plane.gateway.settings import GatewaySettings
from control_plane.gateway.stats import StatsRegistry
from control_plane.keys.service import _limits, validate_limit_name
from control_plane.limits.service import requested_output
from control_plane.providers.errors import ProviderNotAdmittingError, UpstreamError
from control_plane.providers.service import UpstreamResponse, validate_base_url
from control_plane.redaction import REDACTED, Redactor
from control_plane.telemetry.records import RequestTrace
from tests.unit.test_gateway import (
    SECRET_KEY,
    FakeBackend,
    FakeDeployments,
    FakeProviders,
    RunningBackend,
    _multipart,
    _real_providers,
    build_deps,
    make_deployment,
    make_provider,
)
from tests.unit.test_gateway_proxy_metering import _mint, _state
from tests.unit.test_gateway_runtime import _ChunkStream, _RecordingSink, _route_target
from tests.unit.test_providers import Upstream as ProviderUpstream
from tests.unit.test_providers import REAL_KEY, add_openrouter, make_service

# ---------------------------------------------------------------------------
# K3 -- provider base_url
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind, url",
    [
        (ProviderKind.OPENAI, "https://api.openai.com/v1"),
        (ProviderKind.OPENROUTER, "https://openrouter.ai/api/v1"),
        (ProviderKind.CUSTOM, "http://10.0.0.5:8000/v1"),
        (ProviderKind.CUSTOM, "https://inference.example/v1"),
        (ProviderKind.OLLAMA, "http://localhost:11434"),
        (ProviderKind.OLLAMA, "http://192.168.1.20:11434/v1"),
    ],
)
def test_base_url_accepted(kind, url):
    assert validate_base_url(kind, url) == url


@pytest.mark.parametrize(
    "kind, url, why",
    [
        (ProviderKind.OPENAI, "https://attacker.example/v1", "must be on api.openai.com"),
        (ProviderKind.OPENAI, "http://api.openai.com/v1", "must use https"),
        (ProviderKind.OPENROUTER, "https://127.0.0.1/v1", "must be on openrouter.ai"),
        (ProviderKind.CUSTOM, "http://169.254.169.254/", "link-local"),
        (ProviderKind.CUSTOM, "http://[fe80::1]/v1", "link-local"),
        (ProviderKind.CUSTOM, "http://[::ffff:169.254.169.254]/", "link-local"),
        (ProviderKind.CUSTOM, "http://224.0.0.1/v1", "multicast"),
        (ProviderKind.CUSTOM, "http://0.0.0.0:8000/v1", "unspecified"),
        (ProviderKind.CUSTOM, "ftp://box.local/v1", "must use https or http"),
        (ProviderKind.CUSTOM, "box.local/v1", "must use https or http"),
    ],
)
def test_base_url_refused(kind, url, why):
    with pytest.raises(ValueError, match=why):
        validate_base_url(kind, url)


def test_provider_base_url_patches_over_http(tmp_path):
    providers = _real_providers(tmp_path)
    with TestClient(create_app(build_deps(providers=providers))) as client:
        assert client.post(
            "/api/providers",
            json={"provider_id": "openai", "kind": "openai", "api_key": SECRET_KEY},
        ).status_code in (200, 201)
        assert client.post(
            "/api/providers",
            json={"provider_id": "box", "kind": "custom", "base_url": "http://box.local/v1"},
        ).status_code in (200, 201)
        assert client.post(
            "/api/providers",
            json={"provider_id": "lan", "kind": "ollama"},
        ).status_code in (200, 201)
        metadata_add = client.post(
            "/api/providers",
            json={"provider_id": "meta", "kind": "custom", "base_url": "http://169.254.169.254/"},
        )

        attacker = client.patch(
            "/api/providers/openai", json={"base_url": "https://attacker.example/v1"}
        )
        metadata = client.patch("/api/providers/box", json={"base_url": "http://169.254.169.254/"})
        ollama = client.patch("/api/providers/lan", json={"base_url": "http://localhost:11434"})

    assert attacker.status_code == 400
    assert "api.openai.com" in attacker.json()["error"]["message"]
    assert providers.get("openai").base_url == "https://api.openai.com/v1"
    assert metadata.status_code == 400
    assert providers.get("box").base_url == "http://box.local/v1"
    assert metadata_add.status_code == 400
    assert ollama.status_code == 200, ollama.text
    assert providers.get("lan").base_url == "http://localhost:11434"


def test_moving_base_url_lets_go_of_a_pasted_key(tmp_path):
    """The key was pasted for the old host and is never carried to the new."""
    providers = _real_providers(tmp_path)
    providers.add(
        {"provider_id": "box", "kind": ProviderKind.CUSTOM,
         "base_url": "http://box.local/v1", "api_key": SECRET_KEY}
    )
    held = providers.get("box").api_key_ref
    assert held and providers.resolve_key("box") == SECRET_KEY

    providers.update("box", {"base_url": "http://other.local/v1"})

    assert providers.get("box").api_key_ref == ""
    assert providers.secrets.get(held) is None


def test_moving_base_url_with_a_new_key_keeps_the_new_key(tmp_path):
    providers = _real_providers(tmp_path)
    providers.add(
        {"provider_id": "box", "kind": ProviderKind.CUSTOM,
         "base_url": "http://box.local/v1", "api_key": SECRET_KEY}
    )
    providers.update(
        "box", {"base_url": "http://other.local/v1", "api_key": "sk-another-key-0123456789"}
    )
    assert providers.resolve_key("box") == "sk-another-key-0123456789"


# ---------------------------------------------------------------------------
# K1 -- multipart parse differential
# ---------------------------------------------------------------------------


def _post_multipart(body: bytes, content_type: str):
    with TestClient(create_app(build_deps())) as client:
        return client.post(
            "/v1/audio/transcriptions", content=body, headers={"content-type": content_type}
        )


def test_two_boundary_parameters_are_refused():
    body, content_type = _multipart(('name="model"', b"whisper"))
    reply = _post_multipart(body, content_type + "; boundary=other")
    assert reply.status_code == 400
    error = reply.json()["error"]
    assert error["code"] == "invalid_request"
    assert error["message"] == "ambiguous multipart boundary"


@pytest.mark.parametrize(
    "second", ["name=model", 'NAME="model"', 'name="MODEL"', "name = model"]
)
def test_a_second_model_part_in_any_spelling_is_refused(second):
    body, content_type = _multipart(
        ('name="model"', b"whisper-cheap"),
        (second, b"gpt-4o-transcribe"),
        ('name="file"; filename="a.mp3"', b"\x00\x01"),
    )
    reply = _post_multipart(body, content_type)
    assert reply.status_code == 400
    assert reply.json()["error"]["code"] == "invalid_request"


def test_an_unquoted_model_part_alone_is_read():
    from control_plane.gateway.openai_api import _multipart_field

    body, content_type = _multipart(("name=model", b"whisper"))
    assert _multipart_field(body, content_type, "model") == "whisper"


# ---------------------------------------------------------------------------
# K5 -- remote audio goes through the provider gate; K10 on that path
# ---------------------------------------------------------------------------


class _ManagedProviders(FakeProviders):
    """FakeProviders with an ``open_upstream``: the managed path."""

    def __init__(self, providers):
        super().__init__(providers)
        self.calls: list[dict] = []

    @asynccontextmanager
    async def open_upstream(
        self, provider_id, upstream_id, body, stream=False, *, endpoint="chat/completions",
        content=None, content_type=None, request_id=None,
    ):
        self.calls.append(
            {"provider_id": provider_id, "upstream_id": upstream_id, "body": body,
             "endpoint": endpoint, "content": content, "content_type": content_type,
             "request_id": request_id}
        )
        if content is not None:
            payload = {"text": "hello"}
        else:
            payload = {
                "id": "c", "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }

        async def body_iter():
            yield json.dumps(payload).encode()

        yield UpstreamResponse(
            status_code=200, headers={"content-type": "application/json"},
            media_type="application/json", body=body_iter(),
        )


def _audio_app():
    providers = _ManagedProviders(
        [
            make_provider("cloud", served_name="cloud-chat", upstream_id="up/chat"),
            make_provider("audio", served_name="whisper", upstream_id="whisper-1",
                          modality=Modality.TRANSCRIPTION),
        ]
    )
    return create_app(build_deps(providers=providers)), providers


def test_remote_transcription_goes_through_open_upstream_and_counts(tmp_path):
    app, providers = _audio_app()
    key_id, key = _mint(app, requests_cycle=10)
    with TestClient(app) as client:
        reply = client.post(
            "/v1/audio/transcriptions",
            data={"model": "whisper"},
            files={"file": ("clip.mp3", b"ID3audio", "audio/mpeg")},
            headers={"Authorization": f"Bearer {key}", "X-Request-Id": "forged"},
        )
    assert reply.status_code == 200, reply.text
    call = providers.calls[-1]
    assert call["endpoint"] == "/audio/transcriptions"
    assert call["body"] is None
    assert b'name="model"\r\n\r\nwhisper-1\r\n' in call["content"]
    assert call["content_type"].startswith("multipart/form-data; boundary=")
    assert call["request_id"] == reply.headers["x-request-id"]
    assert call["request_id"].startswith("r-")
    assert providers.key_requests == [], "the managed path never resolves a raw key"
    assert _state(app, key_id, "requests_cycle").used == 1


def test_a_key_over_its_usd_budget_gets_429_on_remote_transcription():
    app, _ = _audio_app()
    _, key = _mint(app, budget_usd_cycle=1e-9)
    auth = {"Authorization": f"Bearer {key}"}
    with TestClient(app) as client:
        spent = client.post(
            "/v1/chat/completions",
            json={"model": "cloud-chat", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth,
        )
        assert spent.status_code == 200, spent.text
        reply = client.post(
            "/v1/audio/transcriptions",
            data={"model": "whisper"},
            files={"file": ("clip.mp3", b"ID3audio", "audio/mpeg")},
            headers=auth,
        )
    assert reply.status_code == 429
    assert reply.json()["error"]["limit_source"] == "key:budget_usd_cycle"


def _audio_upstream():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        seen.append(request)
        if request.method == "GET":
            return ProviderUpstream().handler(request)
        return httpx.Response(
            200,
            headers={
                "content-type": "application/json",
                "openai-organization": "org-derate",
                "openai-project": "proj-derate",
                "x-ratelimit-remaining-requests": "4999",
                "x-request-id": "req_upstream",
                "set-cookie": "a=b",
                "cache-control": "no-cache",
            },
            json={"text": "hello"},
        )

    return seen, handler


def test_provider_open_upstream_forwards_an_upload_byte_for_byte(tmp_path):
    seen, handler = _audio_upstream()
    service = make_service(tmp_path, handler=handler)
    add_openrouter(service)
    upload = b"--b\r\ncontent-disposition: form-data; name=\"model\"\r\n\r\nm\r\n--b--\r\n"

    async def run():
        async with service.open_upstream(
            "openrouter", "openai/gpt-4o-mini", None, False,
            endpoint="/audio/transcriptions", content=upload,
            content_type="multipart/form-data; boundary=b", request_id="r-abc",
        ) as up:
            body = b"".join([c async for c in up.body])
            return up.headers, body

    headers, body = asyncio.run(run())
    sent = seen[-1]
    assert sent.content == upload
    assert sent.headers["content-type"] == "multipart/form-data; boundary=b"
    assert sent.headers["x-request-id"] == "r-abc"
    assert json.loads(body) == {"text": "hello"}
    # K6: an allowlist, and the request id is derate's.
    assert {k.lower() for k in headers} == {"content-type", "cache-control", "x-request-id"}
    assert headers["x-request-id"] == "r-abc"
    # Counted on the provider's day even with no tokens to read.
    assert service._entries["openrouter"].runtime.day(time.time()).requests == 1


def test_provider_open_upstream_refuses_an_upload_over_the_daily_budget(tmp_path):
    seen, handler = _audio_upstream()
    service = make_service(tmp_path, handler=handler)
    add_openrouter(service)
    service.update("openrouter", {"daily_budget_usd": 0.01})
    service._entries["openrouter"].runtime.record_usage(time.time(), 10**6, 10**6, 1.0, 1.0)
    posts_before = sum(1 for r in seen if r.method == "POST")

    async def run():
        async with service.open_upstream(
            "openrouter", "openai/gpt-4o-mini", None, False,
            endpoint="/audio/transcriptions", content=b"x",
            content_type="multipart/form-data; boundary=b",
        ):
            pass

    with pytest.raises(ProviderNotAdmittingError):
        asyncio.run(run())
    assert sum(1 for r in seen if r.method == "POST") == posts_before


def test_forward_provider_hands_the_trace_id_to_the_provider():
    seen: list[dict] = []

    @asynccontextmanager
    async def open_upstream(provider_id, upstream_id, body, stream=False, **kw):
        seen.append(kw)

        async def body_iter():
            yield b"{}"

        yield UpstreamResponse(200, {"content-type": "application/json"}, "application/json",
                               body_iter())

    async def run():
        proxy = UpstreamProxy(GatewaySettings())
        trace = RequestTrace(request_id="r-trace", served_name="m", node_id="", streaming=False,
                             body_bytes=0)
        attempt = await proxy.forward_provider(
            target=_route_target("p:m"), provider_id="p", upstream_id="m",
            path="/audio/transcriptions", body=None, stats=StatsRegistry(), streaming=False,
            open_upstream=open_upstream, trace=trace, content=b"raw",
            content_type="multipart/form-data; boundary=b",
        )
        async for _ in attempt.response.body_iterator:
            pass

    asyncio.run(run())
    assert seen == [{"endpoint": "/audio/transcriptions", "request_id": "r-trace",
                     "content": b"raw", "content_type": "multipart/form-data; boundary=b"}]


def test_http_rate_limit_history_keeps_sanitized_reason_after_sibling_success(tmp_path, caplog):
    upstream = ProviderUpstream()
    service = make_service(tmp_path, upstream)
    add_openrouter(service)
    upstream.chat_responses.append(httpx.Response(
        429, headers={"retry-after": "17"},
        json={"error": {"message": f"model quota exhausted for {REAL_KEY}"}},
    ))
    app = create_app(build_deps(providers=service))
    sink = _RecordingSink()
    app.state.ctx.sink = sink
    app.state.ctx.proxy._sink = sink
    caplog.set_level("INFO", logger="control_plane.providers.runtime")
    with TestClient(app) as client:
        limited = client.post("/v1/chat/completions", json={
            "model": "openai/gpt-4o-mini", "messages": [],
        })
        assert limited.status_code == 429
        assert limited.headers["retry-after"] == "17"
        row = sink.records[-1]
        assert row.provider_id == "openrouter"
        assert row.target_id == "openrouter:openai/gpt-4o-mini"
        assert row.status == 429 and row.error_class == "RateLimitError"
        assert row.attempt_chain == [{
            "target_id": row.target_id, "status": 429, "error_class": "RateLimitError",
            "retry_reason": None, "ms": row.duration_ms,
            "error_message": f"model quota exhausted for {REDACTED}", "retry_after_s": 17,
        }]
        saved = json.dumps(row.attempt_chain)
        sibling = client.post("/v1/chat/completions", json={
            "model": "anthropic/claude-sonnet-4.5", "messages": [],
        })
        assert sibling.status_code == 200
        assert json.dumps(row.attempt_chain) == saved
        assert "error_message" not in sink.records[-1].attempt_chain[0]
        assert service.admitting("openrouter")
        assert not service.admitting("openrouter", "openai/gpt-4o-mini")
    cooldowns = [r.getMessage() for r in caplog.records if "rate limited for" in r.getMessage()]
    assert len(cooldowns) == 1
    assert "openrouter" in cooldowns[0] and "openai/gpt-4o-mini" in cooldowns[0]
    assert "17s" in cooldowns[0] and "model quota exhausted" in cooldowns[0]
    assert REAL_KEY not in limited.text + saved + caplog.text


def _translated_rate_limit(tmp_path, wire):
    # Real sign-in, provider state and converters; only the HTTP boundary is fake.
    if wire == "messages":
        from tests.unit import test_claude_sign_in as fixture
        fake = fixture.FakeAnthropic()
        service = fixture.make_service(tmp_path, fake)
        provider = asyncio.run(fixture.sign_in(service, fake))
        model = fixture.MODEL
        events = [fixture.message_start(), {
            "type": "error",
            "error": {"type": "rate_limit_error", "message": f"quota exhausted {fixture.ACCESS}"},
        }]
    else:
        from tests.unit import test_chatgpt_sign_in as fixture
        fake = fixture.OpenAI()
        service = fixture.make_service(tmp_path, fake)
        provider = asyncio.run(fixture.sign_in(service, fake))
        model = "gpt-x"
        events = [{"type": "response.created", "response": {"id": "r"}}, {
            "type": "response.failed", "response": {"id": "r", "error": {
                "code": "subscription_sharing_usage_limit_exceeded",
                "message": f"quota exhausted {fixture.ACCESS}",
            }},
        }]
    source = _ChunkStream([fixture.sse(event) for event in events])
    fake.responses.append(httpx.Response(200, stream=source,
        headers={"content-type": "text/event-stream"}))
    return service, provider, model, fixture.ACCESS, source


@pytest.mark.parametrize("wire", ["messages", "responses"])
@pytest.mark.parametrize("stream", [False, True])
def test_translated_rate_limit_history_records_logical_status(tmp_path, caplog, wire, stream):
    service, provider, model, secret, source = _translated_rate_limit(tmp_path, wire)
    app = create_app(build_deps(providers=service))
    sink = _RecordingSink()
    app.state.ctx.sink = sink
    app.state.ctx.proxy._sink = sink
    caplog.set_level("INFO", logger="control_plane.providers.runtime")
    with TestClient(app) as client:
        reply = client.post("/v1/chat/completions", json={
            "model": service.get(provider.provider_id).models[0].served_name,
            "messages": [{"role": "user", "content": "hi"}], "stream": stream,
        })
        assert reply.status_code == (200 if stream else 429)
        assert "quota exhausted" in reply.text
        row = sink.records[-1]
        assert row.status == 429 and row.error_class == "RateLimitError"
        assert row.provider_id == provider.provider_id
        assert row.target_id == f"{provider.provider_id}:{model}"
        entry = row.attempt_chain[0]
        assert entry["status"] == 429 and entry["error_class"] == "RateLimitError"
        assert entry["error_message"] == f"quota exhausted {REDACTED}"
        assert entry["retry_after_s"] == 1
        assert entry["ms"] == row.duration_ms and entry["retry_reason"] is None
        assert secret not in reply.text + json.dumps(entry) + caplog.text
        assert service.admitting(provider.provider_id)
        assert not service.admitting(provider.provider_id, model)
        assert source.closed
        assert service._entry(provider.provider_id).runtime.outstanding.get(model, 0) == 0
    assert sum("rate limited for" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.parametrize("wire", ["messages", "responses"])
def test_closing_translated_error_frame_records_rate_limit_and_partial_usage(tmp_path, caplog, wire):
    service, provider, model, secret, source = _translated_rate_limit(tmp_path, wire)
    caplog.set_level("INFO", logger="control_plane.providers.runtime")

    async def scenario():
        sink = _RecordingSink()
        stats = StatsRegistry()
        proxy = UpstreamProxy(GatewaySettings(), sink=sink)
        target = _route_target(f"{provider.provider_id}:{model}", kind=TargetKind.REMOTE)
        selection = SimpleNamespace(target=target, provider=provider, deployment=None, config=None)
        attempt = await proxy.forward_provider(
            target=target, provider_id=provider.provider_id, upstream_id=model,
            path="/chat/completions", body={"messages": [{"role": "user", "content": "hi"}]},
            stats=stats, streaming=True, open_upstream=service.open_upstream,
            selection=selection, trace=RequestTrace(
                request_id="close-on-error", served_name=model, prompt_tokens=23,
            ),
        )
        assert attempt.response.status_code == 200
        iterator = attempt.response.body_iterator
        async for chunk in iterator:
            if b'"error"' in chunk:
                break
        else:
            pytest.fail("translated stream never emitted its error frame")
        await iterator.aclose()
        assert len(sink.records) == 1
        row = sink.records[0]
        assert row.status == 429 and row.error_class == "RateLimitError"
        assert row.attempt_chain[0]["error_message"] == f"quota exhausted {REDACTED}"
        assert row.attempt_chain[0]["retry_after_s"] == 1
        assert row.provider_id == provider.provider_id and row.target_id == target.target_id
        assert row.prompt_tokens == 23
        assert row.tokens > 0 and row.tokens_estimated
        assert source.closed
        assert stats.outstanding(target.target_id) == 0
        assert service._entry(provider.provider_id).runtime.outstanding.get(model, 0) == 0
        assert secret not in json.dumps(row.attempt_chain) + caplog.text

    asyncio.run(scenario())
    assert sum("rate limited for" in r.getMessage() for r in caplog.records) == 1


# ---------------------------------------------------------------------------
# K2 -- /v1/audio/voices answers to the key
# ---------------------------------------------------------------------------


def _voices_app(backend_url: str):
    deps = build_deps(
        deployments=FakeDeployments(
            [make_deployment("d-1", "kokoro", backend_url=backend_url,
                             modality=Modality.SPEECH),
             make_deployment("d-2", "llama", backend_url="http://backend.invalid/v1")]
        )
    )
    return create_app(deps)


def test_voices_refuses_a_key_whose_allowlist_lacks_the_model():
    app = _voices_app("http://backend.invalid/v1")
    rows: list = []
    app.state.ctx.sink.request = rows.append
    cookie_key = _mint_allowlisted(app, ["llama"])
    with TestClient(app) as client:
        reply = client.get(
            "/v1/audio/voices", params={"model": "kokoro"},
            headers={"Authorization": f"Bearer {cookie_key}"},
        )
    assert reply.status_code == 403
    assert reply.json()["error"]["code"] == "model_not_allowed"
    assert rows and rows[-1].error_code == "model_not_allowed"
    assert rows[-1].request_id == reply.headers["x-request-id"]


def test_voices_counts_against_the_key():
    backend = FakeBackend()
    with RunningBackend(backend) as running:
        app = _voices_app(running.base_url)
        key_id, key = _mint(app, requests_cycle=5)
        with TestClient(app) as client:
            reply = client.get(
                "/v1/audio/voices", params={"model": "kokoro"},
                headers={"Authorization": f"Bearer {key}"},
            )
    assert reply.status_code == 200
    assert _state(app, key_id, "requests_cycle").used == 1


def _mint_allowlisted(app, models: list[str]) -> str:
    from tests.unit.conftest import BOOT_PASSWORD, credentials

    cookie, _ = credentials(app)
    r = TestClient(app).post(
        "/api/keys",
        json={"name": "narrow", "expiry_days": 30, "allowed_models": models,
              "current_password": BOOT_PASSWORD},
        headers={"Cookie": f"derate_session={cookie}"},
    )
    assert r.status_code == 200, r.text
    return r.json()["secret_once"]


# ---------------------------------------------------------------------------
# K8 -- non-finite limits
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan])
def test_a_non_finite_limit_is_refused(value):
    with pytest.raises(ValueError, match="finite"):
        _limits({"rpm": value}, "limits")


def test_the_form_placeholder_is_not_a_model_limit():
    with pytest.raises(ValueError, match="unknown limit"):
        validate_limit_name("model:<id>:rpm")
    validate_limit_name("model:org/m:rpm")


@pytest.mark.parametrize("value", [math.inf, math.nan, 2**31 + 1])
def test_an_unbounded_output_ceiling_is_refused(value):
    with pytest.raises(ValueError):
        requested_output({"max_tokens": value})
    assert requested_output({"max_tokens": 2**31}) == ("max_tokens", 2**31)


def test_max_tokens_1e999_is_a_400_not_a_500():
    deps = build_deps(
        deployments=FakeDeployments(
            [make_deployment("d-1", "llama", backend_url="http://backend.invalid/v1")]
        )
    )
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/v1/chat/completions",
            content=b'{"model": "llama", "max_tokens": 1e999, "messages": []}',
            headers={"content-type": "application/json"},
        )
    assert reply.status_code == 400
    assert reply.json()["error"]["code"] == "invalid_request"


def test_keys_api_refuses_an_infinite_limit():
    app = create_app(build_deps())
    from tests.unit.conftest import BOOT_PASSWORD, credentials

    cookie, _ = credentials(app)
    r = TestClient(app).post(
        "/api/keys",
        content=b'{"name": "x", "expiry_days": 30, "limits": {"rpm": 1e999}, '
        b'"current_password": "' + BOOT_PASSWORD.encode() + b'"}',
        headers={"Cookie": f"derate_session={cookie}", "content-type": "application/json"},
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# K9 -- redactor variants
# ---------------------------------------------------------------------------

_SECRET = "s3cr3t/value+with=odd&chars\"q"


def test_redactor_catches_basic_auth():
    text = "Authorization: Basic dXNlcjpwYXNzd29yZDEyMzQ1Njc4"
    assert "dXNlcjpw" not in Redactor().scrub(text)


def test_redactor_catches_url_encoded_and_json_escaped_forms():
    from urllib.parse import quote

    redactor = Redactor()
    redactor.remember(_SECRET)
    encoded = f"GET /v1?key={quote(_SECRET, safe='')} HTTP/1.1"
    escaped = json.dumps({"key": _SECRET})
    assert quote(_SECRET, safe="") not in redactor.scrub(encoded)
    assert REDACTED in redactor.scrub(encoded)
    assert json.dumps(_SECRET)[1:-1] not in redactor.scrub(escaped)
    assert REDACTED in redactor.scrub(escaped)
    assert redactor.contains_secret(escaped)


# ---------------------------------------------------------------------------
# K-1 -- a string-typed output ceiling does not slip past the cap
# ---------------------------------------------------------------------------


def test_a_string_max_completion_tokens_is_refused_not_forwarded():
    backend = FakeBackend()
    with RunningBackend(backend) as running:
        app = create_app(build_deps(deployments=FakeDeployments(
            [make_deployment("d-1", "llama", backend_url=running.base_url)]
        )))
        _key_id, key = _mint(app, max_output_tokens_per_request=512)
        with TestClient(app) as client:
            reply = client.post(
                "/v1/chat/completions",
                json={"model": "llama", "messages": [{"role": "user", "content": "go"}],
                      "max_completion_tokens": "100000"},
                headers={"Authorization": f"Bearer {key}"},
            )
    assert reply.status_code == 400, (reply.status_code, backend.requests)
    assert reply.json()["error"]["code"] == "invalid_request"
    assert backend.requests == []


def test_an_injected_cap_replaces_every_other_output_field():
    backend = FakeBackend()
    with RunningBackend(backend) as running:
        app = create_app(build_deps(deployments=FakeDeployments(
            [make_deployment("d-1", "llama", backend_url=running.base_url)]
        )))
        _key_id, key = _mint(app, max_output_tokens_per_request=512)
        with TestClient(app) as client:
            reply = client.post(
                "/v1/chat/completions",
                json={"model": "llama", "messages": [{"role": "user", "content": "go"}]},
                headers={"Authorization": f"Bearer {key}"},
            )
    assert reply.status_code == 200, reply.text
    sent = backend.requests[-1]
    assert sent["max_tokens"] == 512
    assert "max_completion_tokens" not in sent and "max_output_tokens" not in sent


@pytest.mark.parametrize("value", ["100000", 1.5, True])
def test_requested_output_refuses_a_non_integer(value):
    with pytest.raises(ValueError, match="must be an integer"):
        requested_output({"max_completion_tokens": value})


# ---------------------------------------------------------------------------
# K-3 -- the allowlist answers before the catalogue does
# ---------------------------------------------------------------------------


def test_a_restricted_key_cannot_probe_a_model_outside_its_allowlist():
    app = _voices_app("http://backend.invalid/v1")
    key = _mint_allowlisted(app, ["llama"])
    with TestClient(app) as client:
        known = client.post(
            "/v1/chat/completions",
            json={"model": "kokoro", "messages": [{"role": "user", "content": "x"}]},
            headers={"Authorization": f"Bearer {key}"},
        )
        unknown = client.post(
            "/v1/chat/completions",
            json={"model": "no-such-model", "messages": [{"role": "user", "content": "x"}]},
            headers={"Authorization": f"Bearer {key}"},
        )
        voices_unknown = client.get(
            "/v1/audio/voices", params={"model": "no-such-model"},
            headers={"Authorization": f"Bearer {key}"},
        )
    for reply in (known, unknown, voices_unknown):
        assert reply.status_code == 403, reply.text
        assert reply.json()["error"]["code"] == "model_not_allowed"


# ---------------------------------------------------------------------------
# K1r -- a part with two content-disposition headers
# ---------------------------------------------------------------------------


def test_a_part_with_two_dispositions_is_refused():
    boundary = "b0undary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="model"\r\n\r\n'
        "whisper-cheap\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="a.mp3"\r\n'
        'Content-Disposition: form-data; name="model"\r\n\r\n'
        "gpt-4o-transcribe\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    reply = _post_multipart(body, f"multipart/form-data; boundary={boundary}")
    assert reply.status_code == 400
    assert reply.json()["error"]["code"] == "invalid_request"


# ---------------------------------------------------------------------------
# K5r -- remote audio priced per minute when the model carries a rate
# ---------------------------------------------------------------------------


class _TimedAudioProviders(_ManagedProviders):
    #: What the upstream answers, and how many times it was asked.
    answer: bytes = json.dumps({"text": "hello", "duration": 120}).encode()
    opened = 0

    @asynccontextmanager
    async def open_upstream(self, provider_id, upstream_id, body, stream=False, **kw):
        self.opened += 1
        answer = self.answer

        async def body_iter():
            yield answer

        yield UpstreamResponse(
            status_code=200, headers={"content-type": "application/json"},
            media_type="application/json", body=body_iter(),
        )


def _timed_audio_reply(rate, *, answer: dict | None = None, fields: dict | None = None):
    provider = make_provider("audio", served_name="whisper", upstream_id="whisper-1",
                             modality=Modality.TRANSCRIPTION)
    provider.models[0].price_per_minute_usd = rate
    providers = _TimedAudioProviders([provider])
    if answer is not None:
        providers.answer = json.dumps(answer).encode()
    app = create_app(build_deps(providers=providers))
    rows: list = []
    key_id, key = _mint(app, budget_usd_cycle=1.0)
    with TestClient(app) as client:
        app.state.ctx.sink.request = rows.append
        reply = client.post(
            "/v1/audio/transcriptions",
            data={"model": "whisper", **(fields or {})},
            files={"file": ("clip.mp3", b"ID3audio", "audio/mpeg")},
            headers={"Authorization": f"Bearer {key}"},
        )
    return reply, rows, _state(app, key_id, "budget_usd_cycle").used, providers


def _timed_audio(rate, **kw):
    reply, rows, used, _ = _timed_audio_reply(rate, **kw)
    assert reply.status_code == 200, reply.text
    return rows[-1], used


def test_remote_audio_with_a_per_minute_rate_is_metered_on_its_duration():
    row, used = _timed_audio(0.006)
    assert row.cost_usd == pytest.approx(0.012)
    assert row.cost_basis == "metered"
    assert used == pytest.approx(0.012)


def test_plain_json_is_metered_on_its_usage_seconds():
    """OpenAI's default ``json`` answer has no ``duration``, only
    ``usage.seconds``: reading the first alone left every default-format
    request on a per-minute rate free."""
    row, used = _timed_audio(0.006, answer={"text": "hi", "usage": {"type": "duration", "seconds": 120}})
    assert row.cost_usd == pytest.approx(0.012) and used == pytest.approx(0.012)


@pytest.mark.parametrize("fmt", ["text", "srt", "vtt"])
def test_a_per_minute_model_refuses_a_format_that_carries_no_duration(fmt):
    reply, _, used, providers = _timed_audio_reply(0.006, fields={"response_format": fmt})
    assert (reply.status_code, reply.json()["error"]["code"]) == (400, "invalid_request")
    assert providers.opened == 0 and used == 0


def test_a_model_without_a_rate_answers_any_format():
    reply, _, _, providers = _timed_audio_reply(None, fields={"response_format": "text"})
    assert reply.status_code == 200 and providers.opened == 1


def test_a_second_response_format_is_refused():
    """The gateway prices the format it reads; two would let the upstream
    answer in the other."""
    provider = make_provider("audio", served_name="whisper", upstream_id="whisper-1",
                             modality=Modality.TRANSCRIPTION)
    app = create_app(build_deps(providers=_TimedAudioProviders([provider])))
    _, key = _mint(app, budget_usd_cycle=1.0)
    with TestClient(app) as client:
        reply = client.post(
            "/v1/audio/transcriptions",
            data={"model": "whisper", "response_format": ["json", "text"]},
            files={"file": ("clip.mp3", b"ID3audio", "audio/mpeg")},
            headers={"Authorization": f"Bearer {key}"},
        )
    assert (reply.status_code, reply.json()["error"]["code"]) == (400, "invalid_request")


def test_remote_audio_without_a_rate_stays_unpriced():
    row, used = _timed_audio(None)
    assert row.cost_basis != "metered"  # today's behaviour: tokens only, none here
    assert not row.cost_usd
    assert used == 0


def test_the_provider_day_reads_the_transcription_duration():
    from control_plane.providers.usage import UsageSniffer

    sniffer = UsageSniffer(stream=False)
    sniffer.feed(b'{"text": "x", "duration": 120}')
    assert sniffer.audio_duration_s() == 120.0
    sniffer = UsageSniffer(stream=False)
    sniffer.feed(b'{"text": "x", "usage": {"type": "duration", "seconds": 90}}')
    assert sniffer.audio_duration_s() == 90.0


def test_the_tail_parser_is_linear_and_never_raises():
    """A hostile upstream's body: one ``"usage",`` per eight bytes was a copy
    per candidate (quadratic), and a deep array escaped as RecursionError past
    every ``except ValueError`` and leaked the upstream connection."""
    from control_plane.providers.usage import audio_duration_of, object_after_key

    started = time.monotonic()
    assert object_after_key(b'"usage",' * 1_048_576, "usage") is None
    assert object_after_key(b'"usage":' + b"[" * 100_000, "usage") is None
    assert audio_duration_of(b'{"a":' + b"[" * 100_000) is None
    assert time.monotonic() - started < 1.0
    assert object_after_key(b'x"usage": {"a": 1}, "usage":{"b":2}', "usage") == {"b": 2}


# ---------------------------------------------------------------------------
# Round 3: what a provider answer is charged, and per-minute fallthrough
# ---------------------------------------------------------------------------


class _AnsweringProviders(_ManagedProviders):
    """Every provider answers ``payload``; records who was opened."""

    payload: dict = {}
    opened: list

    @asynccontextmanager
    async def open_upstream(self, provider_id, upstream_id, body, stream=False, **kw):
        self.opened.append(provider_id)
        data = json.dumps(self.payload).encode()

        async def body_iter():
            yield data

        yield UpstreamResponse(
            status_code=200, headers={"content-type": "application/json"},
            media_type="application/json", body=body_iter(),
        )


def test_a_per_minute_model_steps_aside_for_a_target_that_can_answer_the_format():
    """A per-minute model asked for text, which carries no duration, used to
    end the request with a 400 -- even when the name had another target that
    could serve it."""
    # First under every policy: higher priority, cheaper, and first by id --
    # otherwise the router may simply try the unpriced one first, and the
    # test passes on code that refuses instead of stepping aside.
    paid = make_provider("a-paid", served_name="whisper", upstream_id="whisper-1",
                         modality=Modality.TRANSCRIPTION, priority=1, output_cost=0.01)
    paid.models[0].price_per_minute_usd = 0.006
    free = make_provider("b-free", served_name="whisper", upstream_id="whisper-local",
                         modality=Modality.TRANSCRIPTION, priority=50)
    providers = _AnsweringProviders([paid, free])
    providers.opened, providers.payload = [], {"text": "hello"}
    app = create_app(build_deps(providers=providers))
    _, key = _mint(app)
    with TestClient(app) as client:
        reply = client.post("/v1/audio/transcriptions", data={"model": "whisper", "response_format": "text"},
                            files={"file": ("clip.mp3", b"ID3audio", "audio/mpeg")},
                            headers={"Authorization": f"Bearer {key}"})
        # The router's own first pick, so this cannot pass by never asking it.
        assert app.state.ctx.router.select("whisper").target.target_id.startswith("a-paid")
    assert reply.status_code == 200, reply.text
    assert providers.opened == ["b-free"]


def test_a_streamed_upload_is_declined_by_a_per_minute_model():
    """An SSE transcript carries no top-level duration either."""
    reply, _, used, providers = _timed_audio_reply(0.006, fields={"stream": "true"})
    assert (reply.status_code, reply.json()["error"]["code"]) == (400, "invalid_request")
    assert providers.opened == 0 and used == 0


def test_a_metered_provider_cost_is_what_the_key_is_charged():
    """OpenRouter says what it charged (``usage.cost``); the key was charged
    tokens x list price instead, which misses a request's paid extras."""
    providers = _AnsweringProviders([make_provider("openrouter", served_name="cloud-chat", upstream_id="up/chat")])
    providers.opened = []
    providers.payload = {
        "id": "c", "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1, "cost": 0.04},
    }
    app = create_app(build_deps(providers=providers))
    key_id, key = _mint(app, budget_usd_cycle=1.0)
    rows: list = []
    with TestClient(app) as client:
        app.state.ctx.sink.request = rows.append
        reply = client.post("/v1/chat/completions", json={"model": "cloud-chat", "messages": [{"role": "user", "content": "hi"}]},
                            headers={"Authorization": f"Bearer {key}"})
    assert reply.status_code == 200, reply.text
    assert (rows[-1].cost_usd, rows[-1].cost_basis) == (pytest.approx(0.04), "metered")
    assert _state(app, key_id, "budget_usd_cycle").used == pytest.approx(0.04)


class _RefusingProviders(_ManagedProviders):
    @asynccontextmanager
    async def open_upstream(self, provider_id, upstream_id, body, stream=False, **kw):
        raise UpstreamError(provider_id, 400, "temperature must be a number")
        yield  # pragma: no cover


def test_a_provider_refusal_counts_against_the_key():
    """A provider's 4xx was settled as nothing, so rpm and the request cycle
    never saw it, and malformed requests reached the provider at any rate."""
    app = create_app(build_deps(providers=_RefusingProviders([make_provider("openrouter", served_name="cloud-chat", upstream_id="up/chat")])))
    key_id, key = _mint(app, requests_cycle=100)
    with TestClient(app) as client:
        reply = client.post("/v1/chat/completions", json={"model": "cloud-chat", "temperature": "hot", "messages": [{"role": "user", "content": "x"}]},
                            headers={"Authorization": f"Bearer {key}"})
        # Inside the block: the rpm window is in memory and leaving the
        # client's lifespan closes the stores.
        assert reply.status_code == 400
        assert _state(app, key_id, "rpm").used == 1
        assert _state(app, key_id, "requests_cycle").used == 1


def test_a_non_finite_usage_count_does_not_stop_the_settle():
    """``Infinity`` in a large body's usage tail raised OverflowError out of
    the accounting, so the attempt never settled: KV commitment held, the
    upstream connection never closed."""
    from control_plane.gateway.proxy import _BodyAccounting

    accounting = _BodyAccounting(0.0)
    accounting.note_chunk(b'{"choices":[{"message":{"content":"' + b"x" * (_BodyAccounting.LIMIT + 10), 1.0)
    accounting.note_chunk(b'"}}],"usage":{"prompt_tokens":Infinity,"completion_tokens":NaN,"cost":Infinity}}', 2.0)
    usage = accounting.finish(3.0)
    assert usage.estimated is True and usage.tokens > 0


def test_a_provider_body_nobody_started_is_reaped_and_gives_back_its_slot():
    """Provider attempts were never tracked by the janitor: a client gone
    between the response line and the first chunk left the provider's
    context open (its connection, its ledger) and the key's slot held."""
    exits: list[int] = []

    class Exiting(_ManagedProviders):
        @asynccontextmanager
        async def open_upstream(self, *a, **k):
            try:
                async with super().open_upstream(*a, **k) as up:
                    yield up
            finally:
                exits.append(1)

    app = create_app(build_deps(providers=Exiting([make_provider("openrouter", served_name="cloud-chat", upstream_id="up/chat")])))
    key_id, key = _mint(app)
    body = json.dumps({"model": "cloud-chat", "messages": [{"role": "user", "content": "hi"}]}).encode()
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "http",
        "method": "POST", "path": "/v1/chat/completions", "raw_path": b"/v1/chat/completions",
        "root_path": "", "query_string": b"", "server": ("testserver", 80), "client": ("127.0.0.1", 5000),
        "headers": [(b"host", b"testserver"), (b"content-type", b"application/json"),
                    (b"authorization", f"Bearer {key}".encode())],
    }
    proxy = app.state.ctx.proxy

    async def run():
        sent = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive():
            if sent:
                return sent.pop()
            await asyncio.sleep(3600)

        async def send(message):
            if message["type"] == "http.response.start":
                raise OSError("the client went away")

        with pytest.raises(OSError):
            await app(scope, receive, send)
        assert [e.started for e in proxy._open.values()] == [False]
        assert (exits, _state(app, key_id, "max_concurrent").used) == ([], 1)
        for entry in proxy._open.values():
            entry.opened_at -= 60.0
        await proxy._reap()
        assert (exits, _state(app, key_id, "max_concurrent").used) == ([1], 0)

    asyncio.run(run())
