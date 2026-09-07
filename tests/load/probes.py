"""The cascades.

A ladder finds where the gateway gets slower. These find where it gets wrong:
overload misdiagnosed as node death, commitments that never come back, retry
storms, memory held on behalf of clients that are gone. Each runs well below
the derated capacity, because none of them are about throughput.
"""

from __future__ import annotations

import time

import contextlib
import threading

from .harness import (
    Cluster,
    HarnessError,
    driver_count,
    merge,
    rps_jobs,
    run_drivers,
    stream_jobs,
)


def _defaults(cluster: Cluster) -> dict:
    return {
        "ttft_s": cluster.ttft,
        "itl_s": cluster.itl,
        "tokens": cluster.tokens,
        "error_every": 0,
        "hang_every": 0,
        "die_every": 0,
        "max_inflight": 0,
    }


@contextlib.contextmanager
def backend_config(cluster: Cluster, spec: dict):
    """Apply a backend configuration and always put it back.

    Every probe owns its full configuration rather than inheriting whatever
    the last one left behind. Without the restore being unconditional, a probe
    that raises hands its 8-second prefill to every probe after it, and their
    verdicts become statements about conditions nobody established.
    """
    cluster.configure_backends({**_defaults(cluster), **spec})
    try:
        yield
    finally:
        cluster.configure_backends(_defaults(cluster))


class CircuitWatcher:
    """Poll the breaker while load is running.

    Sampling circuits once after a run misses a breaker that opened and closed
    inside it, which is exactly what a 30 s cooldown against a 40 s run does.
    """

    def __init__(self, cluster: Cluster, interval: float = 0.5) -> None:
        self._cluster = cluster
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.seen: dict[str, set] = {}
        self.peak_parked = 0

    def _run(self) -> None:
        while not self._stop.is_set():
            snap = self._cluster.probe()
            for target, state in (snap.get("circuits") or {}).items():
                self.seen.setdefault(target, set()).add(state)
            self.peak_parked = max(self.peak_parked, snap.get("parked") or 0)
            self._stop.wait(self._interval)

    def __enter__(self) -> "CircuitWatcher":
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def opened(self) -> dict:
        return {t: sorted(states) for t, states in self.seen.items()}


def _gw(cluster: Cluster) -> str:
    return f"{cluster.gateway_url}/v1/chat/completions"


def settle(cluster: Cluster, timeout: float = 75.0) -> dict:
    """Wait for the gateway to be idle again before the next probe.

    Without this a probe inherits the previous one's backlog and measures it
    instead of its own effect -- which is exactly how a saturated gateway makes
    every later probe look like it "held" while in fact nothing ran.

    The budget has to outlast ``breaker_cooldown_s``: overload can bench every
    target at once, and while it does the gateway serves nothing at all. That
    is a real 30 s outage rather than a harness problem, so waiting it out is
    correct -- but it has to be waited out, not mistaken for readiness.
    """
    cluster.configure_backends(_defaults(cluster))
    deadline = time.time() + timeout
    snap: dict = {}
    while time.time() < deadline:
        # Short timeouts so a wedged gateway is polled repeatedly rather than
        # consuming the whole budget in one hanging call.
        snap = cluster.probe(reset=True, timeout=5.0)
        lag = (snap.get("loop_lag") or {}).get("p99_ms", 0.0)
        idle = not snap.get("outstanding_total") and not snap.get("parked")
        # Idle counters are not readiness. Only a request that comes back 200
        # proves the next probe is measuring its own conditions.
        if idle and lag < 25.0 and cluster.serves_ok(timeout=5.0):
            return snap
        time.sleep(1.0)
    raise HarnessError(
        f"gateway did not return to a serving state within {timeout:.0f}s "
        f"(outstanding={snap.get('outstanding_total')}, parked={snap.get('parked')}, "
        f"circuits={snap.get('circuits')})"
    )


def _finding(name: str, expected: str, observed: str, broke: bool, detail: dict) -> dict:
    return {
        "probe": name,
        "expected": expected,
        "observed": observed,
        "broke": broke,
        "detail": detail,
    }


