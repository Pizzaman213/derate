"""Measured compute, the first-token floor, and POST /api/plan/candidates."""

from __future__ import annotations

import dataclasses
import time

import pytest

from control_plane import computeprobe, measurements
from control_plane.fit.calculator import prefill_floor_seconds
from tests.unit.test_speculative import _client, _plan_body

import tests.fixtures as fx


def test_prefill_floor_is_matrix_work_over_measured_rate():
    shape = dataclasses.replace(fx.GPT_OSS_120B, active_params=1_000_000_000)
    assert prefill_floor_seconds(shape, 1000, 2.0, 1) == 1.0
    assert prefill_floor_seconds(shape, 1000, 2.0, 2) == 0.5
    assert prefill_floor_seconds(shape, 1000, 0.0, 1) is None


def test_parse_reads_the_marker_line():
    out = 'noise\n__derate_compute {"tflops": 91.5, "device": "NVIDIA GB10", "torch": "2.9"}\n'
    assert computeprobe._parse(out) == {"tflops": 91.5, "device": "NVIDIA GB10", "torch": "2.9"}
    assert computeprobe._parse("no marker here") is None
    assert computeprobe._parse("__derate_compute {broken") is None


def test_matching_compute_is_newest_and_exact(tmp_path):
    for t, tf in ((1.0, 10.0), (2.0, 20.0)):
        measurements.save_compute(
            measurements.ComputeRecord("GB10", f"img{int(t)}", "bf16", tf, "n", t),
            directory=tmp_path,
        )
    assert measurements.matching_compute("GB10", image="img2", directory=tmp_path).tflops == 20.0
    assert measurements.matching_compute("H100", image="img2", directory=tmp_path) is None


def _gpu_name(client) -> str:
    return client.app.state.ctx.deps.registry.healthy_nodes()[0].profile.gpu_name


def _concurrency(client):
    r = client.post(
        "/api/plan/concurrency",
        json=_plan_body(model_id="Qwen/Qwen3-30B-A3B", contexts=[8192], max_users=256),
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_without_a_record_there_is_no_floor(tmp_path, monkeypatch):
    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path))
    with _client() as client:
        body = _concurrency(client)
    assert body["compute"] is None
    assert body["ttft_caveat"].startswith("No sooner than this")
    assert body["series"][0]["prompt_tokens"] == 8192
    assert all(p["ttft_floor_s"] is None for p in body["series"][0]["points"])


def test_a_record_gives_a_floor_linear_in_users_and_null_past_the_wall(tmp_path, monkeypatch):
    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path))
    with _client() as client:
        measurements.save_compute(
            measurements.ComputeRecord(
                _gpu_name(client), computeprobe.resolved_image(), "bf16", 50.0, "spark-01",
                time.time(),
            )
        )
        body = _concurrency(client)
    assert body["compute"]["tflops"] == 50.0
    series = body["series"][0]
    pts = {p["users"]: p for p in series["points"]}
    assert pts[1]["ttft_floor_s"] > 0
    assert pts[2]["ttft_floor_s"] == pytest.approx(2 * pts[1]["ttft_floor_s"], abs=1e-9)
    queued = [p for p in series["points"] if p["queued"]]
    assert queued, "max_users=256 must cross the wall"
    assert all(p["ttft_floor_s"] is None for p in queued)
    assert all(p["ttft_floor_s"] is not None for p in series["points"] if not p["queued"])


def _measure_client():
    client = _client()
    reg = client.app.state.ctx.deps.registry
    reg.local_profile = reg.healthy_nodes()[0].profile
    return client


def test_measure_saves_the_record_for_the_local_gpu(tmp_path, monkeypatch):
    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        computeprobe, "run_compute",
        lambda *, image, timeout_s=180.0: computeprobe.ComputeResult(88.0, "GPU", None),
    )
    with _measure_client() as client:
        r = client.post("/api/compute/measure", json={})
        assert r.status_code == 200, r.text
        assert r.json()["tflops"] == 88.0
        listed = client.get("/api/compute").json()["records"]
    assert [x["tflops"] for x in listed] == [88.0]


def test_measure_failure_is_503_and_saves_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        computeprobe, "run_compute",
        lambda *, image, timeout_s=180.0: computeprobe.ComputeResult(
            None, None, "the matmul did not complete: boom"
        ),
    )
    with _measure_client() as client:
        r = client.post("/api/compute/measure", json={})
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "measurement_failed"
        assert client.get("/api/compute").json()["records"] == []


def test_a_second_measurement_while_one_runs_is_409(tmp_path, monkeypatch):
    from control_plane.gateway import internal_api

    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path))
    assert internal_api._COMPUTE_LOCK.acquire(blocking=False)
    try:
        with _measure_client() as client:
            r = client.post("/api/compute/measure", json={})
    finally:
        internal_api._COMPUTE_LOCK.release()
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "measurement_running"


def _two_node_client():
    from fastapi.testclient import TestClient
    from control_plane.fit import FitCalculator
    from control_plane.gateway.app import create_app
    from control_plane.planner import Planner
    from control_plane.resolver.stub import StubResolver as FullResolver
    from tests.unit.test_gateway import FakeRegistry, build_deps, make_node_profile

    registry = FakeRegistry()
    registry.states = [
        fx.node_state(make_node_profile("spark-01")),
        fx.node_state(make_node_profile("spark-02")),
    ]
    deps = dataclasses.replace(
        build_deps(registry=registry),
        fit=FitCalculator(),
        resolver=FullResolver(),
        planner=Planner(),
    )
    return TestClient(create_app(deps))


def test_candidates_list_one_plan_per_kind_each_judged():
    with _two_node_client() as client:
        plan = client.post("/api/plan", json=_plan_body(model_id="Qwen/Qwen3-30B-A3B"))
        r = client.post("/api/plan/candidates", json=_plan_body(model_id="Qwen/Qwen3-30B-A3B"))
    assert plan.status_code == 200, plan.text
    assert r.status_code == 200, r.text
    body = r.json()
    kinds = [c["kind"] for c in body["candidates"]]
    assert len(kinds) == len(set(kinds)) and len(kinds) <= 5
    assert "single_node" in kinds and "tensor" in kinds
    for c in body["candidates"]:
        assert (c["verdict"] is None) == (c["error"] is not None)
        assert set(c["degrees"]) == {
            "tensor_parallel", "pipeline_parallel", "expert_parallel", "data_parallel",
        }
    assert body["recommended_kind"] == plan.json()["recommended_plan"]["kind"]
