"""Measure what models actually decode at, and check the fit gate against it.

``fit.predict_decode_tps`` is arithmetic -- active weights plus the cache for
one sequence, over the node's bandwidth, times one global efficiency constant --
and until this existed it had never been compared with anything. The first
comparison anybody ran was not close: 61.5 tok/s predicted against 120.3
measured for Qwen3-0.6B, and 17.1 against 48.9 for Qwen3-1.7B.

Two separable causes, and this tool exists to tell them apart across enough
models to act on:

* the prediction charges the cache at the FULL requested context, so it answers
  "the rate once the context is full" rather than the rate a short request sees.
  That error scales with the context asked for, which is why the two models
  above are wrong by different multiples rather than by one constant.
* ``DECODE_EFFICIENCY`` (0.55) is too low. Both models came in above even the
  empty-cache prediction, so the constant is wrong independently of the cache
  question. Two dense Qwen checkpoints are not enough to move it; a corpus
  spanning dense, MoE and quantized is.

**The engine's own counters are the authority here, not the wall clock.**
``vllm:generation_tokens_total`` and ``vllm:request_decode_time_seconds`` are
what the engine spent decoding, excluding queue, prefill and every network hop
between here and there. A client-side stopwatch measures all of those too and
would quietly attribute them to the model. The single-stream client rate is
still driven and printed, as a cross-check: when the two disagree by more than
a little, the run says so rather than picking one.

Usage::

    python3 -m tests.decode_sweep                 # measure what is already ready
    python3 -m tests.decode_sweep --report        # the corpus, and what it implies
    python3 -m tests.decode_sweep --corpus dense  # launch and measure a family
    python3 -m tests.decode_sweep --model Qwen/Qwen3-4B --context 8192

Nothing here changes production code. It writes ``DecodeRecord``s through
``control_plane/measurements.py`` and prints; the recalibration those records
justify is a separate, deliberate edit.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass

from control_plane import measurements, metrics_scrape

from .spec_sweep import (
    DEFAULT_BASE,
    SweepError,
    _call,
    drive_stream,
    launch,
    load_workload,
    node_profile,
    runtime_version,
    stop,
    wait_ready,
)

#: Decode tokens a measurement must carry before it is written down. Below this
#: the denominator is a handful of steps, where CUDA-graph warmup and scheduler
#: jitter dominate and the third significant figure is noise recorded as fact.
#: Matches ``gateway/metrics.py::MIN_DECODE_TOKENS``, which guards the same
#: arithmetic on the live path.
MIN_DECODE_TOKENS = 200.0

#: How far the engine's own rate and the client's stopwatch may drift before the
#: run flags it. They measure different things on purpose -- the client includes
#: queue, prefill and two network hops -- so they never agree exactly, and a gap
#: this wide means something other than decode is dominating and the record
#: would be about that instead.
CLIENT_DISAGREEMENT = 0.35

#: Models already in this box's HuggingFace cache, grouped by what makes their
#: decode arithmetic different. The grouping is the point: efficiency is a
#: property of the kernels, and a dense bf16 checkpoint, an MoE reading a
#: fraction of its weights, and a 4-bit checkpoint being dequantized on the fly
#: are three different questions. A constant fitted only to the first is the
#: situation this tool exists to get out of.
CORPUS: dict[str, tuple[str, ...]] = {
    "dense": (
        "EleutherAI/pythia-70m",
        "LiquidAI/LFM2.5-350M",
        "Qwen/Qwen2.5-0.5B-Instruct",
        "Qwen/Qwen3-0.6B",
        "Qwen/Qwen3-1.7B",
        "Qwen/Qwen3-4B",
        "Qwen/Qwen3-8B",
        "microsoft/Phi-3.5-mini-instruct",
        "mistralai/Mistral-7B-Instruct-v0.3",
    ),
    "moe": (
        "Qwen/Qwen3-30B-A3B",
        "openai/gpt-oss-20b",
        "openai/gpt-oss-120b",
    ),
    "quant": (
        "Qwen/Qwen3-4B-AWQ",
        "Qwen/Qwen3.8-27B-FP8",
        "Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8",
    ),
}


@dataclass
class Measured:
    """One model's decode rate, and everything needed to judge it."""

    model_id: str
    served_name: str
    deployment_id: str
    context_length: int
    #: From the engine's own counters, differenced over the drive.
    decode_tps: float
    tokens: float
    decode_seconds: float
    requests: float
    mean_sequence: float
    #: The same rate as a client measured it, first chunk to last. Includes
    #: queue, prefill and the network, so it is a cross-check and never the
    #: record.
    client_tps: float | None
    #: What the fit gate said, both ends of its range.
    predicted_tps: float | None
    predicted_tps_empty: float | None
    #: The memory the gate costed, which is what the implied efficiency divides.
    weights_bytes: int
    kv_cache_bytes: int
    kv_cache_usage: float | None
    preemptions: float

    @property
    def ratio(self) -> float | None:
        if not self.predicted_tps:
            return None
        return self.decode_tps / self.predicted_tps

    def implied_efficiency(self, bandwidth_gbps: float) -> float | None:
        """What ``DECODE_EFFICIENCY`` would have to be for the gate to be right.

        The gate's own arithmetic, solved for the constant: bytes moved per
        decoded token is the active weights plus the cache for the tokens
        ACTUALLY present, and the ceiling is the node's bandwidth over that.
        Dividing the measured rate by that ceiling is the efficiency the
        hardware really achieved.

        The cache term is prorated from the full-context figure by the observed
        mean sequence length. That is an approximation -- a real request grows
        its cache as it decodes, so the average over a generation is not the
        cache at the final length -- but it is the same approximation for every
        model here, which is what makes the numbers comparable to each other.
        """
        if bandwidth_gbps <= 0 or not self.context_length:
            return None
        share = min(1.0, max(0.0, self.mean_sequence / self.context_length))
        moved = self.weights_bytes + self.kv_cache_bytes * share
        if moved <= 0:
            return None
        ceiling = bandwidth_gbps * 1e9 / moved
        return self.decode_tps / ceiling if ceiling > 0 else None


