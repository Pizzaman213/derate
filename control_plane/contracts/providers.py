"""Remote provider contracts. 00-architecture.md section 4.4.

Day-0 file. Transcribed from the architecture doc, not designed here.
api_key_ref holds a reference, never a value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .modality import Modality


class ProviderKind(str, Enum):
    OPENROUTER = "openrouter"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    TOGETHER = "together"
    GROQ = "groq"
    OLLAMA = "ollama"  # another box on the LAN, not ours to orchestrate
    CUSTOM = "custom"  # any OpenAI-compatible base_url


@dataclass
class ProviderModel:
    served_name: str  # what clients call it through us
    upstream_id: str  # what the provider calls it
    context_length: int
    supports_streaming: bool
    supports_tools: bool
    input_cost_per_mtok: float | None  # USD, None when unknown
    output_cost_per_mtok: float | None
    # Inferred from the upstream id by providers/discovery.py, which is a
    # heuristic -- so it only ever moves a model off the TEXT default when
    # the id says so plainly.
    modality: Modality = Modality.TEXT


@dataclass
class Provider:
    provider_id: str
    kind: ProviderKind
    display_name: str
    base_url: str
    api_key_ref: str  # env var name or secret key. NEVER the key itself.
    enabled: bool
    priority: int  # lower is preferred among remotes
    models: list[ProviderModel] = field(default_factory=list)
    healthy: bool = True
    last_error: str | None = None
    last_refreshed: float = 0.0
