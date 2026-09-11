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

from control_plane.contracts.modality import Modality
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
from control_plane.providers.config import BACKOFF_MAX_S, RETRY_AFTER_MAX_S
from control_plane.providers.discovery import parse_models
from control_plane.providers.kinds import spec_for
from control_plane.providers.service import minted_ref
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


OPENROUTER_ENDPOINTS = {
    "data": {
        "id": "anthropic/claude-sonnet-4.5",
        "endpoints": [
            {
                "tag": "anthropic",
                "provider_name": "Anthropic",
                "context_length": 200000,
                "pricing": {"prompt": "0.000003", "completion": "0.000015"},
                "quantization": "unknown",
            },
            {
                "tag": "google-vertex",
                "provider_name": "Google Vertex",
                "context_length": 200000,
                "pricing": {"prompt": "0.0000035", "completion": "0.0000175"},
                "quantization": "unknown",
            },
        ],
    }
}


class Upstream:
    """A scriptable fake provider endpoint behind httpx.MockTransport."""

    def __init__(self, models: dict | None = None, endpoints: dict | None = None) -> None:
        self.models = models if models is not None else OPENROUTER_MODELS
        self.endpoints = endpoints if endpoints is not None else OPENROUTER_ENDPOINTS
        self.model_responses: list[httpx.Response] = []
        self.chat_responses: list[httpx.Response] = []
        self.endpoints_responses: list[httpx.Response] = []
        self.requests: list[httpx.Request] = []
        self.auth_headers: list[str | None] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.auth_headers.append(request.headers.get("authorization"))
        path = request.url.path
        if request.method == "GET" and "/endpoints" in path:
            if self.endpoints_responses:
                return self.endpoints_responses.pop(0)
            return httpx.Response(200, json=self.endpoints)
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
    """Add the provider and switch its whole catalogue on.

    A real provider starts with nothing enabled -- that is the allowlist, and
    the tests for it below add one without this helper. Every other test in
    this file is about something else (forwarding, spend, backoff, health) and
    was written when a catalogue was servable the moment it arrived, so the
    helper keeps that premise true rather than making each of them say so.
    """
    spec = {
        "provider_id": "openrouter",
        "kind": ProviderKind.OPENROUTER,
        "api_key_ref": KEY_REF,
        "priority": 10,
    }
    spec.update(overrides)
    provider = service.add(spec)
    return enable_all(service, provider.provider_id)


def enable_all(service: ProviderService, provider_id: str) -> Provider:
    """Every model the provider currently publishes, switched on."""
    catalogue = [m["upstream_id"] for m in service.catalogue(provider_id)]
    return service.update(provider_id, {"enabled_models": catalogue})


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
    enable_all(service, "groq")
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


def test_a_catalog_says_which_models_are_audio(tmp_path):
    """The bug this field exists to fix.

    An OpenAI catalog is ingested wholesale, so whisper-1 and tts-1 have always
    arrived as ordinary ProviderModels and surfaced in /v1/models -- and in the
    chat picker -- as though they were chat models. Classifying them is what
    lets the gateway keep a chat request off them.
    """
    models = {
        m.upstream_id: m
        for m in parse_models(
            {
                "data": [
                    {"id": "gpt-4o"},
                    {"id": "tts-1"},
                    {"id": "gpt-4o-mini-tts"},
                    {"id": "whisper-1"},
                    {"id": "gpt-4o-transcribe"},
                    {"id": "text-embedding-3-small"},
                ]
            },
            spec_for(ProviderKind.OPENAI),
        )
    }
    assert models["gpt-4o"].modality is Modality.TEXT
    assert models["tts-1"].modality is Modality.SPEECH
    assert models["gpt-4o-mini-tts"].modality is Modality.SPEECH
    assert models["whisper-1"].modality is Modality.TRANSCRIPTION
    # "whisper" must win over "speech"/"tts" or every ASR model reads as TTS.
    assert models["gpt-4o-transcribe"].modality is Modality.TRANSCRIPTION
    assert models["text-embedding-3-small"].modality is Modality.EMBEDDING


def test_an_unknown_model_id_stays_text(tmp_path):
    """The classifier is a heuristic over model ids, so it only ever moves a
    model off the default when the id says so plainly. An unrecognised id is an
    unknown chat model, which is what TEXT means."""
    models = parse_models(
        {"data": [{"id": "some-org/an-unreleased-model-v2"}]},
        spec_for(ProviderKind.OPENROUTER),
    )
    assert models[0].modality is Modality.TEXT


def test_audio_models_are_still_marked_non_streaming(tmp_path):
    """The modality table grew out of the non-streaming list and has to keep
    doing that job: a speech or embedding endpoint has no token stream."""
    models = {
        m.upstream_id: m
        for m in parse_models(
            {"data": [{"id": "gpt-4o"}, {"id": "tts-1"}, {"id": "rerank-v3"}]},
            spec_for(ProviderKind.OPENAI),
        )
    }
    assert models["gpt-4o"].supports_streaming is True
    assert models["tts-1"].supports_streaming is False
    # Not an endpoint family we route, but still not streamable.
    assert models["rerank-v3"].supports_streaming is False
    assert models["rerank-v3"].modality is Modality.TEXT


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
        "catalogue": service.catalogue("openrouter"),
        "servable": [provider_as_dict(p) for p in service.servable()],
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


def test_a_pasted_key_is_stored_under_a_minted_reference(tmp_path):
    """The point of the whole feature: paste a key, and the *record* still holds
    only a name. Nothing about the contract is relaxed -- the value simply has
    somewhere to go now, which it did not before, so the only way to configure a
    provider was to hand-edit secrets.json or restart with a new environment.
    """
    service = make_service(tmp_path, env={})
    provider = service.add({"kind": ProviderKind.OPENROUTER, "api_key": REAL_KEY})

    ref = minted_ref("openrouter")
    assert ref == "DERATE_OPENROUTER_API_KEY"
    assert provider.api_key_ref == ref
    assert provider.enabled is True

    # The value went to secrets.json, at 0600, and nowhere else.
    assert stat.S_IMODE(os.stat(tmp_path / "secrets.json").st_mode) == 0o600
    assert json.loads((tmp_path / "secrets.json").read_text())[ref] == REAL_KEY
    assert service.resolve_key("openrouter") == REAL_KEY
    assert REAL_KEY not in (tmp_path / "providers.json").read_text()

    payload = service.public_dict("openrouter")
    assert payload["api_key_ref"] == ref
    assert payload["api_key"] == "***"


def test_a_pasted_key_can_be_stored_under_a_name_the_operator_chooses(tmp_path):
    """Both fields together: the key, and where to keep it. One step, no restart."""
    service = make_service(tmp_path, env={})
    provider = service.add(
        {
            "kind": ProviderKind.OPENROUTER,
            "api_key_ref": "my-openrouter-key",
            "api_key": REAL_KEY,
        }
    )
    assert provider.api_key_ref == "my-openrouter-key"
    assert service.resolve_key("openrouter") == REAL_KEY
    assert service.secrets.refs() == ["my-openrouter-key"]


def test_the_reference_field_refusal_names_the_field_that_works(tmp_path):
    """The refusal was correct and unhelpful: it said what not to do, at a time
    when there was nothing else to do. Now there is, so it says so."""
    service = make_service(tmp_path, env={})
    with pytest.raises(ValueError, match="NAME of an environment variable") as excinfo:
        service.add({"kind": ProviderKind.OPENROUTER, "api_key_ref": REAL_KEY})
    assert "api_key" in str(excinfo.value)
    assert REAL_KEY not in str(excinfo.value)
    assert service.list() == []


def test_a_refused_spec_leaves_no_secret_behind(tmp_path):
    """Every screen runs before the write, so a rejected add stores nothing.

    Ordering, not luck: the display_name screen below rejects this spec, and if
    the key were written first the file would keep a credential for a provider
    that does not exist and nothing would ever clean it up.
    """
    service = make_service(tmp_path, env={})
    with pytest.raises(ValueError, match="display_name"):
        service.add(
            {
                "kind": ProviderKind.OPENROUTER,
                "display_name": REAL_KEY,
                "api_key": REAL_KEY,
            }
        )
    assert not (tmp_path / "secrets.json").exists()
    assert service.list() == []


def test_an_environment_variable_shadowing_the_minted_reference_is_refused(tmp_path):
    """The environment is read before the file, so a name already taken there
    would win at request time and the provider would authenticate with a key
    nobody here chose. Refuse, naming the variable and never either value."""
    ref = minted_ref("openrouter")
    other = "sk-or-v1-somebodyelseskeymaterial0123456789"
    service = make_service(tmp_path, env={ref: other})

    with pytest.raises(ValueError, match=ref) as excinfo:
        service.add({"kind": ProviderKind.OPENROUTER, "api_key": REAL_KEY})
    message = str(excinfo.value)
    assert REAL_KEY not in message
    assert other not in message
    # Refused before the write, so no inert entry is left in the file.
    assert not (tmp_path / "secrets.json").exists()
    assert service.list() == []


