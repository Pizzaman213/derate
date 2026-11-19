"""The per-request telemetry record's vocabulary.

``control_plane/telemetry/records.py::RequestRecord`` is the row; this is the
list of its column names and the closed vocabularies a few of them draw from,
so the UI, the archive schema and the usage API agree on one spelling.

The rule every optional column follows: a value nobody reported is ``None``,
never ``0``, and where there is a reason it is absent the matching ``*_error``
column carries a :class:`FieldError`. "0 cache hits" and "the runtime did not
say" are different facts and must not render the same.
"""

from __future__ import annotations

from enum import Enum


class ContextBucket(str, Enum):
    B0_8K = "0-8k"
    B8_32K = "8-32k"
    B32_128K = "32-128k"
    B128K_PLUS = "128k+"


class CostBasis(str, Enum):
    #: The upstream reported what it charged.
    METERED = "metered"
    #: Tokens times a configured or published rate.
    ESTIMATED = "estimated"
    #: No rate is known. Never counted toward a USD limit.
    UNPRICED = "unpriced"


class FieldError(str, Enum):
    NOT_REPORTED = "not_reported"
    #: Counters are per engine, so a delta is only attributable to one
    #: request when that request ran alone.
    AMBIGUOUS_CONCURRENCY = "ambiguous_concurrency"
    NOT_ENABLED = "not_enabled"


class FallbackReason(str, Enum):
    CLOUD_POOL_EXHAUSTED = "cloud_pool_exhausted"
    UPSTREAM_FAILED = "upstream_failed"


REQUEST_RECORD_FIELDS = (
    "request_id",
    "ts",
    "served_name",
    "node_id",
    "target_id",
    "target_kind",
    "provider_id",
    "deployment_id",
    "policy",
    "strength_source",
    "attempt_no",
    "attempts",
    "retry_reason",
    "status",
    "error_code",
    "error_class",
    "prompt_tokens",
    "completion_tokens",
    "tokens",
    "tokens_estimated",
    "cost_usd",
    "ttft_ms",
    "decode_ms",
    "duration_ms",
    "parked_ms",
    "streaming",
    "body_bytes",
    "admission_code",
    "kv_bytes",
    # Who asked.
    "key_id",
    "key_name",
    "org_id",
    "client_ip",
    "user_agent",
    # What they asked for, and what answered.
    "model_requested",
    "model_served",
    "attempt_chain",
    "fallback_used",
    "fallback_reason",
    "finish_reason",
    "cancelled",
    "limit_source",
    # How the generation went.
    "generation_ms",
    "tokens_per_s",
    "tpot_mean_ms",
    "tpot_p95_ms",
    "tail_ms",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "max_tokens_requested",
    "message_count",
    "context_bucket",
    "prefix_cache_hit_rate",
    "cache_error",
    # Speculative decoding, attributed from the engine's counters.
    "spec_num_drafts",
    "spec_draft_tokens",
    "spec_accepted_tokens",
    "spec_acceptance_rate",
    "spec_mean_accepted_len",
    "spec_accepted_per_position",
    "spec_decode_error",
    "cost_basis",
    "client_port",
)

USAGE_BUCKETS = ("1m", "1h", "1d")
USAGE_GROUP_BY = ("key_id", "org_id", "model", "node", "provider", "status", "context_bucket")
