"""Launch one model under every placement the planner ranked, and measure each.

The planner picks TP, PP, EP or a single node from measured link facts and a
step-time model, and `estimated_step_seconds` orders candidates and nothing
else -- its docstring says so. Whether the order it produced is the order the
hardware delivers is a question only a launch answers. exo's bench tool asks it
the direct way, the same model under each sharding across node counts, and this
is that tool for derate:

    python3 -m tests.placement_sweep --model Qwen/Qwen3-4B --nodes spark-26af,spark-4d38
    python3 -m tests.placement_sweep --model X --nodes a,b --dry-run    # bodies only
    python3 -m tests.placement_sweep --model X --nodes a,b --kinds tensor,pipeline

**The candidates are the coordinator's, never this file's.** `POST /api/plan`
names the planner's pick and `POST /api/plan/candidates` the best-ranked plan
of each kind on the same node set, each re-judged by the fit gate at the pick's
context and concurrency. A shape the gate refuses is recorded with its own
sentence and not launched; nothing here re-derives a degree, because a second
enumeration would be a second planner that agrees with the first by luck.

**One launch at a time, each torn down before the next.** Two shapes of one
model cannot share the machines being compared, and a measurement taken beside
another deployment measures the neighbour too. Every launch pins `node_ids` and
`parallelism` to exactly the candidate's, which is the body the Serve panel
sends when somebody overrules the planner (`ui/src/tabs/models/launchRequest.ts`).

**Two concurrencies, two different figures.** At 1 the number is one stream's
decode rate, tokens after the first over the window from first chunk to last --
the figure the Verdict card predicts. At N it is the AGGREGATE over the phase's
wall clock, because that is what a pipeline buys and a single-stream number
cannot show: CLAUDE.md's "a pipeline at concurrency 1 costs the same at every
degree" is exactly the case where the two orders disagree. TTFT is from the
request leaving to the first token arriving, at each concurrency.

Requests go to each deployment's own `backend_url`, not through `/v1`, for the
reason `spec_sweep._one` gives: routing by served name can land on another
deployment, and the proxy has no business in a decode measurement.

Auth is `ui/src/check/auth.mjs`'s: `POST /api/auth/login` as
`$DERATE_CHECK_ADMIN_USER` / `$DERATE_CHECK_ADMIN_PASSWORD`, and the session
cookie on every `/api` call. Unset credentials are not a skip -- the first
gated call answers 401 and the run stops saying so.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import httpx

from tests.spec_sweep import READY_TIMEOUT_S, SweepError, load_workload

DEFAULT_BASE = os.environ.get("DERATE_CHECK_ORIGIN", "http://localhost:8088")
DEFAULT_WORKLOAD = "prose"
DEFAULT_CONCURRENCY = 8

#: The fit gate's two passing verdicts. `fits_degraded` loads and is slow by
#: the gate's own prediction -- which is precisely the prediction worth testing.
VIABLE_VERDICTS = ("fits", "fits_degraded")

DEGREE_AXES = ("tensor_parallel", "pipeline_parallel", "expert_parallel", "data_parallel")


# -- the coordinator, over HTTP only ---------------------------------------


def login(client: httpx.Client, user: str, password: str) -> None:
    """Sign *client* in as an admin; every later `/api` call carries the cookie.

    The cookie is copied into a header rather than left to the jar, as
    `auth.mjs` does: the gateway marks it Secure behind https, and a cookie
    jar declines to send a Secure cookie back over plain http.
    """
    r = client.post("/api/auth/login", json={"username": user, "password": password})
    if r.status_code != 200:
        raise SweepError(f"login as {user} answered {r.status_code}: {r.text[:200]}")
    m = re.search(r"((?:__Host-)?derate_session)=([^;]+)", r.headers.get("set-cookie", ""))
    if not m:
        raise SweepError("the login set no derate_session cookie")
    client.headers["cookie"] = f"{m.group(1)}={m.group(2)}"


def _call(client: httpx.Client, method: str, path: str, *, body: dict | None = None,
          timeout: float = 300.0) -> dict | list:
    try:
        r = client.request(method, path, json=body, timeout=timeout)
    except httpx.HTTPError as exc:
        raise SweepError(f"{method} {path}: {exc}") from exc
    if r.status_code >= 400:
        try:
            parsed = r.json()
        except ValueError:
            parsed = None
        err = parsed.get("error") if isinstance(parsed, dict) else None
        message = (err or {}).get("message") if isinstance(err, dict) else None
        # The coordinator's refusal, whole: it names what to change.
        failure = SweepError(
            message or f"{method} {path}: HTTP {r.status_code}: {r.text[:200]}",
            parsed if isinstance(parsed, dict) else None,
        )
        failure.status = r.status_code
        raise failure
    return r.json() if r.content else {}


def plan_body(model_id: str, node_ids: list[str], concurrency: int,
              context: int | None) -> dict:
    """What `/api/plan` and `/api/plan/candidates` are asked -- the Serve
    panel's `buildPlanRequest` with the machines named and the degrees left to
    the planner."""
    body: dict = {
        "model_id": model_id, "target": "throughput", "runtime": "vllm",
        "node_ids": list(node_ids), "concurrency": int(concurrency),
    }
    if context:
        body["context"] = int(context)
    return body


# -- the pure part: which shapes, which bodies, which order -----------------


@dataclass
class Candidate:
    """One shape the planner ranked, as the coordinator described it."""

    kind: str
    degrees: dict[str, int]
    node_ids: list[str]
    #: Position in the planner's ranking, 0 = its pick. The candidates route
    #: keeps the first plan of each kind in ranked order, so this is the order.
    planner_rank: int
    recommended: bool
    verdict: str | None
    reason: str | None
    #: The gate's or the placement check's own sentence when it refused.
    error: str | None

    @property
    def viable(self) -> bool:
        return self.error is None and self.verdict in VIABLE_VERDICTS

    @property
    def label(self) -> str:
        d = self.degrees
        shape = " ".join(
            f"{name}{d.get(axis, 1)}"
            for name, axis in zip(("TP", "PP", "EP", "DP"), DEGREE_AXES)
            if d.get(axis, 1) > 1
        ) or "1 rank"
        return f"{self.kind} ({shape} on {','.join(self.node_ids)})"


def enumerate_candidates(plan: dict, candidates: dict | None) -> list[Candidate]:
    """Every shape to consider, planner-ranked, from the two responses.

    *candidates* None means the gateway predates `/api/plan/candidates`; the
    plan's own `alternatives` are then the list, one per kind in the order
    ranked, with no verdict -- the launch gate still refuses one that does not
    fit, in its own words, so nothing unjudged reaches an engine.
    """
    recommended = plan.get("recommended_plan") or plan.get("plan") or {}
    if candidates is not None:
        rows = candidates.get("candidates") or []
        pick = candidates.get("recommended_kind") or recommended.get("kind")
        return [
            Candidate(
                kind=row["kind"],
                degrees={axis: int((row.get("degrees") or {}).get(axis, 1)) for axis in DEGREE_AXES},
                node_ids=list(row.get("node_ids") or []),
                planner_rank=rank,
                recommended=row["kind"] == pick,
                verdict=row.get("verdict"),
                reason=row.get("reason"),
                error=row.get("error"),
            )
            for rank, row in enumerate(rows)
        ]
    out: list[Candidate] = []
    seen: set[str] = set()
    for alt in plan.get("alternatives") or []:
        if alt["kind"] in seen:
            continue
        seen.add(alt["kind"])
        out.append(Candidate(
            kind=alt["kind"],
            degrees={axis: int(alt.get(axis, 1)) for axis in DEGREE_AXES},
            node_ids=list(alt.get("node_ids") or []),
            planner_rank=len(out),
            recommended=alt["kind"] == recommended.get("kind"),
            verdict="fits",  # unjudged; the launch route is the gate
            reason=None,
            error=None,
        ))
    return out


def served_name_for(model_id: str, kind: str) -> str:
    """Distinct per shape: the served-name rule is cluster-wide, so a shape's
    orphan from a killed run must not collide with the next shape's launch.
    The alphabet is `deploy/recipes.py::_COMMAND_SAFE`'s."""
    raw = f"{model_id.rsplit('/', 1)[-1]}-placement-{kind}"
    return re.sub(r"[^A-Za-z0-9._:/~-]+", "-", raw).strip("-")[:96]


