"""The ladders.

Climb until the gateway stops meeting its contract, then stop and name the
rung. The last passing rung is the derated capacity, which is the number the
whole harness exists to produce.
"""

from __future__ import annotations

import sys
import time

from . import report
from .harness import (
    Cluster,
    driver_count,
    merge,
    rps_jobs,
    run_drivers,
    spread,
    stream_jobs,
)

RUNGS_RPS = [
    1, 2, 5, 10, 25, 50, 100, 250, 500,
    1_000, 2_000, 4_000, 8_000, 16_000, 32_000, 64_000, 128_000, 200_000,
]

RUNGS_STREAM = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192]


def _cpu_pct(before: dict, after: dict) -> float:
    """The gateway is one process on one core, so 100% here is the ceiling."""
    dt = after.get("wall_s", 0.0) - before.get("wall_s", 0.0)
    dc = after.get("cpu_s", 0.0) - before.get("cpu_s", 0.0)
    return round(dc / dt * 100.0, 1) if dt > 0 else 0.0


def _quiesce(cluster: Cluster, cooldown: float) -> dict:
    """Let in-flight work drain, then look for what did not drain."""
    time.sleep(cooldown)
    return cluster.probe()


def _emit(rows: list[list[str]], axis: str) -> None:
    print()
    print(report.table(rows, axis))
    print()


def run_rps(
    cluster: Cluster,
    *,
    duration: float = 15.0,
    cooldown: float = 5.0,
    rungs: list[int] | None = None,
    ab: bool = True,
) -> dict:
    """The 1 -> 200,000 rps ladder against the non-streaming request path."""
    rungs = rungs or RUNGS_RPS
    url = f"{cluster.gateway_url}/v1/chat/completions"
    # The request path, not the decode. A backend that answers instantly means
    # every millisecond measured here belongs to the gateway; leaving the
    # decode delay in would just measure the fake runtime's sleep.
    cluster.configure_backends({"ttft_s": 0.0, "itl_s": 0.0})
    rows: list[list[str]] = []
    baseline_p50 = 0.0
    last_pass = None
    outcome = {"axis": "rps", "rungs": [], "derated": None, "stopped_by": None}

    for rate in rungs:
        k = driver_count(rate)
        print(f"rung {rate:>7g} rps  ({k} driver process{'es' if k > 1 else ''})", flush=True)

        direct = {}
        if ab:
            direct = merge(
                run_drivers(
                    spread(rps_jobs(url, rate, duration, k), cluster.backend_urls)
                )
            )
            time.sleep(1.0)

        before = cluster.probe(reset=True)
        gateway = merge(run_drivers(rps_jobs(url, rate, duration, k)))
        during = cluster.probe()
        during["cpu_pct"] = _cpu_pct(before, during)
        after = _quiesce(cluster, cooldown)

        if not gateway:
            outcome["stopped_by"] = f"no driver results at {rate} rps"
            break

        # If the fake runtimes could not absorb the rung either, the ceiling
        # found is the harness's own and says nothing about the gateway.
        driver_limited = bool(
            ab and direct and direct["achieved_rps"] < rate * report.KEEPUP_MIN
        )

        probe = dict(during)
        probe["outstanding_total"] = after.get("outstanding_total", 0)
        probe["circuits"] = after.get("circuits", {})

        if baseline_p50 == 0.0:
            baseline_p50 = gateway["latency"].pct(0.50)

        reasons = report.judge(
            gateway,
            baseline_p50_ms=baseline_p50,
            probe=probe,
            driver_limited=driver_limited,
        )
        rows.append(report.row(gateway, probe, reasons))
        if ab:
            print(report.delta_line(gateway, direct), flush=True)

        outcome["rungs"].append(
            {
                "rate": rate,
                "achieved_rps": gateway["achieved_rps"],
                "error_rate": gateway["error_rate"],
                "p50_ms": gateway["latency"].pct(0.50),
                "p99_ms": gateway["latency"].pct(0.99),
                "loop_lag_p99_ms": (probe.get("loop_lag") or {}).get("p99_ms", 0.0),
                "cpu_pct": probe.get("cpu_pct", 0.0),
                "drop_rate": gateway.get("drop_rate", 0.0),
                "outstanding_leaked": probe["outstanding_total"],
                "reasons": reasons,
            }
        )

        if reasons:
            outcome["stopped_by"] = reasons
            print(f"  STOP: {'; '.join(reasons)}", flush=True)
            break
        last_pass = rate

    outcome["derated"] = last_pass
    _emit(rows, "rps")
    return outcome


