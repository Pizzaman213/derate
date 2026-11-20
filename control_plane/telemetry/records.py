"""What gets recorded, and the seam every producer writes through.

Four kinds share one journal, because the collection protocol then needs one
cursor instead of four. Typing happens on the coordinator, in archive.py.

The sink is a Protocol with a no-op default. NodeAgent, UpstreamProxy and the
deployment manager must all still construct and unit-test on a machine with
telemetry switched off, so no producer may assume a real sink is present.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Protocol

# Journal row kinds. Producers code against these, not string literals.
KIND_SAMPLE = "sample"
KIND_REQUEST = "request"
KIND_EVENT = "event"
KIND_LOG = "log"
KIND_ACCESS = "access"

KINDS = (KIND_SAMPLE, KIND_REQUEST, KIND_EVENT, KIND_LOG, KIND_ACCESS)


@dataclass
class RequestRecord:
    """One completed request, from the gateway's point of view.

    This is the record that does not exist anywhere today: proxy.settle()
    computes tokens, duration, decode and TTFT, folds them into an EWMA, and
    the raw numbers are gone. Everything here is available at that one call.

    ``tokens`` is the completion count actually used for throughput. It comes
    from the upstream's own ``usage`` block when there is one, and falls back
    to counting SSE frames -- ``tokens_estimated`` says which.
    """

    request_id: str
    ts: float
    served_name: str
    node_id: str = ""

    # Where it went.
    target_id: str = ""
    target_kind: str = ""  # "local" | "remote"
    provider_id: str = ""
    deployment_id: str = ""
    policy: str = ""
    strength_source: str = ""  # measured | predicted | bandwidth | default

    # attempt_no is the index of the attempt this row describes. `attempts` is
    # how many had been made when this row was written, which for a failed
    # attempt is not the final total -- the authoritative count is
    # COUNT(*) GROUP BY request_id, since each attempt writes its own row.
    attempt_no: int = 0
    attempts: int = 1
    # "" | "transport" | "http_5xx" | "pool_exhausted". The last is kept apart
    # from "transport" on purpose: it means this gateway had no free upstream
    # connection, which is not evidence that the backend was unreachable.
    retry_reason: str = ""

    # How it ended.
    status: int | None = None
    error_code: str = ""  # our own error code, never the backend's body
    error_class: str = ""  # exception class name only

    # What it cost.
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tokens: int = 0
    tokens_estimated: bool = True
    cost_usd: float | None = None

    # How it felt.
    ttft_ms: float | None = None
    decode_ms: float | None = None
    duration_ms: float | None = None
    parked_ms: float | None = None

    # What it asked for.
    streaming: bool = False
    body_bytes: int = 0
    admission_code: str = ""
    kv_bytes: int = 0

    # -- Everything below was added with inbound auth (contracts/telemetry.py).
    # The rule: a value nobody reported is None, never 0, and where there is a
    # reason for the absence the matching *_error column carries a FieldError.

    # Who asked.
    key_id: str | None = None
    key_name: str | None = None
    org_id: str | None = None
    client_ip: str | None = None
    user_agent: str | None = None

    # What they asked for, and what answered.
    model_requested: str | None = None
    model_served: str | None = None
    attempt_chain: list[dict] | None = None
    fallback_used: bool = False
    fallback_reason: str | None = None
    finish_reason: str | None = None
    cancelled: bool = False
    limit_source: str | None = None

    # How the generation went.
    generation_ms: float | None = None
    tokens_per_s: float | None = None
    tpot_mean_ms: float | None = None
    tpot_p95_ms: float | None = None
    tail_ms: float | None = None
    reasoning_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    max_tokens_requested: int | None = None
    message_count: int | None = None
    context_bucket: str | None = None
    prefix_cache_hit_rate: float | None = None
    cache_error: str | None = None

    # Speculative decoding, attributed from the engine's counters.
    spec_num_drafts: int | None = None
    spec_draft_tokens: int | None = None
    spec_accepted_tokens: int | None = None
    spec_acceptance_rate: float | None = None
    spec_mean_accepted_len: float | None = None
    spec_accepted_per_position: list[int] | None = None
    spec_decode_error: str | None = None
    cost_basis: str | None = None

    # The peer's source port, beside client_ip (security audit, F8). None
    # when the transport did not report one.
    client_port: int | None = None

    def __post_init__(self) -> None:
        # Derived, so every producer gets it -- not only RequestTrace.record().
        if self.context_bucket is None:
            self.context_bucket = context_bucket(self.prompt_tokens)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


#: Column order of a request row. Must equal
#: ``control_plane.contracts.telemetry.REQUEST_RECORD_FIELDS`` (test_single_source).
REQUEST_RECORD_FIELDS = tuple(f.name for f in fields(RequestRecord))


@dataclass
class AccessRecord:
    """One HTTP request the gateway answered, or one gated socket on close.

    The read side of the record: who asked for what, from which address and
    port, and what they were told. ``query`` is the first 200 characters of
    the query string after the gateway's Redactor; ``who`` is
    ``session:<user>``, ``key:drt_<id>`` or ``-``.
    """

    request_id: str
    ts: float
    client_ip: str | None = None
    client_port: int | None = None
    who: str = "-"
    method: str = ""
    path: str = ""
    query: str = ""
    status: int | None = None
    duration_ms: float | None = None
    user_agent: str | None = None
    node_id: str = ""
    # The socket's own peer. Differs from client_ip only when that peer is a
    # proxy in DERATE_TRUSTED_PROXIES and X-Forwarded-For named the client.
    peer_ip: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


#: Column order of an ``access`` archive row.
ACCESS_RECORD_FIELDS = tuple(f.name for f in fields(AccessRecord))

#: Lower edges of the context buckets; an edge belongs to the upper bucket.
_CONTEXT_EDGES = ((131072, "128k+"), (32768, "32-128k"), (8192, "8-32k"))


def context_bucket(prompt_tokens: int | None) -> str | None:
    """The ContextBucket for a prompt size. None when the size is unknown.

    0 counts as unknown: the trace defaults prompt_tokens to 0 when neither the
    upstream nor admission produced a number, and "0-8k" would then claim a
    fact nobody measured.
    """
    if not prompt_tokens or prompt_tokens < 0:
        return None
    for edge, name in _CONTEXT_EDGES:
        if prompt_tokens >= edge:
            return name
    return "0-8k"


@dataclass
class RequestTrace:
    """Mutable accumulator for one client request.

    Created once in openai_api._proxy and threaded through the dispatch loop,
    because the facts a record needs are spread across it: the model comes
    from the body, the policy and strength from the Selection, prompt tokens
    and kv_bytes from the AdmissionDecision, and the timings only exist inside
    proxy.settle(). One object collects them rather than six parameters.
    """

    request_id: str
    served_name: str
    started_at: float = field(default_factory=time.time)
    node_id: str = ""
    strength_source: str = ""
    streaming: bool = False
    body_bytes: int = 0
    attempts: int = 0
    parked_ms: float | None = None
    admission_code: str = ""
    kv_bytes: int = 0
    prompt_tokens: int = 0
    error_code: str = ""

    # Identity and request shape, set by the gateway once the principal and
    # body are known. None until then: a row without a principal says so.
    key_id: str | None = None
    key_name: str | None = None
    org_id: str | None = None
    client_ip: str | None = None
    client_port: int | None = None
    user_agent: str | None = None
    model_requested: str | None = None
    max_tokens_requested: int | None = None
    message_count: int | None = None
    # Request-level outcomes the limiter decides, the same for every attempt.
    limit_source: str | None = None
    fallback_used: bool = False
    fallback_reason: str | None = None

    def record(
        self,
        *,
        selection: Any = None,
        attempt_no: int = 0,
        retry_reason: str = "",
        status: int | None = None,
        error_class: str = "",
        tokens: int = 0,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        tokens_estimated: bool = True,
        ttft_s: float | None = None,
        decode_s: float | None = None,
        duration_s: float | None = None,
        cost_usd: float | None = None,
        cost_basis: str | None = None,
        model_served: str | None = None,
        attempt_chain: list[dict] | None = None,
        finish_reason: str | None = None,
        cancelled: bool = False,
        generation_s: float | None = None,
        tokens_per_s: float | None = None,
        tpot_mean_ms: float | None = None,
        tpot_p95_ms: float | None = None,
        tail_ms: float | None = None,
        reasoning_tokens: int | None = None,
        cache_read_tokens: int | None = None,
        cache_write_tokens: int | None = None,
        cache_error: str | None = None,
        spec: Any = None,
    ) -> RequestRecord:
        """Fold one attempt's outcome into a finished record.

        Derived here so no caller re-derives them: ``context_bucket`` from the
        prompt size, ``prefix_cache_hit_rate`` from cache reads over prompt
        (``cache_error='not_reported'`` when the upstream gave no cache count),
        ``tokens_per_s`` from completion tokens over ``generation_s`` when not
        given, and the ``spec_*`` columns from ``spec`` (see spec_fields).
        """
        target_id = target_kind = provider_id = deployment_id = policy = ""
        node_id = self.node_id
        if selection is not None:
            target = getattr(selection, "target", None)
            if target is not None:
                target_id = getattr(target, "target_id", "") or ""
                kind = getattr(target, "kind", None)
                target_kind = getattr(kind, "value", kind) or ""
            config = getattr(selection, "config", None)
            if config is not None:
                pol = getattr(config, "policy", None)
                policy = getattr(pol, "value", pol) or ""
            provider = getattr(selection, "provider", None)
            if provider is not None:
                provider_id = getattr(provider, "provider_id", "") or ""
            deployment = getattr(selection, "deployment", None)
            if deployment is not None:
                deployment_id = getattr(deployment, "deployment_id", "") or ""
                # The node that served a local target; RouteTarget carries no
                # node list, the deployment's plan does. Remote targets keep
                # the coordinator.
                plan = getattr(deployment, "plan", None)
                ids = list(getattr(plan, "node_ids", None) or [])
                if ids:
                    node_id = ids[0]

        completion = completion_tokens if completion_tokens is not None else tokens
        prompt = prompt_tokens if prompt_tokens is not None else self.prompt_tokens

        if cache_read_tokens is not None and prompt and prompt > 0:
            hit_rate: float | None = cache_read_tokens / prompt
        else:
            hit_rate = None
        if cache_error is None and cache_read_tokens is None:
            cache_error = "not_reported"

        generation_ms = None if generation_s is None else generation_s * 1000.0
        if tokens_per_s is None and generation_s and generation_s > 0 and completion:
            tokens_per_s = completion / generation_s

        return RequestRecord(
            request_id=self.request_id,
            ts=self.started_at,
            served_name=self.served_name,
            node_id=node_id,
            target_id=target_id,
            target_kind=target_kind,
            provider_id=provider_id,
            deployment_id=deployment_id,
            policy=policy,
            strength_source=self.strength_source,
            attempt_no=attempt_no,
            attempts=max(self.attempts, attempt_no + 1),
            retry_reason=retry_reason,
            status=status,
            error_code=self.error_code,
            error_class=error_class,
            prompt_tokens=prompt,
            completion_tokens=completion,
            tokens=tokens,
            tokens_estimated=tokens_estimated,
            cost_usd=cost_usd,
            ttft_ms=None if ttft_s is None else ttft_s * 1000.0,
            decode_ms=None if decode_s is None else decode_s * 1000.0,
            duration_ms=None if duration_s is None else duration_s * 1000.0,
            parked_ms=self.parked_ms,
            streaming=self.streaming,
            body_bytes=self.body_bytes,
            admission_code=self.admission_code,
            kv_bytes=self.kv_bytes,
            key_id=self.key_id,
            key_name=self.key_name,
            org_id=self.org_id,
            client_ip=self.client_ip,
            client_port=self.client_port,
            user_agent=self.user_agent,
            model_requested=self.model_requested,
            model_served=model_served if model_served is not None else self.served_name,
            attempt_chain=attempt_chain,
            fallback_used=self.fallback_used,
            fallback_reason=self.fallback_reason,
            finish_reason=finish_reason,
            cancelled=cancelled,
            limit_source=self.limit_source,
            generation_ms=generation_ms,
            tokens_per_s=tokens_per_s,
            tpot_mean_ms=tpot_mean_ms,
            tpot_p95_ms=tpot_p95_ms,
            tail_ms=tail_ms,
            reasoning_tokens=reasoning_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            max_tokens_requested=self.max_tokens_requested,
            message_count=self.message_count,
            context_bucket=context_bucket(prompt),
            prefix_cache_hit_rate=hit_rate,
            cache_error=cache_error,
            cost_basis=cost_basis,
            **spec_fields(spec),
        )


def spec_fields(spec: Any) -> dict[str, Any]:
    """The seven ``spec_*`` columns for a sample, an error code, or nothing.

    ``spec`` is an ``engine_metrics.SpecSample`` (duck-typed: ``num_drafts``,
    ``draft_tokens``, ``accepted_tokens``, ``accepted_per_position``), a
    FieldError / its string value, or None -- which is ``not_reported``,
    because no attribution was attempted.
    """
    out: dict[str, Any] = {
        "spec_num_drafts": None,
        "spec_draft_tokens": None,
        "spec_accepted_tokens": None,
        "spec_acceptance_rate": None,
        "spec_mean_accepted_len": None,
        "spec_accepted_per_position": None,
        "spec_decode_error": None,
    }
    if spec is None:
        out["spec_decode_error"] = "not_reported"
        return out
    if isinstance(spec, str):  # FieldError is a str Enum
        out["spec_decode_error"] = getattr(spec, "value", spec)
        return out
    drafts = int(spec.num_drafts)
    draft_tokens = int(spec.draft_tokens)
    accepted = int(spec.accepted_tokens)
    out["spec_num_drafts"] = drafts
    out["spec_draft_tokens"] = draft_tokens
    out["spec_accepted_tokens"] = accepted
    out["spec_acceptance_rate"] = accepted / draft_tokens if draft_tokens else None
    # vLLM's definition: 1 + accepted/drafts (each draft yields one bonus token).
    out["spec_mean_accepted_len"] = 1.0 + accepted / drafts if drafts else None
    per_pos = getattr(spec, "accepted_per_position", None)
    out["spec_accepted_per_position"] = list(per_pos) if per_pos is not None else None
    return out


class TelemetrySink(Protocol):
    """What a producer needs. Nothing more.

    Every method must return promptly and must never raise: the callers are a
    1 Hz sampler, a request path, an event bus and a logging handler, and none
    of them can afford to wait on a disk or to grow a new failure mode.
    """

    def sample(self, node_id: str, sample: Any) -> None: ...

    def request(self, record: RequestRecord) -> None: ...

    def event(self, source: str, event: dict[str, Any]) -> None: ...

    def log(self, entry: dict[str, Any]) -> None: ...

    def access(self, record: AccessRecord) -> None: ...


class NullSink:
    """The default. Records nothing, costs a method call."""

    enabled = False

    def sample(self, node_id: str, sample: Any) -> None:
        return None

    def request(self, record: RequestRecord) -> None:
        return None

    def event(self, source: str, event: dict[str, Any]) -> None:
        return None

    def log(self, entry: dict[str, Any]) -> None:
        return None

    def access(self, record: AccessRecord) -> None:
        return None


NULL_SINK = NullSink()


class RecordsOnlySink:
    """What ``DERATE_TELEMETRY=0`` still records: request and access rows,
    and the agents' and shells' own events.

    Attribution is evidence, not telemetry, so switching metrics off must not
    switch off the only key -> address -> model record (F10), nor a node
    agent's record of what it did and refused, and for whom (EV-2). Samples,
    log lines and every other event are dropped as before.
    """

    enabled = False

    def __init__(self, journal: Any) -> None:
        self._journal = journal

    def sample(self, node_id: str, sample: Any) -> None:
        return None

    def request(self, record: RequestRecord) -> None:
        self._journal.request(record)

    #: Event sources that are evidence rather than telemetry.
    EVIDENCE_SOURCES = frozenset({"agent", "shell"})

    def event(self, source: str, event: dict[str, Any]) -> None:
        if source in self.EVIDENCE_SOURCES:
            self._journal.event(source, event)

    def log(self, entry: dict[str, Any]) -> None:
        return None

    def access(self, record: AccessRecord) -> None:
        self._journal.access(record)