def test_an_environment_variable_holding_the_same_key_is_not_a_conflict(tmp_path):
    """Only a *different* value is a shadow. Re-pasting the key an operator
    already exported is a no-op, not an error."""
    ref = minted_ref("openrouter")
    service = make_service(tmp_path, env={ref: REAL_KEY})
    provider = service.add({"kind": ProviderKind.OPENROUTER, "api_key": REAL_KEY})
    assert provider.api_key_ref == ref
    assert service.resolve_key("openrouter") == REAL_KEY


def test_patching_a_key_rotates_it_in_place(tmp_path):
    service = make_service(tmp_path, env={})
    service.add({"kind": ProviderKind.OPENROUTER, "api_key": REAL_KEY})
    ref = minted_ref("openrouter")

    rotated = "sk-or-v1-rotatedkeymaterial0123456789abcdef"
    provider = service.update("openrouter", {"api_key": rotated})

    # The same reference, so everything pointing at that name keeps working.
    assert provider.api_key_ref == ref
    assert service.resolve_key("openrouter") == rotated
    assert json.loads((tmp_path / "secrets.json").read_text())[ref] == rotated
    assert rotated not in (tmp_path / "providers.json").read_text()


def test_a_rotated_key_revives_a_provider_disabled_for_a_missing_one(tmp_path):
    """The provider that most needs a new key is the one switched off for want
    of one -- and it is disabled by definition, so a check gated on `enabled`
    would never run for exactly the case it exists to fix."""
    upstream = Upstream()
    add_openrouter(make_service(tmp_path, upstream, env={KEY_REF: REAL_KEY}))
    service = make_service(tmp_path, upstream, env={})
    assert service.get("openrouter").enabled is False

    provider = service.update("openrouter", {"api_key": REAL_KEY})
    assert provider.enabled is True
    assert provider.last_error is None
    # An operator's own reference is not overwritten; a new one is minted.
    assert provider.api_key_ref == minted_ref("openrouter")
    assert service.resolve_key("openrouter") == REAL_KEY


def test_a_key_patched_into_the_reference_field_gets_the_same_sentence(tmp_path):
    """PATCH refused this too, in different words, which read as a different
    rule rather than the same one."""
    service = make_service(tmp_path)
    add_openrouter(service)
    with pytest.raises(ValueError, match="NAME of an environment variable"):
        service.update("openrouter", {"api_key_ref": REAL_KEY})


def test_removing_a_provider_deletes_a_minted_key_but_not_an_operators_own(tmp_path):
    """Ours to delete because we minted it under a name that says so. An
    operator's name may be an environment variable, or shared with something
    else, and removing a provider did not ask for it to be revoked."""
    service = make_service(tmp_path, env={})
    service.add({"provider_id": "minted", "kind": ProviderKind.OPENROUTER, "api_key": REAL_KEY})
    service.secrets.put(KEY_REF, REAL_KEY)
    service.add({"provider_id": "byhand", "kind": ProviderKind.OPENROUTER, "api_key_ref": KEY_REF})
    assert sorted(service.secrets.refs()) == sorted([KEY_REF, minted_ref("minted")])

    service.remove("minted")
    service.remove("byhand")
    assert service.secrets.refs() == [KEY_REF]


def test_key_status_names_where_the_key_resolves_from(tmp_path):
    """The whole of what a screen with no reveal control may say about a key.

    Not the value, not its length, not its first characters: a state word and
    which of the two places answered. The provenance is not decoration: a key
    in secrets.json is one this coordinator wrote and can replace in place,
    while a key in the environment belongs to whatever started the process --
    pasting a replacement mints a reference and moves the provider onto it,
    which is a different thing to have done and worth knowing beforehand.
    """
    service = make_service(tmp_path, env={KEY_REF: REAL_KEY})
    add_openrouter(service)
    service.add(
        {"provider_id": "pasted", "kind": ProviderKind.OPENROUTER, "api_key": REAL_KEY}
    )
    service.add({"provider_id": "ollama", "kind": ProviderKind.OLLAMA})

    assert service.key_status("openrouter") == {
        "key_state": "set",
        "key_source": "environment",
    }
    assert service.key_status("pasted") == {
        "key_state": "set",
        "key_source": "secrets.json",
    }
    # A kind that takes no key is not a kind whose key is missing.
    assert service.key_status("ollama") == {"key_state": "not_needed", "key_source": None}

    # Same providers.json, same secrets.json, no environment. The provider
    # that lived in the environment is now the one that does not resolve, and
    # says so rather than reporting the reference it still carries as working.
    reloaded = make_service(tmp_path, env={})
    assert reloaded.key_status("openrouter") == {"key_state": "missing", "key_source": None}
    assert reloaded.key_status("pasted") == {
        "key_state": "set",
        "key_source": "secrets.json",
    }


def test_key_status_answers_from_a_closed_vocabulary(tmp_path):
    """Two fields, and every value either a state word or the name of a place.

    Asserted rather than assumed because this dict is rendered: a status that
    could carry an arbitrary string is a status that could carry a key.
    """
    service = make_service(tmp_path, env={KEY_REF: REAL_KEY})
    add_openrouter(service)
    status = service.key_status("openrouter")
    assert set(status) == {"key_state", "key_source"}
    assert status["key_state"] in {"set", "missing", "not_needed"}
    assert status["key_source"] in {"environment", "secrets.json", None}
    assert REAL_KEY not in json.dumps(status)


def test_key_status_on_an_unknown_provider_is_the_usual_refusal(tmp_path):
    service = make_service(tmp_path)
    with pytest.raises(UnknownProviderError):
        service.key_status("nope")


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


@pytest.mark.parametrize("bad_shape", [[], "nonsense"], ids=["list", "string"])
def test_non_dict_secrets_file_is_treated_as_empty_not_a_crash(tmp_path, bad_shape, caplog):
    """Valid JSON, wrong top-level shape. Optional file: never fail startup over it.

    ``raw.items()`` used to raise ``AttributeError`` on a list or string
    before the isinstance guard ever ran, and that propagated straight
    through ``ProviderService.__init__`` — an optional file with the wrong
    shape took the whole service down.
    """
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps(bad_shape))
    store = SecretStore(path, env={})
    with caplog.at_level(logging.WARNING):
        assert store.get(KEY_REF) is None
        assert store.refs() == []
    # Warn that the file is unusable, but never log its content.
    warnings = [r.getMessage() for r in caplog.records]
    assert any("secrets.json" in message for message in warnings)
    assert not any("nonsense" in message for message in warnings)

    # And the failure mode a real user hits: constructing the whole service
    # over a malformed-but-valid secrets.json must not raise.
    service = ProviderService(data_path=tmp_path, secrets=store)
    assert service.list() == []


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


def test_redaction_filter_reaches_child_loggers_not_just_the_package_logger(tmp_path, caplog):
    """M-18 regression.

    A ``logging.Filter`` attached to a ``Logger`` only runs for records
    logged directly through *that* logger object — never for a child in the
    hierarchy. Every module in this package logs via
    ``logging.getLogger(__name__)``, which is a child of the package logger
    (``control_plane.providers``), not the package logger itself. A filter
    installed only on ``logging.getLogger(__package__)`` would therefore
    never see a single real log record from this package: this test logs
    through ``control_plane.providers.runtime`` directly, bypassing
    ``ProviderService`` entirely, and would fail if the filter were attached
    only to the parent.
    """
    secret = "unprefixed-remembered-upstream-secret-42"
    service = make_service(tmp_path)
    # Remembered, not merely regex-shaped: this value matches none of the
    # vendor key patterns, so redaction here can only be working because the
    # filter is live on this exact child logger.
    service.redactor.remember(secret)

    child_log = logging.getLogger("control_plane.providers.runtime")
    with caplog.at_level(logging.DEBUG):
        child_log.debug("upstream said: %s", secret)

    assert secret not in caplog.text
    for record in caplog.records:
        assert secret not in record.getMessage()
    assert "upstream said: ***" in caplog.text


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


def test_a_pinned_backend_is_carried_in_the_forwarded_body(tmp_path):
    upstream = Upstream()
    service = make_service(tmp_path, upstream)
    add_openrouter(service)
    service.update(
        "openrouter", {"backend_pins": {"anthropic/claude-sonnet-4.5": "google-vertex"}}
    )

    run(
        collect(
            service.forward(
                "openrouter",
                "anthropic/claude-sonnet-4.5",
                {"model": "anthropic/claude-sonnet-4.5", "messages": []},
            )
        )
    )
    sent = json.loads(upstream.requests[-1].content)
    assert sent["provider"] == {"only": ["google-vertex"]}


