"""Merging driver results and printing the derating curve."""

from __future__ import annotations

from .driver import Hist

# A rung fails on any of these. They are separated so the report can name the
# mechanism rather than just saying the rung failed.
ERROR_RATE_MAX = 0.01
KEEPUP_MIN = 0.90
TAIL_BLOWUP_FACTOR = 10.0
LOOP_LAG_P99_MAX_MS = 250.0


def merge(results: list[dict]) -> dict:
    """Combine K driver processes into one rung.

    Histograms merge exactly because every process uses the same bucket
    layout; averaging percentiles across processes would not be a percentile
    of anything.
    """
    results = [r for r in results if r]
    if not results:
        return {}
    out = {
        "mode": results[0]["mode"],
        "offered": sum(r["offered"] for r in results),
        "concurrency": sum(r["concurrency"] for r in results),
        "wall_s": max(r["wall_s"] for r in results),
        "sent": sum(r["sent"] for r in results),
        "ok": sum(r["ok"] for r in results),
        "dropped": sum(r["dropped"] for r in results),
        "abandoned": sum(r["abandoned"] for r in results),
        "tokens": sum(r["tokens"] for r in results),
        "chunks": sum(r["chunks"] for r in results),
        "widest_chunk": max(r["widest_chunk"] for r in results),
        "peak_inflight": sum(r["peak_inflight"] for r in results),
        "conns": sum(r["conns"] for r in results),
        "errors": {},
    }
    for r in results:
        for key, count in r["errors"].items():
            out["errors"][key] = out["errors"].get(key, 0) + count
    for field in ("latency", "service", "ttft", "itl", "send_delay"):
        hist = Hist()
        for r in results:
            hist.merge(Hist.from_json(r[field]))
        out[field] = hist
    out["achieved_rps"] = round(out["ok"] / max(out["wall_s"], 1e-9), 2)
    out["attempted_rps"] = round(out["sent"] / max(out["wall_s"], 1e-9), 2)
    # Drops and errors are different failures: a drop never left the driver
    # because the service was too far behind to take it, an error came back.
    failed = out["sent"] - out["ok"] - out["abandoned"] - out["dropped"]
    out["error_rate"] = round(max(0, failed) / max(1, out["sent"]), 5)
    out["drop_rate"] = round(out["dropped"] / max(1, out["sent"]), 5)
    out["loss_rate"] = round(
        max(0, out["sent"] - out["ok"] - out["abandoned"]) / max(1, out["sent"]), 5
    )
    return out


def judge(rung: dict, *, baseline_p50_ms: float, probe: dict, driver_limited: bool) -> list[str]:
    """Why this rung failed, or an empty list if it passed."""
    reasons = []
    if driver_limited:
        return ["DRIVER-LIMITED"]

    if rung["error_rate"] > ERROR_RATE_MAX:
        top = sorted(rung["errors"].items(), key=lambda kv: -kv[1])[:3]
        detail = ", ".join(f"{k}x{v}" for k, v in top) or "no status returned"
        reasons.append(f"error rate {rung['error_rate'] * 100:.2f}% ({detail})")

    if rung["mode"] == "rps" and rung["offered"] > 0:
        keepup = rung["achieved_rps"] / rung["offered"]
        if keepup < KEEPUP_MIN:
            reasons.append(
                f"kept up with only {keepup * 100:.0f}% of offered "
                f"({rung['achieved_rps']:.0f} of {rung['offered']:.0f} rps)"
            )

    if rung["dropped"]:
        reasons.append(f"{rung['dropped']} requests never sent (inflight cap)")

    p99 = rung["latency"].pct(0.99)
    if baseline_p50_ms > 0 and p99 > baseline_p50_ms * TAIL_BLOWUP_FACTOR:
        reasons.append(
            f"p99 {p99:.1f} ms is {p99 / baseline_p50_ms:.0f}x the "
            f"idle p50 of {baseline_p50_ms:.1f} ms"
        )

    lag = (probe or {}).get("loop_lag") or {}
    if lag.get("p99_ms", 0.0) > LOOP_LAG_P99_MAX_MS:
        reasons.append(f"gateway event-loop lag p99 {lag['p99_ms']:.0f} ms")

    if (probe or {}).get("outstanding_total"):
        reasons.append(
            f"{probe['outstanding_total']} outstanding counters leaked after quiesce"
        )

    if (probe or {}).get("circuits"):
        reasons.append(
            f"breaker opened on {sorted(probe['circuits'])} with every backend healthy"
        )
    return reasons


_HEADERS = (
    "offered",
    "achieved",
    "err%",
    "drop%",
    "p50",
    "p99",
    "p999",
    "ttft p99",
    "itl p99",
    "lag p99",
    "cpu%",
    "rss MB",
    "verdict",
)


def row(rung: dict, probe: dict, reasons: list[str]) -> list[str]:
    lag = (probe or {}).get("loop_lag") or {}
    axis = rung["offered"] if rung["mode"] == "rps" else rung["concurrency"]
    return [
        f"{axis:g}",
        f"{rung['achieved_rps']:.0f}",
        f"{rung['error_rate'] * 100:.2f}",
        f"{rung.get('drop_rate', 0) * 100:.2f}",
        f"{rung['latency'].pct(0.50):.1f}",
        f"{rung['latency'].pct(0.99):.1f}",
        f"{rung['latency'].pct(0.999):.1f}",
        f"{rung['ttft'].pct(0.99):.1f}",
        f"{rung['itl'].pct(0.99):.1f}" if rung["itl"].count else "-",
        f"{lag.get('p99_ms', 0.0):.1f}",
        f"{(probe or {}).get('cpu_pct', 0.0):.0f}",
        f"{(probe or {}).get('rss_bytes', 0) / 1024 ** 2:.0f}",
        "ok" if not reasons else reasons[0],
    ]


def table(rows: list[list[str]], axis_label: str = "offered") -> str:
    headers = list(_HEADERS)
    headers[0] = axis_label
    widths = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(cell))
    lines = [
        "  ".join(h.rjust(widths[i]) for i, h in enumerate(headers)),
        "  ".join("-" * w for w in widths),
    ]
    for r in rows:
        lines.append("  ".join(c.rjust(widths[i]) for i, c in enumerate(r)))
    return "\n".join(lines)


def delta_line(gateway: dict, direct: dict) -> str:
    """The number that actually matters: what the gateway added.

    Both sides ran through the same driver at the same rung, so driver
    overhead cancels and what is left is the gateway's own cost.
    """
    if not direct:
        return "  (no direct A/B for this rung)"
    parts = []
    for label, field, q in (
        ("p50", "latency", 0.50),
        ("p99", "latency", 0.99),
        ("ttft p99", "ttft", 0.99),
        ("itl p99", "itl", 0.99),
    ):
        g = gateway[field].pct(q)
        d = direct[field].pct(q)
        if g or d:
            parts.append(f"{label} +{g - d:.2f} ms")
    return "  added vs direct: " + ", ".join(parts) if parts else "  added vs direct: n/a"
