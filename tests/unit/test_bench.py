"""gateway/bench.py: the math, the throwaway copy always stopped, one at a time."""

from __future__ import annotations

import asyncio
import itertools
import json
from types import SimpleNamespace

import httpx
import pytest

from control_plane import measurements
from control_plane.contracts.deployment import DeploymentState
from control_plane.gateway import bench
from control_plane.gateway.bench import BenchBusy, BenchRunner, _stream_one, summarize


def _sse(words: list[str]) -> bytes:
    lines = [
        "data: " + json.dumps({"choices": [{"delta": {"content": w}}]})
        for w in words
    ]
    lines.append("data: " + json.dumps({"choices": [{"delta": {}}]}))
    lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode()


class _SlowStream(httpx.AsyncByteStream):
    def __init__(self, words, gate: asyncio.Event | None):
        self._words, self._gate = words, gate

    async def __aiter__(self):
        if self._gate is not None:
            await self._gate.wait()
        yield _sse(self._words)


def _transport(words=("a", "b", "c", "d", "e"), gate=None, seen=None):
    async def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, stream=_SlowStream(list(words), gate))

    return httpx.MockTransport(handler)


class _Deps:
    def __init__(self, ready_after: int = 1, fail: bool = False):
        self.calls = 0
        self.ready_after = ready_after
        self.fail = fail
        self.stopped: list[tuple[str, str | None]] = []

    def get(self, deployment_id):
        self.calls += 1
        if self.fail:
            state = DeploymentState.FAILED
        elif self.calls >= self.ready_after:
            state = DeploymentState.READY
        else:
            state = DeploymentState.LAUNCHING
        return SimpleNamespace(
            state=state, backend_url="http://engine/v1", last_error="engine died"
        )

    def runtime_api_key(self, deployment_id):
        return "rk"

    def stop(self, deployment_id, *, reason=None):
        self.stopped.append((deployment_id, reason))


def _launcher(status=201, seen=None):
    async def launch(payload, *, actor, request=None):
        if seen is not None:
            seen.append((payload, actor, request))
        if status != 201:
            return status, {"error": {"message": "would not fit"}}
        return 201, {"deployment_id": "dep-1", "plan": {"node_ids": ["spark-01"]}}

    return launch


def _runner(tmp_path, deps, launcher, transport):
    return BenchRunner(
        launcher=lambda: launcher, deployments=deps, directory=tmp_path,
        transport=transport, ready_poll_s=0.0, ready_timeout_s=5.0,
    )


REQ = {"model_id": "Qwen/Qwen3-0.6B", "served_name": "x", "lifecycle": {"priority": "high"},
       "speculative": None}


def test_summarize_math():
    # (tokens, sent, first, last, err)
    results = [
        (5, 0.0, 0.1, 0.5, ""),   # ttft 100 ms, 4 tokens / 0.4 s = 10 tok/s
        (3, 0.0, 0.3, 0.7, ""),   # ttft 300 ms, 2 / 0.4 = 5 tok/s
        (0, 0.0, None, None, "HTTP 500: boom"),
    ]
    point = summarize(2, results, wall=2.0)
    assert point["users"] == 2
    assert point["ttft_ms_p50"] == pytest.approx(200.0)
    assert point["tps_each_mean"] == pytest.approx(7.5)
    assert point["tps_total"] == pytest.approx(4.0)  # 8 tokens / 2 s
    assert point["requests"] == 3 and point["errors"] == 1


def test_stream_one_clocks_first_and_last_chunk():
    ticks = itertools.count(0.0, 0.25)
    seen = []

    async def go():
        async with httpx.AsyncClient(transport=_transport(seen=seen)) as client:
            return await _stream_one(
                client, "http://engine/v1", "bench-x", "rk",
                {"prompt": "hi", "max_tokens": 999}, clock=lambda: next(ticks),
            )

    tokens, sent, first, last, err = asyncio.run(go())
    assert (tokens, sent, first, last, err) == (5, 0.0, 0.25, 1.25, "")
    body = json.loads(seen[0].content)
    assert body["max_tokens"] == 256 and body["stream"] is True
    assert body["temperature"] == 0 and body["model"] == "bench-x"
    assert seen[0].headers["authorization"] == "Bearer rk"
    assert str(seen[0].url) == "http://engine/v1/chat/completions"