def test_no_pin_means_no_provider_field_at_all(tmp_path):
    upstream = Upstream()
    service = make_service(tmp_path, upstream)
    add_openrouter(service)

    run(
        collect(
            service.forward(
                "openrouter",
                "anthropic/claude-sonnet-4.5",
                {"model": "anthropic/claude-sonnet-4.5", "messages": []},
            )
        )
    )
    sent = json.loads(upstream.requests[-1].content)
    assert "provider" not in sent


def test_a_callers_own_provider_field_is_not_overridden(tmp_path):
    upstream = Upstream()
    service = make_service(tmp_path, upstream)
    add_openrouter(service)
    service.update(
        "openrouter", {"backend_pins": {"anthropic/claude-sonnet-4.5": "google-vertex"}}
    )

    run(
        collect(
            service.forward(
                "openrouter",
                "anthropic/claude-sonnet-4.5",
                {
                    "model": "anthropic/claude-sonnet-4.5",
                    "messages": [],
                    "provider": {"only": ["anthropic"]},
                },
            )
        )
    )
    sent = json.loads(upstream.requests[-1].content)
    assert sent["provider"] == {"only": ["anthropic"]}


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


def test_retry_after_beyond_the_backoff_cap_is_still_honoured(tmp_path):
    """An upstream-sent Retry-After is not our exponential backoff.

    ``BACKOFF_MAX_S`` (60s) bounds *our* exponential curve when the upstream
    sends nothing. It must not silently reinterpret an upstream's explicit,
    larger request as a request to re-admit at 60s instead — that is
    re-admitting earlier than the upstream asked.
    """
    clock = Clock()
    upstream = Upstream()
    upstream.chat_responses.append(
        httpx.Response(429, headers={"retry-after": "200"}, json={"error": {"message": "slow"}})
    )
    service = make_service(tmp_path, upstream, now=clock)
    add_openrouter(service)

    assert 200.0 > BACKOFF_MAX_S  # the case only means something if this holds
    with pytest.raises(UpstreamError) as excinfo:
        run(collect(service.forward("openrouter", "openai/gpt-4o-mini", {"messages": []})))
    assert excinfo.value.retry_after_s == pytest.approx(200.0)

    clock.advance(61)  # past our own backoff cap...
    assert not any(t.admitting for t in service.route_targets())  # ...but not admitting yet
    clock.advance(140)
    assert all(t.admitting for t in service.route_targets())


def test_retry_after_above_the_dos_guard_is_clamped(tmp_path):
    """An upstream (or a spoofed header) asking for an unreasonable wait is capped."""
    clock = Clock()
    upstream = Upstream()
    upstream.chat_responses.append(
        httpx.Response(429, headers={"retry-after": "10000"}, json={"error": {"message": "slow"}})
    )
    service = make_service(tmp_path, upstream, now=clock)
    add_openrouter(service)

    with pytest.raises(UpstreamError) as excinfo:
        run(collect(service.forward("openrouter", "openai/gpt-4o-mini", {"messages": []})))
    assert excinfo.value.retry_after_s == pytest.approx(RETRY_AFTER_MAX_S)

    clock.advance(RETRY_AFTER_MAX_S - 1)
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


def test_a_self_hosted_server_with_nothing_pulled_is_empty_not_broken(tmp_path):
    """The state every Ollama box is in between being added and being pulled to.

    A provider has to exist before anything can be pulled onto it, so this is
    the order the UI asks for -- and marking it unhealthy there puts a red
    error on the screen for doing exactly the right thing.

    Healthy with nothing in it routes nowhere on its own: an empty catalogue
    contributes no route targets, so /v1/models is unchanged and no request can
    land here. Health is a claim about the server answering, which it did.
    """
    upstream = Upstream()
    service = make_service(tmp_path, upstream)
    # Queued before add(), which refreshes: this is the catalogue the provider
    # is created against, so nothing is ever cached. What a real Ollama holding
    # no models answers is a well-formed empty list wearing a null.
    upstream.model_responses.append(
        httpx.Response(200, json={"object": "list", "data": None})
    )
    provider = service.add({"provider_id": "ollama", "kind": ProviderKind.OLLAMA,
                            "base_url": "http://pi:11434/v1"})

    assert provider.models == []
    assert provider.healthy is True
    assert provider.last_error is None
    # Healthy, and still routing nowhere -- the two are not in tension.
    assert service.route_targets() == []


def test_an_unparseable_catalogue_is_still_a_failure_for_a_pullable_kind(tmp_path):
    """The exemption is for a shape we recognise, not for any empty answer."""
    upstream = Upstream()
    service = make_service(tmp_path, upstream)
    upstream.model_responses.append(httpx.Response(200, json={"surprise": True}))
    provider = service.add({"provider_id": "ollama", "kind": ProviderKind.OLLAMA,
                            "base_url": "http://pi:11434/v1"})

    assert provider.healthy is False
    assert "empty or unrecognized" in provider.last_error


