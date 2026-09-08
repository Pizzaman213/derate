"""What one model looks like once the five sources are folded together.

This is the server-side twin of ``ui/src/tabs/models/rows.ts``'s ``ModelRow``,
minus the fit fields. Those stay on ``/api/capacity`` because a verdict is an
answer to a *question* -- (model, context, concurrency, node set) -- and a row
here that carried one would be asserting a verdict nobody asked for. The UI
still joins them on, exactly as it does today.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Every facet a model can carry, in the order they are canonicalised into.
#: Local, polled, structured facts before a search payload.
#:
#: ``hub`` is listed here so the ordering lives in one place, but this process
#: never emits it: HuggingFace search is keystroke-driven and stays its own
#: endpoint, so the browser adds that facet when it folds search hits onto
#: these rows.
FACET_ORDER: tuple[str, ...] = (
    "running",
    "ondisk",
    "catalog",
    "provider",
    "offered",
    "hub",
)

#: What a refresh of the live sources can produce.
SERVER_FACETS = frozenset(FACET_ORDER) - {"hub"}


@dataclass
class RowDeployment:
    """One deployment of this model.

    Plural on the row, because a repository can be served twice under two
    names and collapsing that to one scalar loses the second.
    """

    deployment_id: str
    served_name: str
    state: str
    runtime: str
    node_ids: list[str] = field(default_factory=list)
    last_error: str | None = None


@dataclass
class RowProvider:
    """One provider's facts about one model.

    ``served`` is the allowlist answer: True when the provider is configured
    to serve it, False when the provider merely publishes it. Read off the
    records and nothing derived -- and, deliberately, nothing key-shaped.
    ``api_key_ref`` is a name, not a value, and it is not carried here either.
    """

    provider_id: str
    display_name: str
    served_name: str
    upstream_id: str
    served: bool
    #: Whether the provider itself is switched on. Not the same question as
    #: ``served``: the allowlist and ``provider.enabled`` are filtered in two
    #: different places, so an allowlisted model on a disabled provider is
    #: listed today while nothing routes to it.
    provider_enabled: bool | None = None
    context_length: int | None = None
    modality: str | None = None
    input_cost_per_mtok: float | None = None
    output_cost_per_mtok: float | None = None
    supports_tools: bool | None = None
    supports_streaming: bool | None = None
    healthy: bool | None = None
    last_error: str | None = None
    admitting: bool | None = None
    admission_block: str | None = None


@dataclass
class RowCache:
    """One node's copy of this model's weights."""

    node_id: str
    folder: str
    bytes: int
    blob_count: int | None = None


@dataclass
class ModelRecord:
    """One row of the registry: everything known about one model id.

    Everything *these sources* know, which is narrower than what the screen
    draws. There is no ``total_params``, ``native_dtype``, ``downloads``,
    ``likes`` or ``tags`` here: the first two are produced by the fit gate's
    capacity walk and the rest by a HuggingFace search, and neither is one of
    the five things being folded. The browser joins them on afterwards, as it
    does today.
    """

    model_id: str
    label: str
    facets: list[str] = field(default_factory=list)
    served_names: list[str] = field(default_factory=list)
    detail: str = ""
    default_context: int | None = None
    default_concurrency: int | None = None
    deployments: list[RowDeployment] = field(default_factory=list)
    providers: list[RowProvider] = field(default_factory=list)
    cached: list[RowCache] = field(default_factory=list)
    observed_at: float = 0.0

    @property
    def bytes_on_disk(self) -> int | None:
        """The largest figure any node reports, never the sum.

        Two node records can share one physical cache -- this cluster
        registers a node and a probe worker on the same host, both reporting
        the same repositories -- and summing would claim double the size for
        a single download. The question this answers is "how big is this
        model", which is a per-repository fact; how many nodes hold it is
        :attr:`cached_on`, carried separately.
        """
        if not self.cached:
            return None
        return max(c.bytes for c in self.cached)

    @property
    def cached_on(self) -> list[str]:
        return sorted({c.node_id for c in self.cached})


@dataclass
class CacheScan:
    """Whether one node's weight cache could be read at all.

    ``available=False`` with a reason is a different answer from a node
    holding nothing, and the two must never render alike.
    """

    node_id: str
    available: bool
    reason: str | None
    #: Last successful read. ``None`` means this node's cache has never been
    #: read at all, which is a different answer from a node holding nothing
    #: and must never render as one.
    observed_at: float | None
    #: Last attempt, successful or not. Moves even when ``observed_at`` does
    #: not, so a screen can say "still asking, last answer 40 minutes ago".
    attempted_at: float