def run_stream(
    cluster: Cluster,
    *,
    duration: float = 15.0,
    cooldown: float = 5.0,
    rungs: list[int] | None = None,
) -> dict:
    """Concurrent SSE streams. The axis that the latency contract is written in.

    ``agents/G-gateway.md:136`` -- "streaming passes through with no measurable
    added latency against calling the backend directly" -- is a statement about
    inter-token latency, so this ladder reports the delta rather than a rate.
    """
    rungs = rungs or RUNGS_STREAM
    url = f"{cluster.gateway_url}/v1/chat/completions"
    # Streaming is the opposite case: the decode delay is the thing the added
    # latency is measured against, so it goes back in.
    cluster.configure_backends({"ttft_s": cluster.ttft, "itl_s": cluster.itl})
    rows: list[list[str]] = []
    baseline_p50 = 0.0
    last_pass = None
    outcome = {"axis": "streams", "rungs": [], "derated": None, "stopped_by": None}

    # One token period. Added inter-token latency above this is measurable by
    # definition: the client waits longer for a token than the backend took.
    token_period_ms = cluster.itl * 1000.0

    for concurrency in rungs:
        k = max(1, min(8, concurrency))
        print(f"rung {concurrency:>6} streams  ({k} driver processes)", flush=True)

        direct = merge(
            run_drivers(
                spread(stream_jobs(url, concurrency, duration, k), cluster.backend_urls)
            )
        )
        time.sleep(1.0)

        before = cluster.probe(reset=True)
        gateway = merge(run_drivers(stream_jobs(url, concurrency, duration, k)))
        during = cluster.probe()
        during["cpu_pct"] = _cpu_pct(before, during)
        after = _quiesce(cluster, cooldown)

        if not gateway:
            outcome["stopped_by"] = f"no driver results at {concurrency} streams"
            break

        probe = dict(during)
        probe["outstanding_total"] = after.get("outstanding_total", 0)
        probe["circuits"] = after.get("circuits", {})

        if baseline_p50 == 0.0:
            baseline_p50 = gateway["latency"].pct(0.50)

        driver_limited = bool(direct and direct["ok"] == 0)
        reasons = report.judge(
            gateway,
            baseline_p50_ms=baseline_p50,
            probe=probe,
            driver_limited=driver_limited,
        )

        added_itl = gateway["itl"].pct(0.99) - direct["itl"].pct(0.99) if direct else 0.0
        if token_period_ms and added_itl > token_period_ms:
            reasons.append(
                f"added inter-token p99 {added_itl:.1f} ms exceeds one "
                f"{token_period_ms:.0f} ms token period"
            )
        if gateway["widest_chunk"] > 1:
            reasons.append(
                f"{gateway['widest_chunk']} SSE frames arrived in one chunk "
                "(gateway backpressure)"
            )

        rows.append(report.row(gateway, probe, reasons))
        print(report.delta_line(gateway, direct), flush=True)

        outcome["rungs"].append(
            {
                "concurrency": concurrency,
                "achieved_rps": gateway["achieved_rps"],
                "added_itl_p99_ms": round(added_itl, 3),
                "itl_p99_ms": gateway["itl"].pct(0.99),
                "ttft_p99_ms": gateway["ttft"].pct(0.99),
                "loop_lag_p99_ms": (probe.get("loop_lag") or {}).get("p99_ms", 0.0),
                "outstanding_leaked": probe["outstanding_total"],
                "widest_chunk": gateway["widest_chunk"],
                "reasons": reasons,
            }
        )

        if reasons:
            outcome["stopped_by"] = reasons
            print(f"  STOP: {'; '.join(reasons)}", flush=True)
            break
        last_pass = concurrency

    outcome["derated"] = last_pass
    _emit(rows, "streams")
    return outcome


def calibrate(cluster: Cluster, *, duration: float = 8.0) -> dict:
    """The driver's own ceiling, measured before the gateway is touched.

    Any gateway rung above this number would be reporting the harness rather
    than the subject, so this runs first and is printed first.
    """
    # Same zero-delay backends the rps ladder uses, or this would measure the
    # fake runtime's sleep rather than the harness's ceiling.
    cluster.configure_backends({"ttft_s": 0.0, "itl_s": 0.0})
    ceiling = 0.0
    rows = []
    for rate in [500, 1_000, 2_000, 5_000, 10_000, 20_000, 40_000, 80_000]:
        k = driver_count(rate)
        jobs = spread(
            rps_jobs(f"{cluster.gateway_url}/v1/chat/completions", rate, duration, k),
            cluster.backend_urls,
        )
        result = merge(run_drivers(jobs))
        if not result:
            break
        achieved = result["achieved_rps"]
        keepup = achieved / rate
        rows.append(
            f"  offered {rate:>7,}  achieved {achieved:>9,.0f}  "
            f"({keepup * 100:5.1f}%)  p99 {result['latency'].pct(0.99):.1f} ms"
        )
        print(rows[-1], flush=True)
        if keepup < report.KEEPUP_MIN:
            break
        ceiling = achieved
    print(f"\n  driver + fake-runtime ceiling: ~{ceiling:,.0f} rps", flush=True)
    return {"ceiling_rps": ceiling, "rows": rows}