def _pull_handler(seen: list, *, total: int = 4096):
    """An Ollama-shaped native pull: NDJSON frames, then a /v1/models refresh."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/api/pull"):
            seen.append(json.loads(request.content or b"{}"))
            frames = [
                json.dumps({"status": "pulling", "total": total, "digest": "sha256:ab"}),
                json.dumps({"status": "success"}),
            ]
            return httpx.Response(200, text="\n".join(frames) + "\n")
        if request.method == "GET" and request.url.path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": None})
        return httpx.Response(404, json={"error": {"message": "nope"}})

    return handler


def test_a_gguf_repository_name_is_not_key_material(tmp_path):
    """The screen on a model name catches keys, not long repository names.

    Every GGUF variant of Mistral-Small-24B-Instruct-2501 was refused by the
    entropy fallback -- 32+ contiguous mixed-case-and-digit characters is a
    key shape and also an ordinary model name -- while Qwen2.5 and Llama-3.1
    passed, because their dots break the run. A whole family failing in a way
    that reads as random, and the ValueError surfaced as a 502 telling the
    operator they had pasted an API key.

    The name is forwarded to the upstream and neither stored nor displayed,
    so the vendor-prefixed patterns are the whole screen it needs.
    """
    seen: list = []
    service = make_service(tmp_path, handler=_pull_handler(seen), env={})
    service.add({"provider_id": "ollama", "kind": ProviderKind.OLLAMA,
                 "base_url": "http://pi:11434/v1"})

    ref = "hf.co/bartowski/Mistral-Small-24B-Instruct-2501-GGUF:Q4_K_M"
    result = asyncio.run(service.pull("ollama", ref))

    assert result["model"] == ref
    assert seen == [{"model": ref, "stream": True}]


def test_every_frame_reaches_on_progress_not_just_the_first(tmp_path):
    """The progress was always arriving; nothing was listening.

    Ollama's pull stream carries `completed` alongside `total` on every frame.
    This loop parsed each one and kept only the first `total` and the last
    `digest`, which is why a pull was a single number reported once followed by
    minutes of silence with no way to ask how far along it was.
    """
    seen: list = []
    frames = [
        {"status": "pulling manifest"},
        {"status": "pulling sha256:ab", "total": 4096, "completed": 1024,
         "digest": "sha256:ab"},
        {"status": "pulling sha256:ab", "total": 4096, "completed": 4096,
         "digest": "sha256:ab"},
        {"status": "verifying sha256 digest"},
        {"status": "success"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/api/pull"):
            seen.append(json.loads(request.content or b"{}"))
            return httpx.Response(
                200, text="\n".join(json.dumps(f) for f in frames) + "\n"
            )
        if request.method == "GET" and request.url.path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": None})
        return httpx.Response(404, json={"error": {"message": "nope"}})

    service = make_service(tmp_path, handler=handler, env={})
    service.add({"provider_id": "ollama", "kind": ProviderKind.OLLAMA,
                 "base_url": "http://pi:11434/v1"})

    got: list = []
    asyncio.run(service.pull("ollama", "qwen2.5:0.5b", on_progress=got.append))

    assert got == frames
    # The two the screen actually draws with, in the order they arrived.
    assert [f.get("completed") for f in got] == [None, 1024, 4096, None, None]
    # The upstream's own words, which the UI renders verbatim rather than
    # paraphrasing into a second, worse vocabulary.
    assert got[0]["status"] == "pulling manifest"
    assert got[3]["status"] == "verifying sha256 digest"


def test_a_pull_without_a_progress_sink_is_unchanged(tmp_path):
    """`on_progress` is additive. Every caller that predates it still works."""
    seen: list = []
    service = make_service(tmp_path, handler=_pull_handler(seen), env={})
    service.add({"provider_id": "ollama", "kind": ProviderKind.OLLAMA,
                 "base_url": "http://pi:11434/v1"})

    result = asyncio.run(service.pull("ollama", "qwen2.5:0.5b"))

    assert result == {"provider_id": "ollama", "model": "qwen2.5:0.5b",
                      "digest": "sha256:ab"}


def test_a_refused_pull_reports_no_progress(tmp_path):
    """A refusal downloaded nothing, so it must not put a bar on the screen.

    `on_size` raising is what aborts the transfer. Any frame reported after
    that would describe a download that was stopped at its first byte.
    """
    seen: list = []
    service = make_service(tmp_path, handler=_pull_handler(seen), env={})
    service.add({"provider_id": "ollama", "kind": ProviderKind.OLLAMA,
                 "base_url": "http://pi:11434/v1"})

    got: list = []

    def refuse(total: int) -> None:
        raise RuntimeError("too big")

    with pytest.raises(RuntimeError, match="too big"):
        asyncio.run(service.pull("ollama", "qwen2.5:0.5b",
                                 on_size=refuse, on_progress=got.append))

    assert got == []


def test_a_pasted_key_is_still_refused_as_a_model_name(tmp_path):
    """Narrowing the screen must not open the hole it was there to close."""
    seen: list = []
    service = make_service(tmp_path, handler=_pull_handler(seen), env={})
    service.add({"provider_id": "ollama", "kind": ProviderKind.OLLAMA,
                 "base_url": "http://pi:11434/v1"})

    with pytest.raises(ValueError, match="key material"):
        asyncio.run(service.pull("ollama", "sk-ant-api03-" + "a" * 32))
    # Refused before the wire, not after.
    assert seen == []


def test_a_hosted_api_returning_nothing_is_still_a_failed_refresh(tmp_path):
    """OpenRouter with no models is broken, not empty -- it hosts no weights
    of its own, so there is no pull that would explain the gap."""
    upstream = Upstream()
    service = make_service(tmp_path, upstream)
    add_openrouter(service)
    upstream.model_responses.append(httpx.Response(200, json={"data": []}))
    provider = service.refresh("openrouter")
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
    enable_all(service, "groq")
    run(collect(service.forward("groq", "llama-3.3-70b", {"messages": []})))
    payload = service.public_dict("groq")
    assert payload["spend_today_usd"] == 0.0
    assert payload["requests_today"] == 1
    # A zero spend must not be read as free when we simply cannot price it.
    assert payload["unpriced_requests_today"] == 1


def test_openrouter_cost_is_read_from_the_response_not_the_rate_card(tmp_path):
    """The charge OpenRouter reports wins over what we compute from `pricing`.

    Its `pricing` object carries thirteen components -- cached prompt tokens,
    long-context tiers, reasoning, image, audio, web search -- and our
    `ProviderModel` carries two. So the rate-card figure is a forecast and the
    `usage.cost` in the response is the ledger. Here they deliberately disagree:
    a cached prompt makes the real charge a fraction of the predicted one, and
    the fraction is what must be banked.
    """
    upstream = Upstream()
    upstream.chat_responses.append(
        httpx.Response(
            200,
            json={
                "id": "x",
                "model": "openai/gpt-4o-mini",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "cost": 0.00004,
                    "cost_details": {"upstream_inference_cost": 0.00003},
                    "prompt_tokens_details": {"cached_tokens": 8},
                },
            },
        )
    )
    service = make_service(tmp_path, upstream)
    add_openrouter(service)
    run(collect(service.forward("openrouter", "openai/gpt-4o-mini", {"messages": []})))

    from_rate_card = 10 * 0.15 / 1e6 + 20 * 0.60 / 1e6
    assert service.spend_today("openrouter") == pytest.approx(0.00004)
    assert service.spend_today("openrouter") != pytest.approx(from_rate_card)

    payload = service.public_dict("openrouter")
    assert payload["requests_today"] == 1
    assert payload["metered_requests_today"] == 1
    assert payload["unpriced_requests_today"] == 0
    assert payload["tokens_today"] == {"input": 10, "output": 20}


def test_a_metered_zero_is_priced_not_unpriced(tmp_path):
    """A free model charging nothing is knowledge; only silence is a gap."""
    upstream = Upstream()
    upstream.chat_responses.append(
        httpx.Response(
            200,
            json={
                "id": "x",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "cost": 0},
            },
        )
    )
    service = make_service(tmp_path, upstream)
    add_openrouter(service)
    run(collect(service.forward("openrouter", "openai/gpt-4o-mini", {"messages": []})))

    payload = service.public_dict("openrouter")
    assert payload["spend_today_usd"] == 0.0
    assert payload["metered_requests_today"] == 1
    assert payload["unpriced_requests_today"] == 0


def test_metered_cost_falls_back_to_the_rate_card_when_absent(tmp_path):
    """No `cost` in the usage block is the old path, unchanged."""
    upstream = Upstream()  # its default response carries tokens and no cost
    service = make_service(tmp_path, upstream)
    add_openrouter(service)
    run(collect(service.forward("openrouter", "openai/gpt-4o-mini", {"messages": []})))

    assert service.spend_today("openrouter") == pytest.approx(
        10 * 0.15 / 1e6 + 20 * 0.60 / 1e6
    )
    payload = service.public_dict("openrouter")
    assert payload["metered_requests_today"] == 0
    assert payload["unpriced_requests_today"] == 0


def test_a_cost_from_an_unmetered_kind_is_ignored(tmp_path):
    """`cost` only means dollars where the kind is known to publish it.

    Groq's spec says it meters nothing. A `cost` on its response is a number in
    an unknown unit, and banking it would invent spend out of a field we do not
    understand.
    """
    upstream = Upstream(models={"data": [{"id": "llama-3.3-70b"}]})
    upstream.chat_responses.append(
        httpx.Response(
            200,
            json={
                "id": "x",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "cost": 999.0},
            },
        )
    )
    service = make_service(tmp_path, upstream)
    service.add({"provider_id": "groq", "kind": ProviderKind.GROQ, "api_key_ref": KEY_REF})
    enable_all(service, "groq")
    run(collect(service.forward("groq", "llama-3.3-70b", {"messages": []})))

    payload = service.public_dict("groq")
    assert payload["spend_today_usd"] == 0.0
    assert payload["metered_requests_today"] == 0
    assert payload["unpriced_requests_today"] == 1


def test_streaming_cost_is_read_from_the_final_usage_frame(tmp_path):
    """OpenRouter puts the charge in the last SSE frame, same as the tokens."""
    frames = sse_chunks("openai/gpt-4o-mini")[:-1]  # drop [DONE], re-added below
    frames.append(
        b"data: "
        + json.dumps(
            {
                "id": "chatcmpl-stub",
                "object": "chat.completion.chunk",
                "model": "openai/gpt-4o-mini",
                "choices": [],
                "usage": {"prompt_tokens": 24, "completion_tokens": 8, "cost": 0.00007},
            }
        ).encode()
        + b"\n\n"
    )
    frames.append(b"data: [DONE]\n\n")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=OPENROUTER_MODELS)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_frames(frames),
        )

    service = make_service(tmp_path, upstream=None, handler=handler)
    add_openrouter(service)
    run(collect(service.forward("openrouter", "openai/gpt-4o-mini", {"messages": []}, True)))

    assert service.spend_today("openrouter") == pytest.approx(0.00007)
    assert service.public_dict("openrouter")["metered_requests_today"] == 1


def test_a_negative_cost_is_dropped_rather_than_credited(tmp_path):
    """A field we do not understand must not run the day's spend backwards."""
    upstream = Upstream()
    upstream.chat_responses.append(
        httpx.Response(
            200,
            json={
                "id": "x",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "cost": -5.0},
            },
        )
    )
    service = make_service(tmp_path, upstream)
    add_openrouter(service)
    run(collect(service.forward("openrouter", "openai/gpt-4o-mini", {"messages": []})))

    # Falls through to the rate card, which is the honest remaining answer.
    assert service.spend_today("openrouter") == pytest.approx(
        10 * 0.15 / 1e6 + 20 * 0.60 / 1e6
    )
    assert service.public_dict("openrouter")["metered_requests_today"] == 0


