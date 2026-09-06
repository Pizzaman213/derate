"""What gets recorded, and the seam every producer writes through.

Four kinds share one journal, because the collection protocol then needs one
cursor instead of four. Typing happens on the coordinator, in archive.py.

The sink is a Protocol with a no-op default. NodeAgent, UpstreamProxy and the
deployment manager must all still construct and unit-test on a machine with
telemetry switched off, so no producer may assume a real sink is present.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

# Journal row kinds. Producers code against these, not string literals.
KIND_SAMPLE = "sample"
KIND_REQUEST = "request"
KIND_EVENT = "event"
KIND_LOG = "log"

KINDS = (KIND_SAMPLE, KIND_REQUEST, KIND_EVENT, KIND_LOG)


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
    retry_reason: str = ""  # "" | "transport" | "http_5xx"

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

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


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
    ) -> RequestRecord:
        """Fold one attempt's outcome into a finished record."""
        target_id = target_kind = provider_id = deployment_id = policy = ""
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

        completion = completion_tokens if completion_tokens is not None else tokens
        return RequestRecord(
            request_id=self.request_id,
            ts=self.started_at,
            served_name=self.served_name,
            node_id=self.node_id,
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
            prompt_tokens=(
                prompt_tokens if prompt_tokens is not None else self.prompt_tokens
            ),
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
        )


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


NULL_SINK = NullSink()
