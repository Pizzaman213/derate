"""Gateway tunables. Everything here has a defensible default so that
``create_app()`` with no arguments produces a working gateway.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class GatewaySettings:
    # --- server ---
    host: str = "0.0.0.0"
    port: int = 8080
    cluster_id: str = "c-local"
    coordinator_node_id: str | None = None
    # Directory containing the built UI (an index.html plus its assets). When
    # set and the directory exists, create_app() serves it at "/" so the
    # gateway alone is a complete deployable: no separate static host. None
    # (the default) leaves "/" unmounted, which is what every existing test
    # and the day-0 stub gateway expects.
    ui_dir: str | None = field(
        default_factory=lambda: os.environ.get("DERATE_UI_DIR")
    )
    # Origins a browser may call this gateway from, comma separated. Empty (the
    # default) installs no CORS middleware at all, which is the deployed shape:
    # the coordinator serves the UI itself, so the only browser that talks to it
    # is same-origin and needs no permission. It is only when the UI is pointed
    # at a coordinator OTHER than the one that served it -- Settings ->
    # Coordinator, or `npm run dev` against a remote box -- that the browser
    # starts asking, and then the answer has to be given here by name. This is
    # not a formality to wave through with "*": every /api route is unauthenticated
    # cluster control, so an origin listed here can stop deployments and remove
    # nodes from any page a person happens to have open.
    allowed_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            origin.strip()
            for origin in os.environ.get("DERATE_ALLOWED_ORIGINS", "").split(",")
            if origin.strip()
        )
    )

    # --- upstream proxying ---
    # Connect fast, read forever. A decode that takes ten minutes is not an
    # error, so there is deliberately no read timeout on the response body.
    upstream_connect_timeout_s: float = 5.0
    upstream_read_timeout_s: float | None = None
    upstream_pool_limit: int = 256
    # Per-origin ceiling. One pool shared by every target means one slow or
    # leaking backend can consume every slot and take the others down with it:
    # on 2026-09-08 a 30B model's abandoned streams filled all 256 and a 0.5B
    # that had leaked nothing stopped answering 65 ms later. A client per origin
    # keeps that blast radius to the backend that earned it.
    upstream_per_origin_limit: int = 64
    # How long an origin's client may go unused before it is closed. Every
    # launch takes a fresh port, so without this the client map grows for the
    # life of the process.
    upstream_client_idle_s: float = 300.0
    # How often the proxy looks for upstream responses nothing will ever close
    # and for idle origin clients. Nothing here is urgent -- the grace period it
    # enforces is measured in tens of seconds -- so this is deliberately slow.
    upstream_janitor_interval_s: float = 5.0

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
    # Operator-set, persisted to $DERATE_DATA_DIR/settings.json and applied
    # through the existing `admitting` gate rather than a new mechanism.
    # local_only is a HARD block, not a routing preference: policies.eligible()
    # filters on admitting before any of the seven selectors run, so it outranks
    # them all -- whereas local_first is merely a policy.
    local_only: bool = False
    # None means no cap; 0.0 means spend nothing. Two different instructions,
    # and a form whose empty field yields 0.0 would silently swap them.
    daily_spend_cap_usd: float | None = None
    # A deployment that crashes (fatal runtime marker, exited container, a
    # health probe that stops answering) relaunches itself, up to
    # MAX_RESTART_ATTEMPTS with backoff (restart.py). Never fires for an
    # operator-requested stop -- manager.py tags that case with its own
    # "stopped during launch" reason, which the restart coordinator excludes.
    # Defaults on: a crashed model nobody is watching should come back on its
    # own rather than sit FAILED until someone happens to notice.
    auto_restart_crashed_deployments: bool = True

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

    # --- failover ---
    # A node that dies mid-request is resent to another target serving the same
    # model. Two attempts, not more: the second one is the answer to "that node
    # is gone", and a third is only ever the answer to "this request is the
    # problem", which retrying cannot fix.
    failover_max_attempts: int = 2
    # Wall-clock ceiling on the whole chain, so a slow chain of dying nodes
    # cannot outlive a client's own timeout.
    #
    # It bounds the chain only BEFORE a response is committed. Once headers are
    # on the wire the budget is attempts, not seconds, and `_make_reopen` in
    # openai_api.py deliberately does not consult this. The reason is arithmetic:
    # a 30B's prefill is 76-130s, so this deadline expires long before its first
    # byte, and enforcing it there would refuse recovery for exactly the slow
    # requests that recovery is worth most to. A client that has already waited
    # two minutes wants the answer, not a 502 for punctuality.
    failover_deadline_s: float = 30.0
    # Race a second target when the first has produced no byte within this
    # long, and take whichever answers first. None disables it, which is the
    # default and the right one for most clusters: a hedge buys latency with a
    # whole extra inference, so it only pays between targets of comparable
    # strength -- replicas of one model on similar machines. `hedge_candidate`
    # refuses the rest, and refuses outright under LOCAL_FIRST, where sending
    # to the remote is a saturation decision rather than a speed one.
    hedge_after_s: float | None = None
    # Consecutive transport failures before the gateway benches a target on its
    # own account. Three, because the deploy manager needs two health polls
    # (~10s) to reach the same conclusion and one failure is a coincidence.
    breaker_failure_threshold: int = 3
    # How long a benched target stays out. Long enough for a vLLM restart to
    # get past its own startup, short enough that a recovered node is not
    # stranded for a noticeable fraction of a demo.
    breaker_cooldown_s: float = 30.0
    # Retries may not exceed this fraction of real traffic over the window
    # below. A deterministic 500 -- one the backend will give every node,
    # because the request is what it objects to -- would otherwise fan out
    # across the fleet at N times the cost, and does so hardest when the
    # cluster is already unwell.
    retry_budget_ratio: float = 0.1
    retry_budget_window_s: float = 10.0
    # ...but never fewer than this many, or a quiet cluster would get no
    # failover at all, which is when one node dying matters most.
    retry_budget_floor: int = 3
    # How long to hold the response line waiting for the first upstream byte.
    # Past this the status goes out and the attempt stops being retryable: a
    # long prefill is not a failure, and an intermediary in front of us may
    # have its own read timeout on the headers.
    upstream_header_hold_s: float = 10.0

    # --- audio ---
    # A transcription request carries an audio file, and unlike every other
    # body on this path it is read whole before anything is sent: the model
    # name lives in a form field that could be the last part in the stream, and
    # a target that fails still has to be retryable, which means the bytes must
    # still be in hand. So the cap is a real limit rather than a formality.
    # 25 MiB is what OpenAI accepts, which is the number a client will expect.
    max_audio_upload_bytes: int = 25 * 1024 * 1024

    # --- request body ---
    # A multimodal chat body can carry a base64 image or audio clip inline and
    # is not reliably smaller than an audio upload, so it gets the same cap
    # and the same reasoning: 25 MiB is what OpenAI accepts.
    max_json_body_bytes: int = 25 * 1024 * 1024

    # --- parking ---
    # When a model's only nodes have just died, hold the request this long for
    # one to come back rather than refusing it. Zero disables the lot. This is
    # bounded on purpose: the rule is "503 rather than queueing indefinitely",
    # and ten seconds is not indefinitely. A request refused by admission
    # control -- rate limited, draining, memory critical -- is never parked,
    # because that is a decision rather than an outage.
    park_grace_s: float = 10.0
    # Only park a model that was serving within this window. A model that never
    # had a live target is unknown or still launching, and both of those
    # already have an honest answer.
    park_eligible_memory_s: float = 60.0
    park_poll_interval_s: float = 0.25
    park_max_waiters: int = 256
    park_max_per_model: int = 64
    # A held request is held in memory. An enormous prompt is refused rather
    # than parked, because 256 copies of one is a different order of problem.
    park_max_body_bytes: int = 1024 * 1024

    # --- metrics ---
    metrics_interval_s: float = 1.0
    metrics_rate_window_s: float = 10.0
    metrics_queue_depth: int = 8
