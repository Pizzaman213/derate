"""Agent I: remote providers.

The test that matters most is :func:`test_no_key_material_in_any_output`. A
leaked key in a screenshot is unrecoverable, and this is a tool people
screenshot. Every response shape this package can emit is serialized there and
asserted to be free of key material.

Async tests run through :func:`run` rather than a pytest plugin, so the suite
needs nothing beyond pytest and httpx.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import time
from collections.abc import AsyncIterator, Callable

import httpx
import pytest

from control_plane.contracts.providers import Provider, ProviderKind, ProviderModel
from control_plane.contracts.routing import (
    RouteTarget,
    RoutingConfig,
    RoutingPolicy,
    TargetKind,
)
from control_plane.providers import (
    AdapterUnsupportedError,
    MissingKeyError,
    ProviderNotAdmittingError,
    ProviderService,
    SecretStore,
    UnknownProviderError,
    UpstreamError,
    build_stub_service,
    looks_like_secret,
)
from control_plane.providers.discovery import parse_models
from control_plane.providers.kinds import spec_for
from control_plane.providers.stub import sse_chunks

# A key value distinctive enough that finding it in any output is unambiguous.
REAL_KEY = "sk-or-v1-supersecretkeymaterial0123456789abcdef"
KEY_REF = "OPENROUTER_API_KEY"


def run(coro):
    return asyncio.run(coro)


class Clock:
    """A hand-cranked clock, so backoff windows are tested without sleeping."""

    def __init__(self, t: float = 1_700_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


OPENROUTER_MODELS = {
    "data": [
        {
            "id": "anthropic/claude-sonnet-4.5",
            "context_length": 200000,
            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
            "supported_parameters": ["tools", "temperature"],
        },
        {
            "id": "openai/gpt-4o-mini",
            "context_length": 128000,
            "pricing": {"prompt": "0.00000015", "completion": "0.0000006"},
            "supported_parameters": ["tools"],
        },
        {
            "id": "meta-llama/llama-3.3-70b-instruct",
            "context_length": 131072,
            "pricing": {"prompt": "0.00000012", "completion": "0.0000003"},
            "supported_parameters": [],
        },
    ]
}


class Upstream:
    """A scriptable fake provider endpoint behind httpx.MockTransport."""

    def __init__(self, models: dict | None = None) -> None:
        self.models = models if models is not None else OPENROUTER_MODELS
        self.model_responses: list[httpx.Response] = []
        self.chat_responses: list[httpx.Response] = []
        self.requests: list[httpx.Request] = []
        self.auth_headers: list[str | None] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.auth_headers.append(request.headers.get("authorization"))
        path = request.url.path
        if request.method == "GET" and path.endswith("/models"):
            if self.model_responses:
                return self.model_responses.pop(0)
            return httpx.Response(200, json=self.models)
        if request.method == "POST":
            if self.chat_responses:
                return self.chat_responses.pop(0)
            body = json.loads(request.content or b"{}")
            return httpx.Response(
                200,
                json={
                    "id": "x",
                    "model": body.get("model"),
                    "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 20},
                },
            )
        return httpx.Response(404, json={"error": {"message": "nope"}})


def make_service(
    tmp_path,
    upstream: Upstream | None = None,
    *,
    env: dict | None = None,
    now: Callable[[], float] | None = None,
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
) -> ProviderService:
    upstream = upstream or Upstream()
    secrets = SecretStore(tmp_path / "secrets.json", env=env if env is not None else {KEY_REF: REAL_KEY})
    return ProviderService(
        data_path=tmp_path,
        secrets=secrets,
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler or upstream.handler)
        ),
        now=now or time.time,
    )


def add_openrouter(service: ProviderService, **overrides) -> Provider:
    spec = {
        "provider_id": "openrouter",
        "kind": ProviderKind.OPENROUTER,
        "api_key_ref": KEY_REF,
        "priority": 10,
    }
    spec.update(overrides)
    return service.add(spec)


# ---------------------------------------------------------------------------
# 1. Registry and discovery
# ---------------------------------------------------------------------------


def test_adding_openrouter_pulls_its_model_list(tmp_path):
    upstream = Upstream()
    service = make_service(tmp_path, upstream)
    provider = add_openrouter(service)

    assert provider.enabled is True
    assert provider.healthy is True
    assert len(provider.models) == 3

    # The names clients will use, unchanged from upstream: people already know
    # anthropic/claude-sonnet-4.5 and renaming it helps nobody.
    served = [model.served_name for _, model in service.models()]
    assert "anthropic/claude-sonnet-4.5" in served
    assert served == sorted(served)

    sonnet = service.find_model("openrouter", "anthropic/claude-sonnet-4.5")
    assert sonnet.context_length == 200000
    assert sonnet.supports_tools is True
    assert sonnet.input_cost_per_mtok == pytest.approx(3.0)
    assert sonnet.output_cost_per_mtok == pytest.approx(15.0)


def test_kind_defaults_mean_a_name_and_a_key_reference_is_enough(tmp_path):
    service = make_service(tmp_path)
    provider = service.add({"kind": ProviderKind.OPENROUTER, "api_key_ref": KEY_REF})
    assert provider.base_url == "https://openrouter.ai/api/v1"
    assert provider.display_name == "OpenRouter"
    assert provider.provider_id == "openrouter"


def test_unpublished_pricing_stays_none_rather_than_guessed(tmp_path):
    upstream = Upstream(models={"data": [{"id": "llama-3.3-70b", "context_window": 131072}]})
    service = make_service(tmp_path, upstream)
    service.add(
        {"provider_id": "groq", "kind": ProviderKind.GROQ, "api_key_ref": KEY_REF}
    )
    model = service.find_model("groq", "llama-3.3-70b")
    assert model.context_length == 131072
    assert model.input_cost_per_mtok is None
    assert model.output_cost_per_mtok is None
    # COST_AWARE must skip a target it cannot price, not treat it as free.
    target = service.route_targets()[0]
    assert target.cost_per_mtok is None


def test_context_length_unknown_is_zero_not_invented(tmp_path):
    upstream = Upstream(models={"data": [{"id": "mystery-model"}]})
    service = make_service(tmp_path, upstream)
    service.add({"provider_id": "custom", "kind": ProviderKind.CUSTOM,
                 "base_url": "http://box.local/v1"})
    assert service.find_model("custom", "mystery-model").context_length == 0


def test_together_pricing_is_read_as_dollars_per_million(tmp_path):
    models = parse_models(
        {"data": [{"id": "m", "context_length": 8192, "pricing": {"input": 0.88, "output": 0.88}}]},
        spec_for(ProviderKind.TOGETHER),
    )
    assert models[0].input_cost_per_mtok == pytest.approx(0.88)


def test_ollama_needs_no_key(tmp_path):
    upstream = Upstream(models={"data": [{"id": "qwen3:30b"}]})
    service = make_service(tmp_path, upstream, env={})
    provider = service.add(
        {"provider_id": "lan-ollama", "kind": ProviderKind.OLLAMA,
         "base_url": "http://box.local:11434/v1"}
    )
    assert provider.enabled is True
    assert provider.healthy is True
    assert service.resolve_key("lan-ollama") == ""


def test_disabled_provider_offers_no_models_or_targets(tmp_path):
    service = make_service(tmp_path)
    add_openrouter(service)
    service.update("openrouter", {"enabled": False})
    assert service.models() == []
    assert service.route_targets() == []


def test_unknown_provider_raises(tmp_path):
    service = make_service(tmp_path)
    with pytest.raises(UnknownProviderError):
        service.health("nope")


# ---------------------------------------------------------------------------
# 2. Secrets. The part to get right.
# ---------------------------------------------------------------------------


def test_no_key_material_in_any_output(tmp_path, caplog):
    """Serialize a fully configured provider every way we can, find no key.

    This runs against every response shape the package emits: the port's own
    list, the public API dicts, the model list, route targets, health, the
    persisted record on disk, an upstream error that echoes the key back, and
    the log stream at DEBUG.
    """
    upstream = Upstream()
    # An upstream that helpfully quotes the credential back at us. Providers
    # really do this, and this is the path that would put a live key into a
    # client-visible error body.
    upstream.chat_responses.append(
        httpx.Response(
            401,
            json={"error": {"message": f"Invalid API key: {REAL_KEY}", "code": "invalid_api_key"}},
        )
    )
    service = make_service(tmp_path, upstream)
    add_openrouter(service, daily_budget_usd=25.0, aliases={"openai/gpt-4o-mini": "gpt-oss-120b"})

    # The value really is resolvable, so any leak would be a real one.
    assert service.resolve_key("openrouter") == REAL_KEY

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(UpstreamError) as excinfo:
            run(collect(service.forward("openrouter", "openai/gpt-4o-mini", {"messages": []})))
        error = excinfo.value

    shapes = {
        "ProviderPort.list": [provider_as_dict(p) for p in service.list()],
        "public_list": service.public_list(),
        "public_dict": service.public_dict("openrouter"),
        "public_models": service.public_models("openrouter"),
        "models": [(pid, m.__dict__) for pid, m in service.models()],
        "route_targets": [t.__dict__ for t in service.route_targets()],
        "route_targets_by_served_name": {
            name: [t.__dict__ for t in targets]
            for name, targets in service.route_targets_by_served_name().items()
        },
        "health": list(service.health("openrouter")),
        "spend_today": service.spend_today("openrouter"),
        "kinds": __import__(
            "control_plane.providers.serialization", fromlist=["kinds_public"]
        ).kinds_public(),
        "upstream_error_str": str(error),
        "upstream_error_message": error.message,
        "upstream_error_body": error.body,
        "upstream_error_openai_shape": error.to_openai_error(),
    }
    for name, payload in shapes.items():
        serialized = json.dumps(payload, default=str)
        assert REAL_KEY not in serialized, f"key material leaked in {name}"
        assert "supersecret" not in serialized, f"key fragment leaked in {name}"

    # The persisted record.
    on_disk = (tmp_path / "providers.json").read_text()
    assert REAL_KEY not in on_disk
    assert "supersecret" not in on_disk
    # It holds the reference, which is a name, and is safe and necessary.
    assert KEY_REF in on_disk

    # Every log line, at every level.
    for record in caplog.records:
        assert REAL_KEY not in record.getMessage()
    assert REAL_KEY not in caplog.text

    # The upstream's own message survives, minus the credential.
    assert error.status_code == 401
    assert "Invalid API key" in error.message
    assert "***" in error.message


def provider_as_dict(provider: Provider) -> dict:
    return {
        "provider_id": provider.provider_id,
        "kind": provider.kind.value,
        "display_name": provider.display_name,
        "base_url": provider.base_url,
        "api_key_ref": provider.api_key_ref,
        "enabled": provider.enabled,
        "priority": provider.priority,
        "healthy": provider.healthy,
        "last_error": provider.last_error,
        "last_refreshed": provider.last_refreshed,
        "models": [m.__dict__ for m in provider.models],
    }


def test_public_dict_renders_the_key_as_stars_only(tmp_path):
    service = make_service(tmp_path)
    add_openrouter(service)
    payload = service.public_dict("openrouter")
    assert payload["api_key"] == "***"
    assert payload["api_key_ref"] == KEY_REF
    # There is no reveal control, and nothing for one to call.
    assert "api_key_value" not in payload


def test_api_key_ref_must_be_a_reference_not_a_key(tmp_path):
    service = make_service(tmp_path)
    with pytest.raises(ValueError, match="NAME of an environment variable"):
        service.add({"kind": ProviderKind.OPENROUTER, "api_key_ref": REAL_KEY})
    with pytest.raises(ValueError, match="key material"):
        service.add(
            {
                "kind": ProviderKind.CUSTOM,
                "base_url": f"https://box.local/v1?api_key={REAL_KEY}",
            }
        )
    assert service.list() == []


def test_looks_like_secret_accepts_ordinary_references():
    assert looks_like_secret(REAL_KEY)
    assert looks_like_secret("gsk_abcdefghijklmnopqrstuvwxyz012345")
    assert not looks_like_secret("OPENROUTER_API_KEY")
    assert not looks_like_secret("my-openrouter-key")
    assert not looks_like_secret("https://openrouter.ai/api/v1")


def test_missing_key_reference_disables_without_failing_startup(tmp_path):
    upstream = Upstream()
    service = make_service(tmp_path, upstream, env={KEY_REF: REAL_KEY})
    add_openrouter(service)

    # Restart with the environment variable gone. This must not raise.
    restarted = make_service(tmp_path, upstream, env={})
    provider = restarted.get("openrouter")
    assert provider.enabled is False
    assert provider.last_error is not None
    assert KEY_REF in provider.last_error
    assert REAL_KEY not in provider.last_error
    # And it still lists, rather than vanishing.
    assert [p.provider_id for p in restarted.list()] == ["openrouter"]
    assert restarted.route_targets() == []

    with pytest.raises(MissingKeyError) as excinfo:
        restarted.resolve_key("openrouter")
    assert KEY_REF in str(excinfo.value)
    assert REAL_KEY not in str(excinfo.value)


def test_provider_re_enables_when_the_reference_resolves_again(tmp_path):
    upstream = Upstream()
    add_openrouter(make_service(tmp_path, upstream, env={KEY_REF: REAL_KEY}))

    env: dict[str, str] = {}
    service = make_service(tmp_path, upstream, env=env)
    assert service.get("openrouter").enabled is False

    env[KEY_REF] = REAL_KEY
    run(service.refresh_async("openrouter"))
    provider = service.get("openrouter")
    assert provider.enabled is True
    assert provider.last_error is None
    assert len(provider.models) == 3


def test_secrets_file_is_written_at_mode_0600(tmp_path):
    store = SecretStore(tmp_path / "secrets.json", env={})
    store.put(KEY_REF, REAL_KEY)
    mode = stat.S_IMODE(os.stat(tmp_path / "secrets.json").st_mode)
    assert mode == 0o600
    assert store.get(KEY_REF) == REAL_KEY
    assert store.refs() == [KEY_REF]  # names, never values


def test_environment_beats_the_secrets_file(tmp_path):
    store = SecretStore(tmp_path / "secrets.json", env={KEY_REF: "from-env-value-长"})
    store.put(KEY_REF, "from-file")
    assert store.get(KEY_REF) == "from-env-value-长"


def test_request_bodies_are_never_logged(tmp_path, caplog):
    """Prompts are private and sometimes carry credentials of their own."""
    service = make_service(tmp_path)
    add_openrouter(service)
    prompt = "unmistakable-prompt-text-9f3a"
    with caplog.at_level(logging.DEBUG):
        run(
            collect(
                service.forward(
                    "openrouter",
                    "openai/gpt-4o-mini",
                    {"messages": [{"role": "user", "content": prompt}]},
                )
            )
        )
    assert prompt not in caplog.text


# ---------------------------------------------------------------------------
# 3. Forwarding
# ---------------------------------------------------------------------------


async def collect(iterator: AsyncIterator[bytes]) -> list[bytes]:
    return [chunk async for chunk in iterator]


def test_streaming_passes_through_without_buffering(tmp_path):
    """Each chunk must reach the caller before the next one is produced.

    The upstream refuses to produce chunk N+1 until the caller has taken chunk
    N. An implementation that buffered the stream would deadlock here, which is
    exactly the failure we want to catch.
    """
    produced: list[int] = []
    gate = None

    class GatedStream(httpx.AsyncByteStream):
        def __init__(self, frames: list[bytes]) -> None:
            self._frames = frames

        async def __aiter__(self):
            for index, frame in enumerate(self._frames):
                if index:
                    await gate.wait()
                    gate.clear()
                produced.append(index)
                yield frame

        async def aclose(self) -> None:
            return None

    upstream = Upstream()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=OPENROUTER_MODELS)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=GatedStream(sse_chunks("openai/gpt-4o-mini")),
        )

    service = make_service(tmp_path, upstream, handler=handler)
    add_openrouter(service)

    async def drive():
        nonlocal gate
        gate = asyncio.Event()
        seen = 0
        stream = service.forward(
            "openrouter", "openai/gpt-4o-mini", {"messages": []}, True
        )
        async for _chunk in stream:
            seen += 1
            # Nothing beyond what we have consumed has been produced yet.
            assert len(produced) == seen, "stream was buffered ahead of the caller"
            gate.set()
        return seen

    seen = asyncio.run(asyncio.wait_for(drive(), timeout=5))
    assert seen == len(produced) > 5


def test_forward_rewrites_the_model_and_carries_the_key(tmp_path):
    upstream = Upstream()
    service = make_service(tmp_path, upstream)
    add_openrouter(service, aliases={"openai/gpt-4o-mini": "gpt-oss-120b"})

    run(
        collect(
            service.forward(
                "openrouter", "openai/gpt-4o-mini", {"model": "gpt-oss-120b", "messages": []}
            )
        )
    )
    sent = json.loads(upstream.requests[-1].content)
    # The client asked for the served name; the upstream is told its own id.
    assert sent["model"] == "openai/gpt-4o-mini"
    assert upstream.auth_headers[-1] == f"Bearer {REAL_KEY}"


def test_upstream_error_keeps_its_status_and_message(tmp_path):
    upstream = Upstream()
    upstream.chat_responses.append(
        httpx.Response(
            400,
            json={
                "error": {
                    "message": "This model's maximum context length is 128000 tokens",
                    "type": "invalid_request_error",
                    "code": "context_length_exceeded",
                }
            },
        )
    )
    service = make_service(tmp_path, upstream)
    add_openrouter(service)

    with pytest.raises(UpstreamError) as excinfo:
        run(collect(service.forward("openrouter", "openai/gpt-4o-mini", {"messages": []})))

    error = excinfo.value
    assert error.status_code == 400  # not a generic 502
    assert "maximum context length is 128000" in error.message
    assert error.to_openai_error()["error"]["message"] == error.message
    # A 4xx that is not auth or rate limiting says nothing about health.
    assert service.health("openrouter")[0] is True


def test_open_upstream_exposes_status_and_content_type(tmp_path):
    upstream = Upstream()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=OPENROUTER_MODELS)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream", "authorization": "Bearer leak"},
            stream=_frames(sse_chunks("openai/gpt-4o-mini")),
        )

    service = make_service(tmp_path, upstream, handler=handler)
    add_openrouter(service)

    async def drive():
        async with service.open_upstream(
            "openrouter", "openai/gpt-4o-mini", {"messages": []}, True
        ) as response:
            body = [chunk async for chunk in response.body]
            return response, body

    response, body = run(drive())
    assert response.status_code == 200
    assert response.media_type.startswith("text/event-stream")
    # Hop-by-hop and credential headers are not copied onto our response.
    assert "authorization" not in {k.lower() for k in response.headers}
    assert b"[DONE]" in b"".join(body)


def _frames(frames: list[bytes]) -> httpx.AsyncByteStream:
    class _S(httpx.AsyncByteStream):
        async def __aiter__(self):
            for frame in frames:
                yield frame

        async def aclose(self) -> None:
            return None

    return _S()


def test_anthropic_is_not_forwarded_and_says_what_to_do_instead(tmp_path):
    upstream = Upstream(models={"data": [{"id": "claude-sonnet-4-5"}]})
    service = make_service(tmp_path, upstream)
    service.add(
        {"provider_id": "anthropic", "kind": ProviderKind.ANTHROPIC, "api_key_ref": KEY_REF}
    )
    with pytest.raises(AdapterUnsupportedError, match="OpenRouter"):
        run(collect(service.forward("anthropic", "claude-sonnet-4-5", {"messages": []})))


def test_disabled_provider_refuses_to_forward(tmp_path):
    service = make_service(tmp_path)
    add_openrouter(service)
    service.update("openrouter", {"enabled": False})
    with pytest.raises(ProviderNotAdmittingError, match="disabled"):
        run(collect(service.forward("openrouter", "openai/gpt-4o-mini", {"messages": []})))


# ---------------------------------------------------------------------------
# 4. Health, rate limits, budget
# ---------------------------------------------------------------------------


def test_429_sets_a_backoff_window_and_clears_on_expiry(tmp_path):
    clock = Clock()
    upstream = Upstream()
    upstream.chat_responses.append(
        httpx.Response(429, json={"error": {"message": "rate limit exceeded"}})
    )
    service = make_service(tmp_path, upstream, now=clock)
    add_openrouter(service)

    assert all(t.admitting for t in service.route_targets())

    with pytest.raises(UpstreamError) as excinfo:
        run(collect(service.forward("openrouter", "openai/gpt-4o-mini", {"messages": []})))
    assert excinfo.value.status_code == 429
    assert "rate limit exceeded" in excinfo.value.message

    # Not admitting, but not unhealthy: rate limited is busy, not broken.
    assert service.health("openrouter")[0] is True
    assert all(not t.admitting for t in service.route_targets())
    assert all(t.healthy for t in service.route_targets())
    with pytest.raises(ProviderNotAdmittingError):
        run(collect(service.forward("openrouter", "openai/gpt-4o-mini", {"messages": []})))

    clock.advance(1.5)  # first backoff is one second
    assert all(t.admitting for t in service.route_targets())
    run(collect(service.forward("openrouter", "openai/gpt-4o-mini", {"messages": []})))


def test_backoff_grows_exponentially_to_a_sixty_second_cap(tmp_path):
    clock = Clock()
    upstream = Upstream()
    service = make_service(tmp_path, upstream, now=clock)
    add_openrouter(service)
    runtime = service._entries["openrouter"].runtime

    seen = []
    for _ in range(10):
        seen.append(runtime.note_rate_limit(clock(), None, "slow down"))
    assert seen[:4] == [1.0, 2.0, 4.0, 8.0]
    assert max(seen) == 60.0


def test_retry_after_header_is_honoured(tmp_path):
    clock = Clock()
    upstream = Upstream()
    upstream.chat_responses.append(
        httpx.Response(429, headers={"retry-after": "30"}, json={"error": {"message": "slow"}})
    )
    service = make_service(tmp_path, upstream, now=clock)
    add_openrouter(service)

    with pytest.raises(UpstreamError) as excinfo:
        run(collect(service.forward("openrouter", "openai/gpt-4o-mini", {"messages": []})))
    assert excinfo.value.retry_after_s == pytest.approx(30.0)

    clock.advance(29)
    assert not any(t.admitting for t in service.route_targets())
    clock.advance(2)
    assert all(t.admitting for t in service.route_targets())


def test_5xx_is_retried_once_then_marks_unhealthy(tmp_path):
    upstream = Upstream()
    upstream.chat_responses.extend(
        [
            httpx.Response(503, json={"error": {"message": "upstream overloaded"}}),
            httpx.Response(503, json={"error": {"message": "upstream overloaded"}}),
        ]
    )
    service = make_service(tmp_path, upstream)
    add_openrouter(service)

    with pytest.raises(UpstreamError) as excinfo:
        run(collect(service.forward("openrouter", "openai/gpt-4o-mini", {"messages": []})))
    assert excinfo.value.status_code == 503

    posts = [r for r in upstream.requests if r.method == "POST"]
    assert len(posts) == 2, "one retry, not zero and not a storm"
    healthy, last_error = service.health("openrouter")
    assert healthy is False
    assert "503" in last_error


def test_a_single_5xx_recovers_on_the_retry(tmp_path):
    upstream = Upstream()
    upstream.chat_responses.append(httpx.Response(500, json={"error": {"message": "blip"}}))
    service = make_service(tmp_path, upstream)
    add_openrouter(service)
    run(collect(service.forward("openrouter", "openai/gpt-4o-mini", {"messages": []})))
    assert service.health("openrouter")[0] is True


def test_unhealthy_provider_recovers_on_the_next_successful_request(tmp_path):
    upstream = Upstream()
    upstream.chat_responses.extend(
        [httpx.Response(500, json={}), httpx.Response(500, json={})]
    )
    service = make_service(tmp_path, upstream)
    add_openrouter(service)
    with pytest.raises(UpstreamError):
        run(collect(service.forward("openrouter", "openai/gpt-4o-mini", {"messages": []})))
    assert service.health("openrouter")[0] is False

    # Unhealthy does not block the attempt; that is how it recovers.
    run(collect(service.forward("openrouter", "openai/gpt-4o-mini", {"messages": []})))
    assert service.health("openrouter")[0] is True
    assert service.health("openrouter")[1] is None


def test_auth_failure_is_immediate_and_actionable(tmp_path):
    upstream = Upstream()
    upstream.chat_responses.append(
        httpx.Response(401, json={"error": {"message": "No auth credentials found"}})
    )
    service = make_service(tmp_path, upstream)
    add_openrouter(service)

    with pytest.raises(UpstreamError) as excinfo:
        run(collect(service.forward("openrouter", "openai/gpt-4o-mini", {"messages": []})))
    assert excinfo.value.status_code == 401

    posts = [r for r in upstream.requests if r.method == "POST"]
    assert len(posts) == 1, "retrying a bad key just burns time"
    healthy, last_error = service.health("openrouter")
    assert healthy is False
    assert KEY_REF in last_error  # names the reference to fix
    assert REAL_KEY not in last_error


def test_transport_failure_does_not_become_a_silent_success(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=OPENROUTER_MODELS)
        raise httpx.ConnectError("connection refused")

    service = make_service(tmp_path, Upstream(), handler=handler)
    add_openrouter(service)
    with pytest.raises(UpstreamError) as excinfo:
        run(collect(service.forward("openrouter", "openai/gpt-4o-mini", {"messages": []})))
    assert excinfo.value.status_code == 502
    assert service.health("openrouter")[0] is False


def test_refresh_failure_keeps_the_cached_model_list(tmp_path):
    upstream = Upstream()
    service = make_service(tmp_path, upstream)
    add_openrouter(service)
    assert len(service.get("openrouter").models) == 3

    upstream.model_responses.append(httpx.Response(500, text="gateway blew up"))
    provider = service.refresh("openrouter")

    assert len(provider.models) == 3, "a failed refresh must not empty the catalogue"
    assert provider.last_error is not None
    assert "500" in provider.last_error
    # Still serving from cache, so still healthy and still a target.
    assert provider.healthy is True
    assert len(service.route_targets()) == 3


def test_empty_model_list_is_treated_as_a_failed_refresh(tmp_path):
    upstream = Upstream()
    service = make_service(tmp_path, upstream)
    add_openrouter(service)
    upstream.model_responses.append(httpx.Response(200, json={"data": []}))
    provider = service.refresh("openrouter")
    assert len(provider.models) == 3
    assert "empty" in provider.last_error


def test_cold_start_does_not_depend_on_the_network(tmp_path):
    upstream = Upstream()
    service = make_service(tmp_path, upstream)
    add_openrouter(service)

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no network")

    restarted = make_service(tmp_path, Upstream(), handler=offline)
    provider = restarted.get("openrouter")
    assert len(provider.models) == 3
    assert provider.models[0].input_cost_per_mtok is not None
    assert len(restarted.route_targets()) == 3


# ---------------------------------------------------------------------------
# 5. Spend and budget
# ---------------------------------------------------------------------------


def test_spend_is_tracked_per_provider_per_day(tmp_path):
    clock = Clock()
    upstream = Upstream()
    service = make_service(tmp_path, upstream, now=clock)
    add_openrouter(service)

    run(collect(service.forward("openrouter", "openai/gpt-4o-mini", {"messages": []})))
    # 10 prompt tokens at $0.15/Mtok, 20 completion at $0.60/Mtok.
    expected = 10 * 0.15 / 1e6 + 20 * 0.60 / 1e6
    assert service.spend_today("openrouter") == pytest.approx(expected)

    run(collect(service.forward("openrouter", "openai/gpt-4o-mini", {"messages": []})))
    assert service.spend_today("openrouter") == pytest.approx(expected * 2)

    clock.advance(48 * 3600)  # a new UTC day
    assert service.spend_today("openrouter") == 0.0


def test_streaming_spend_is_read_from_the_usage_frame(tmp_path):
    upstream = Upstream()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=OPENROUTER_MODELS)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_frames(sse_chunks("openai/gpt-4o-mini")),
        )

    service = make_service(tmp_path, upstream, handler=handler)
    add_openrouter(service)
    run(collect(service.forward("openrouter", "openai/gpt-4o-mini", {"messages": []}, True)))
    # The stub's usage frame: 24 prompt, 8 completion.
    assert service.spend_today("openrouter") == pytest.approx(
        24 * 0.15 / 1e6 + 8 * 0.60 / 1e6
    )


def test_unpriced_requests_are_counted_not_charged(tmp_path):
    upstream = Upstream(models={"data": [{"id": "llama-3.3-70b"}]})
    service = make_service(tmp_path, upstream)
    service.add({"provider_id": "groq", "kind": ProviderKind.GROQ, "api_key_ref": KEY_REF})
    run(collect(service.forward("groq", "llama-3.3-70b", {"messages": []})))
    payload = service.public_dict("groq")
    assert payload["spend_today_usd"] == 0.0
    assert payload["requests_today"] == 1
    # A zero spend must not be read as free when we simply cannot price it.
    assert payload["unpriced_requests_today"] == 1


def test_exceeding_the_daily_budget_stops_admitting(tmp_path):
    clock = Clock()
    upstream = Upstream()
    service = make_service(tmp_path, upstream, now=clock)
    add_openrouter(service, daily_budget_usd=0.000005)

    assert all(t.admitting for t in service.route_targets())
    run(collect(service.forward("openrouter", "openai/gpt-4o-mini", {"messages": []})))

    assert service.spend_today("openrouter") >= 0.000005
    assert all(not t.admitting for t in service.route_targets())
    with pytest.raises(ProviderNotAdmittingError, match="budget"):
        run(collect(service.forward("openrouter", "openai/gpt-4o-mini", {"messages": []})))
    assert "budget" in service.public_dict("openrouter")["admission_block"]

    clock.advance(24 * 3600)  # tomorrow, the ceiling resets
    assert all(t.admitting for t in service.route_targets())


def test_budget_can_be_set_and_cleared_live(tmp_path):
    service = make_service(tmp_path)
    add_openrouter(service)
    service.update("openrouter", {"daily_budget_usd": 5.0})
    assert service.public_dict("openrouter")["daily_budget_usd"] == 5.0
    service.update("openrouter", {"daily_budget_usd": None})
    assert service.public_dict("openrouter")["daily_budget_usd"] is None


# ---------------------------------------------------------------------------
# 6. Route targets. The seam with Agent G.
# ---------------------------------------------------------------------------


def test_route_targets_are_remote_and_carry_admission(tmp_path):
    service = make_service(tmp_path)
    add_openrouter(service)
    targets = service.route_targets()
    assert len(targets) == 3
    for target in targets:
        assert target.kind is TargetKind.REMOTE
        assert target.backend_url == "https://openrouter.ai/api/v1"
        assert target.target_id.startswith("openrouter:")
        assert target.healthy is True
        assert target.admitting is True
        assert target.cost_per_mtok is not None
    assert ProviderService.split_target_id("openrouter:openai/gpt-4o-mini") == (
        "openrouter",
        "openai/gpt-4o-mini",
    )


def test_a_remote_model_can_share_a_served_name_with_a_local_deployment(tmp_path):
    """The spill mechanism, not a collision."""
    service = make_service(tmp_path)
    add_openrouter(service, aliases={"meta-llama/llama-3.3-70b-instruct": "llama-3.3-70b"})

    by_name = service.route_targets_by_served_name()
    assert "llama-3.3-70b" in by_name
    remote = by_name["llama-3.3-70b"][0]

    local = RouteTarget(
        target_id="d-1",
        kind=TargetKind.LOCAL,
        backend_url="http://spark-01:8000/v1",
        weight=1.0,
        outstanding=3,
        healthy=True,
        admitting=True,
        strength=1.0,
        cost_per_mtok=0.0,
    )
    config = RoutingConfig(
        served_name="llama-3.3-70b",
        policy=RoutingPolicy.LOCAL_FIRST,
        targets=[local, remote],
    )
    assert {t.kind for t in config.targets} == {TargetKind.LOCAL, TargetKind.REMOTE}
    assert len(config.targets) == 2
    # And the model is one entry in /v1/models with two targets behind it.
    assert [m.served_name for _, m in service.models()].count("llama-3.3-70b") == 1


def test_two_providers_can_serve_the_same_name(tmp_path):
    service = make_service(tmp_path)
    add_openrouter(service)
    service.add(
        {
            "provider_id": "together",
            "kind": ProviderKind.TOGETHER,
            "api_key_ref": KEY_REF,
            "priority": 20,
            "aliases": {"anthropic/claude-sonnet-4.5": "house-model"},
        }
    )
    service.update("openrouter", {"aliases": {"anthropic/claude-sonnet-4.5": "house-model"}})
    targets = service.route_targets_by_served_name()["house-model"]
    assert len(targets) == 2
    assert {ProviderService.split_target_id(t.target_id)[0] for t in targets} == {
        "openrouter",
        "together",
    }


def test_outstanding_is_live_during_a_request(tmp_path):
    upstream = Upstream()
    seen: list[int] = []

    class Watching(httpx.AsyncByteStream):
        def __init__(self, service_ref) -> None:
            self._service = service_ref

        async def __aiter__(self):
            seen.append(self._service()[0].outstanding)
            yield b'{"usage": {"prompt_tokens": 1, "completion_tokens": 1}}'

        async def aclose(self) -> None:
            return None

    holder: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=OPENROUTER_MODELS)
        return httpx.Response(200, stream=Watching(lambda: holder["service"].route_targets()))

    service = make_service(tmp_path, upstream, handler=handler)
    holder["service"] = service
    add_openrouter(service)
    run(collect(service.forward("openrouter", "anthropic/claude-sonnet-4.5", {"messages": []})))
    assert seen == [1]
    assert all(t.outstanding == 0 for t in service.route_targets())


def test_priority_orders_the_listing(tmp_path):
    service = make_service(tmp_path)
    add_openrouter(service, priority=50)
    service.add(
        {"provider_id": "groq", "kind": ProviderKind.GROQ, "api_key_ref": KEY_REF, "priority": 10}
    )
    assert [p.provider_id for p in service.list()] == ["groq", "openrouter"]
    service.update("openrouter", {"priority": 1})
    assert [p.provider_id for p in service.list()] == ["openrouter", "groq"]


# ---------------------------------------------------------------------------
# 7. Port conformance and the day 0 stub
# ---------------------------------------------------------------------------


def test_service_satisfies_the_provider_port(tmp_path):
    service = make_service(tmp_path)
    add_openrouter(service)

    # Structural conformance with ProviderPort, method by method.
    for name in ("list", "add", "refresh", "models", "resolve_key", "health"):
        assert callable(getattr(service, name)), f"ProviderPort.{name} missing"
    for name in ("forward", "route_targets", "spend_today"):
        assert callable(getattr(service, name)), f"{name} missing"

    providers = service.list()
    assert all(isinstance(p, Provider) for p in providers)
    models = service.models()
    assert all(
        isinstance(pid, str) and isinstance(model, ProviderModel) for pid, model in models
    )
    healthy, last_error = service.health("openrouter")
    assert isinstance(healthy, bool)
    assert last_error is None or isinstance(last_error, str)
    assert isinstance(service.resolve_key("openrouter"), str)
    assert isinstance(service.refresh("openrouter"), Provider)
    assert isinstance(service.spend_today("openrouter"), float)
    assert all(isinstance(t, RouteTarget) for t in service.route_targets())


def test_day_zero_stub_is_a_healthy_provider_with_three_priced_models(tmp_path):
    service = build_stub_service(tmp_path, delay_s=0)
    providers = service.list()
    assert len(providers) == 1
    assert providers[0].healthy is True
    assert len(providers[0].models) == 3
    assert all(m.input_cost_per_mtok is not None for m in providers[0].models)

    targets = service.route_targets()
    assert len(targets) == 3
    assert all(t.kind is TargetKind.REMOTE and t.admitting for t in targets)

    chunks = run(
        collect(
            service.forward(
                "openrouter-stub", "openai/gpt-4o-mini", {"messages": []}, True
            )
        )
    )
    text = b"".join(chunks).decode()
    assert text.count("data: ") > 5
    assert text.rstrip().endswith("[DONE]")
    assert service.spend_today("openrouter-stub") > 0

    # Even the stub's key is a reference to a 0600 secret, not a literal.
    assert "***" == service.public_dict("openrouter-stub")["api_key"]
    from control_plane.providers.stub import STUB_KEY_VALUE

    assert STUB_KEY_VALUE not in json.dumps(service.public_list())
    run(service.aclose())


def test_stub_survives_being_rebuilt_on_the_same_directory(tmp_path):
    build_stub_service(tmp_path, delay_s=0)
    again = build_stub_service(tmp_path, delay_s=0)
    assert len(again.list()) == 1


# ---------------------------------------------------------------------------
# 8. Lifecycle
# ---------------------------------------------------------------------------


def test_the_refresh_timer_re_pulls_model_lists(tmp_path):
    """Discovery runs on add, on demand, and on a timer."""
    upstream = Upstream()
    secrets = SecretStore(tmp_path / "secrets.json", env={KEY_REF: REAL_KEY})
    service = ProviderService(
        data_path=tmp_path,
        secrets=secrets,
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(upstream.handler)
        ),
        refresh_interval_s=0.05,
    )
    add_openrouter(service)
    gets = lambda: len([r for r in upstream.requests if r.method == "GET"])  # noqa: E731
    after_add = gets()
    assert after_add == 1

    async def drive():
        await service.start()
        await asyncio.sleep(0.2)
        await service.aclose()

    run(drive())
    assert gets() > after_add
    # And the cycle stops cleanly when the service closes.
    settled = gets()
    run(asyncio.sleep(0.1))
    assert gets() == settled


def test_a_refresh_cycle_failure_does_not_kill_the_timer(tmp_path):
    upstream = Upstream()
    upstream.model_responses.append(httpx.Response(500, text="boom"))
    secrets = SecretStore(tmp_path / "secrets.json", env={KEY_REF: REAL_KEY})
    service = ProviderService(
        data_path=tmp_path,
        secrets=secrets,
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(upstream.handler)
        ),
        refresh_interval_s=0.05,
    )
    add_openrouter(service)  # consumes the 500, so the provider has no models

    async def drive():
        await service.start()
        await asyncio.sleep(0.2)
        await service.aclose()

    run(drive())
    assert len(service.get("openrouter").models) == 3


def test_concurrent_requests_are_counted_in_flight(tmp_path):
    """LOCAL_FIRST spills on saturation, so the in-flight count must be live."""
    upstream = Upstream()
    release = None
    peak: list[int] = []

    class Held(httpx.AsyncByteStream):
        async def __aiter__(self):
            peak.append(
                sum(t.outstanding for t in holder["service"].route_targets())
            )
            await release.wait()
            yield b'{"usage": {"prompt_tokens": 1, "completion_tokens": 1}}'

        async def aclose(self) -> None:
            return None

    holder: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=OPENROUTER_MODELS)
        return httpx.Response(200, stream=Held())

    service = make_service(tmp_path, upstream, handler=handler)
    holder["service"] = service
    add_openrouter(service)

    async def drive():
        nonlocal release
        release = asyncio.Event()
        tasks = [
            asyncio.create_task(
                collect(service.forward("openrouter", "openai/gpt-4o-mini", {"messages": []}))
            )
            for _ in range(3)
        ]
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.gather(*tasks)

    run(asyncio.wait_for(drive(), timeout=5))
    assert max(peak) == 3
    assert all(t.outstanding == 0 for t in service.route_targets())


def test_closing_flushes_pending_spend_to_disk(tmp_path):
    clock = Clock()
    upstream = Upstream()
    service = make_service(tmp_path, upstream, now=clock)
    add_openrouter(service)
    run(collect(service.forward("openrouter", "openai/gpt-4o-mini", {"messages": []})))
    run(service.aclose())

    reopened = make_service(tmp_path, Upstream(), now=clock)
    assert reopened.spend_today("openrouter") == service.spend_today("openrouter") > 0


def test_removing_a_provider_removes_its_targets(tmp_path):
    service = make_service(tmp_path)
    add_openrouter(service)
    service.remove("openrouter")
    assert service.list() == []
    assert service.route_targets() == []
    assert make_service(tmp_path, Upstream()).list() == []


def test_a_provider_that_was_unhealthy_gets_another_chance_after_a_restart(tmp_path):
    """Otherwise it never admits, so nothing reaches it, so nothing clears it."""
    upstream = Upstream()
    upstream.chat_responses.extend([httpx.Response(500, json={}), httpx.Response(500, json={})])
    service = make_service(tmp_path, upstream)
    add_openrouter(service)
    with pytest.raises(UpstreamError):
        run(collect(service.forward("openrouter", "openai/gpt-4o-mini", {"messages": []})))
    assert service.health("openrouter")[0] is False
    assert not any(t.admitting for t in service.route_targets())
    run(service.aclose())

    restarted = make_service(tmp_path, Upstream())
    assert restarted.health("openrouter") == (True, None)
    assert all(t.admitting for t in restarted.route_targets())
    # An unresolvable key, which is durable rather than transient, still bites.
    offline = make_service(tmp_path, Upstream(), env={})
    assert offline.get("openrouter").enabled is False


def test_list_reflects_live_health_without_a_persist(tmp_path):
    upstream = Upstream()
    upstream.chat_responses.append(httpx.Response(401, json={"error": {"message": "bad key"}}))
    service = make_service(tmp_path, upstream)
    add_openrouter(service)
    with pytest.raises(UpstreamError):
        run(collect(service.forward("openrouter", "openai/gpt-4o-mini", {"messages": []})))
    assert service.list()[0].healthy is False
    assert service.get("openrouter").last_error is not None