def test_metered_request_counts_survive_a_restart(tmp_path):
    clock = Clock()
    upstream = Upstream()
    upstream.chat_responses.append(
        httpx.Response(
            200,
            json={
                "id": "x",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "cost": 0.00004},
            },
        )
    )
    service = make_service(tmp_path, upstream, now=clock)
    add_openrouter(service)
    run(collect(service.forward("openrouter", "openai/gpt-4o-mini", {"messages": []})))
    run(service.aclose())

    restarted = make_service(tmp_path, Upstream(), now=clock)
    payload = restarted.public_dict("openrouter")
    assert payload["spend_today_usd"] == pytest.approx(0.00004)
    assert payload["metered_requests_today"] == 1


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
    enable_all(service, "together")
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


def test_retry_after_dos_guard_boundary_values(tmp_path):
    """Exactly 300 is honoured; 301 is clamped to the guard."""
    for header, expected in (("300", 300.0), ("301", RETRY_AFTER_MAX_S)):
        clock = Clock()
        upstream = Upstream()
        upstream.chat_responses.append(
            httpx.Response(
                429, headers={"retry-after": header}, json={"error": {"message": "slow"}}
            )
        )
        service = make_service(tmp_path / header, upstream, now=clock)
        add_openrouter(service)
        with pytest.raises(UpstreamError) as excinfo:
            run(collect(service.forward("openrouter", "openai/gpt-4o-mini", {"messages": []})))
        assert excinfo.value.retry_after_s == pytest.approx(expected)


def test_key_shaped_display_name_and_aliases_are_refused(tmp_path):
    """WF-5 finding: the looks_like_secret screen guarded api_key_ref and
    base_url while display_name and aliases were echoed and persisted
    verbatim -- the one place a pasted key could still leak."""
    key_shaped = "sk-or-v1-aaaabbbbccccdddd1234"
    service = make_service(tmp_path, Upstream())
    with pytest.raises(ValueError, match="key material"):
        service.add({"kind": "openrouter", "api_key_ref": KEY_REF, "display_name": key_shaped})
    with pytest.raises(ValueError, match="key material"):
        service.add({"kind": "openrouter", "api_key_ref": KEY_REF, "aliases": {"m": key_shaped}})
    add_openrouter(service)
    provider_id = service.list()[0].provider_id
    with pytest.raises(ValueError, match="key material"):
        service.update(provider_id, {"display_name": key_shaped})
    with pytest.raises(ValueError, match="key material"):
        service.update(provider_id, {"aliases": {key_shaped: "x"}})
    # Ordinary names still pass both paths.
    service.update(provider_id, {"display_name": "OpenRouter (primary)"})
    service.update(provider_id, {"aliases": {"anthropic/claude-sonnet-4.5": "sonnet"}})


# ---------------------------------------------------------------------------
# 9. The allowlist. What a provider is willing to serve.
# ---------------------------------------------------------------------------


def test_a_new_provider_serves_nothing_until_something_is_enabled(tmp_path):
    """OpenRouter publishes hundreds of models and a fresh one serves none.

    The whole point: adding a provider used to make its entire catalogue
    servable in one call, which is a wall of models nobody chose.
    """
    service = make_service(tmp_path)
    service.add(
        {"provider_id": "openrouter", "kind": ProviderKind.OPENROUTER, "api_key_ref": KEY_REF}
    )
    assert service.models() == []
    assert service.route_targets() == []
    assert service.servable()[0].models == []
    # The catalogue is still there to choose from; it is only not served.
    assert len(service.catalogue("openrouter")) > 0
    assert not any(m["enabled"] for m in service.catalogue("openrouter"))


def test_enabling_two_models_serves_exactly_those_two(tmp_path):
    service = make_service(tmp_path)
    service.add(
        {"provider_id": "openrouter", "kind": ProviderKind.OPENROUTER, "api_key_ref": KEY_REF}
    )
    published = [m["upstream_id"] for m in service.catalogue("openrouter")]
    assert len(published) > 2, "fixture must publish more than we enable"
    chosen = published[:2]
    service.update("openrouter", {"enabled_models": chosen})

    assert sorted(m.upstream_id for _, m in service.models()) == sorted(chosen)
    assert sorted(m.upstream_id for m in service.servable()[0].models) == sorted(chosen)
    assert len(service.route_targets()) == 2
    # And the catalogue still lists every one of them, flagged.
    enabled = {m["upstream_id"] for m in service.catalogue("openrouter") if m["enabled"]}
    assert enabled == set(chosen)


def test_a_record_written_before_the_allowlist_serves_its_whole_catalogue(tmp_path):
    """Grandfathering. An upgrade must not silently stop routing.

    A providers.json with no `enabled_models` key predates this feature, and
    the provider it describes was serving everything the moment before the
    coordinator restarted.
    """
    service = make_service(tmp_path)
    add_openrouter(service)
    published = {m.upstream_id for m in service.get("openrouter").models}

    raw = json.loads((tmp_path / "providers.json").read_text())
    for record in raw["providers"]:
        record.pop("enabled_models", None)
    (tmp_path / "providers.json").write_text(json.dumps(raw))

    reloaded = make_service(tmp_path)
    assert {m.upstream_id for _, m in reloaded.models()} == published
    assert all(m["enabled"] for m in reloaded.catalogue("openrouter"))


def test_an_empty_allowlist_is_not_a_missing_one(tmp_path):
    """`[]` means "serve nothing"; absent means "this record predates us".

    Collapsing the two would either break every upgraded install or make an
    explicit choice to serve nothing un-expressible.
    """
    service = make_service(tmp_path)
    service.add(
        {"provider_id": "openrouter", "kind": ProviderKind.OPENROUTER, "api_key_ref": KEY_REF}
    )
    raw = json.loads((tmp_path / "providers.json").read_text())
    assert raw["providers"][0]["enabled_models"] == []

    reloaded = make_service(tmp_path)
    assert reloaded.models() == []


def test_a_model_the_provider_does_not_publish_is_refused_by_name(tmp_path):
    """The id is named, because the fix is to stop asking for that one."""
    service = make_service(tmp_path)
    add_openrouter(service)
    with pytest.raises(ValueError, match="does not publish"):
        service.update("openrouter", {"enabled_models": ["nobody/such-model"]})
    with pytest.raises(ValueError, match="nobody/such-model"):
        service.update("openrouter", {"enabled_models": ["nobody/such-model"]})


# ---------------------------------------------------------------------------
# Backend routing: OpenRouter's own per-model backend-host preference
# ---------------------------------------------------------------------------


def test_list_backends_reports_openrouters_own_endpoints(tmp_path):
    upstream = Upstream()
    service = make_service(tmp_path, upstream)
    add_openrouter(service)

    rows = run(service.list_backends_async("openrouter", "anthropic/claude-sonnet-4.5"))
    tags = {r["tag"] for r in rows}
    assert tags == {"anthropic", "google-vertex"}
    anthropic = next(r for r in rows if r["tag"] == "anthropic")
    assert anthropic["provider_name"] == "Anthropic"
    assert anthropic["context_length"] == 200000
    assert anthropic["input_cost_per_mtok"] == pytest.approx(3.0)
    assert anthropic["pinned"] is False


def test_list_backends_marks_the_pinned_row(tmp_path):
    upstream = Upstream()
    service = make_service(tmp_path, upstream)
    add_openrouter(service)
    service.update(
        "openrouter", {"backend_pins": {"anthropic/claude-sonnet-4.5": "google-vertex"}}
    )

    rows = run(service.list_backends_async("openrouter", "anthropic/claude-sonnet-4.5"))
    pinned = {r["tag"] for r in rows if r["pinned"]}
    assert pinned == {"google-vertex"}


def test_list_backends_refuses_a_kind_that_does_not_aggregate_hosts(tmp_path):
    upstream = Upstream()
    service = make_service(tmp_path, upstream, env={KEY_REF: REAL_KEY, "TOGETHER_KEY": REAL_KEY})
    service.add({"provider_id": "together", "kind": ProviderKind.TOGETHER, "api_key_ref": "TOGETHER_KEY"})

    with pytest.raises(AdapterUnsupportedError):
        run(service.list_backends_async("together", "meta-llama/Llama-3-70b"))


def test_backend_pins_are_refused_for_a_model_not_in_the_catalogue(tmp_path):
    service = make_service(tmp_path)
    add_openrouter(service)
    with pytest.raises(ValueError, match="does not publish"):
        service.update("openrouter", {"backend_pins": {"nobody/such-model": "anthropic"}})


def test_backend_pins_are_refused_for_a_kind_that_does_not_aggregate_hosts(tmp_path):
    service = make_service(tmp_path, env={KEY_REF: REAL_KEY, "TOGETHER_KEY": REAL_KEY})
    provider = service.add(
        {"provider_id": "together", "kind": ProviderKind.TOGETHER, "api_key_ref": "TOGETHER_KEY"}
    )
    model_id = provider.models[0].upstream_id if provider.models else "meta-llama/Llama-3-70b"
    with pytest.raises(ValueError, match="does not aggregate"):
        service.update("together", {"backend_pins": {model_id: "anything"}})