def launch_body(c: Candidate, model_id: str, context: int, concurrency: int) -> dict:
    """`POST /api/deployments` for exactly this shape on exactly these hosts.

    Both halves are sent, so the gateway's `_check_every_node_used` holds the
    launch to the candidate rather than letting the planner re-choose: naming
    machines alone leaves the degrees as advice.
    """
    return {
        "model_id": model_id,
        "context": int(context),
        "concurrency": int(concurrency),
        "target": "throughput",
        "runtime": "vllm",
        "node_ids": list(c.node_ids),
        "parallelism": dict(c.degrees),
        "served_name": served_name_for(model_id, c.kind),
    }


@dataclass
class Sample:
    """One streamed completion. Times are `time.perf_counter()` seconds."""

    sent: float
    first: float | None
    last: float | None
    tokens: int
    error: str = ""


@dataclass
class Phase:
    """One concurrency's figures for one shape."""

    concurrency: int
    requests: int = 0
    errors: int = 0
    reason: str = ""
    #: Mean per-request decode rate, first token excluded.
    decode_tps_each: float | None = None
    #: All tokens over the phase's wall clock, first send to last token.
    aggregate_tps: float | None = None
    ttft_ms_p50: float | None = None
    ttft_ms_mean: float | None = None


def summarise_phase(samples: list[Sample], concurrency: int) -> Phase:
    out = Phase(concurrency=concurrency)
    ok = [s for s in samples if not s.error and s.first is not None]
    out.errors = len(samples) - len(ok)
    out.reason = next((s.error for s in samples if s.error), "")
    out.requests = len(ok)
    if not ok:
        return out
    rates = [
        (s.tokens - 1) / (s.last - s.first)
        for s in ok
        if s.tokens > 1 and s.last is not None and s.last > s.first
    ]
    if rates:
        out.decode_tps_each = statistics.fmean(rates)
    wall = max(s.last if s.last is not None else s.first for s in ok) - min(s.sent for s in ok)
    if wall > 0:
        out.aggregate_tps = sum(s.tokens for s in ok) / wall
    ttfts = [(s.first - s.sent) * 1000.0 for s in ok]
    out.ttft_ms_p50 = statistics.median(ttfts)
    out.ttft_ms_mean = statistics.fmean(ttfts)
    return out


