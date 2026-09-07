"""What we know about each provider kind before anyone configures anything.

Adding OpenRouter should be a display name and a key reference. Everything
else in this table is the reason it can be.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..contracts.providers import ProviderKind


class AuthStyle(str, Enum):
    BEARER = "bearer"  # Authorization: Bearer <key>
    X_API_KEY = "x_api_key"  # x-api-key: <key>
    NONE = "none"  # a box on the LAN with no auth


class PricingUnit(str, Enum):
    NONE = "none"  # provider publishes no pricing; costs stay None
    PER_TOKEN_USD = "per_token_usd"  # OpenRouter: dollars per single token
    PER_MTOK_USD = "per_mtok_usd"  # Together: dollars per million tokens


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
    requires_key: bool = True
    requires_base_url: bool = False
    # OpenAI's stream_options.include_usage. Only set it where the upstream is
    # known to accept it; an unexpected field is a 400 on a strict server.
    supports_stream_usage: bool = True
    # False means we have no adapter for this wire format in this build.
    forwardable: bool = True
    unsupported_reason: str = ""


_SPECS: dict[ProviderKind, KindSpec] = {
    ProviderKind.OPENROUTER: KindSpec(
        kind=ProviderKind.OPENROUTER,
        display_name="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        models_path="models",
        auth=AuthStyle.BEARER,
        pricing=PricingUnit.PER_TOKEN_USD,
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