def test_backend_pins_survive_a_restart(tmp_path):
    service = make_service(tmp_path)
    add_openrouter(service)
    service.update(
        "openrouter", {"backend_pins": {"anthropic/claude-sonnet-4.5": "google-vertex"}}
    )

    reloaded = make_service(tmp_path)
    rows = run(reloaded.list_backends_async("openrouter", "anthropic/claude-sonnet-4.5"))
    pinned = {r["tag"] for r in rows if r["pinned"]}
    assert pinned == {"google-vertex"}


def test_an_ordinary_model_id_is_not_mistaken_for_key_material(tmp_path):
    """A GGUF-shaped name has a 32-character mixed-case run, and is not a key.

    `_screen_free_text`'s entropy fallback refused every variant of
    Mistral-Small-24B-Instruct-2501 on exactly this shape. The allowlist is
    checked against the provider's own catalogue instead, which no pasted key
    can ever be in.
    """
    long_id = "mistralai/MistralSmall24BInstruct2501x"
    upstream = Upstream(models={"data": [{"id": long_id}]})
    service = make_service(tmp_path, upstream)
    service.add(
        {"provider_id": "openrouter", "kind": ProviderKind.OPENROUTER, "api_key_ref": KEY_REF}
    )
    service.update("openrouter", {"enabled_models": [long_id]})
    assert [m.upstream_id for _, m in service.models()] == [long_id]


def test_a_refresh_that_finds_a_new_model_leaves_it_switched_off(tmp_path):
    """A provider adding a model to its catalogue must not add it to yours."""
    upstream = Upstream(models={"data": [{"id": "a/one"}]})
    service = make_service(tmp_path, upstream)
    service.add(
        {"provider_id": "openrouter", "kind": ProviderKind.OPENROUTER, "api_key_ref": KEY_REF}
    )
    service.update("openrouter", {"enabled_models": ["a/one"]})

    upstream.models = {"data": [{"id": "a/one"}, {"id": "b/brand-new"}]}
    service.refresh("openrouter")

    assert [m.upstream_id for _, m in service.models()] == ["a/one"]
    catalogue = {m["upstream_id"]: m["enabled"] for m in service.catalogue("openrouter")}
    assert catalogue == {"a/one": True, "b/brand-new": False}


def test_an_id_that_leaves_the_catalogue_is_not_pruned_from_the_allowlist(tmp_path):
    """A bad refresh must not silently un-choose a model.

    Pruning on absence would mean one flaky upstream response permanently
    disables what the operator picked.
    """
    upstream = Upstream(models={"data": [{"id": "a/one"}, {"id": "a/two"}]})
    service = make_service(tmp_path, upstream)
    service.add(
        {"provider_id": "openrouter", "kind": ProviderKind.OPENROUTER, "api_key_ref": KEY_REF}
    )
    service.update("openrouter", {"enabled_models": ["a/one", "a/two"]})

    upstream.models = {"data": [{"id": "a/one"}]}
    service.refresh("openrouter")
    assert [m.upstream_id for _, m in service.models()] == ["a/one"]

    upstream.models = {"data": [{"id": "a/one"}, {"id": "a/two"}]}
    service.refresh("openrouter")
    assert sorted(m.upstream_id for _, m in service.models()) == ["a/one", "a/two"]


def test_the_allowlist_survives_a_restart(tmp_path):
    service = make_service(tmp_path)
    add_openrouter(service)
    published = [m["upstream_id"] for m in service.catalogue("openrouter")]
    service.update("openrouter", {"enabled_models": published[:1]})

    reloaded = make_service(tmp_path)
    assert [m.upstream_id for _, m in reloaded.models()] == published[:1]


def test_modality_survives_a_restart(tmp_path):
    """`_model_to_dict` wrote it and `_model_from_dict` dropped it.

    Every provider model came back from disk as TEXT, so a coordinator restart
    put whisper-1 in the chat picker as an ordinary chat model -- reopening the
    defect the modality field exists to close, silently and on every boot.
    """
    upstream = Upstream(
        models={"data": [{"id": "whisper-1"}, {"id": "tts-1"}, {"id": "gpt-4o"}]}
    )
    service = make_service(tmp_path, upstream)
    add_openrouter(service)
    assert service.find_model("openrouter", "whisper-1").modality is Modality.TRANSCRIPTION

    reloaded = make_service(tmp_path)
    assert reloaded.find_model("openrouter", "whisper-1").modality is Modality.TRANSCRIPTION
    assert reloaded.find_model("openrouter", "tts-1").modality is Modality.SPEECH
    assert reloaded.find_model("openrouter", "gpt-4o").modality is Modality.TEXT


def test_forwarding_to_a_model_that_is_not_enabled_is_refused(tmp_path):
    """Defence in depth: the routing index that chose the target is cached.

    A model switched off a moment ago can still be selected from a stale
    index, and the refusal here is what stops that becoming a billed call.
    """
    service = make_service(tmp_path)
    add_openrouter(service)
    service.update("openrouter", {"enabled_models": []})
    with pytest.raises(ProviderNotAdmittingError, match="not enabled"):
        run(collect(service.forward("openrouter", "openai/gpt-4o-mini", {"messages": []})))


def test_counts_say_both_what_is_served_and_what_is_published(tmp_path):
    """One number cannot express "2 of 312"."""
    service = make_service(tmp_path)
    add_openrouter(service)
    published = [m["upstream_id"] for m in service.catalogue("openrouter")]
    service.update("openrouter", {"enabled_models": published[:1]})

    payload = service.public_dict("openrouter")
    assert payload["model_count"] == 1
    assert payload["catalogue_count"] == len(published)
    assert [m["upstream_id"] for m in payload["models"]] == published[:1]


def test_models_chosen_separates_a_legacy_record_from_a_fully_enabled_one(tmp_path):
    """The two counts cannot say this, because they are equal in both cases.

    A screen that infers "nobody ever chose" from `model_count ==
    catalogue_count` says exactly that over a provider whose operator
    deliberately switched everything on. `enabled_models` is a request field
    and is on no response, so this boolean is the whole of what the wire says.
    """
    service = make_service(tmp_path)
    add_openrouter(service)  # the helper enables the whole catalogue explicitly
    chosen = service.public_dict("openrouter")
    assert chosen["models_chosen"] is True
    assert chosen["model_count"] == chosen["catalogue_count"]

    raw = json.loads((tmp_path / "providers.json").read_text())
    for record in raw["providers"]:
        record.pop("enabled_models", None)
    (tmp_path / "providers.json").write_text(json.dumps(raw))

    legacy = make_service(tmp_path).public_dict("openrouter")
    assert legacy["models_chosen"] is False
    # Identical on the counts, which is why the boolean has to exist.
    assert legacy["model_count"] == chosen["model_count"]
    assert legacy["catalogue_count"] == chosen["catalogue_count"]


# ── Provider logos ──────────────────────────────────────────────────────────
#
# The mark on the cluster screen's provider bus. Cosmetic by design: every
# failure below has to end as "draw a monogram instead", never as an error the
# operator has to read.


def _provider(kind: ProviderKind, base_url: str, provider_id: str = "p") -> Provider:
    return Provider(
        provider_id=provider_id,
        kind=kind,
        display_name=kind.value,
        base_url=base_url,
        api_key_ref="",
        enabled=True,
        priority=10,
    )


def test_a_kinds_mark_is_never_looked_for_on_its_api_host():
    """No API host serves one. Probed 2026-09-07: api.openai.com/favicon.ico,
    api.anthropic.com and api.groq.com all 404. The table exists because the
    obvious construction from KindSpec.base_url finds nothing."""
    from control_plane.providers.logos import candidate_urls

    for kind, base in (
        (ProviderKind.OPENAI, "https://api.openai.com/v1"),
        (ProviderKind.ANTHROPIC, "https://api.anthropic.com/v1"),
        (ProviderKind.GROQ, "https://api.groq.com/openai/v1"),
    ):
        for url in candidate_urls(_provider(kind, base)):
            assert not url.startswith("https://api."), url


def test_every_kind_offers_a_mark_or_derives_one():
    """Adding a kind and silently losing its logo becomes a failing test."""
    from control_plane.providers.logos import candidate_urls
    from control_plane.providers.kinds import spec_for

    for kind in ProviderKind:
        urls = candidate_urls(_provider(kind, spec_for(kind).base_url))
        # CUSTOM ships no base_url, so it has nothing to derive from until an
        # operator gives it one -- which is the honest answer, not a bug.
        if kind is ProviderKind.CUSTOM:
            assert urls == []
        else:
            assert urls, kind


