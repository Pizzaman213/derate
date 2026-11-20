"""What we know about each provider kind before anyone configures anything.

Adding OpenRouter should be a display name and a key reference. Everything
else in this table is the reason it can be.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..contracts.providers import ProviderKind
from .config import CHATGPT_RESOURCE


class AuthStyle(str, Enum):
    BEARER = "bearer"  # Authorization: Bearer <key>
    X_API_KEY = "x_api_key"  # x-api-key: <key>
    NONE = "none"  # a box on the LAN with no auth


class PricingUnit(str, Enum):
    NONE = "none"  # provider publishes no pricing; costs stay None
    PER_TOKEN_USD = "per_token_usd"  # OpenRouter: dollars per single token
    PER_MTOK_USD = "per_mtok_usd"  # Together: dollars per million tokens


class ChatWire(str, Enum):
    """What the upstream's chat endpoint speaks.

    Clients always send Chat Completions to derate. Adapted kinds translate
    to Responses (``responses_adapter.py``) or native Messages
    (``anthropic_adapter.py``), then translate the stream back.
    """

    CHAT_COMPLETIONS = "chat_completions"
    RESPONSES = "responses"
    ANTHROPIC_MESSAGES = "anthropic_messages"


@dataclass(frozen=True)
class KindSpec:
    kind: ProviderKind
    display_name: str
    base_url: str
    models_path: str
    auth: AuthStyle
    pricing: PricingUnit
    extra_headers: dict[str, str] = field(default_factory=dict)
    chat_path: str = "chat/completions"
    completions_path: str = "completions"
    embeddings_path: str = "embeddings"
    speech_path: str = "audio/speech"
    transcriptions_path: str = "audio/transcriptions"
    #: Whether this kind hosts its own weights and can be told to fetch one.
    #: True only for a server the operator runs: a hosted API already has every
    #: model it is going to have, and "pull" there would be a control that does
    #: nothing. The native path is deliberately not the OpenAI-compatible one --
    #: pulling is not in that spec, so it is named per kind rather than assumed.
    pull_path: str = ""
    requires_key: bool = True
    requires_base_url: bool = False
    # OpenAI's stream_options.include_usage. Only set it where the upstream is
    # known to accept it; an unexpected field is a 400 on a strict server.
    supports_stream_usage: bool = True
    #: Whether this kind reports what it charged, in dollars, in the response's
    #: own ``usage`` block. Where it does, that figure is the ledger and the
    #: published `pricing` table is only a forecast: OpenRouter's own number
    #: already accounts for cached prompt tokens (79 of its models price those
    #: differently), the long-context tiers 43 of them switch to above a token
    #: threshold, and the reasoning/image/audio/web-search components -- none of
    #: which a flat input/output pair can express. Off by default, because a
    #: `cost` from an upstream we do not recognize is a number in an unknown
    #: unit, and banking it would be worse than pricing from the table.
    meters_cost: bool = False
    # False means we have no adapter for this wire format in this build.
    forwardable: bool = True
    unsupported_reason: str = ""
    #: Whether this kind aggregates several backend hosts per model and
    #: exposes an endpoints-listing call plus a request-time `provider` field
    #: to pick among them. True only for OpenRouter -- no other kind here
    #: multiplexes a model id over more than one upstream host.
    supports_backend_routing: bool = False
    #: The wire the chat endpoint speaks upstream. See :class:`ChatWire`.
    chat_wire: ChatWire = ChatWire.CHAT_COMPLETIONS
    #: The sign-in flow that supplies this kind's credential, or "" for a kind
    #: whose credential is a key. A sign-in kind is never added by a key and
    #: never has an ``api_key_ref``.
    sign_in: str = ""


_SPECS: dict[ProviderKind, KindSpec] = {
    ProviderKind.OPENROUTER: KindSpec(
        kind=ProviderKind.OPENROUTER,
        display_name="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        models_path="models",
        auth=AuthStyle.BEARER,
        pricing=PricingUnit.PER_TOKEN_USD,
        meters_cost=True,
        supports_backend_routing=True,
    ),
    ProviderKind.OPENAI: KindSpec(
        kind=ProviderKind.OPENAI,
        display_name="OpenAI",
        base_url="https://api.openai.com/v1",
        models_path="models",
        auth=AuthStyle.BEARER,
        pricing=PricingUnit.NONE,
    ),
    ProviderKind.ANTHROPIC: KindSpec(
        kind=ProviderKind.ANTHROPIC,
        display_name="Anthropic",
        base_url="https://api.anthropic.com/v1",
        models_path="models",
        auth=AuthStyle.X_API_KEY,
        pricing=PricingUnit.NONE,
        extra_headers={"anthropic-version": "2023-06-01"},
        supports_stream_usage=False,
        forwardable=False,
        unsupported_reason=(
            "the Anthropic Messages API is not OpenAI-compatible and this build "
            "has no adapter for it; add Anthropic models through an OpenRouter "
            "provider instead"
        ),
    ),
    ProviderKind.TOGETHER: KindSpec(
        kind=ProviderKind.TOGETHER,
        display_name="Together AI",
        base_url="https://api.together.xyz/v1",
        models_path="models",
        auth=AuthStyle.BEARER,
        pricing=PricingUnit.PER_MTOK_USD,
    ),
    ProviderKind.GROQ: KindSpec(
        kind=ProviderKind.GROQ,
        display_name="Groq",
        base_url="https://api.groq.com/openai/v1",
        models_path="models",
        auth=AuthStyle.BEARER,
        pricing=PricingUnit.NONE,
    ),
    ProviderKind.OLLAMA: KindSpec(
        kind=ProviderKind.OLLAMA,
        display_name="Ollama",
        base_url="http://localhost:11434/v1",
        models_path="models",
        auth=AuthStyle.NONE,
        pricing=PricingUnit.NONE,
        requires_key=False,
        # Ollama's OpenAI shim has rejected unknown stream fields historically.
        supports_stream_usage=False,
        # Native API, not the /v1 shim: pulling has no OpenAI equivalent. The
        # shim's own prefix is stripped before this is joined on.
        pull_path="api/pull",
    ),
    ProviderKind.CUSTOM: KindSpec(
        kind=ProviderKind.CUSTOM,
        display_name="Custom",
        base_url="",
        models_path="models",
        auth=AuthStyle.BEARER,
        pricing=PricingUnit.NONE,
        requires_key=False,
        requires_base_url=True,
        # Unknown upstream. Assume nothing beyond the OpenAI core.
        supports_stream_usage=False,
    ),
    ProviderKind.CHATGPT: KindSpec(
        kind=ProviderKind.CHATGPT,
        display_name="ChatGPT plan",
        base_url=CHATGPT_RESOURCE,
        models_path="models",
        auth=AuthStyle.BEARER,
        # Plan usage is not billed per token to derate.
        pricing=PricingUnit.NONE,
        requires_key=True,
        supports_stream_usage=False,
        chat_wire=ChatWire.RESPONSES,
        sign_in="chatgpt",
    ),
    ProviderKind.CLAUDE_CODE: KindSpec(
        kind=ProviderKind.CLAUDE_CODE,
        display_name="Anthropic plan (unofficial)",
        base_url="https://api.anthropic.com/v1",
        models_path="models",
        auth=AuthStyle.BEARER,
        pricing=PricingUnit.NONE,
        supports_stream_usage=False,
        chat_wire=ChatWire.ANTHROPIC_MESSAGES,
        sign_in="claude",
    ),
}


def spec_for(kind: ProviderKind) -> KindSpec:
    return _SPECS[ProviderKind(kind)]


def known_kinds() -> list[KindSpec]:
    return list(_SPECS.values())


def auth_headers(spec: KindSpec, key: str | None) -> dict[str, str]:
    """Headers carrying the resolved key. Built at request time, never stored."""
    headers = dict(spec.extra_headers)
    if not key:
        return headers
    if spec.auth is AuthStyle.BEARER:
        headers["Authorization"] = f"Bearer {key}"
    elif spec.auth is AuthStyle.X_API_KEY:
        headers["x-api-key"] = key
    return headers


def join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def native_base(base_url: str) -> str:
    """The server's own root, with the OpenAI-compatibility prefix removed.

    A provider's ``base_url`` addresses the OpenAI shim, because that is what
    every request in this system speaks. Pulling weights has no equivalent in
    that spec and lives on the server's native API one level up, so the shim
    segment is stripped rather than a second URL being configured -- one
    address for the box, and no way for the two to disagree about which machine
    is meant.
    """
    trimmed = base_url.rstrip("/")
    if trimmed.endswith("/v1"):
        return trimmed[: -len("/v1")]
    return trimmed
