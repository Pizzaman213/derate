"""Persistence for provider records.

``/data/providers.json`` holds references, never values. The model list is
persisted with each record so a cold start does not depend on the network
being reachable.

Every write is checked against the redactor before it touches disk. A key in
this file would survive a restart, which is the difference between a mistake
and an incident.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from ..contracts.providers import Provider, ProviderKind, ProviderModel
from .config import PROVIDERS_FILE, data_dir
from .runtime import DaySpend, ProviderRuntime
from .secrets import Redactor

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


def _model_to_dict(model: ProviderModel) -> dict:
    return asdict(model)


def _model_from_dict(raw: dict) -> ProviderModel:
    return ProviderModel(
        served_name=str(raw.get("served_name", "")),
        upstream_id=str(raw.get("upstream_id", "")),
        context_length=int(raw.get("context_length") or 0),
        supports_streaming=bool(raw.get("supports_streaming", True)),
        supports_tools=bool(raw.get("supports_tools", False)),
        input_cost_per_mtok=_opt_float(raw.get("input_cost_per_mtok")),
        output_cost_per_mtok=_opt_float(raw.get("output_cost_per_mtok")),
    )


def _opt_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def provider_to_dict(provider: Provider, runtime: ProviderRuntime | None = None) -> dict:
    """The persisted shape. ``api_key_ref`` is a name; there is no key field at all."""
    record = {
        "provider_id": provider.provider_id,
        "kind": ProviderKind(provider.kind).value,
        "display_name": provider.display_name,
        "base_url": provider.base_url,
        "api_key_ref": provider.api_key_ref,
        "enabled": bool(provider.enabled),
        "priority": int(provider.priority),
        "healthy": bool(provider.healthy),
        "last_error": provider.last_error,
        "last_refreshed": float(provider.last_refreshed),
        "models": [_model_to_dict(m) for m in provider.models],
    }
    if runtime is not None:
        record["daily_budget_usd"] = runtime.daily_budget_usd
        record["aliases"] = dict(runtime.aliases)
        record["spend"] = {day: asdict(s) for day, s in runtime.spend.items()}
    return record


def provider_from_dict(raw: dict) -> tuple[Provider, ProviderRuntime]:
    try:
        kind = ProviderKind(raw.get("kind", "custom"))
    except ValueError:
        kind = ProviderKind.CUSTOM
    provider = Provider(
        provider_id=str(raw["provider_id"]),
        kind=kind,
        display_name=str(raw.get("display_name") or raw["provider_id"]),
        base_url=str(raw.get("base_url", "")),
        api_key_ref=str(raw.get("api_key_ref", "")),
        enabled=bool(raw.get("enabled", True)),
        priority=int(raw.get("priority", 100)),
        models=[_model_from_dict(m) for m in raw.get("models", []) if isinstance(m, dict)],
        healthy=bool(raw.get("healthy", True)),
        last_error=raw.get("last_error"),
        last_refreshed=float(raw.get("last_refreshed") or 0.0),
    )
    # Health and last_error are written to the file for a human reading it,
    # but they are not restored. They describe our last interaction with the
    # upstream, and after a restart there has not been one. Restoring
    # "unhealthy" would leave the provider not admitting, which means no
    # request reaches it, which means nothing ever clears it. The durable
    # condition -- an unresolvable key reference -- is re-derived on load.
    provider.healthy = True
    provider.last_error = None
    runtime = ProviderRuntime(
        provider_id=provider.provider_id,
        healthy=True,
        last_error=None,
        last_refreshed=provider.last_refreshed,
        daily_budget_usd=_opt_float(raw.get("daily_budget_usd")),
        aliases={str(k): str(v) for k, v in (raw.get("aliases") or {}).items()},
    )
    for day, spend in (raw.get("spend") or {}).items():
        if isinstance(spend, dict):
            runtime.spend[str(day)] = DaySpend(
                usd=float(spend.get("usd") or 0.0),
                input_tokens=int(spend.get("input_tokens") or 0),
                output_tokens=int(spend.get("output_tokens") or 0),
                requests=int(spend.get("requests") or 0),
                unpriced_requests=int(spend.get("unpriced_requests") or 0),
            )
    return provider, runtime


class ProviderStore:
    """Reads and writes ``providers.json``. Atomic, and never with a key in it."""

    def __init__(self, path: Path | None = None, *, redactor: Redactor | None = None) -> None:
        self.path = Path(path) if path is not None else data_dir() / PROVIDERS_FILE
        self.redactor = redactor or Redactor()

    def load(self) -> list[tuple[Provider, ProviderRuntime]]:
        """Load persisted providers. A corrupt file is not a reason to fail startup."""
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text("utf-8") or "{}")
        except (OSError, json.JSONDecodeError) as exc:
            log.error("could not read %s (%s); starting with no providers", self.path, exc)
            return []
        records = raw.get("providers") if isinstance(raw, dict) else raw
        if not isinstance(records, list):
            return []
        loaded: list[tuple[Provider, ProviderRuntime]] = []
        for record in records:
            if not isinstance(record, dict) or "provider_id" not in record:
                continue
            try:
                loaded.append(provider_from_dict(record))
            except (KeyError, TypeError, ValueError) as exc:
                log.error("skipping malformed provider record: %s", type(exc).__name__)
        return loaded

    def save(self, entries: list[tuple[Provider, ProviderRuntime]]) -> None:
        payload = {
            "version": SCHEMA_VERSION,
            "providers": [provider_to_dict(p, r) for p, r in entries],
        }
        text = json.dumps(payload, indent=2, sort_keys=True)
        # The guarantee, enforced rather than assumed.
        self.redactor.assert_clean(text, f"persisted record {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".providers-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