def pool_exhaustion(cluster: Cluster, *, streams: int = 600, duration: float = 40.0) -> dict:
    """Overload past the connection pool, misread as the node dying.

    ``upstream_pool_limit`` is 256 and it is one pool shared by every target,
    while ``upstream_read_timeout_s`` is None so a slow decode holds its slot
    for as long as it likes. Past 256 concurrent upstream requests httpx raises
    PoolTimeout -- an httpx.HTTPError, which proxy.py cannot tell apart from a
    node that has gone away. Three in a row and the breaker benches a target
    that is perfectly healthy.
    """
    # Long prefill, almost no tokens: each request holds an upstream
    # connection for seconds while costing the gateway's loop nearly nothing.
    # Without that separation the loop saturates first and the pool is never
    # reached, which is a different finding.
    # 8 s of prefill against a 5 s pool-acquire timeout: with 256 slots and 600
    # requests, the 344 that cannot get a slot must wait longer than httpx is
    # willing to, which is what turns overload into a transport failure.
    with backend_config(cluster, {"ttft_s": 8.0, "itl_s": 0.0, "tokens": 2}):
        time.sleep(0.5)
        cluster.probe(reset=True)
        with CircuitWatcher(cluster) as watcher:
            result = merge(
                run_drivers(stream_jobs(_gw(cluster), streams, duration, 8))
            )
        time.sleep(6.0)
        after = cluster.probe()
        backends = cluster.backend_stats()

    circuits = watcher.opened
    late_circuits = after.get("circuits") or {}
    all_healthy = all(b.get("refused", 0) == 0 for b in backends)
    broke = bool(circuits or late_circuits) and all_healthy

    return _finding(
        "pool exhaustion misread as node death",
        "overload sheds load; the breaker stays closed while every backend is healthy",
        (
            f"breaker opened on {sorted(set(circuits) | set(late_circuits))} "
            "while every backend was healthy"
            if (circuits or late_circuits)
            else "breaker stayed closed"
        ),
        broke,
        {
            "streams": streams,
            "errors": result.get("errors", {}),
            "ok": result.get("ok", 0),
            "circuits_sampled_during": circuits,
            "circuits_after": late_circuits,
            "backend_peak_inflight": [b.get("peak_inflight") for b in backends],
            "backend_refused": [b.get("refused") for b in backends],
        },
    )


def disconnect_storm(cluster: Cluster, *, streams: int = 64, duration: float = 20.0) -> dict:
    """Audit H-1. Every stream hangs up three tokens in.

    The failure this guards against is not a slowdown: a leaked ``outstanding``
    never comes back, so least-outstanding routing inverts and the KV
    commitment is never released, ending in a permanent 429 that only a restart
    clears.
    """
    cluster.probe(reset=True)
    k = min(8, streams)
    result = merge(
        run_drivers(
            stream_jobs(
                _gw(cluster), streams, duration, k, abandon_every=1, abandon_after=3
            )
        )
    )
    time.sleep(8.0)
    after = cluster.probe()

    leaked = after.get("outstanding_total", 0)
    kv_429 = result.get("errors", {}).get("429", 0)
    return _finding(
        "client disconnect storm (H-1)",
        "every commitment released; outstanding returns to zero; no 429",
        f"{leaked} outstanding leaked, {kv_429} x 429",
        bool(leaked or kv_429),
        {
            "abandoned": result.get("abandoned", 0),
            "targets": after.get("targets", {}),
            "errors": result.get("errors", {}),
        },
    )


