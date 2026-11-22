"""Benchmarks: launch a throwaway copy of a launch request, measure it, stop it.

One run at a time. A run is an asyncio task over the same launcher
`POST /api/deployments` uses (`ctx.launcher`), so a benchmark is refused for
exactly the reasons a launch would be, and the copy's served name is
`bench-<id tail>` so the gateway never routes a real client to it. Whatever
happens -- success, a failure mid-sweep, a cancel -- the `finally` stops the
copy: a benchmark that leaves a model holding memory is worse than no number.

Requests go to the deployment's own `backend_url`, not through the gateway,
for the reason `tests/spec_sweep.py::_one` gives: a served name can have more
than one target, and the proxy has no business in a decode-rate measurement.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import statistics
import time
from pathlib import Path
from typing import Awaitable, Callable

import httpx

from control_plane import measurements, metrics_scrape
from control_plane.contracts.deployment import DeploymentState

log = logging.getLogger("gateway.bench")

DATA = Path(__file__).resolve().parents[1] / "bench_data"
WORKLOADS = ("prose", "code_edit", "extraction")
DEFAULT_USERS = (1, 4, 8, 16)
#: Each concurrent stream sends this many prompts back to back.
PROMPTS_PER_STREAM = 2
MAX_TOKENS = 256
READY_POLL_S = 3.0
READY_TIMEOUT_S = 1800.0
REQUEST_TIMEOUT_S = 300.0

Launcher = Callable[..., Awaitable[tuple[int, dict]]]


class BenchBusy(RuntimeError):
    """A benchmark is already running; there is one at a time."""


def load_workload(name: str) -> list[dict]:
    path = DATA / f"{name}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


async def _stream_one(
    client: httpx.AsyncClient, base: str, served_name: str, api_key: str | None,
    row: dict, clock: Callable[[], float] = time.monotonic,
) -> tuple[int, float, float | None, float | None, str]:
    """One streamed completion: (tokens, sent, first chunk, last chunk, error).

    Lifted from `tests/spec_sweep.py::_one`. Streamed because prefill and
    decode are separate clocks: TTFT is first - sent, and the decode rate
    charges tokens after the first against last - first.
    """
    body = {
        "model": served_name,
        "messages": [{"role": "user", "content": row["prompt"]}],
        "max_tokens": min(int(row.get("max_tokens", MAX_TOKENS)), MAX_TOKENS),
        "temperature": 0,
        "stream": True,
    }
    headers = {"content-type": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    sent = clock()
    first = last = None
    tokens = 0
    try:
        async with client.stream(
            "POST", base.rstrip("/") + "/chat/completions",
            json=body, headers=headers, timeout=REQUEST_TIMEOUT_S,
        ) as response:
            if response.status_code >= 400:
                detail = (await response.aread()).decode("utf-8", "replace")[:200]
                return 0, sent, None, None, f"HTTP {response.status_code}: {detail}"
            async for line in response.aiter_lines():
                line = line.strip()
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
                now = clock()
                if first is None:
                    first = now
                last = now
                tokens += 1
    except (httpx.HTTPError, OSError) as exc:
        return 0, sent, None, None, str(exc)[:200] or type(exc).__name__
    return tokens, sent, first, last, ""


def summarize(users: int, results: list[tuple], wall: float) -> dict:
    """One point from one user count's requests. Pure, so the math is tested."""
    ttfts = [(first - sent) * 1000 for _, sent, first, _, err in results
             if not err and first is not None]
    rates = [(tokens - 1) / (last - first) for tokens, _, first, last, err in results
             if not err and tokens >= 2 and last is not None and last > first]
    total_tokens = sum(r[0] for r in results if not r[4])
    return {
        "users": users,
        "ttft_ms_p50": statistics.median(ttfts) if ttfts else None,
        "tps_each_mean": statistics.fmean(rates) if rates else None,
        "tps_total": total_tokens / wall if wall > 0 else None,
        "requests": len(results),
        "errors": sum(1 for r in results if r[4]),
    }