def test_a_custom_provider_falls_back_to_its_configured_base_url():
    """CUSTOM has no brand of ours to guess at."""
    from control_plane.providers.logos import brand_origin, candidate_urls

    p = _provider(ProviderKind.CUSTOM, "https://box.example/v1")
    assert brand_origin(p) == "https://box.example"
    assert candidate_urls(p)[0] == "https://box.example/favicon.svg"
    # The path they configured for inference is never a path we then fetch.
    assert all("/v1" not in u for u in candidate_urls(p))


def test_a_base_url_that_is_not_a_url_has_nowhere_to_look():
    """An unset CUSTOM base_url must not become a request to nothing."""
    from control_plane.providers.logos import brand_origin, candidate_urls

    p = _provider(ProviderKind.CUSTOM, "")
    assert brand_origin(p) is None
    assert candidate_urls(p) == []


def test_an_ollama_pointed_at_a_lan_box_uses_that_box_not_ollama_com():
    """That machine is the operator's, and ollama.com's logo would be a claim
    about it that nobody made. The default install still gets the brand."""
    from control_plane.providers.logos import brand_origin

    lan = _provider(ProviderKind.OLLAMA, "http://192.168.1.9:11434/v1")
    assert brand_origin(lan) == "http://192.168.1.9:11434"
    default = _provider(ProviderKind.OLLAMA, "http://localhost:11434/v1")
    assert brand_origin(default) == "https://ollama.com"


def test_a_logo_larger_than_the_cap_is_refused_mid_stream():
    """Refused while reading, so a broken server cannot make us hold it all."""
    from control_plane.providers import logos

    oversized = b"x" * (logos.MAX_LOGO_BYTES + 1)

    def handler(request):
        return httpx.Response(200, content=oversized, headers={"content-type": "image/png"})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await logos.fetch_image(client, "https://example/favicon.png")

    assert run(go()) is None


def test_a_non_image_content_type_is_refused():
    """An HTML error page served as a logo renders as a broken image with
    nothing in the log to explain it."""
    from control_plane.providers import logos

    def handler(request):
        return httpx.Response(200, content=b"<html>404</html>", headers={"content-type": "text/html"})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await logos.fetch_image(client, "https://example/favicon.ico")

    assert run(go()) is None


def test_html_wearing_an_image_content_type_is_still_refused():
    """The header is not enough on its own: together.ai answers a 404 with
    222 KB of SPA HTML, and a server that mislabels one is the case this
    guards. The type served is the type recognised, not the type claimed."""
    from control_plane.providers import logos

    def handler(request):
        return httpx.Response(
            200,
            content=b"<!DOCTYPE html><html><body><svg/></body></html>" + b"x" * 4000,
            headers={"content-type": "image/png"},
        )

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await logos.fetch_image(client, "https://example/favicon.png")

    assert run(go()) is None


def test_sniff_accepts_the_allowlist_and_nothing_else():
    """What the bytes are, never what the server said they are."""
    from control_plane.providers.logos import sniff

    assert sniff(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8) == "image/png"
    assert sniff(b"\xff\xd8\xff\xe0" + b"\x00" * 8) == "image/jpeg"
    assert sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert sniff(b"\x00\x00\x01\x00\x03\x00\x10\x10") == "image/x-icon"
    assert sniff(b"<svg xmlns='http://www.w3.org/2000/svg'/>") == "image/svg+xml"
    assert sniff(b"<?xml version='1.0'?><svg/>") == "image/svg+xml"

    assert sniff(b"<!DOCTYPE html><html><svg/></html>") is None
    assert sniff(b'{"error": "nope"}') is None
    assert sniff(b"PK\x03\x04") is None
    assert sniff(b"") is None
    # An empty icon directory is not an icon.
    assert sniff(b"\x00\x00\x01\x00\x00\x00") is None


def test_the_first_candidate_that_answers_wins(tmp_path):
    """SVG first: it is the only one that stays sharp at the size the bus
    draws it. A 404 on it falls through rather than giving up."""
    from control_plane.providers import logos

    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/favicon.svg":
            return httpx.Response(404)
        return httpx.Response(
            200, content=b"\x89PNG\r\n\x1a\n", headers={"content-type": "image/png"}
        )

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            urls = logos.candidate_urls(_provider(ProviderKind.CUSTOM, "https://box.example/v1"))
            for url in urls:
                found = await logos.fetch_image(client, url)
                if found:
                    return found
        return None

    body, content_type = run(go())
    assert body == b"\x89PNG\r\n\x1a\n"
    assert content_type == "image/png"
    assert seen[0] == "/favicon.svg"


def test_a_missing_logo_is_not_refetched_on_every_request(tmp_path):
    """Without negative caching a vendor with no favicon is a network request
    every time the Cluster tab repaints."""
    from control_plane.providers.logos import LogoCache

    cache = LogoCache(tmp_path)
    assert cache.is_fresh_miss("p") is False
    cache.remember_miss("p", permanent=True)
    assert cache.is_fresh_miss("p") is True
    assert cache.get("p") is None


def test_a_transient_failure_backs_off_rather_than_hammering(tmp_path):
    """A timeout is not an answer, so the wait doubles and a rate limit
    recovers on its own."""
    from control_plane.providers.logos import LogoCache

    cache = LogoCache(tmp_path)
    cache.remember_miss("p", permanent=False, now=1000.0)
    first = cache._memory["p"].expires_at - 1000.0
    cache.remember_miss("p", permanent=False, now=1000.0)
    second = cache._memory["p"].expires_at - 1000.0
    assert second == pytest.approx(first * 2)


def test_a_cached_logo_survives_a_coordinator_restart(tmp_path):
    """Two tiers, like resolver/cache.py: a cold process reads it off disk
    rather than re-fetching seven vendors on every boot."""
    from control_plane.providers.logos import LogoCache

    LogoCache(tmp_path).put("openrouter", b"\x89PNG", "image/png")
    assert LogoCache(tmp_path).get("openrouter") == (b"\x89PNG", "image/png")


def test_a_provider_id_never_becomes_a_path_segment_raw(tmp_path):
    """An operator names their own providers, so the id is untrusted here."""
    from control_plane.providers.logos import LogoCache

    cache = LogoCache(tmp_path)
    cache.put("../../etc/passwd", b"\x89PNG", "image/png")
    written = [p.name for p in tmp_path.iterdir()]
    assert written == ["______etc_passwd.png"]


def test_no_api_key_is_sent_when_fetching_a_logo():
    """A public favicon does not need a credential, and spending one here
    would put key material on a request that had no business carrying it."""
    from control_plane.providers import logos

    headers = {}

    def handler(request):
        headers.update(request.headers)
        return httpx.Response(
            200, content=b"\x89PNG\r\n\x1a\n", headers={"content-type": "image/png"}
        )

    p = _provider(ProviderKind.OPENROUTER, "https://openrouter.ai/api/v1")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await logos.fetch_image(client, logos.candidate_urls(p)[0])

    run(go())
    assert "authorization" not in headers
    assert "x-api-key" not in headers
    assert "cookie" not in headers


def test_a_transport_failure_expires_instead_of_latching(tmp_path):
    """The block used to prevent its own cure.

    `note_transport_error` set `healthy = False`, routing selects on `healthy`,
    and `note_success` -- which needs a request to actually get through -- was
    the only thing that could set it back. So an unreachable provider was never
    offered a request, and never got the one thing that would have cleared it.
    The only other way out was the model-list refresh, `PROVIDER_REFRESH_S`
    hours away. On 2026-09-08 one ReadTimeout took an Ollama box off the air for
    a working day while it answered pings in 11 ms.
    """
    from control_plane.providers.runtime import ProviderRuntime

    runtime = ProviderRuntime(provider_id="pi")

    runtime.note_transport_error(0.0, "ReadTimeout")
    assert runtime.healthy is False
    assert runtime.admission_block(0.0), "still failing, so still blocked"
    assert runtime.retry_in(0.0) > 0, "a block with no expiry is the bug"

    # Still inside the backoff.
    runtime.half_open(runtime.failure_backoff_until - 0.01)
    assert runtime.healthy is False

    # Past it: offered again, so that one request can settle the question.
    runtime.half_open(runtime.failure_backoff_until + 0.01)
    assert runtime.healthy is True
    assert runtime.admission_block(runtime.failure_backoff_until + 0.01) is None
    assert runtime.last_error, (
        "half open is not a claim of recovery; the last error stays readable "
        "until something actually succeeds"
    )

    runtime.note_success(100.0)
    assert runtime.last_error is None and runtime.retry_in(100.0) == 0.0