def error_storm(cluster: Cluster, *, rate: float = 200.0, duration: float = 15.0) -> dict:
    """Every upstream answers 500. The retry budget must cap the amplification.

    ``retry_budget_ratio`` is 0.1 over a 10 s window with a floor of 3, so a
    deterministic 500 -- one every node will give, because the request is what
    they object to -- must not fan out across the fleet.
    """
    before = sum(b.get("seq", 0) for b in cluster.backend_stats())
    with backend_config(
        cluster, {"error_every": 1, "ttft_s": 0.0, "itl_s": 0.0, "tokens": 2}
    ):
        cluster.probe(reset=True)
        k = driver_count(rate)
        result = merge(run_drivers(rps_jobs(_gw(cluster), rate, duration, k)))
        after_stats = cluster.backend_stats()

    upstream = sum(b.get("seq", 0) for b in after_stats) - before
    # Requests that actually got an answer. `sent` counts everything the driver
    # scheduled, including what it dropped because the gateway was behind, and
    # dividing by those would understate the fan-out rather than measure it.
    answered = result.get("ok", 0) + sum(
        n for key, n in result.get("errors", {}).items() if key != "timeout"
    )
    client = max(1, answered)
    amplification = upstream / client
    return _finding(
        "upstream 5xx storm",
        "retry budget caps upstream amplification at about 1.1x",
        f"{amplification:.2f}x ({upstream} upstream for {client} client requests)",
        amplification > 1.25,
        {
            "answered_requests": client,
            "scheduled_requests": result.get("sent", 0),
            "upstream_requests": upstream,
            "unanswered": result.get("errors", {}).get("timeout", 0),
            "errors": result.get("errors", {}),
        },
    )


def backend_death(cluster: Cluster, *, streams: int = 64, duration: float = 25.0) -> dict:
    """Kill every runtime mid-load. The parking lot is what catches it.

    ``park_max_waiters`` 256 x ``park_max_body_bytes`` 1 MiB is up to 256 MiB of
    request bodies held in the gateway's memory, each waking every 250 ms to
    ask whether its client is still there.
    """
    cluster.probe(reset=True)
    baseline_rss = cluster.probe().get("rss_bytes", 0)

    k = min(8, streams)
    jobs = stream_jobs(_gw(cluster), streams, duration, k)

    import threading

    box: dict = {}

    def drive() -> None:
        box["result"] = merge(run_drivers(jobs))

    thread = threading.Thread(target=drive, daemon=True)
    thread.start()

    time.sleep(duration * 0.35)
    for proc in cluster.backend_procs:
        proc.stop()
    peak_rss, peak_parked = baseline_rss, 0
    deadline = time.time() + 10.0
    while time.time() < deadline:
        snap = cluster.probe()
        peak_rss = max(peak_rss, snap.get("rss_bytes", 0))
        peak_parked = max(peak_parked, snap.get("parked", 0))
        time.sleep(0.25)

    for proc in cluster.backend_procs:
        proc.start()
    cluster._await([f"http://127.0.0.1:{p}/health" for p in cluster.backend_ports])
    # Restarted processes come up on their command-line defaults, not on
    # whatever the probe had set, so the fleet has to be put back explicitly.
    cluster.configure_backends(_defaults(cluster))
    thread.join(timeout=120)
    time.sleep(6.0)
    after = cluster.probe()

    result = box.get("result", {})
    if not result or not result.get("ok"):
        return _finding(
            "every backend killed mid-load",
            "requests park briefly, then 503; memory returns; nothing leaks",
            f"no stream ever completed ({result.get('errors', {}) if result else 'no result'})",
            None,
            {"errors": result.get("errors", {}) if result else {}},
        )
    leaked = after.get("outstanding_total", 0)
    growth_mb = (peak_rss - baseline_rss) / 1024**2
    return _finding(
        "every backend killed mid-load",
        "requests park briefly, then 503; memory returns; nothing leaks",
        f"parked up to {peak_parked}, RSS grew {growth_mb:.0f} MB, {leaked} outstanding leaked",
        bool(leaked) or growth_mb > 300,
        {
            "peak_parked": peak_parked,
            "baseline_rss_mb": round(baseline_rss / 1024**2, 1),
            "peak_rss_mb": round(peak_rss / 1024**2, 1),
            "errors": result.get("errors", {}),
            "recovered_ok": result.get("ok", 0),
        },
    )