def ready_vllm(base: str) -> list[dict]:
    """Every vLLM deployment currently serving, newest first.

    Only READY, and only vLLM: the counters this reads are vLLM's, and a
    backend that is still launching answers nothing.
    """
    rows = _call(base, "/api/deployments", timeout=60.0)
    if not isinstance(rows, list):
        rows = rows.get("deployments") or []
    out = [
        r for r in rows
        if r.get("state") == "ready" and r.get("runtime") == "vllm"
        and r.get("backend_url")
    ]
    out.sort(key=lambda r: r.get("started_at") or 0, reverse=True)
    return out


def pad(prompt: str, target_tokens: int) -> str:
    """Grow a prompt toward *target_tokens*, roughly.

    Four characters to the token is the usual English approximation and is
    plenty here: the band a record is filed under comes from the engine's OWN
    count of prompt and generation tokens, never from this estimate. This only
    has to get the cache into the right neighbourhood; the record says where it
    actually landed.
    """
    if target_tokens <= 0:
        return prompt
    filler = (
        "Background material follows, which you may summarise or ignore. "
        "The history of computing is long and the details are many. "
    )
    want = target_tokens * 4
    if len(prompt) >= want:
        return prompt
    body = filler * (1 + (want - len(prompt)) // len(filler))
    return f"{body[: want - len(prompt)]}\n\n{prompt}"


def measure(
    dep: dict, *, workload: str, max_tokens: int, prompt_tokens: int,
    timeout: float,
) -> Measured | None:
    """Drive one ready deployment and read what the engine says it did.

    Counters are differenced across the drive rather than read once, because an
    engine that has served anything before carries that history in its totals --
    and on a box where deployments are long-lived, that history is most of it.
    """
    backend = dep["backend_url"]
    before = metrics_scrape.read_engine_load(backend)
    if before is None:
        print(f"    unreachable: {backend}")
        return None

    prompts = [
        {**row, "prompt": pad(row.get("prompt") or row.get("text") or "", prompt_tokens)}
        for row in load_workload(workload)
    ]
    # The ENGINE, not the coordinator. spec_sweep::_one says why and it matters
    # doubly here: routing by served name can land a request on a different
    # deployment of the same model -- this box has carried two Qwen3-8B at once
    # -- while the counters are read from this one, which would silently
    # measure one engine and attribute it to another. It also keeps the proxy
    # out of a decode measurement, where it has no business being.
    driven = drive_stream(
        backend, dep["served_name"], prompts, timeout=timeout, max_tokens=max_tokens
    )
    after = metrics_scrape.read_engine_load(backend)
    if after is None:
        print("    engine stopped answering mid-drive")
        return None
    window = metrics_scrape.load_delta(before, after)

    if window.decode_tps is None:
        print(f"    nothing decoded ({driven.errors} error(s): {driven.reason[:60]})")
        return None
    if (window.generation_tokens - window.decode_count) < MIN_DECODE_TOKENS:
        print(
            f"    only {window.generation_tokens - window.decode_count:.0f} decode "
            f"tokens; under the {MIN_DECODE_TOKENS:.0f} needed to mean anything"
        )
        return None

    fit = dep.get("fit") or {}
    breakdown = fit.get("breakdown") or {}
    client = (driven.tokens / driven.decode_s) if driven.decode_s > 0 else None
    return Measured(
        model_id=dep["model_id"],
        served_name=dep["served_name"],
        deployment_id=dep["deployment_id"],
        context_length=int(dep.get("context_length") or 0),
        decode_tps=window.decode_tps,
        tokens=window.generation_tokens - window.decode_count,
        decode_seconds=window.decode_time_s,
        requests=window.decode_count,
        mean_sequence=window.mean_sequence_tokens or 0.0,
        client_tps=client,
        predicted_tps=fit.get("predicted_decode_tps"),
        predicted_tps_empty=fit.get("predicted_decode_tps_empty"),
        weights_bytes=int(breakdown.get("weights") or 0),
        kv_cache_bytes=int(breakdown.get("kv_cache") or 0),
        kv_cache_usage=after.kv_cache_usage,
        preemptions=window.preemptions,
    )


def record_for(m: Measured, node: dict, version: str) -> measurements.DecodeRecord:
    return measurements.DecodeRecord(
        model_id=m.model_id,
        gpu_name=str(node.get("gpu_name") or ""),
        memory_bandwidth_gbps=float(node.get("memory_bandwidth_gbps") or 0.0),
        runtime_version=version,
        context_band=measurements.band(m.mean_sequence or 1.0),
        concurrency_band=1,
        decode_tps=round(m.decode_tps, 2),
        tokens=m.tokens,
        decode_seconds=round(m.decode_seconds, 3),
        requests=m.requests,
        predicted_tps=m.predicted_tps,
        measured_at=time.time(),
    )


def report_line(m: Measured, bandwidth: float) -> str:
    eff = m.implied_efficiency(bandwidth)
    ratio = m.ratio
    return (
        f"  {m.model_id:38} ctx={m.context_length:>6} seq~{m.mean_sequence:>5.0f}  "
        f"pred {m.predicted_tps or 0:>7.1f}  measured {m.decode_tps:>7.1f}  "
        f"{(f'{ratio:.2f}x' if ratio else '    -'):>7}  "
        f"eff {(f'{eff:.3f}' if eff else '  -'):>6}"
    )


def summarise(found: list[Measured], bandwidth: float) -> int:
    """Print the corpus and what it implies for the efficiency constant.

    The spread is the finding, not the mean. If dense, MoE and quantized
    checkpoints land on visibly different efficiencies then no single constant
    describes them and the honest answer is a small table or a measurement --
    which is exactly what this refuses to decide on the caller's behalf.
    """
    if not found:
        print("\nnothing measured.")
        return 1
    print("\n" + "=" * 100)
    print(f"{len(found)} measurement(s) at {bandwidth:.0f} GB/s\n")
    for m in found:
        print(report_line(m, bandwidth))

    effs = [e for e in (m.implied_efficiency(bandwidth) for m in found) if e]
    ratios = [r for r in (m.ratio for m in found) if r]
    print()
    if ratios:
        print(
            f"  gate error       : {min(ratios):.2f}x .. {max(ratios):.2f}x "
            f"(measured over predicted; 1.00x would be exact)"
        )
    if effs:
        lo, hi = min(effs), max(effs)
        mean = sum(effs) / len(effs)
        print(f"  implied efficiency: {lo:.3f} .. {hi:.3f}, mean {mean:.3f}")
        print(f"  DECODE_EFFICIENCY is 0.55 today")
        spread = (hi - lo) / mean if mean else 0.0
        if spread > 0.15:
            print(
                f"\n  The spread is {spread * 100:.0f}% of the mean. That is wide enough "
                f"that one constant\n  does not describe these models, and fitting one "
                f"to their average would be\n  choosing which of them to be wrong about. "
                f"Look at the per-family numbers\n  above before moving it."
            )
        else:
            print(
                f"\n  The spread is {spread * 100:.0f}% of the mean, so one constant does "
                f"describe these\n  models. {mean:.2f} is what they imply."
            )
    preempted = [m for m in found if m.preemptions > 0]
    if preempted:
        print(
            f"\n  {len(preempted)} run(s) preempted -- the KV cache ran out mid-drive, "
            f"so those\n  rates include eviction and are not clean decode:"
        )
        for m in preempted:
            print(f"    {m.model_id}: {m.preemptions:.0f} preemption(s)")
    return 0


def _flag_client_gap(m: Measured) -> None:
    """Say when the two rates disagree enough that the record may be about
    something other than decode."""
    if m.client_tps is None or m.decode_tps <= 0:
        return
    gap = abs(m.decode_tps - m.client_tps) / m.decode_tps
    if gap > CLIENT_DISAGREEMENT:
        print(
            f"    NOTE engine says {m.decode_tps:.1f} tok/s, the client saw "
            f"{m.client_tps:.1f} ({gap * 100:.0f}% apart). Queue, prefill or the "
            f"network is a large share of this drive."
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument(
        "--model", action="append", default=[],
        help="launch and measure this model; repeatable",
    )
    ap.add_argument(
        "--corpus", choices=sorted(CORPUS) + ["all"],
        help="launch and measure a whole family from the local HuggingFace cache",
    )
    ap.add_argument(
        "--node", action="append", default=[],
        help="pin launches to these node ids; repeatable. Worth doing on a "
             "shared box: the planner picks by its own score and will happily "
             "choose the fullest machine, and a sweep wants the emptiest.",
    )
    ap.add_argument("--context", type=int, default=8192, help="context to launch at")
    ap.add_argument("--workload", default="prose", help="prompt set from spec_data/")
    ap.add_argument("--tokens", type=int, default=200, help="max_tokens per request")
    ap.add_argument(
        "--prompt-tokens", type=int, default=0,
        help="pad prompts toward this length, to measure with a fuller cache",
    )
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument(
        "--keep", action="store_true",
        help="leave launched deployments running (default is to stop them)",
    )
    ap.add_argument(
        "--report", action="store_true",
        help="print the stored corpus and exit; measures nothing",
    )
    ap.add_argument("--no-save", action="store_true", help="measure but write nothing")
    args = ap.parse_args(argv)

    if args.report:
        return _report_stored()

    node = node_profile(args.base)
    bandwidth = float(node.get("memory_bandwidth_gbps") or 0.0)
    if not bandwidth:
        print("no node with a bandwidth figure; nothing can be keyed", file=sys.stderr)
        return 2
    print(f"node {node.get('node_id')} ({node.get('gpu_name')}), {bandwidth:.0f} GB/s")

    wanted: list[str] = list(args.model)
    if args.corpus:
        families = sorted(CORPUS) if args.corpus == "all" else [args.corpus]
        for family in families:
            wanted.extend(CORPUS[family])

    found: list[Measured] = []

    # Already serving first, and free: their engines are up and their counters
    # are readable without launching anything. On a box where deployments are
    # long-lived this is most of the corpus for no cost at all.
    serving = ready_vllm(args.base)
    print(f"\n{len(serving)} deployment(s) already serving")
    for dep in serving:
        print(f"  {dep['served_name']} ({dep['model_id']})")
        m = measure(
            dep, workload=args.workload, max_tokens=args.tokens,
            prompt_tokens=args.prompt_tokens, timeout=args.timeout,
        )
        if m is None:
            continue
        _flag_client_gap(m)
        found.append(m)
        wanted = [w for w in wanted if w != m.model_id]

    # Then the ones that are not, ONE AT A TIME.
    #
    # deploy/manager.py::_hold_starting serialises launches per node anyway --
    # two vLLM startups on one device bill each other's allocations to their own
    # memory profiling -- so firing several would not make them parallel. It
    # would make them QUEUE, for up to 1800s each, and the ones that lose pass a
    # fit check taken against memory the winner has since allocated, fail inside
    # the runtime, and get recorded as `fit_miss`: "the fit calculator's estimate
    # was low". That is the one telemetry channel this whole exercise exists to
    # make trustworthy, so a sweep must not be the thing that poisons it.
    for model_id in wanted:
        print(f"\nlaunching {model_id} at {args.context}")
        started = None
        try:
            started = launch(
                args.base, model_id, args.context, None,
                node_ids=args.node or None,
            )
            deployment_id = started["deployment_id"]
            wait_ready(args.base, deployment_id)
            dep = _call(args.base, f"/api/deployments/{deployment_id}", timeout=60.0)
            m = measure(
                dep, workload=args.workload, max_tokens=args.tokens,
                prompt_tokens=args.prompt_tokens, timeout=args.timeout,
            )
            if m is not None:
                _flag_client_gap(m)
                found.append(m)
                print(report_line(m, bandwidth))
        except SweepError as exc:
            print(f"  refused or failed: {str(exc)[:160]}")
        finally:
            if started and not args.keep:
                stop(args.base, started["deployment_id"])

    if not args.no_save and found:
        version = runtime_version(args.base, found[0].model_id)
        written = 0
        for m in found:
            if measurements.save_decode(record_for(m, node, version)) is not None:
                written += 1
        print(f"\nwrote {written} record(s) to {measurements.decode_records_dir()}")

    return summarise(found, bandwidth)


def _report_stored() -> int:
    """Everything measured so far, from disk. Measures nothing itself."""
    records = measurements.load_decode_all()
    if not records:
        print(f"no decode records under {measurements.decode_records_dir()}")
        return 1
    print(f"{len(records)} stored record(s)\n")
    print(
        f"  {'model':38} {'gpu':14} {'ctx':>6} {'seq':>5} "
        f"{'measured':>9} {'predicted':>10} {'ratio':>7}"
    )
    for r in sorted(records, key=lambda r: (r.model_id, r.context_band)):
        ratio = r.ratio
        print(
            f"  {r.model_id:38} {r.gpu_name:14} {r.context_band:>6} "
            f"{r.concurrency_band:>5} {r.decode_tps:>9.1f} "
            f"{(r.predicted_tps or 0):>10.1f} "
            f"{(f'{ratio:.2f}x' if ratio else '-'):>7}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