@dataclass
class Result:
    """What happened to one candidate."""

    candidate: Candidate
    #: measured | refused (the gate or the launch said no) | failed (launched
    #: and never served, or every request failed) | dry-run.
    status: str
    error: str | None = None
    deployment_id: str | None = None
    phases: list[Phase] = field(default_factory=list)

    def phase(self, concurrency: int) -> Phase | None:
        return next((p for p in self.phases if p.concurrency == concurrency), None)


def _order(results: list[Result], concurrency: int, metric: str) -> list[str]:
    scored = []
    for r in results:
        p = r.phase(concurrency)
        value = getattr(p, metric) if p is not None else None
        if r.status == "measured" and value is not None:
            scored.append((value, r.candidate.kind))
    return [kind for _, kind in sorted(scored, key=lambda x: -x[0])]


def compare(results: list[Result], concurrency: int) -> dict:
    """The planner's order against the measured one, at 1 and at *concurrency*.

    At 1 the shapes are ordered by one stream's decode rate, at N by the
    aggregate -- the two questions a placement can be asked. `agrees` is None
    when the pick was not measured: a pick that never served has no rank to
    agree or disagree with, and False there would blame the planner for a
    launch failure.
    """
    pick = next((r.candidate.kind for r in results if r.candidate.recommended), None)
    planner_order = [
        r.candidate.kind
        for r in sorted(results, key=lambda r: r.candidate.planner_rank)
        if r.status == "measured"
    ]
    out: dict = {"planner_pick": pick, "planner_order": planner_order, "by_concurrency": []}
    for c, metric in ((1, "decode_tps_each"), (concurrency, "aggregate_tps")):
        if c == 1 and metric == "aggregate_tps":
            continue
        order = _order(results, c, metric)
        best = order[0] if order else None
        figure = {
            r.candidate.kind: getattr(r.phase(c), metric)
            for r in results if r.phase(c) is not None
        }
        pick_value = figure.get(pick) if pick else None
        best_value = figure.get(best) if best else None
        out["by_concurrency"].append({
            "concurrency": c,
            "metric": metric,
            "measured_order": order,
            "measured_best": best,
            "agrees": None if pick not in order else best == pick,
            #: The pick's figure over the best one's; 1.0 when they agree.
            "pick_vs_best": (
                pick_value / best_value
                if pick_value is not None and best_value else None
            ),
        })
    return out


