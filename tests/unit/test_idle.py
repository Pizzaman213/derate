"""Lifecycle: idle unload, start at boot, and the routes that set them."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from control_plane.contracts import DeploymentState, Lifecycle
from control_plane.gateway import create_app
from control_plane.gateway.idle import IdleMonitor, boot_relaunch
from control_plane.gateway.internal_api import _parse_lifecycle
from control_plane.gateway.stats import StatsRegistry
from tests.unit.test_gateway import FakeDeployments, build_deps, make_deployment

S = DeploymentState


class StopRecorder(FakeDeployments):
    def __init__(self, deployments=None):
        super().__init__(deployments)
        self.stops: list[tuple[str, str | None]] = []

    def stop(self, deployment_id, *, reason=None):
        self.stops.append((deployment_id, reason))
        super().stop(deployment_id, reason=reason)


def _dep(dep_id, name, *, state=S.READY, started_at=1000.0, lifecycle=Lifecycle()):
    dep = make_deployment(dep_id, name, backend_url="http://x/v1", state=state)
    dep.started_at = started_at
    dep.lifecycle = lifecycle
    return dep


def _ctx(deployments, launcher=None):
    return SimpleNamespace(
        deps=SimpleNamespace(deployments=deployments),
        stats=StatsRegistry(),
        router=SimpleNamespace(rebuild=lambda **kw: None),
        launcher=launcher,
    )


IDLE = Lifecycle(idle_unload_s=300)


def test_an_idle_deployment_is_stopped_with_the_reason():
    deps = StopRecorder([_dep("d-1", "m", lifecycle=IDLE)])
    clock = [1000.0 + 299]
    monitor = IdleMonitor(_ctx(deps), clock=lambda: clock[0])
    assert asyncio.run(monitor.tick()) == []
    clock[0] = 1000.0 + 300
    assert asyncio.run(monitor.tick()) == ["d-1"]
    assert deps.stops == [("d-1", "idle for 5 minutes")]


def test_a_recent_request_resets_the_idle_clock():
    deps = StopRecorder([_dep("d-1", "m", lifecycle=IDLE)])
    ctx = _ctx(deps)
    st = ctx.stats.get("d-1")
    st.begin()
    assert st.last_begin_at is not None
    st.outstanding = 0
    st.last_begin_at = 1200.0
    monitor = IdleMonitor(ctx, clock=lambda: 1450.0)
    assert asyncio.run(monitor.tick()) == []
    assert deps.stops == []


def test_a_deployment_with_requests_in_flight_is_not_stopped():
    deps = StopRecorder([_dep("d-1", "m", lifecycle=IDLE)])
    ctx = _ctx(deps)
    ctx.stats.get("d-1").outstanding = 1
    monitor = IdleMonitor(ctx, clock=lambda: 10_000_000.0)
    assert asyncio.run(monitor.tick()) == []
    assert deps.stops == []


def test_no_idle_setting_or_not_ready_is_left_alone():
    deps = StopRecorder([
        _dep("d-1", "a"),
        _dep("d-2", "b", state=S.LAUNCHING, lifecycle=IDLE),
    ])
    monitor = IdleMonitor(_ctx(deps), clock=lambda: 10_000_000.0)
    assert asyncio.run(monitor.tick()) == []


def test_a_failing_stop_is_retried_next_tick():
    class Flaky(StopRecorder):
        fail = True

        def stop(self, deployment_id, *, reason=None):
            if self.fail:
                self.fail = False
                raise RuntimeError("busy")
            super().stop(deployment_id, reason=reason)

    deps = Flaky([_dep("d-1", "m", lifecycle=IDLE)])
    monitor = IdleMonitor(_ctx(deps), clock=lambda: 10_000_000.0)
    assert asyncio.run(monitor.tick()) == []
    assert asyncio.run(monitor.tick()) == ["d-1"]


def _launcher(calls, status=201):
    async def launch(payload, *, actor, request=None):
        calls.append((payload, actor))
        return status, {"error": {"message": "refused"}} if status != 201 else {}
    return launch


BOOT = Lifecycle(start_at_boot=True)


def test_start_at_boot_relaunches_only_the_newest_terminal_record():
    old = _dep("d-old", "m", state=S.STOPPED, started_at=1.0, lifecycle=BOOT)
    new = _dep("d-new", "m", state=S.STOPPED, started_at=2.0, lifecycle=BOOT)
    calls: list = []
    launched = asyncio.run(boot_relaunch(_ctx(FakeDeployments([old, new]), _launcher(calls))))
    assert launched == ["m"]
    assert len(calls) == 1 and calls[0][1] == "boot"
    assert calls[0][0]["served_name"] == "m"


def test_start_at_boot_skips_when_the_newest_record_did_not_ask():
    old = _dep("d-old", "m", state=S.STOPPED, started_at=1.0, lifecycle=BOOT)
    new = _dep("d-new", "m", state=S.STOPPED, started_at=2.0)
    calls: list = []
    asyncio.run(boot_relaunch(_ctx(FakeDeployments([old, new]), _launcher(calls))))
    assert calls == []


def test_start_at_boot_skips_a_name_already_live():
    dead = _dep("d-dead", "m", state=S.FAILED, started_at=5.0, lifecycle=BOOT)
    live = _dep("d-live", "m", state=S.LAUNCHING, started_at=1.0)
    calls: list = []
    asyncio.run(boot_relaunch(_ctx(FakeDeployments([dead, live]), _launcher(calls))))
    assert calls == []


def test_a_refused_boot_relaunch_is_logged_not_raised():
    dep = _dep("d-1", "m", state=S.STOPPED, lifecycle=BOOT)
    calls: list = []
    assert asyncio.run(boot_relaunch(_ctx(FakeDeployments([dep]), _launcher(calls, 400)))) == []
    assert len(calls) == 1


def test_parse_lifecycle_validates_and_merges():
    assert _parse_lifecycle({}) == Lifecycle()
    merged = _parse_lifecycle({"lifecycle": {"priority": "high"}}, IDLE)
    assert merged == Lifecycle(idle_unload_s=300, priority="high")
    for bad in (
        {"nope": 1},
        {"idle_unload_s": 10},
        {"idle_unload_s": True},
        {"priority": "urgent"},
        {"start_at_boot": "yes"},
    ):
        with pytest.raises(ValueError):
            _parse_lifecycle({"lifecycle": bad})


def test_patch_sets_lifecycle_and_delete_clears_start_at_boot():
    deployments = FakeDeployments([_dep("d-1", "m", lifecycle=BOOT)])
    with TestClient(create_app(build_deps(deployments=deployments))) as client:
        reply = client.patch(
            "/api/deployments/d-1", json={"lifecycle": {"idle_unload_s": 900}}
        )
        assert reply.status_code == 200, reply.text
        assert deployments.get("d-1").lifecycle == Lifecycle(
            idle_unload_s=900, start_at_boot=True
        )
        bad = client.patch("/api/deployments/d-1", json={"lifecycle": {"x": 1}})
        assert bad.status_code == 400
        assert client.patch(
            "/api/deployments/d-nope", json={"lifecycle": {}}
        ).status_code == 404
        assert client.delete("/api/deployments/d-1").status_code == 200
    assert deployments.get("d-1").lifecycle.start_at_boot is False
    assert deployments.get("d-1").lifecycle.idle_unload_s == 900


def test_create_passes_lifecycle_and_refuses_a_bad_one():
    deployments = FakeDeployments()
    with TestClient(create_app(build_deps(deployments=deployments))) as client:
        bad = client.post(
            "/api/deployments",
            json={
                "model_id": "meta-llama/Llama-3.3-70B-Instruct",
                "lifecycle": {"priority": "urgent"},
            },
        )
        assert bad.status_code == 400
        assert deployments.launched == []
        ok = client.post(
            "/api/deployments",
            json={
                "model_id": "meta-llama/Llama-3.3-70B-Instruct",
                "lifecycle": {"idle_unload_s": 3600, "priority": "pinned"},
            },
        )
        assert ok.status_code == 201, ok.text
    assert deployments.launched[0].lifecycle == Lifecycle(
        idle_unload_s=3600, priority="pinned"
    )
