"""Public JSON shapes for providers.

Everything the gateway hands to a client or the UI goes through here, and
every dict this module produces is checked against the redactor before it is
returned. ``api_key`` is present and is always ``"***"``: the field exists so
that a UI has something to render and no reason to build a reveal control.

``api_key_ref`` is shown, because it is a name. Showing it is how someone
fixes a provider that says its reference does not resolve.
"""

from __future__ import annotations

import json
from dataclasses import asdict

from ..contracts.providers import Provider, ProviderKind, ProviderModel
from .config import REDACTED
from .kinds import known_kinds
from .runtime import ProviderRuntime, utc_day
from .secrets import Redactor


def model_public_dict(model: ProviderModel) -> dict:
    return asdict(model)


def provider_public_dict(
    provider: Provider,
    runtime: ProviderRuntime,
    now: float,
    *,
    include_models: bool = True,
) -> dict:
    spend = runtime.spend.get(utc_day(now))
    payload: dict = {
        "provider_id": provider.provider_id,
        "kind": ProviderKind(provider.kind).value,
        "display_name": provider.display_name,
        "base_url": provider.base_url,
        "api_key_ref": provider.api_key_ref,
        "api_key": REDACTED,
        "enabled": provider.enabled,
        "priority": provider.priority,
        "healthy": runtime.healthy,
        "admitting": runtime.admitting(now),
        "admission_block": runtime.admission_block(now),
        "last_error": runtime.last_error,
        "last_refreshed": runtime.last_refreshed,
        "model_count": len(provider.models),
        "daily_budget_usd": runtime.daily_budget_usd,
        "spend_today_usd": round(runtime.spend_today(now), 6),
        "tokens_today": {
            "input": spend.input_tokens if spend else 0,
            "output": spend.output_tokens if spend else 0,
        },
        "requests_today": spend.requests if spend else 0,
        "unpriced_requests_today": spend.unpriced_requests if spend else 0,
        "retry_in_s": round(runtime.retry_in(now), 3),
        "aliases": dict(runtime.aliases),
    }
    if include_models:
        payload["models"] = [model_public_dict(m) for m in provider.models]
    return payload


def kinds_public() -> list[dict]:
    """What the UI needs to render an add-provider form with sane defaults."""
    out = []
    for spec in known_kinds():
        out.append(
            {
                "kind": spec.kind.value,
                "display_name": spec.display_name,
                "base_url": spec.base_url,
                "requires_key": spec.requires_key,
                "requires_base_url": spec.requires_base_url,
                "publishes_pricing": spec.pricing.value != "none",
                "forwardable": spec.forwardable,
                "unsupported_reason": spec.unsupported_reason or None,
            }
        )
    return out


def checked_dump(payload: object, redactor: Redactor, where: str) -> str:
    """Serialize, then refuse to hand back anything containing key material."""
    text = json.dumps(payload, default=str)
    redactor.assert_clean(text, where)
    return text


def assert_no_key_material(payload: object, redactor: Redactor, where: str) -> None:
    redactor.assert_clean(json.dumps(payload, default=str), where)