def _fmt(value: float | None, spec: str = ".1f") -> str:
    return "-" if value is None else format(value, spec)


def render_table(results: list[Result], comparison: dict, concurrency: int) -> str:
    lines = [
        f"{'rank':<5}{'shape':<44}{'status':<10}"
        f"{'tok/s @1':>10}{'ttft@1 ms':>11}"
        f"{f'agg @{concurrency}':>11}{f'ttft@{concurrency} ms':>12}",
    ]
    for r in sorted(results, key=lambda r: r.candidate.planner_rank):
        one, many = r.phase(1), r.phase(concurrency)
        mark = "*" if r.candidate.recommended else " "
        lines.append(
            f"{mark}{r.candidate.planner_rank:<4}{r.candidate.label[:43]:<44}{r.status:<10}"
            f"{_fmt(one.decode_tps_each if one else None):>10}"
            f"{_fmt(one.ttft_ms_p50 if one else None, '.0f'):>11}"
            f"{_fmt(many.aggregate_tps if many else None):>11}"
            f"{_fmt(many.ttft_ms_p50 if many else None, '.0f'):>12}"
        )
        if r.error:
            lines.append(f"     {r.error}")
    lines.append("  * the planner's pick")
    for row in comparison["by_concurrency"]:
        if not row["measured_order"]:
            lines.append(f"  at {row['concurrency']}: nothing was measured")
            continue
        verdict = {True: "agrees with", False: "disagrees with", None: "cannot be compared to"}[row["agrees"]]
        lines.append(
            f"  at {row['concurrency']} ({row['metric']}): measured "
            f"{' > '.join(row['measured_order'])}; {verdict} the planner's pick "
            f"({comparison['planner_pick']})"
            + (f", which delivered {row['pick_vs_best']:.0%} of the best"
               if row["agrees"] is False and row["pick_vs_best"] is not None else "")
        )
    return "\n".join(lines)


# -- launching and driving --------------------------------------------------


def wait_ready(client: httpx.Client, deployment_id: str, *, timeout: float) -> dict:
    """READY, or the runtime's own words -- `spec_sweep.wait_ready` over a
    signed-in client, including its dead-engine check."""
    try:
        from control_plane.deploy.progress import _RUNTIME_FATAL
    except Exception:
        _RUNTIME_FATAL = ()
    deadline = time.time() + timeout
    polls = 0
    while time.time() < deadline:
        row = _call(client, "GET", f"/api/deployments/{deployment_id}")
        state = row.get("state")
        if state == "ready":
            return row
        if state in ("failed", "stopped"):
            raise SweepError(row.get("last_error") or f"launch ended {state}")
        polls += 1
        if polls % 10 == 0 and _RUNTIME_FATAL:
            try:
                logs = _call(client, "GET", f"/api/deployments/{deployment_id}/logs", timeout=30.0)
            except SweepError:
                logs = {}
            for line in reversed([str(l) for l in (logs.get("lines") or [])]):
                if any(marker in line for marker in _RUNTIME_FATAL):
                    raise SweepError(line.strip())
        time.sleep(3.0)
    raise SweepError(f"still not ready after {timeout:.0f}s")


STOP_TIMEOUT_S = 300.0


def _backend_answers(backend: str) -> bool:
    """True for ANY HTTP response, a 5xx included: an engine that answers 500
    is still alive and still holds its memory. Only a connection-level failure
    means it is gone."""
    try:
        httpx.get(backend.rstrip("/") + "/models", timeout=5.0)
    except httpx.HTTPError:
        return False
    return True