def test_success_measures_each_user_count_and_stops_the_copy(tmp_path):
    deps = _Deps(ready_after=2)
    launched = []

    async def go():
        r = _runner(tmp_path, deps, _launcher(seen=launched), _transport())
        rec = r.start(REQ, users=[4, 1])
        await r.wait()
        return rec

    rec = asyncio.run(go())
    assert rec.state == "done", rec.error
    assert [p["users"] for p in rec.points] == [1, 4]
    assert rec.points[1]["requests"] == 8 and rec.points[1]["errors"] == 0
    assert rec.points[0]["tps_each_mean"] > 0
    payload, actor, request = launched[0]
    assert actor == "benchmark" and request is None
    assert "lifecycle" not in payload
    assert payload["served_name"] == rec.served_name == f"bench-{rec.bench_id[-6:]}"
    assert deps.stopped == [("dep-1", "benchmark finished")]
    stored = measurements.load_bench(rec.bench_id, directory=tmp_path)
    assert stored.state == "done" and stored.node_ids == ["spark-01"]


@pytest.mark.parametrize("runtime", ["sglang", "llamacpp"])
def test_a_record_is_described_by_the_runtime_it_benchmarked(tmp_path, runtime):
    """The version a benchmark is filed under is the image the measured
    machine runs for THIS runtime -- an sglang run is not a vLLM one."""
    asked: list[tuple[list[str], str]] = []

    def describe(node_ids, runtime):
        asked.append((list(node_ids), runtime))
        return "GPU", f"{runtime}-1.0"

    async def go():
        r = BenchRunner(
            launcher=lambda: _launcher(), deployments=_Deps(), directory=tmp_path,
            transport=_transport(), ready_poll_s=0.0, ready_timeout_s=5.0,
            describe=describe,
        )
        rec = r.start({**REQ, "runtime": runtime}, users=[1])
        await r.wait()
        return rec

    rec = asyncio.run(go())
    assert asked == [(["spark-01"], runtime)]
    assert (rec.gpu_name, rec.runtime_version) == ("GPU", f"{runtime}-1.0")


def test_refused_launch_fails_with_the_servers_message(tmp_path):
    deps = _Deps()

    async def go():
        r = _runner(tmp_path, deps, _launcher(status=400), _transport())
        rec = r.start(REQ, users=[1])
        await r.wait()
        return rec

    rec = asyncio.run(go())
    assert rec.state == "failed" and rec.error == "would not fit"
    assert deps.stopped == []


def test_failure_mid_sweep_still_stops(tmp_path, monkeypatch):
    deps = _Deps()
    calls = {"n": 0}
    real = bench.summarize

    def boom(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("sweep broke")
        return real(*a, **k)

    monkeypatch.setattr(bench, "summarize", boom)

    async def go():
        r = _runner(tmp_path, deps, _launcher(), _transport())
        rec = r.start(REQ, users=[1, 4])
        await r.wait()
        return rec

    rec = asyncio.run(go())
    assert rec.state == "failed" and rec.error == "sweep broke"
    assert len(rec.points) == 1
    assert deps.stopped == [("dep-1", "benchmark finished")]


def test_engine_failure_while_launching_fails_and_stops(tmp_path):
    deps = _Deps(fail=True)

    async def go():
        r = _runner(tmp_path, deps, _launcher(), _transport())
        rec = r.start(REQ, users=[1])
        await r.wait()
        return rec

    rec = asyncio.run(go())
    assert rec.state == "failed" and rec.error == "engine died"
    assert deps.stopped == [("dep-1", "benchmark finished")]


def test_cancel_stops_the_copy_and_a_second_run_is_refused(tmp_path):
    deps = _Deps()
    gate = asyncio.Event  # created inside the loop below

    async def go():
        g = gate()
        r = _runner(tmp_path, deps, _launcher(), _transport(gate=g))
        rec = r.start(REQ, users=[1])
        with pytest.raises(BenchBusy):
            r.start(REQ, users=[1])
        for _ in range(50):
            await asyncio.sleep(0)
            if rec.state == "measuring":
                break
        assert rec.state == "measuring"
        assert r.cancel(rec.bench_id)
        await r.wait()
        return rec

    rec = asyncio.run(go())
    assert rec.state == "cancelled"
    assert deps.stopped == [("dep-1", "benchmark finished")]
    assert measurements.load_bench(rec.bench_id, directory=tmp_path).state == "cancelled"


def test_list_filters_by_model_and_delete_removes(tmp_path):
    for i, model in enumerate(("a/b", "c/d")):
        measurements.save_bench(
            measurements.BenchRecord(
                bench_id=f"b-{i:012x}", model_id=model, request={"model_id": model},
                served_name="bench", started_at=float(i),
            ),
            directory=tmp_path,
        )
    r = _runner(tmp_path, _Deps(), _launcher(), _transport())
    assert [x.model_id for x in r.list()] == ["c/d", "a/b"]
    assert [x.model_id for x in r.list("a/b")] == ["a/b"]
    assert r.delete("b-000000000000")
    assert r.get("b-000000000000") is None
