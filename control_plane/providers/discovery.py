"""Turning an upstream model list into :class:`ProviderModel` records.

Normalize what the provider publishes. Do not fill in what it does not.
A cost we guessed is worse than a cost we left as None, because COST_AWARE
will happily route on a number nobody checked.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..contracts.modality import Modality
from ..contracts.providers import ProviderModel
from .kinds import KindSpec, PricingUnit

log = logging.getLogger(__name__)

# Context length is an int in the contract. Zero is the honest answer when the
# provider does not publish one, and reads as "unknown" everywhere downstream.
CONTEXT_UNKNOWN = 0

# What a provider's model id says it does. Matched on the upstream id, which is
# a heuristic, and is why every pattern here only ever moves a model *off* the
# TEXT default -- an id that says nothing stays text, which is what an unknown
# chat model is. Order matters: the first match wins, so the narrow families
# come before the broad ones ("whisper" before "speech", or every ASR model
# would classify as TTS).
#
# This table also drives the streaming flag it grew out of: a speech,
# transcription or embedding endpoint has no token stream to open.
_MODALITY_PATTERNS: tuple[tuple[re.Pattern[str], "Modality | None"], ...] = (
    (
        re.compile(r"(whisper|transcrib|speech[-_]?to[-_]?text|\bstt\b|\basr\b)", re.I),
        Modality.TRANSCRIPTION,
    ),
    (
        re.compile(r"(\btts\b|text[-_]?to[-_]?speech|\bvoice\b|speech)", re.I),
        Modality.SPEECH,
    ),
    (re.compile(r"(embed|embedding)", re.I), Modality.EMBEDDING),
    # Known non-streaming, but not an endpoint family this gateway routes.
    # Left as TEXT so nothing claims to serve them, while the streaming flag
    # is still cleared the way it always was.
    (re.compile(r"(rerank|moderation|dall-e|stable-diffusion|flux)", re.I), None),
)


def _modality(upstream_id: str) -> Modality:
    """Best guess at what this model answers on. TEXT when the id does not say."""
    for pattern, modality in _MODALITY_PATTERNS:
        if pattern.search(upstream_id):
            return modality or Modality.TEXT
    return Modality.TEXT


def _streamable(upstream_id: str) -> bool:
    """False for every family in the table, including the unrouted ones."""
    return not any(pattern.search(upstream_id) for pattern, _ in _MODALITY_PATTERNS)


def _entries(payload: Any) -> list[dict]:
    """Pull the list of models out of whatever shape the provider returned."""
    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, dict):
        for key in ("data", "models", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates = value
                break
        else:
            candidates = []
    else:
        candidates = []
    return [entry for entry in candidates if isinstance(entry, dict)]


def _upstream_id(entry: dict) -> str | None:
    for key in ("id", "model", "name", "model_name"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _context_length(entry: dict) -> int:
    for key in ("context_length", "context_window", "max_context_length", "context"):
        value = entry.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    # OpenRouter also carries it under top_provider, and that value is the one
    # that reflects the currently routed backend.
    top = entry.get("top_provider")
    if isinstance(top, dict):
        value = top.get("context_length")
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    config = entry.get("config")
    if isinstance(config, dict):
        value = config.get("context_length") or config.get("max_position_embeddings")
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    return CONTEXT_UNKNOWN


def _supports_tools(entry: dict) -> bool:
    """Only true when the provider says so. Unadvertised is not the same as absent."""
    params = entry.get("supported_parameters")
    if isinstance(params, list):
        return any(str(p).lower() in ("tools", "tool_choice", "functions") for p in params)
    for key in ("supports_tools", "supports_function_calling", "tool_use"):
        value = entry.get(key)
        if isinstance(value, bool):
            return value
    caps = entry.get("capabilities")
    if isinstance(caps, dict):
        for key in ("tools", "function_calling", "tool_use"):
            if isinstance(caps.get(key), bool):
                return bool(caps[key])
    if isinstance(caps, list):
        return any(str(c).lower() in ("tools", "function_calling", "tool_use") for c in caps)
    return False


def _supports_streaming(entry: dict, upstream_id: str) -> bool:
    value = entry.get("supports_streaming")
    if isinstance(value, bool):
        return value
    params = entry.get("supported_parameters")
    if isinstance(params, list) and params:
        # OpenRouter lists every accepted parameter; stream is implied for chat
        # models and absent for the rest.
        return _streamable(upstream_id)
    return _streamable(upstream_id)


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _pricing(entry: dict, unit: PricingUnit) -> tuple[float | None, float | None]:
    """Input and output cost per million tokens, or None where unpublished."""
    if unit is PricingUnit.NONE:
        return None, None
    pricing = entry.get("pricing")
    if not isinstance(pricing, dict):
        return None, None

    def pick(*keys: str) -> float | None:
        for key in keys:
            if key in pricing:
                value = _as_float(pricing[key])
                # OpenRouter uses -1 for "varies"; that is not a price.
                if value is not None and value >= 0:
                    return value
        return None

    raw_in = pick("prompt", "input", "input_tokens")
    raw_out = pick("completion", "output", "output_tokens")
    if unit is PricingUnit.PER_TOKEN_USD:
        scale = 1_000_000.0
    else:
        scale = 1.0
    return (
        None if raw_in is None else raw_in * scale,
        None if raw_out is None else raw_out * scale,
    )


def parse_models(
    payload: Any,
    spec: KindSpec,
    *,
    aliases: dict[str, str] | None = None,
) -> list[ProviderModel]:
    """Normalize a provider's model list.

    ``aliases`` maps an upstream id to the ``served_name`` clients use. The
    default is the upstream id unchanged: people already know
    ``anthropic/claude-sonnet-4.5`` and renaming it helps nobody. An alias is
    how a remote model deliberately shares a name with a local deployment,
    which is the spill mechanism, not a collision.
    """
    aliases = aliases or {}
    models: list[ProviderModel] = []
    seen: set[str] = set()
    for entry in _entries(payload):
        upstream_id = _upstream_id(entry)
        if upstream_id is None or upstream_id in seen:
            continue
        seen.add(upstream_id)
        input_cost, output_cost = _pricing(entry, spec.pricing)
        models.append(
            ProviderModel(
                served_name=aliases.get(upstream_id, upstream_id),
                upstream_id=upstream_id,
                context_length=_context_length(entry),
                supports_streaming=_supports_streaming(entry, upstream_id),
                supports_tools=_supports_tools(entry),
                input_cost_per_mtok=input_cost,
                output_cost_per_mtok=output_cost,
                modality=_modality(upstream_id),
            )
        )
    models.sort(key=lambda m: m.served_name)
    return models
