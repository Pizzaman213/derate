"""Gateway tunables. Everything here has a defensible default so that
``create_app()`` with no arguments produces a working gateway.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GatewaySettings:
    # --- server ---
    host: str = "0.0.0.0"
    port: int = 8080
    cluster_id: str = "c-local"
    coordinator_node_id: str | None = None

    # --- upstream proxying ---
    # Connect fast, read forever. A decode that takes ten minutes is not an
    # error, so there is deliberately no read timeout on the response body.
    upstream_connect_timeout_s: float = 5.0
    upstream_read_timeout_s: float | None = None
    upstream_pool_limit: int = 256

    # --- startup ---
    # Startup must not block on a slow or unreachable node. Every step below
    # is bounded and a failure degrades rather than aborts.
    startup_step_timeout_s: float = 5.0

    # --- routing ---
    weight_refresh_interval_s: float = 60.0
    # How long a built target index is reused. Outstanding counts and the
    # admitting flag are refreshed per request regardless, so a critical
    # memory event takes effect immediately rather than after this window.
    index_ttl_s: float = 1.0
    measured_strength_min_requests: int = 100
    weak_target_floor: float = 0.15
    auto_weighted_spread: float = 0.25
    remote_default_strength: float = 1.0
    sticky_ttl_s: int = 0

    # --- cost ---
    # Local targets price from measured power draw against this rate.
    # Zero by default, which makes local free and therefore always cheapest.
    electricity_rate_usd_per_kwh: float = 0.0

    # --- admission control ---
    admission_reconcile_interval_s: float = 0.5
    critical_memory_pct: float = 0.95
    kv_budget_fraction: float = 1.0
    assumed_max_tokens: int = 512
    chars_per_token: float = 4.0
    tokens_per_message_overhead: int = 4
    retry_after_default_s: int = 2
    # Deployment does not carry a KV dtype, so admission assumes the runtime
    # default. Agent D's kv_bytes_per_token supersedes this when available.
    default_kv_dtype: str = "fp16"

    # --- metrics ---
    metrics_interval_s: float = 1.0
    metrics_rate_window_s: float = 10.0
    metrics_queue_depth: int = 8