def control_plane_interference(
    cluster: Cluster, *, streams: int = 32, duration: float = 20.0
) -> dict:
    """Audit M-14: sync port calls inside async handlers.

    With stubs these calls are cheap, so this establishes the baseline that
    makes the regression visible the moment real ports land -- where
    ``links.measure`` is seconds of nccl-tests on the one loop that is also
    carrying every token stream.
    """
    import threading

    import httpx

    k = min(8, streams)
    jobs = stream_jobs(_gw(cluster), streams, duration, k)

    cluster.configure_backends(_defaults(cluster))
    cluster.probe(reset=True)
    quiet = merge(run_drivers(jobs))

    stop = threading.Event()

    def hammer() -> None:
        with httpx.Client(timeout=20.0) as client:
            while not stop.is_set():
                for path in ("/api/topology", "/api/routing", "/api/deployments"):
                    try:
                        client.get(f"{cluster.gateway_url}{path}")
                    except Exception:
                        pass
                try:
                    client.post(
                        f"{cluster.gateway_url}/api/plan",
                        json={
                            "model_id": "meta-llama/Llama-3.3-70B-Instruct",
                            "context": 32768,
                            "concurrency": 8,
                            "target": "throughput",
                        },
                    )
                except Exception:
                    pass

    thread = threading.Thread(target=hammer, daemon=True)
    thread.start()
    cluster.probe(reset=True)
    noisy = merge(run_drivers(jobs))
    stop.set()
    thread.join(timeout=10)
    probe = cluster.probe()

    quiet_itl = quiet["itl"].pct(0.99) if quiet else 0.0
    noisy_itl = noisy["itl"].pct(0.99) if noisy else 0.0
    delta = noisy_itl - quiet_itl
    return _finding(
        "control-plane traffic alongside token streams (M-14)",
        "control-plane calls do not disturb in-flight streams",
        f"inter-token p99 {quiet_itl:.1f} ms quiet -> {noisy_itl:.1f} ms under /api load ({delta:+.1f} ms)",
        delta > max(5.0, quiet_itl * 0.5),
        {
            "quiet_itl_p99_ms": quiet_itl,
            "noisy_itl_p99_ms": noisy_itl,
            "loop_lag": probe.get("loop_lag"),
        },
    )


def index_rebuild(cluster: Cluster, *, rate: float = 50.0, duration: float = 20.0) -> dict:
    """``index_ttl_s`` is 1.0, so one request a second pays a full rebuild.

    ``build_index`` allocates a fresh RouteTarget per target and calls three
    ports synchronously on the request path. With stubs it is cheap; the shape
    to watch for is a tail that is far off the median at a rate nowhere near
    saturation.
    """
    # The rebuild is on the request path, so the request must be the only cost.
    with backend_config(cluster, {"ttft_s": 0.0, "itl_s": 0.0, "tokens": 2}):
        cluster.probe(reset=True)
        k = driver_count(rate)
        result = merge(run_drivers(rps_jobs(_gw(cluster), rate, duration, k)))
    if not result or not result.get("ok"):
        return _finding(
            "index rebuild on the request path",
            "-",
            f"no successful requests ({(result or {}).get('errors', {})})",
            None,
            {"errors": (result or {}).get("errors", {})},
        )

    p50 = result["latency"].pct(0.50)
    p999 = result["latency"].pct(0.999)
    ratio = p999 / p50 if p50 else 0.0
    expected_rebuilds = int(duration)
    return _finding(
        "index rebuild on the request path",
        "no visible tail from the 1 Hz rebuild at a rate far below saturation",
        f"p50 {p50:.2f} ms, p99.9 {p999:.2f} ms ({ratio:.0f}x) over ~{expected_rebuilds} rebuilds",
        ratio > 20.0,
        {
            "p50_ms": p50,
            "p99_ms": result["latency"].pct(0.99),
            "p999_ms": p999,
            "max_ms": round(result["latency"].max, 2),
            "requests": result["ok"],
        },
    )


ALL = (
    disconnect_storm,
    pool_exhaustion,
    error_storm,
    index_rebuild,
    control_plane_interference,
    backend_death,
)