def stop(client: httpx.Client, deployment_id: str, *, timeout: float = STOP_TIMEOUT_S,
         poll_s: float = 2.0, answers: Callable[[str], bool] = _backend_answers,
         sleep: Callable[[float], None] = time.sleep,
         clock: Callable[[], float] = time.time) -> None:
    """Stop *deployment_id* and return only once it is gone: the record says
    stopped or failed (or no longer exists) AND its engine no longer answers.

    The next shape must never run beside a still-alive engine -- it would share
    the memory and the wire and the comparison would measure both. So a stop
    the coordinator refuses, or one that does not finish within *timeout*,
    raises SweepError and the sweep ends.
    """
    backend = ""
    try:
        row = _call(client, "GET", f"/api/deployments/{deployment_id}", timeout=30.0)
        backend = str(row.get("backend_url") or "")
    except SweepError as exc:
        if getattr(exc, "status", None) != 404:
            raise SweepError(f"could not read {deployment_id} before stopping it: {exc}") from exc
        return
    try:
        _call(client, "DELETE", f"/api/deployments/{deployment_id}", timeout=120.0)
    except SweepError as exc:
        if getattr(exc, "status", None) != 404:
            raise SweepError(f"stopping {deployment_id} failed: {exc}; ending the sweep so "
                             f"the next shape does not run beside it") from exc
    deadline = clock() + timeout
    state: str | None = "unknown"
    while True:
        try:
            state = _call(client, "GET", f"/api/deployments/{deployment_id}",
                          timeout=30.0).get("state")
        except SweepError as exc:
            if getattr(exc, "status", None) != 404:
                raise
            state = None  # the record is gone: stopped
        if (state is None or state in ("stopped", "failed")) \
                and not (backend and answers(backend)):
            return
        if clock() >= deadline:
            what = (f"is still {state}" if state not in (None, "stopped", "failed")
                    else f"is {state or 'gone'} but its engine at {backend} still answers")
            raise SweepError(f"{deployment_id} {what} {timeout:.0f}s after it was stopped; "
                             f"ending the sweep so the next shape does not run beside it")
        sleep(poll_s)


