"""Measure speculative acceptance, and solve for the k that maximises tok/s.

The fit gate states a range -- a floor where nothing is accepted and a ceiling
where everything is -- and says outright that the acceptance rate deciding
between them is not measured. On `Qwen/Qwen3-4B` with ngram that range is 18 to
109 tok/s, which is too wide to decide anything with. This is the tool that
closes it.

    python3 -m tests.spec_sweep --model Qwen/Qwen3-4B
    python3 -m tests.spec_sweep --model X --method ngram --max-k 10
    python3 -m tests.spec_sweep --model X --workload code_edit
    python3 -m tests.spec_sweep --smoke          # plumbing only, tiny model

**One launch answers for every k.** vLLM exports
`vllm:spec_decode_num_accepted_tokens_per_pos`, so a single run at k=10 reports
how often position 1 was accepted, and position 2, and so on to 10. Expected
accepted length at any smaller k is a prefix sum of that, which means the curve
over k comes out of one deployment instead of ten. When an image does not export
the per-position series the tool says so and falls back to relaunching per k --
`metrics_scrape.SpecDecode.basis` is what decides, and it is reported either way
rather than assumed.

**Three workloads, never blended.** Acceptance is a property of the traffic, not
of the model: ngram accepts most of what it drafts when the answer repeats the
question and nearly none of it on open prose. `tests/spec_data/README.md` says
what each set is for and why they are hand-written rather than generated.

**Nothing here assumes an acceptance rate.** Every figure printed is either read
off the engine's own counters or measured from requests this tool sent, and the
records it writes carry the workload, the hardware and the image version they
were taken under -- `control_plane/measurements.py` refuses to match a record
across any of those changing.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import concurrent.futures as cf
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib import error, parse, request

from control_plane import head_scan, measurements, metrics_scrape
from control_plane.fit.calculator import speculative_best_k, speculative_overhead

DATA = Path(__file__).parent / "spec_data"
DEFAULT_BASE = "http://localhost:8088"

#: A launch that is still coming up is normal for minutes -- image pull, weight
#: load, then CUDA graph capture, which CLAUDE.md measures at 40s+ on its own.
READY_TIMEOUT_S = 900.0

#: What `--smoke` serves: a real, small, ordinary checkpoint, with ngram.
#:
#: Two tiny random fixtures were tried first and neither loads, which is worth
#: recording because both looked fine to every gate derate has:
#:
#:   katuni4ka/tiny-random-deepseek-v3   1.7M params, declares an `auto_map`.
#:       sparkrun runs the container with HF_HUB_OFFLINE=1, so transformers
#:       could not fetch the custom modeling module and died with "We couldn't
#:       connect to huggingface.co".
#:   tiny-random/glm-4-moe-lite          5.8M params, no remote code, and still
#:       fails: "No valid MLA prefill backend found with ... qk_nope_head_dim=64,
#:       qk_rope_head_dim=192, v_head_dim=64". A randomly shaped fixture has
#:       randomly shaped attention, and no kernel serves it.
#:
#: So the smoke path uses a checkpoint that really runs. ngram rather than MTP
#: because it needs no module from the checkpoint at all -- which makes this a
#: test of launch, drive, scrape and solve, and not of any one model's head.
SMOKE_MODEL = "Qwen/Qwen3-0.6B"
SMOKE_METHOD = "ngram"


class SweepError(RuntimeError):
    """Something the run cannot continue past, with a sentence saying what.

    Carries the coordinator's whole error body when there was one, because the
    interesting part is often beside the message rather than in it -- an
    `already_deployed` refusal names the deployment holding the node, and that
    id is what `--stop-conflicting` acts on.
    """

    def __init__(self, message: str, body: dict | None = None) -> None:
        super().__init__(message)
        self.body = body or {}

    @property
    def conflict_id(self) -> str | None:
        """The deployment already holding this model, when that is the problem.

        A sweep launches and stops the same model over and over, so its own
        orphan -- left by a run that was killed before its `finally` could fire
        -- is the likeliest thing standing in the way of the next one.
        """
        if (self.body.get("error") or {}).get("code") != "already_deployed":
            return None
        return (self.body.get("conflict") or {}).get("deployment_id")


# -- the coordinator, over HTTP only ---------------------------------------


def _call(base: str, path: str, *, body: dict | None = None, method: str = "GET",
          timeout: float = 300.0) -> dict | list:
    data = None if body is None else json.dumps(body).encode()
    req = request.Request(
        base.rstrip("/") + path, data=data, method=method,
        headers={"content-type": "application/json"} if data else {},
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
        except ValueError:
            raise SweepError(f"{method} {path}: HTTP {exc.code}: {raw[:200]}") from exc
        message = (parsed.get("error") or {}).get("message") or raw[:200]
        # The coordinator's own refusal, whole. These are the strings the whole
        # project treats as the product; summarising one here would lose the
        # part that says what to change.
        raise SweepError(message, parsed) from exc
    except (error.URLError, OSError) as exc:
        raise SweepError(f"{method} {path}: {exc}") from exc
    return json.loads(raw) if raw else {}


def plan(base: str, model_id: str, spec: dict | None = None) -> dict:
    body: dict = {"model_id": model_id, "target": "throughput"}
    if spec:
        body["speculative"] = spec
    return _call(base, "/api/plan", body=body, method="POST")


def launch(
    base: str, model_id: str, context: int, spec: dict | None,
    node_ids: list[str] | None = None, served_name: str | None = None,
) -> dict:
    """Start one deployment. *node_ids* pins placement.

    Pinning is worth doing for a sweep even on a uniform cluster: every launch
    lands on the same machine, so its HuggingFace cache stays warm and a head
    downloads once instead of once per node the planner happens to pick -- and
    every record then describes one GPU, which is what `measurements.py` keys
    on anyway.
    """
    body: dict = {
        "model_id": model_id, "context": context, "concurrency": 1,
        "target": "throughput", "runtime": "vllm",
    }
    if spec:
        body["speculative"] = spec
    if node_ids:
        body["node_ids"] = list(node_ids)
    if served_name:
        # Distinct per method, because the served-name rule is cluster-wide:
        # two copies of one model cannot both answer to the model's own name,
        # however they are placed. This is what makes running two methods at
        # once possible at all.
        body["served_name"] = served_name
    return _call(base, "/api/deployments", body=body, method="POST")


def engine_died(base: str, deployment_id: str) -> str | None:
    """The runtime's own line saying it failed to start, if it said one.

    A solo launch execs the serve command inside a container that sleeps
    forever, so an engine that dies leaves the record reading `launching` and
    the port refusing -- CLAUDE.md's "a dead engine does not kill its
    container". Waiting that out costs this tool the full readiness timeout per
    drafted-token count, which on a ten-step walk down is hours.

    The markers are imported from `deploy/progress.py` rather than copied. That
    list is maintained against real vLLM output and a copy here would be one
    more thing to update on a version bump -- and the failure of a stale copy
    is silent, which is the whole reason that module keeps them in one place.
    """
    try:
        from control_plane.deploy.progress import _RUNTIME_FATAL
    except Exception:
        return None
    try:
        body = _call(base, f"/api/deployments/{deployment_id}/logs", timeout=30.0)
    except SweepError:
        return None
    for line in reversed([str(l) for l in (body.get("lines") or [])]):
        if any(marker in line for marker in _RUNTIME_FATAL):
            return line.strip()
    return None


def wait_ready(base: str, deployment_id: str, *, timeout: float = READY_TIMEOUT_S) -> dict:
    """Poll until READY, or fail with the runtime's own words.

    Three ways out. The record going READY is the good one; the record going
    FAILED carries `last_error`, which for this tool is usually the refusal
    saying a drafted-token count is out of range. The third is
    :func:`engine_died` -- the record still says `launching` and the engine has
    already exited, which is the case that would otherwise cost the whole
    timeout.
    """
    deadline = time.time() + timeout
    polls = 0
    while time.time() < deadline:
        row = _call(base, f"/api/deployments/{deployment_id}")
        state = row.get("state")
        if state == "ready":
            return row
        if state in ("failed", "stopped"):
            raise SweepError(row.get("last_error") or f"launch ended {state}")
        polls += 1
        # Not every pass: this is a second request against a coordinator that
        # is busy starting something. Often enough that a dead engine costs
        # half a minute instead of the full timeout.
        if polls % 10 == 0:
            fatal = engine_died(base, deployment_id)
            if fatal:
                raise SweepError(fatal)
        time.sleep(3.0)
    raise SweepError(f"still not ready after {timeout:.0f}s")


def stop(base: str, deployment_id: str) -> None:
    try:
        _call(base, f"/api/deployments/{deployment_id}", method="DELETE", timeout=120.0)
    except SweepError:
        pass  # best effort; the run has its numbers and the record says so


# -- driving one workload ---------------------------------------------------


@dataclass
class Driven:
    """What sending one prompt set produced."""

    tokens: int = 0
    decode_s: float = 0.0
    requests: int = 0
    errors: int = 0
    #: The first failure's own words. Empty when nothing failed.
    reason: str = ""

    @property
    def tps(self) -> float | None:
        """Decode tokens per second, first token excluded.

        Timed from the first streamed chunk to the last, so prefill is not
        counted against the decode rate -- the same reason `gateway/stats.py`
        prefers a decode window over whole-request duration.
        """
        if self.decode_s <= 0 or self.tokens <= 0:
            return None
        return self.tokens / self.decode_s


def load_workload(name: str) -> list[dict]:
    path = DATA / f"{name}.jsonl"
    if not path.is_file():
        raise SweepError(f"no workload {name!r} at {path}")
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def drive_concurrent(
    base: str, served_name: str, prompts: list[dict], *, timeout: float,
    workers: int = 8, max_tokens: int | None = None,
) -> Driven:
    """Fire the whole prompt set at once. Only the COUNTERS are read from this.

    Acceptance is counted per draft round by the engine, so it does not depend
    on how many sequences are in flight -- which is what makes this safe to
    parallelise and why it is the difference between a run of minutes and a run
    of tens of minutes. The tok/s this returns is a batched aggregate and is
    deliberately ignored by the caller; :func:`drive_stream` supplies the rate.

    The batch-invariance is second order rather than exact -- a batched step
    drafts for several sequences at once -- and the single-stream phase is the
    cross-check on it: if the two disagree about how much was drafted per
    round, the record's `drafts` count says so.
    """
    out = Driven()
    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [
            pool.submit(_one, base, served_name, row, timeout, max_tokens)
            for row in prompts
        ]
        for future in cf.as_completed(futures):
            tokens, first, last, failed = future.result()
            if failed:
                out.errors += 1
                out.reason = out.reason or failed
                continue
            out.requests += 1
            if first is not None and last is not None and last > first and tokens > 1:
                out.tokens += tokens - 1
                out.decode_s += last - first
    return out


def drive_stream(
    base: str, served_name: str, prompts: list[dict], *, timeout: float,
    max_tokens: int | None = None,
) -> Driven:
    """Send the prompts one at a time, to anchor tok/s.

    Sequential on purpose and deliberately short: this is the only figure in
    the run that has to be a single stream's own decode rate, measured the way
    `gateway/stats.py` measures one -- tokens after the first, over the window
    between the first chunk and the last. Batching it would fold the scheduler
    into the number the whole curve is anchored on.
    """
    out = Driven()
    for row in prompts:
        tokens, first, last, failed = _one(base, served_name, row, timeout, max_tokens)
        if failed:
            out.errors += 1
            out.reason = out.reason or failed
            continue
        out.requests += 1
        if first is not None and last is not None and last > first and tokens > 1:
            out.tokens += tokens - 1
            out.decode_s += last - first
    return out


def _one(
    base: str, served_name: str, row: dict, timeout: float, max_tokens: int | None
) -> tuple[int, float | None, float | None, str]:
    """One streamed completion: (tokens, first chunk, last chunk, why it failed).

    The failure is a SENTENCE, not a flag. An earlier version returned a bare
    bool and the caller could only say "every request failed", which sent a
    diagnosis down the wrong path for an hour: the real cause was a backend
    that was not answering, and the tool had the reason in hand and threw it
    away.

    *base* is the deployment's own ``backend_url``, NOT the coordinator. Going
    through the gateway would route by served name, and a served name can have
    more than one target -- this box has carried two `Qwen3-8B` deployments at
    once -- so the requests could land on a deployment other than the one being
    measured, while the counters were read from this one. Talking to the engine
    directly also takes the proxy out of a decode-rate measurement, which it
    has no business being in.

    Streamed rather than a single JSON response because the two clocks matter
    separately: prefill is not decode, and a rate that counts it is not the
    rate the fit gate predicts. `first` is when the first content chunk landed,
    `last` when the final one did; the caller charges tokens after the first
    against the window between them, which is what `gateway/stats.py` does with
    a real request.
    """
    body = {
        "model": served_name,
        "messages": [{"role": "user", "content": row["prompt"]}],
        "max_tokens": int(max_tokens or row.get("max_tokens", 192)),
        "temperature": 0,
        "stream": True,
    }
    req = request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(), method="POST",
        headers={"content-type": "application/json"},
    )
    first = last = None
    tokens = 0
    try:
        with request.urlopen(req, timeout=timeout) as response:
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    event = json.loads(chunk)
                except ValueError:
                    continue
                delta = (event.get("choices") or [{}])[0].get("delta") or {}
                if not delta.get("content"):
                    continue
                now = time.time()
                if first is None:
                    first = now
                last = now
                tokens += 1
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        return 0, None, None, f"HTTP {exc.code}: {detail}"
    except (error.URLError, OSError) as exc:
        return 0, None, None, str(exc)[:200]
    return tokens, first, last, ""


# -- the sweep --------------------------------------------------------------


def _await_backend(backend: str, *, tries: int = 10, delay: float = 3.0) -> str | None:
    """None once the engine answers `/v1/models`, else the last reason.

    A few short retries rather than one probe: a server that has just bound its
    port can refuse a connection for a moment, and failing the whole method for
    that would be as wrong as not checking at all.
    """
    last = "not attempted"
    for _ in range(max(1, tries)):
        try:
            with request.urlopen(backend.rstrip("/") + "/models", timeout=10.0) as r:
                if r.status == 200:
                    return None
                last = f"HTTP {r.status}"
        except (error.URLError, OSError) as exc:
            last = str(exc)[:120]
        time.sleep(delay)
    return last


def sweep_method(
    base: str, model_id: str, option: dict, context: int, max_k: int,
    workloads: list[str], node: dict, runtime_version: str, timeout: float,
    baseline_tps: float | None, verbose: bool, stop_conflicting: bool = False,
    node_ids: list[str] | None = None, max_tokens: int | None = None,
    save: bool = True, served_name: str | None = None,
) -> list[measurements.SpecRecord]:
    """One method: launch as high as the engine allows, then measure each set."""
    method = option["method"]
    spec_model = option.get("head_model")
    wanted = min(int(max_k), int(option.get("max_tokens") or max_k))
    records: list[measurements.SpecRecord] = []

    deployment = None
    k = max(1, wanted)
    refusals: list[str] = []
    while k >= 1 and deployment is None:
        spec = {"method": method, "num_speculative_tokens": k}
        if spec_model:
            spec["model"] = spec_model
        started = None
        try:
            try:
                started = launch(base, model_id, context, spec, node_ids, served_name)
            except SweepError as exc:
                # Almost always this sweep's own orphan: a run killed before its
                # `finally` fired leaves the model held on a node. Stopping it
                # is opt-in because the holder might equally be somebody's
                # production deployment, and this tool must not take one down
                # on a guess.
                holder = exc.conflict_id
                if not (stop_conflicting and holder):
                    raise
                print(f"    stopping the deployment holding this model: {holder}")
                stop(base, holder)
                time.sleep(5.0)
                started = launch(base, model_id, context, spec, node_ids, served_name)
            deployment = wait_ready(base, started["deployment_id"])
        except SweepError as exc:
            # The cap this project could not verify, discovered. `speculators.py`
            # caps MTP at the checkpoint's declared nextn count because nothing
            # here had checked whether the runtime re-runs the head; whatever the
            # engine actually refuses is the answer, and it is recorded in the
            # engine's own words.
            refusals.append(f"k={k}: {exc}")
            print(f"    k={k:<3} refused: {str(exc)[:110]}")
            # Some refusals do not get better at a smaller k, and walking down
            # through them costs a launch a rung for nothing. A KV shortfall is
            # the one this run met: the draft model's own cache scales with
            # CONTEXT, not with drafted tokens, so k=1 asks for very nearly what
            # k=8 did. Stop and say so rather than spending five launches
            # rediscovering it.
            if "KV cache" in str(exc):
                print("    (a KV shortfall does not shrink with k — not "
                      "descending; lower --context instead)")
                if started is not None:
                    stop(base, started["deployment_id"])
                break
            # A launch that was accepted and then died still holds a record and
            # possibly a container. Tear it down before trying the next k, or a
            # ten-step walk down leaves ten of them behind.
            if started is not None:
                stop(base, started["deployment_id"])
            k -= 1
    if deployment is None:
        print(f"    {method}: no drafted-token count was accepted")
        return records

    launched_k = k
    served = deployment["served_name"]
    backend = deployment.get("backend_url") or ""
    if not backend:
        print("    the deployment reports no backend_url; nothing to drive "
              "or scrape")
        stop(base, deployment["deployment_id"])
        return records
    # derate calling a deployment READY is not the same as this process being
    # able to reach the engine, and the difference is not theoretical: a launch
    # went READY on a port a just-stopped deployment had been using, and every
    # request against it failed. Ports are reused. Ask the engine itself before
    # attributing three workloads' worth of failures to the model.
    reachable = _await_backend(backend)
    if reachable is not None:
        print(f"    the backend at {backend} never answered: {reachable}")
        stop(base, deployment["deployment_id"])
        return records
    print(f"    launched at k={launched_k} ({served})")
    try:
        for workload in workloads:
            prompts = load_workload(workload)
            before = metrics_scrape.read(backend)
            # Two phases against one window. The concurrent pass produces the
            # draft rounds the counters need, fast; the short single-stream
            # pass produces the only tok/s figure the curve is anchored on.
            # Both are inside the same before/after read, so the counters cover
            # every round either phase caused.
            drive_concurrent(
                backend, served, prompts, timeout=timeout, max_tokens=max_tokens
            )
            driven = drive_stream(
                backend, served, prompts[:2], timeout=timeout, max_tokens=max_tokens
            )
            after = metrics_scrape.read(backend)
            if before is None or after is None:
                print(f"    {workload:<11} no /metrics from {backend or 'the backend'}")
                continue
            window = metrics_scrape.delta(before, after)
            record = summarise(
                model_id, method, workload, window, driven, launched_k,
                option, node, runtime_version, baseline_tps, refusals, verbose,
            )
            if record is not None:
                records.append(record)
                # Written here, not at the end of the run. Four sweeps on this
                # box were killed mid-flight, and a run that saves only on
                # completion loses every measurement it already took. A kill
                # now costs the method in flight and nothing behind it.
                if save:
                    path = measurements.save(record)
                    if path is not None and verbose:
                        print(f"        -> {path}")
    finally:
        stop(base, deployment["deployment_id"])
    return records


def summarise(
    model_id: str, method: str, workload: str, window, driven: Driven,
    launched_k: int, option: dict, node: dict, runtime_version: str,
    baseline_tps: float | None, refusals: list[str], verbose: bool,
) -> measurements.SpecRecord | None:
    """Turn one window of counters into a curve over k, and print it."""
    observed = driven.tps
    if driven.errors and driven.requests == 0:
        # Every request failed. Saying "drafted nothing" here would report a
        # transport problem as a property of the model, which is the silent
        # failure this whole tool exists to avoid.
        print(f"    {workload:<11} every request failed ({driven.errors}): "
              f"{driven.reason or 'no reason reported'}")
        return None
    if driven.errors:
        print(f"    {workload:<11} {driven.errors} request(s) failed")
    if window.drafts <= 0:
        # Two very different failures, and the earlier version of this message
        # ran them together. `basis == "none"` means the engine exported no
        # `vllm:spec_decode_*` series AT ALL, which vLLM only does when
        # speculation is disabled -- the flag did not take. Anything else means
        # the counters are there and simply never fired: speculation is on and
        # the drafter found nothing worth proposing, which for ngram on prose
        # is a real answer rather than a fault.
        if window.basis == "none":
            print(f"    {workload:<11} the engine exported no speculation "
                  f"counters — the flag did not reach it")
        else:
            print(f"    {workload:<11} speculation is on and drafted nothing "
                  f"on this workload")
        return None
    if not window.consistent:
        print(f"    {workload:<11} per-position counts disagree with the total; "
              f"treating as aggregate only")

    draft_ratio = 0.0
    active = option.get("draft_params") or 0
    if active and option.get("active_params"):
        draft_ratio = active / option["active_params"]

    # The base rate solved back out of what was actually observed, rather than
    # taken from the bandwidth model. The model says what the hardware could
    # do; this says what it did, and the projection over k is anchored on the
    # second because that is the one that already includes this launch's real
    # scheduling, its real cache pressure and its real prefill.
    expected = window.expected_accepted(launched_k) or 0.0
    base = None
    if observed:
        base = observed * speculative_overhead(launched_k, draft_ratio) / (1.0 + expected)

    curve = speculative_best_k(base or 0.0, draft_ratio, [
        window.acceptance_at(i) or 0.0 for i in range(len(window.accepted_per_pos))
    ])
    best = max(curve, key=lambda p: p.tps) if curve else None

    mean = window.mean_acceptance
    print(f"    {workload:<11} basis={window.basis} drafts={window.drafts:.0f} "
          f"accept={mean if mean is None else round(mean, 3)} "
          f"observed={observed if observed is None else round(observed, 1)} tok/s")
    if verbose:
        for point in curve:
            mark = " <- best" if best and point.k == best.k else ""
            print(f"        k={point.k:<3} E[acc]={point.expected_accepted:5.2f} "
                  f"{point.tps:7.1f} tok/s{mark}")
    if best is None or base is None:
        return None

    return measurements.SpecRecord(
        model_id=model_id, method=method, workload=workload,
        gpu_name=node.get("gpu_name") or "", 
        memory_bandwidth_gbps=float(node.get("memory_bandwidth_gbps") or 0.0),
        runtime_version=runtime_version,
        launched_k=launched_k, best_k=best.k, best_tps=best.tps,
        baseline_tps=float(baseline_tps or 0.0),
        accept_cumulative=[window.acceptance_at(i) or 0.0
                           for i in range(len(window.accepted_per_pos))],
        mean_acceptance=mean, drafts=window.drafts, measured_at=time.time(),
        basis=window.basis, notes=list(refusals),
    )


def node_profile(base: str) -> dict:
    """The hardware a measurement is about, from the coordinator's own roster.

    The first healthy node with a bandwidth figure. A sweep places one
    deployment at a time and the planner puts it on one machine, so a mixed
    cluster would need the placement read back per launch -- worth doing when
    this tool grows a `--node`, and honest to skip while it does not, because
    the record names the GPU it claims and a wrong claim would not match later.
    """
    try:
        cluster = _call(base, "/api/cluster", timeout=30.0)
    except SweepError:
        return {}
    for row in cluster.get("nodes", []):
        profile = row.get("profile") or row
        if profile.get("memory_bandwidth_gbps"):
            return profile
    return {}


def runtime_version(base: str, model_id: str) -> str:
    """The image version a measurement was taken under, or ''.

    Taken from the support verdict on `/api/models/detail`, which is where
    `imageprobe`'s answer surfaces: `record_probe` stores it and
    `RuntimeSupport.version` carries it out. There is no `/api/runtimes` route
    -- the first draft of this asked for one and got a silent empty string,
    which would have written records that never matched anything.

    Empty when the coordinator could not probe its image. A record with no
    version never matches one that has a version, which is the safe direction:
    a measurement whose runtime is unknown must not be presented as current
    for a runtime that is known.
    """
    try:
        detail = _call(
            base, f"/api/models/detail?model_id={parse.quote(model_id, safe='')}",
            timeout=120.0,
        )
    except SweepError:
        return ""
    for entry in ((detail.get("support") or {}).get("runtimes") or []):
        if entry.get("runtime") == "vllm":
            return str(entry.get("version") or "")
    return ""


def _print_scan(model_id: str, base: float, scored: list, rejected: list,
                picked: list, node: dict) -> None:
    print(f"model     {model_id}")
    print(f"hardware  {node.get('gpu_name') or 'unknown'} "
          f"@ {node.get('memory_bandwidth_gbps') or 0:.0f} GB/s")
    print(f"baseline  {base:.1f} tok/s with no speculation")
    print()
    print(f"{len(scored) + len(rejected)} candidates, {len(scored)} usable")
    print()
    print("%-46s %-7s %3s %9s %18s" % ("head", "method", "k", "cost", "if all accepted"))
    # Capped. A popular base model has dozens of published heads and most of a
    # long list is near-duplicates -- nine checkpoints of one training run at
    # identical scores says nothing a single row does not. The shortlist below
    # is what the list is FOR.
    shown = scored[:head_scan.TABLE_ROWS]
    for ceiling, row, option in shown:
        print("%-46s %-7s %3d %6.2f GB %8.0f tok/s (%.1fx)" % (
            row["model_id"][:46], option.method.value, option.max_tokens,
            (option.draft_bytes or 0) / 1e9, ceiling, ceiling / base if base else 0))
    if len(scored) > len(shown):
        print("   ...and %d more usable, mostly variants of the above"
              % (len(scored) - len(shown)))
    if rejected:
        print()
        print(f"rejected ({len(rejected)}):")
        for row, why in rejected[:12]:
            print("   %-46s %s" % (row["model_id"][:46], str(why)[:88]))
        if len(rejected) > 12:
            print(f"   ...and {len(rejected) - 12} more")
    print()
    print("shortlist: " + ", ".join(
        [o.method.value + " " + head_scan.short_name(r["model_id"]) for _, r, o in picked]))
    print("  Ranked by CEILING -- every drafted token accepted. That rewards a")
    print("  small head with a high k, which is not the same as rewarding")
    print("  acceptance, and within a family it barely separates anything.")
    print("  Which of these is actually fastest is only knowable by measuring.")


def run_scan(args) -> tuple[int, list]:
    """The scan. Returns (exit code, the shortlist as sweep options)."""
    from control_plane.resolver import ModelResolver, imageprobe

    model_id = args.model
    resolver = ModelResolver()
    try:
        base_res = resolver.resolve_full(model_id)
    except Exception as exc:
        print(f"could not resolve {model_id}: {exc}", file=sys.stderr)
        return 2, []

    specs = None
    try:
        found = imageprobe.probe(
            "vllm",
            os.environ.get("DERATE_VLLM_IMAGE")
            or "ghcr.io/pizzaman213/derate/vllm-audio:latest",
            cache_dir=resolver.cache.directory.parent / "runtimes",
        )
        specs = found.speculators if found else None
    except Exception:
        specs = None  # no docker, no image: no opinion, never a refusal

    node = node_profile(args.base)
    bandwidth = float(node.get("memory_bandwidth_gbps") or 0.0) or 273.0

    rows = head_scan.candidates(resolver, model_id)
    usable, rejected = head_scan.price(resolver, base_res.shape, rows, specs)
    base, scored = head_scan.rank(usable, base_res.shape, bandwidth)
    picked = head_scan.shortlist(scored, args.measure_top or 4)
    _print_scan(model_id, base, scored, rejected, picked, node)
    return (0 if scored else 1), picked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default=DEFAULT_BASE, help="coordinator base URL")
    parser.add_argument("--model", default=None, help="the model to serve")
    parser.add_argument("--method", action="append", default=None,
                        help="only this method; repeatable")
    parser.add_argument("--head", action="append", default=None,
                        help="an externally published draft head's repository; "
                             "repeatable")
    parser.add_argument("--node", action="append", default=None,
                        help="pin every launch to this node; repeatable. Keeps "
                             "one HuggingFace cache warm and every record on "
                             "one GPU")
    parser.add_argument("--max-tokens", type=int, default=None,
                        help="override each prompt's max_tokens; 96 is plenty "
                             "for a stable acceptance figure and half the "
                             "generation time of the sets' own 192")
    parser.add_argument("--scan", action="store_true",
                        help="find every published draft head for this model, "
                             "price it and rank it. Launches NOTHING, so it "
                             "runs on a busy box")
    parser.add_argument("--measure-top", type=int, default=0,
                        help="after --scan, measure this many from the "
                             "shortlist (one per method family). Needs a "
                             "lane's worth of free memory per method")
    parser.add_argument("--parallel", type=int, default=1,
                        help="methods to run at once, capped at the number of "
                             "--node lanes. Two engines on ONE gpu would share "
                             "its bandwidth and depress the tok/s this anchors "
                             "on, so a lane is a node")
    parser.add_argument("--served-prefix", default=None,
                        help="prefix for each launch's served name; the "
                             "method is appended. Needed because a served name "
                             "is unique cluster-wide")
    parser.add_argument("--baseline", action="store_true",
                        help="also launch WITHOUT speculation to measure the "
                             "reference rate, instead of solving it back out "
                             "of the speculating run")
    parser.add_argument("--workload", action="append", default=None,
                        help="only this prompt set; repeatable")
    parser.add_argument("--max-k", type=int, default=10,
                        help="highest drafted-token count to ask for (default 10)")
    parser.add_argument("--context", type=int, default=8192)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--smoke", action="store_true",
                        help=f"plumbing only, against {SMOKE_MODEL}")
    parser.add_argument("--stop-conflicting", action="store_true",
                        help="stop a deployment already holding this model "
                             "(usually this sweep's own orphan) and retry")
    parser.add_argument("--no-save", action="store_true",
                        help="print the curve and write no record")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="print tok/s at every k, not only the best")
    args = parser.parse_args(argv)

    model_id = args.model or (SMOKE_MODEL if args.smoke else None)
    if not model_id:
        parser.error("--model is required (or --smoke)")
    workloads = args.workload or ["code_edit", "extraction", "prose"]
    if args.smoke:
        # One short set is enough to prove launch, drive, scrape and solve.
        # code_edit rather than prose: ngram accepts most of what it drafts
        # when the answer repeats the question, so a smoke run that is working
        # produces a visibly non-zero acceptance rather than a plausible zero.
        workloads = args.workload or ["code_edit"]
        args.method = args.method or [SMOKE_METHOD]

    if args.scan:
        code, picked = run_scan(args)
        if code or not args.measure_top:
            return code
        # The shortlist becomes the head list for the ordinary path below, so
        # everything downstream -- lanes, served names, the KV accounting, the
        # per-position solve -- is the code that already works.
        args.head = [row["model_id"] for _, row, _ in picked]
        args.method = None
        print()
        print(f"measuring the top {len(args.head)}:")

    print(f"model     {model_id}")
    print(f"base      {args.base}")

    try:
        offered = plan(args.base, model_id)
    except SweepError as exc:
        print(f"could not plan {model_id}: {exc}", file=sys.stderr)
        return 2

    options = list(offered.get("speculative_options") or [])
    shape = offered.get("shape") or {}
    active = shape.get("active_params") or shape.get("total_params") or 0
    for option in options:
        option["active_params"] = active
    for head in args.head or []:
        # An external head is not on the offered list -- nothing knew it existed
        # until it was named -- so each is priced by asking for it. A head the
        # coordinator refuses is reported and skipped rather than taking the
        # run down: one bad repository out of four should cost one method.
        try:
            priced = plan(args.base, model_id, {
                "method": "eagle3", "num_speculative_tokens": 1, "model": head,
            })
        except SweepError as exc:
            print(f"  skipping {head}: {exc}")
            continue
        options.extend(
            o | {"head_model": head, "active_params": active}
            for o in (priced.get("speculative_options") or [])
            if o.get("source") == "head"
        )
    if args.method:
        options = [o for o in options if o["method"] in args.method]
    options = [o for o in options if o.get("launchable")]
    if not options:
        print("nothing to sweep: no launchable speculative method was offered")
        return 1

    node = node_profile(args.base)
    version = runtime_version(args.base, model_id)
    print(f"hardware  {node.get('gpu_name') or 'unknown'} "
          f"@ {node.get('memory_bandwidth_gbps') or 0:.0f} GB/s")
    print(f"runtime   {version or 'unknown'}")

    # The reference every speedup is against. Off by default: the base rate is
    # already solved back out of each speculating run --
    # `base = observed * overhead(k) / (1 + E[accepted])` -- and that figure is
    # better than a separate launch's, because it carries the same launch's own
    # scheduling and cache pressure. `--baseline` spends a launch to cross-check
    # it, which is worth doing once per model and not once per run.
    baseline_tps = None
    if args.baseline:
        baseline_tps = _measure_baseline(args, model_id, workloads)

    # One method per node, run at once. Two rules cap this and both are
    # `manager._find_conflict`'s: a node runs one copy of a model, and a served
    # name is unique cluster-wide. So the ceiling is the node count, and each
    # launch needs a name of its own -- which is why `--served-prefix` exists.
    #
    # Two engines decoding on ONE GPU would be a different matter: they share
    # its memory bandwidth, and the single-stream tok/s this anchors every
    # curve on would read low for a reason that has nothing to do with the
    # method. Across nodes there is no shared bandwidth, so the measurement
    # stays clean -- provided the nodes are comparably loaded, which the run
    # header prints so a reader can judge.
    lanes = args.node or [None]
    width = max(1, min(len(lanes), int(args.parallel or 1)))
    records: list[measurements.SpecRecord] = []
    for batch_start in range(0, len(options), width):
        batch = options[batch_start:batch_start + width]
        if len(batch) > 1:
            print(f"\n  running {len(batch)} in parallel, one per node")
        with cf.ThreadPoolExecutor(max_workers=len(batch)) as pool:
            futures = []
            for offset, option in enumerate(batch):
                lane = lanes[offset % len(lanes)]
                suffix = option["method"]
                if option.get("head_model"):
                    suffix += "-" + option["head_model"].split("/")[-1]
                served = _safe_name(f"{args.served_prefix or model_id.split('/')[-1]}-{suffix}")
                print(f"  {option['method']}"
                      + (f" via {option.get('head_model')}" if option.get("head_model") else "")
                      + (f"  [{lane}, as {served}]" if lane else f"  [as {served}]"))
                futures.append(pool.submit(
                    sweep_method,
                    args.base, model_id, option, args.context, args.max_k,
                    workloads, node, version, args.timeout, baseline_tps,
                    args.verbose, args.stop_conflicting,
                    [lane] if lane else None, args.max_tokens,
                    not args.no_save, served,
                ))
            for future in futures:
                try:
                    records.extend(future.result())
                except Exception as exc:  # one lane must not take the run down
                    print(f"    a lane failed: {exc}")
    return _report(records)


def _safe_name(raw: str) -> str:
    """A served name the recipe grammar will accept.

    `deploy/recipes.py::_COMMAND_SAFE` admits letters, digits and
    ``. _ - : / ~``, and this name reaches a shell command, so anything else is
    replaced rather than passed along to be refused later.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._:/~-]+", "-", raw).strip("-")
    return cleaned[:96] or "spec-sweep"