class BenchRunner:
    def __init__(
        self, *, launcher: Callable[[], Launcher | None], deployments,
        directory: Path | None = None, transport: httpx.AsyncBaseTransport | None = None,
        describe: Callable[[list[str]], tuple[str | None, str | None]] | None = None,
        ready_poll_s: float = READY_POLL_S, ready_timeout_s: float = READY_TIMEOUT_S,
        on_done: Callable[[measurements.BenchRecord], None] | None = None,
    ) -> None:
        # `launcher` is a getter: ctx.launcher is installed by create_router,
        # possibly after this runner is built.
        self._launcher = launcher
        self._deployments = deployments
        self._directory = directory
        self._transport = transport
        self._describe = describe
        self._poll = ready_poll_s
        self._timeout = ready_timeout_s
        self._on_done = on_done
        self._task: asyncio.Task | None = None
        self._current: measurements.BenchRecord | None = None
        # Every served name this runner has launched. A bench copy is
        # throwaway: the crash-restart coordinator asks `owns()` and must never
        # relaunch one, or a copy that died at startup comes back after the
        # run's `finally` has already stopped the dead one.
        self._served: set[str] = set()

    # -- reads ---------------------------------------------------------------

    def list(self, model_id: str | None = None) -> list[measurements.BenchRecord]:
        out = measurements.load_bench_all(directory=self._directory)
        if model_id:
            out = [r for r in out
                   if r.model_id == model_id or r.request.get("model_id") == model_id]
        return out

    def get(self, bench_id: str) -> measurements.BenchRecord | None:
        if self._current is not None and self._current.bench_id == bench_id:
            return self._current
        return measurements.load_bench(bench_id, directory=self._directory)

    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def owns(self, served_name: str) -> bool:
        return served_name in self._served

    # -- writes --------------------------------------------------------------

    def start(
        self, request: dict, *, model_id: str | None = None,
        workload: str = "prose", users: list[int] | None = None,
    ) -> measurements.BenchRecord:
        if self.running():
            raise BenchBusy("A benchmark is already running; cancel it or wait for it to finish.")
        bench_id = "b-" + secrets.token_hex(6)
        record = measurements.BenchRecord(
            bench_id=bench_id,
            model_id=model_id or str(request.get("model_id") or ""),
            request=dict(request),
            served_name=f"bench-{bench_id[-6:]}",
            runtime=request.get("runtime"),
            node_ids=list(request.get("node_ids") or []),
            workload=workload,
            users=sorted(users or DEFAULT_USERS),
            started_at=time.time(),
        )
        self._current = record
        self._served.add(record.served_name)
        self._save(record)
        self._task = asyncio.get_running_loop().create_task(self._run(record))
        return record

    def cancel(self, bench_id: str) -> bool:
        """Cancel a running bench; the run's `finally` still stops the copy."""
        if self.running() and self._current and self._current.bench_id == bench_id:
            self._task.cancel()
            return True
        return False

    def delete(self, bench_id: str) -> bool:
        if self.running() and self._current and self._current.bench_id == bench_id:
            return self.cancel(bench_id)
        return measurements.delete_bench(bench_id, directory=self._directory)

    async def wait(self) -> None:
        """Test hook: until the current run has settled."""
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # -- the run -------------------------------------------------------------

    def _save(self, record: measurements.BenchRecord) -> None:
        measurements.save_bench(record, directory=self._directory)

    async def _run(self, record: measurements.BenchRecord) -> None:
        deployment_id: str | None = None
        try:
            launcher = self._launcher()
            if launcher is None:
                raise RuntimeError("no launcher is wired on this coordinator")
            payload = dict(record.request)
            payload.pop("lifecycle", None)
            payload["served_name"] = record.served_name
            status, body = await launcher(payload, actor="benchmark", request=None)
            if status != 201:
                message = ((body.get("error") or {}).get("message")
                           if isinstance(body.get("error"), dict) else None)
                raise RuntimeError(message or f"the launch was refused ({status})")
            deployment_id = body.get("deployment_id")
            record.deployment_id = deployment_id
            record.node_ids = list((body.get("plan") or {}).get("node_ids")
                                   or record.node_ids)
            if self._describe is not None:
                try:
                    record.gpu_name, record.runtime_version = self._describe(record.node_ids)
                except Exception:
                    log.debug("bench describe failed", exc_info=True)
            self._save(record)

            dep = await self._until_ready(deployment_id)
            record.state = "measuring"
            self._save(record)

            base = dep.backend_url
            api_key = getattr(self._deployments, "runtime_api_key", lambda _id: None)(
                deployment_id
            )
            before = None
            if record.request.get("speculative"):
                before = await asyncio.to_thread(metrics_scrape.read, base)

            rows = load_workload(record.workload)
            cursor = 0
            async with httpx.AsyncClient(transport=self._transport) as client:
                for n in record.users:
                    batch: list[dict] = []
                    for _ in range(n * PROMPTS_PER_STREAM):
                        batch.append(rows[cursor % len(rows)])
                        cursor += 1

                    async def stream(i: int, batch=batch) -> list[tuple]:
                        out = []
                        for k in range(PROMPTS_PER_STREAM):
                            out.append(await _stream_one(
                                client, base, record.served_name, api_key,
                                batch[i * PROMPTS_PER_STREAM + k],
                            ))
                        return out

                    t0 = time.monotonic()
                    per = await asyncio.gather(*(stream(i) for i in range(n)))
                    wall = time.monotonic() - t0
                    results = [r for chunk in per for r in chunk]
                    record.points.append(summarize(n, results, wall))
                    self._save(record)

            if before is not None:
                after = await asyncio.to_thread(metrics_scrape.read, base)
                if after is not None:
                    record.spec_mean_acceptance = metrics_scrape.delta(
                        before, after
                    ).mean_acceptance
            record.state = "done"
        except asyncio.CancelledError:
            record.state = "cancelled"
        except Exception as exc:
            record.state = "failed"
            record.error = str(exc) or type(exc).__name__
            log.warning("benchmark %s failed: %s", record.bench_id, record.error)
        finally:
            if deployment_id:
                try:
                    await asyncio.to_thread(
                        self._deployments.stop, deployment_id, reason="benchmark finished"
                    )
                except Exception:
                    log.warning("benchmark %s could not stop %s", record.bench_id,
                                deployment_id, exc_info=True)
            record.finished_at = time.time()
            self._save(record)
            if record.state == "done" and self._on_done is not None:
                try:
                    self._on_done(record)
                except Exception:
                    log.warning("benchmark %s on_done failed", record.bench_id, exc_info=True)

    async def _until_ready(self, deployment_id: str):
        deadline = time.monotonic() + self._timeout
        while True:
            dep = self._deployments.get(deployment_id)
            if dep is not None:
                if dep.state == DeploymentState.READY:
                    return dep
                if dep.state in (DeploymentState.FAILED, DeploymentState.STOPPED):
                    raise RuntimeError(dep.last_error or f"the copy {dep.state.value}")
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"the copy was not ready after {int(self._timeout)} s"
                )
            await asyncio.sleep(self._poll)