def one_request(backend: str, served: str, row: dict, *, timeout: float,
                max_tokens: int | None) -> Sample:
    """One streamed chat completion straight to the engine.

    The token count is the engine's own `usage.completion_tokens` when it
    sends one (vLLM does under `include_usage`) and the number of content
    chunks otherwise. `reasoning_content` counts: a thinking model's first
    tokens are reasoning, and leaving them out would time TTFT to the end of
    its thinking.
    """
    body = {
        "model": served,
        "messages": [{"role": "user", "content": row["prompt"]}],
        "max_tokens": int(max_tokens or row.get("max_tokens", 192)),
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    sent = time.perf_counter()
    first = last = None
    chunks = 0
    usage = None
    try:
        with httpx.stream("POST", backend.rstrip("/") + "/chat/completions",
                          json=body, timeout=timeout) as r:
            if r.status_code != 200:
                return Sample(sent, None, None, 0, f"HTTP {r.status_code}: {r.read()[:200]!r}")
            for line in r.iter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    event = json.loads(payload)
                except ValueError:
                    continue
                if event.get("usage"):
                    usage = event["usage"].get("completion_tokens")
                delta = ((event.get("choices") or [{}])[0] or {}).get("delta") or {}
                if not (delta.get("content") or delta.get("reasoning_content")):
                    continue
                now = time.perf_counter()
                first = now if first is None else first
                last = now
                chunks += 1
    except httpx.HTTPError as exc:
        return Sample(sent, None, None, 0, str(exc)[:200])
    if first is None:
        return Sample(sent, None, None, 0, "the engine streamed no tokens")
    return Sample(sent, first, last, int(usage or chunks))


def drive(backend: str, served: str, prompts: list[dict], concurrency: int, *,
          timeout: float, max_tokens: int | None) -> Phase:
    """The prompt set at *concurrency* streams; at least *concurrency* requests,
    cycling the set, so the last wave is as full as the first."""
    count = max(len(prompts), concurrency)
    rows = [prompts[i % len(prompts)] for i in range(count)]
    with cf.ThreadPoolExecutor(max_workers=concurrency) as pool:
        samples = list(pool.map(
            lambda row: one_request(backend, served, row, timeout=timeout, max_tokens=max_tokens),
            rows,
        ))
    return summarise_phase(samples, concurrency)


def _await_backend(backend: str, *, tries: int = 10, delay: float = 3.0) -> str | None:
    """None once the engine answers `/models`; READY is not reachability."""
    last = "not attempted"
    for _ in range(max(1, tries)):
        try:
            r = httpx.get(backend.rstrip("/") + "/models", timeout=10.0)
            if r.status_code == 200:
                return None
            last = f"HTTP {r.status_code}"
        except httpx.HTTPError as exc:
            last = str(exc)[:120]
        time.sleep(delay)
    return last


def measure_one(client: httpx.Client, c: Candidate, body: dict, prompts: list[dict],
                concurrency: int, args) -> Result:
    try:
        started = _call(client, "POST", "/api/deployments", body=body)
    except SweepError as exc:
        holder = exc.conflict_id
        if not (args.stop_conflicting and holder):
            return Result(c, "refused", str(exc))
        print(f"    stopping the deployment holding this model: {holder}")
        stop(client, holder, timeout=getattr(args, "stop_timeout", STOP_TIMEOUT_S))
        try:
            started = _call(client, "POST", "/api/deployments", body=body)
        except SweepError as again:
            return Result(c, "refused", str(again))
    dep_id = started["deployment_id"]
    result = Result(c, "failed", deployment_id=dep_id)
    try:
        try:
            ready = wait_ready(client, dep_id, timeout=args.ready_timeout)
        except SweepError as exc:
            result.error = str(exc)
            return result
        backend = ready.get("backend_url") or ""
        unreachable = "no backend_url" if not backend else _await_backend(backend)
        if unreachable is not None:
            result.error = f"the engine at {backend or '?'} never answered: {unreachable}"
            return result
        served = ready.get("served_name") or body["served_name"]
        # One warm-up request: the first one through a fresh engine pays for
        # lazy initialisation that no later request does.
        one_request(backend, served, prompts[0], timeout=args.request_timeout, max_tokens=8)
        for level in sorted({1, concurrency}):
            phase = drive(backend, served, prompts, level,
                          timeout=args.request_timeout, max_tokens=args.max_tokens)
            result.phases.append(phase)
            print(f"    @{level:<3} {_fmt(phase.decode_tps_each)} tok/s each, "
                  f"{_fmt(phase.aggregate_tps)} aggregate, ttft p50 "
                  f"{_fmt(phase.ttft_ms_p50, '.0f')} ms, {phase.errors} errors")
        if all(p.requests == 0 for p in result.phases):
            result.error = next((p.reason for p in result.phases if p.reason), "every request failed")
            return result
        result.status = "measured"
        return result
    finally:
        # Confirmed gone, then settle, before the next shape claims the hosts.
        # A stop that cannot be confirmed raises and ends the sweep.
        stop(client, dep_id, timeout=getattr(args, "stop_timeout", STOP_TIMEOUT_S))
        time.sleep(args.settle)


# -- the run -----------------------------------------------------------------


def _default_out(model_id: str) -> Path:
    from control_plane.paths import data_dir

    stamp = time.strftime("%Y%m%dT%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", model_id)
    return data_dir() / "measurements" / "placement" / f"{safe}-{stamp}.json"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python3 -m tests.placement_sweep",
        description=__doc__.splitlines()[0],
    )
    p.add_argument("--model", required=True, help="HuggingFace id to launch under each shape")
    p.add_argument("--nodes", required=True,
                   help="comma-separated node ids to plan across, pipeline head first")
    p.add_argument("--base", default=DEFAULT_BASE,
                   help=f"coordinator origin (default {DEFAULT_BASE}, or $DERATE_CHECK_ORIGIN)")
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                   help=f"the second concurrency to drive, and what the plan is sized for "
                        f"(default {DEFAULT_CONCURRENCY})")
    p.add_argument("--context", type=int, default=None,
                   help="context to plan and launch at (default: the coordinator derives one)")
    p.add_argument("--workload", default=DEFAULT_WORKLOAD,
                   help=f"prompt set from control_plane/bench_data/ (default {DEFAULT_WORKLOAD})")
    p.add_argument("--max-tokens", type=int, default=None,
                   help="override each prompt's max_tokens")
    p.add_argument("--kinds", default="",
                   help="comma-separated kinds to keep (single_node,tensor,pipeline,expert,hybrid)")
    p.add_argument("--dry-run", action="store_true",
                   help="print each launch body and launch nothing")
    p.add_argument("--stop-conflicting", action="store_true",
                   help="stop a deployment holding this model rather than skipping the shape")
    p.add_argument("--ready-timeout", type=float, default=READY_TIMEOUT_S)
    p.add_argument("--request-timeout", type=float, default=300.0)
    p.add_argument("--settle", type=float, default=10.0,
                   help="seconds to wait after a stop before the next launch")
    p.add_argument("--stop-timeout", type=float, default=STOP_TIMEOUT_S,
                   help="seconds a stopped shape has to be gone before the sweep ends")
    p.add_argument("--out", type=Path, default=None,
                   help="where the JSON record goes (default data_dir()/measurements/placement/)")
    return p