def test_repeated_failures_back_off_further_each_time(tmp_path):
    """A provider that stays down must not be probed at a fixed rate."""
    from control_plane.providers.runtime import ProviderRuntime

    runtime = ProviderRuntime(provider_id="pi")
    waits = []
    now = 0.0
    for _ in range(4):
        runtime.note_transport_error(now, "ConnectError")
        waits.append(runtime.retry_in(now))
        now = runtime.failure_backoff_until
        runtime.half_open(now)

    assert waits == sorted(waits) and waits[-1] > waits[0], waits


def test_a_rejected_key_never_expires(tmp_path):
    """The one unhealthy state that must stay latched.

    Retrying a key the upstream refused burns quota and can get an account
    locked, and no amount of waiting turns a wrong key into a right one.
    """
    from control_plane.providers.runtime import ProviderRuntime

    runtime = ProviderRuntime(provider_id="openrouter")
    runtime.note_auth_failure(0.0, 401, "OPENROUTER_API_KEY", "invalid key")

    runtime.half_open(1e9)
    assert runtime.healthy is False
    assert runtime.admission_block(1e9), "a bad key is not a transient outage"

    runtime.note_success(1e9)
    assert runtime.admission_block(1e9) is None, "an operator fixed the key"


def test_an_unreachable_provider_is_not_reported_as_rate_limited(tmp_path):
    """Two different waits, and a screen must not confuse them.

    `rate_limited()` reads `backoff_until`, so folding a transport failure into
    that field would have an unreachable box telling the operator it was being
    throttled by an upstream it never reached.
    """
    from control_plane.providers.runtime import ProviderRuntime

    runtime = ProviderRuntime(provider_id="pi")
    runtime.note_transport_error(0.0, "ReadTimeout")

    assert runtime.rate_limited(0.0) is False
    assert "rate limited" not in (runtime.admission_block(0.0) or "")
    assert "could not reach upstream" in (runtime.admission_block(0.0) or "")


def test_the_service_puts_a_recovered_provider_back_in_front_of_routing(tmp_path):
    """End to end: the half-open transition has to reach what routing reads.

    `gateway/targets.py` builds a remote RouteTarget with
    `healthy=provider.healthy`, and `provider.healthy` is mirrored from the
    runtime by `ProviderService._sync` on every read. If the transition is not
    made there, the runtime can recover all it likes and the router will still
    never offer it a request.
    """
    clock = {"t": 1000.0}
    upstream = Upstream()
    upstream.chat_responses.append(httpx.Response(200, json={"choices": []}))
    service = make_service(tmp_path, upstream, now=lambda: clock["t"])
    add_openrouter(service)

    service._entry("openrouter").runtime.note_transport_error(clock["t"], "ReadTimeout")
    assert service.get("openrouter").healthy is False, "still failing"
    wait = service._entry("openrouter").runtime.retry_in(clock["t"])
    assert wait > 0

    clock["t"] += wait + 0.1
    assert service.get("openrouter").healthy is True, (
        "past its backoff the provider must be offered again, or nothing will "
        "ever discover that it came back"
    )

    # And a request that gets through settles it for good.
    run(collect(service.forward("openrouter", "openai/gpt-4o-mini", {"messages": []})))
    assert service.health("openrouter") == (True, None)


# -- the multipart path -----------------------------------------------------
#
# `/v1/audio/transcriptions` used to bypass this class entirely: the managed
# path builds its payload with dict(body), an opaque upload has none, so a
# transcription took a raw forward that carried the key and nothing else.
# Nothing it spent was ever recorded -- which also meant the daily budget gate
# it passes on the way in never saw the traffic it was gating.


async def _raw(service, provider_id, upstream_id, content, content_type, endpoint):
    async with service.open_upstream_raw(
        provider_id, upstream_id, content, content_type, endpoint=endpoint
    ) as opened:
        return b"".join([chunk async for chunk in opened.body])


MULTIPART = (
    b"--BOUNDARY\r\n"
    b'Content-Disposition: form-data; name="model"\r\n\r\n'
    b"whisper-1\r\n"
    b"--BOUNDARY\r\n"
    b'Content-Disposition: form-data; name="file"; filename="a.wav"\r\n\r\n'
    b"RIFFfake\r\n"
    b"--BOUNDARY--\r\n"
)
MULTIPART_TYPE = "multipart/form-data; boundary=BOUNDARY"


def test_a_transcription_is_forwarded_byte_for_byte_under_its_own_content_type(tmp_path):
    """The boundary is part of the body's meaning: rewriting the header, or
    re-encoding the body, invalidates the upload."""
    upstream = Upstream()
    upstream.chat_responses.append(httpx.Response(200, json={"text": "hello"}))
    service = make_service(tmp_path, upstream)
    add_openrouter(service)

    run(_raw(service, "openrouter", "openai/gpt-4o-mini", MULTIPART,
             MULTIPART_TYPE, "audio/transcriptions"))

    sent = upstream.requests[-1]
    assert sent.content == MULTIPART
    assert sent.headers["content-type"] == MULTIPART_TYPE


def test_a_transcriptions_reported_cost_is_banked(tmp_path):
    """The usage block OpenRouter returns on a transcription, verbatim in
    shape. `providers/usage.py` already reads `cost` and the `input_tokens`/
    `output_tokens` spellings -- the only reason none of it was ever recorded
    is that these bytes never reached this class."""
    upstream = Upstream()
    upstream.chat_responses.append(
        httpx.Response(
            200,
            json={
                "text": "the transcript",
                "usage": {
                    "seconds": 9.2,
                    "type": "tokens",
                    "total_tokens": 113,
                    "input_tokens": 83,
                    "output_tokens": 30,
                    "cost": 0.000508,
                },
            },
        )
    )
    service = make_service(tmp_path, upstream)
    add_openrouter(service)

    run(_raw(service, "openrouter", "openai/gpt-4o-mini", MULTIPART,
             MULTIPART_TYPE, "audio/transcriptions"))

    assert service.spend_today("openrouter") == pytest.approx(0.000508)
    payload = service.public_dict("openrouter")
    assert payload["requests_today"] == 1
    assert payload["metered_requests_today"] == 1
    assert payload["tokens_today"] == {"input": 83, "output": 30}


def test_a_transcription_nobody_can_price_is_still_counted(tmp_path):
    """Accounting and pricing are separate. A response with no usage block --
    a duration-priced whisper-1, a Custom box -- must leave a mark, or a day
    of audio traffic renders as "$0.00, nothing served today"."""
    upstream = Upstream(models={"data": [{"id": "llama-3.3-70b"}]})
    upstream.chat_responses.append(httpx.Response(200, json={"text": "no usage here"}))
    service = make_service(tmp_path, upstream)
    service.add({"provider_id": "groq", "kind": ProviderKind.GROQ, "api_key_ref": KEY_REF})
    enable_all(service, "groq")

    run(_raw(service, "groq", "llama-3.3-70b", MULTIPART,
             MULTIPART_TYPE, "audio/transcriptions"))

    payload = service.public_dict("groq")
    assert payload["requests_today"] == 1
    assert payload["unpriced_requests_today"] == 1
    assert payload["spend_today_usd"] == 0.0
    # No token counts at all, rather than a measured-looking pair of zeros.
    assert payload["tokens_today"] == {"input": 0, "output": 0}


def test_a_provider_over_budget_refuses_a_transcription_before_it_is_sent(tmp_path):
    """The cap is enforceable without knowing this request's price -- it reads
    what has already been spent. This is the half that was missing: the gate
    ran, but nothing multipart spent ever moved the number it reads."""
    upstream = Upstream()
    service = make_service(tmp_path, upstream)
    add_openrouter(service, daily_budget_usd=0.01)
    entry = service._entry("openrouter")
    entry.runtime.record_usage(
        service._now(), 0, 0, None, None, metered_cost_usd=5.0
    )

    before = len(upstream.requests)
    with pytest.raises(ProviderNotAdmittingError):
        run(_raw(service, "openrouter", "openai/gpt-4o-mini", MULTIPART,
                 MULTIPART_TYPE, "audio/transcriptions"))
    # Refused before a byte left: the upload never reached the network.
    assert len(upstream.requests) == before


def test_a_raw_upstream_still_latches_an_auth_failure(tmp_path):
    """One retry ladder for both body shapes, so the raw path cannot quietly
    stop matching the JSON one."""
    upstream = Upstream()
    upstream.chat_responses.append(httpx.Response(401, json={"error": "bad key"}))
    service = make_service(tmp_path, upstream)
    add_openrouter(service)

    with pytest.raises(UpstreamError) as caught:
        run(_raw(service, "openrouter", "openai/gpt-4o-mini", MULTIPART,
                 MULTIPART_TYPE, "audio/transcriptions"))
    assert caught.value.status_code == 401
    assert service._entry("openrouter").runtime.auth_rejected is True