def _measure_baseline(args, model_id: str, workloads: list[str]) -> float | None:
    """One launch with speculation off, driven single-stream. Opt-in."""
    print("\n  baseline (no speculation)")
    started = None
    try:
        try:
            started = launch(args.base, model_id, args.context, None, args.node)
        except SweepError as exc:
            holder = exc.conflict_id
            if not (args.stop_conflicting and holder):
                raise
            print(f"    stopping the deployment holding this model: {holder}")
            stop(args.base, holder)
            time.sleep(5.0)
            started = launch(args.base, model_id, args.context, None, args.node)
        ready = wait_ready(args.base, started["deployment_id"])
        driven = drive_stream(
            ready.get("backend_url") or args.base, ready["served_name"],
            load_workload(workloads[0])[:3],
            timeout=args.timeout, max_tokens=args.max_tokens,
        )
        print(f"    {workloads[0]:<11} "
              f"{driven.tps if driven.tps is None else round(driven.tps, 1)} tok/s")
        return driven.tps
    except SweepError as exc:
        print(f"    baseline failed: {exc}")
        return None
    finally:
        if started is not None:
            stop(args.base, started["deployment_id"])


def _report(records: list[measurements.SpecRecord]) -> int:
    """The table, and the exit code. Records are already on disk by now."""
    print()
    if not records:
        print("no measurement was taken")
        return 1
    print(f"{'method':<10} {'workload':<11} {'accept':>7} {'best k':>7} "
          f"{'tok/s':>8} {'vs base':>8} {'drafts':>8}")
    for r in records:
        speedup = r.speedup
        print(f"{r.method:<10} {r.workload:<11} {r.mean_acceptance or 0:>7.3f} "
              f"{r.best_k:>7} {r.best_tps:>8.1f} "
              f"{'—' if speedup is None else format(speedup, '.2f') + 'x':>8} "
              f"{r.drafts:>8.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