def main(argv: list[str] | None = None, *, client: httpx.Client | None = None) -> int:
    args = build_parser().parse_args(argv)
    nodes = [n.strip() for n in args.nodes.split(",") if n.strip()]
    if not nodes:
        print("--nodes names no machine", file=sys.stderr)
        return 2
    if args.concurrency < 1:
        print("--concurrency must be at least 1", file=sys.stderr)
        return 2
    owned = client is None
    if client is None:
        client = httpx.Client(base_url=args.base.rstrip("/"))
        user = os.environ.get("DERATE_CHECK_ADMIN_USER", "")
        if user:
            try:
                login(client, user, os.environ.get("DERATE_CHECK_ADMIN_PASSWORD", ""))
            except SweepError as exc:
                print(f"login: {exc}", file=sys.stderr)
                return 1
    try:
        return _run(client, args, nodes)
    except SweepError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if owned:
            client.close()


def _run(client: httpx.Client, args, nodes: list[str]) -> int:
    asked = plan_body(args.model, nodes, args.concurrency, args.context)
    plan = _call(client, "POST", "/api/plan", body=asked)
    try:
        candidates = _call(client, "POST", "/api/plan/candidates", body=asked)
    except SweepError as exc:
        # A gateway that predates the route; anything else is a real refusal.
        if getattr(exc, "status", None) != 404:
            raise
        candidates = None
    context = int(plan.get("context") or args.context or 0)
    concurrency = int(plan.get("concurrency") or args.concurrency)
    if not context:
        raise SweepError("the plan echoed no context; pass --context")

    found = enumerate_candidates(plan, candidates)
    keep = {k.strip() for k in args.kinds.split(",") if k.strip()}
    if keep:
        found = [c for c in found if c.kind in keep]
    if not found:
        raise SweepError("the planner offered no shape for these machines")

    pick = plan.get("recommended_plan") or plan.get("plan") or {}
    print(f"model       {args.model}")
    print(f"machines    {', '.join(nodes)}")
    print(f"sized at    context {context}, concurrency {concurrency}")
    print(f"planner     {pick.get('kind')}: {pick.get('reason', '')}")
    print()

    prompts = [] if args.dry_run else load_workload(args.workload)
    results: list[Result] = []
    for c in found:
        mark = "*" if c.recommended else " "
        print(f"{mark}{c.planner_rank} {c.label}  [{c.verdict or 'refused'}]")
        if not c.viable:
            # The gate's own sentence, whole.
            print(f"    not launched: {c.error or f'verdict {c.verdict}'}")
            results.append(Result(c, "refused", c.error or f"verdict {c.verdict}"))
            continue
        # The context and concurrency every candidate was judged at, echoed by
        # the coordinator -- launching at anything else launches a different
        # verdict than the one printed above.
        body = launch_body(c, args.model, context, concurrency)
        if args.dry_run:
            print("    POST /api/deployments " + json.dumps(body, sort_keys=True))
            results.append(Result(c, "dry-run"))
            continue
        result = measure_one(client, c, body, prompts, args.concurrency, args)
        if result.error:
            print(f"    {result.status}: {result.error}")
        results.append(result)

    if args.dry_run:
        print(f"\ndry run: {sum(r.status == 'dry-run' for r in results)} of "
              f"{len(results)} shapes would launch, one at a time")
        return 0

    comparison = compare(results, args.concurrency)
    print()
    print(render_table(results, comparison, args.concurrency))
    record = {
        "model_id": args.model,
        "node_ids": nodes,
        "context": context,
        "concurrency": args.concurrency,
        "workload": args.workload,
        "measured_at": time.time(),
        "planner_reason": pick.get("reason"),
        "results": [
            {**asdict(r.candidate), "status": r.status, "error": r.error,
             "deployment_id": r.deployment_id, "phases": [asdict(p) for p in r.phases]}
            for r in results
        ],
        "comparison": comparison,
    }
    out = args.out or _default_out(args.model)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2) + "\n")
    print(f"\nrecord: {out}")
    return 0 if any(r.status == "measured" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
