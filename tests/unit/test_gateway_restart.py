"""P-RESTART: auto-restart of a crashed deployment.

RestartCoordinator is exercised directly against a hand-built GatewayContext
and a real EventBus -- not through create_app()/TestClient -- so every
attempt runs on the one asyncio loop asyncio.run() gives the test, with no
cross-thread timing to race. _attempt_relaunch runs for real against the same
StubResolver/StubPlanner/StubFit/StubLinks/FakeRegistry fixtures test_gateway.py
uses for its own create_deployment coverage, so this also exercises the actual
planning-and-launch path rather than a mock standing in for it.

MAX_RESTART_ATTEMPTS is left at its real value (3); only RESTART_BACKOFF_S is
patched down so a full three-attempt exhaustion runs in well under a second.
"""

from __future__ import annotations

import asyncio

from control_plane.contracts import DeploymentState
from control_plane.deploy.events import EventBus
from control_plane.gateway import restart as restart_module
from control_plane.gateway.deps import GatewayContext
from control_plane.gateway.restart import RestartCoordinator
from control_plane.telemetry.events import GatewayEvents, RESTART_ATTEMPTED, RESTART_EXHAUSTED

from tests.fixtures import MODEL_SHAPES, wont_fit
from tests.unit.test_gateway import FakeDeployments, StubFit, build_deps, make_deployment

FAST_BACKOFF = (0.01, 0.01, 0.01)


def run(coro):
    """Run one coroutine. Avoids a hard dependency on pytest-asyncio."""
    return asyncio.run(coro)


class FakeDeploymentsWithBus(FakeDeployments):
    """FakeDeployments, plus the events()/bus a real DeploymentManager offers.

    test_gateway.py's own FakeDeployments has neither, which is why
    _consume_deployment_events (and now RestartCoordinator) is a no-op across
    virtually its whole suite. A separate subclass, used only here, so this
    file's fixture doesn't quietly wake up either consumer in every other
    gateway test.
    """

    def __init__(self, deployments=None):
        super().__init__(deployments)
        self.bus = EventBus()

    def events(self):
        return self.bus.subscribe()


class FakeRouter:
    def __init__(self):
        self.rebuilds = 0

    def rebuild(self, force_scores=False):
        self.rebuilds += 1


class RefusingFit(StubFit):
    """Always WONT_FIT, so a relaunch attempt never reaches launch()."""

    def check(self, req, nodes):
        return wont_fit()


def build_ctx(*, deployments=None, fit=None, auto_restart=True):
    deployments = deployments if deployments is not None else FakeDeploymentsWithBus()
    deps = build_deps(deployments=deployments)
    if fit is not None:
        deps.fit = fit
    deps.settings.auto_restart_crashed_deployments = auto_restart
    ctx = GatewayContext(
        deps=deps,
        settings=deps.settings,
        stats=None,
        admission=None,
        router=FakeRouter(),
        proxy=None,
        metrics=None,
        events=GatewayEvents(),
    )
    return ctx, deployments


def crashed_deployment(deployment_id="d-crashed"):
    # FakeDeployments.launch() (test_gateway.py) takes a shortcut and names a
    # new deployment after shape.model_id rather than running the real
    # default_served_name(shape) -- fine for its own tests, but it means a
    # relaunch's served_name only matches the original's here if the original
    # was already given that same name, so this uses it too rather than an
    # arbitrary label a real relaunch would never reproduce.
    served_name = MODEL_SHAPES["llama-3.3-70b"].model_id
    return make_deployment(
        deployment_id, served_name, backend_url=None, state=DeploymentState.FAILED,
    )


def crash_event(deployment, *, reason="the backend died during startup: exit 1"):
    return {
        "type": "state_changed",
        "deployment_id": deployment.deployment_id,
        "served_name": deployment.served_name,
        "from": "launching",
        "to": "failed",
        "reason": reason,
        "last_error": reason,
    }


def ready_event(served_name, deployment_id):
    return {
        "type": "state_changed",
        "deployment_id": deployment_id,
        "served_name": served_name,
        "from": "launching",
        "to": "ready",
        "reason": None,
        "last_error": None,
    }


def attempted_events(ctx):
    return [e for e in ctx.events.bus.recent() if e["type"] == RESTART_ATTEMPTED]


def exhausted_events(ctx):
    return [e for e in ctx.events.bus.recent() if e["type"] == RESTART_EXHAUSTED]


async def _run_coordinator(ctx, body):
    coordinator = RestartCoordinator(ctx)
    await coordinator.start()
    # start() only schedules the consumer task; give the loop a turn so it
    # actually reaches EventBus.subscribe() and registers before the test
    # emits anything, or the event fires into an empty subscriber list.
    await asyncio.sleep(0)
    try:
        await body(coordinator)
    finally:
        await coordinator.stop()


def test_a_crash_schedules_and_fires_a_restart_with_a_reconstructed_payload(monkeypatch):
    monkeypatch.setattr(restart_module, "RESTART_BACKOFF_S", FAST_BACKOFF)
    ctx, deployments = build_ctx()
    deployment = crashed_deployment()
    deployments.deployments.append(deployment)

    async def body(coordinator):
        deployments.bus.emit(**crash_event(deployment))
        for _ in range(200):
            if deployments.launched:
                break
            await asyncio.sleep(0.01)
        assert len(deployments.launched) == 1, "no relaunch was attempted"
        new = deployments.launched[0]
        assert new.deployment_id != deployment.deployment_id
        assert new.served_name == deployment.served_name
        assert ctx.router.rebuilds == 1
        attempted = attempted_events(ctx)
        assert len(attempted) == 1
        assert attempted[0]["outcome"] == "launched"
        assert attempted[0]["previous_deployment_id"] == deployment.deployment_id

    run(_run_coordinator(ctx, body))


def test_stopped_during_launch_does_not_trigger_a_restart(monkeypatch):
    monkeypatch.setattr(restart_module, "RESTART_BACKOFF_S", FAST_BACKOFF)
    ctx, deployments = build_ctx()
    deployment = crashed_deployment()
    deployments.deployments.append(deployment)

    async def body(coordinator):
        deployments.bus.emit(
            **crash_event(deployment, reason=restart_module.OPERATOR_STOP_REASON)
        )
        await asyncio.sleep(0.3)
        assert deployments.launched == [], "an operator-requested stop was retried"
        assert attempted_events(ctx) == []

    run(_run_coordinator(ctx, body))


def test_a_wont_fit_relaunch_counts_as_a_failed_attempt_and_eventually_gives_up(
    monkeypatch,
):
    monkeypatch.setattr(restart_module, "RESTART_BACKOFF_S", FAST_BACKOFF)
    ctx, deployments = build_ctx(fit=RefusingFit())
    deployment = crashed_deployment()
    deployments.deployments.append(deployment)

    async def body(coordinator):
        deployments.bus.emit(**crash_event(deployment))
        for _ in range(300):
            if len(exhausted_events(ctx)) == 1:
                break
            await asyncio.sleep(0.01)
        assert deployments.launched == [], "a WONT_FIT relaunch must never launch"
        attempted = attempted_events(ctx)
        assert len(attempted) == restart_module.MAX_RESTART_ATTEMPTS
        assert [a["outcome"] for a in attempted] == ["wont_fit"] * restart_module.MAX_RESTART_ATTEMPTS
        assert [a["attempt"] for a in attempted] == [1, 2, 3]
        exhausted = exhausted_events(ctx)
        assert len(exhausted) == 1
        assert exhausted[0]["attempts"] == restart_module.MAX_RESTART_ATTEMPTS

    run(_run_coordinator(ctx, body))


def test_a_duplicate_deployment_exception_is_treated_as_success_not_failure(monkeypatch):
    monkeypatch.setattr(restart_module, "RESTART_BACKOFF_S", FAST_BACKOFF)

    class _Existing:
        deployment_id = "d-someone-else-relaunched-it"

    class ConflictOnceDeployments(FakeDeploymentsWithBus):
        def __init__(self):
            super().__init__()
            self.conflicted = False

        def launch(self, *a, **kw):
            if not self.conflicted:
                self.conflicted = True
                exc = RuntimeError("already deployed")
                exc.existing = _Existing()
                raise exc
            return super().launch(*a, **kw)

    deployments = ConflictOnceDeployments()
    ctx, _ = build_ctx(deployments=deployments)
    deployment = crashed_deployment()
    deployments.deployments.append(deployment)

    async def body(coordinator):
        deployments.bus.emit(**crash_event(deployment))
        for _ in range(200):
            if attempted_events(ctx):
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.2)  # give a second attempt a chance to (wrongly) fire
        attempted = attempted_events(ctx)
        assert len(attempted) == 1, "a conflict must stop the retry loop, not continue it"
        assert attempted[0]["outcome"] == "conflict"
        assert deployments.launched == [], "the conflicting launch call must not count as ours"
        assert exhausted_events(ctx) == []

    run(_run_coordinator(ctx, body))


def test_a_ready_event_cancels_an_in_flight_retry(monkeypatch):
    # A slow first backoff, long enough to land the READY event inside it.
    monkeypatch.setattr(restart_module, "RESTART_BACKOFF_S", (5.0, 5.0, 5.0))
    ctx, deployments = build_ctx(fit=RefusingFit())
    deployment = crashed_deployment()
    deployments.deployments.append(deployment)

    async def body(coordinator):
        deployments.bus.emit(**crash_event(deployment))
        await asyncio.sleep(0.05)  # let _handle schedule the retry loop
        deployments.bus.emit(**ready_event(deployment.served_name, "d-manual-relaunch"))
        await asyncio.sleep(0.2)
        assert attempted_events(ctx) == [], "READY must cancel the pending retry outright"
        state = coordinator._state.get(deployment.served_name)
        assert state is None, "READY must clear the served_name's restart state"

    run(_run_coordinator(ctx, body))


def test_toggling_the_setting_off_cancels_a_pending_retry(monkeypatch):
    monkeypatch.setattr(restart_module, "RESTART_BACKOFF_S", (0.15, 0.15, 0.15))
    ctx, deployments = build_ctx()
    deployment = crashed_deployment()
    deployments.deployments.append(deployment)

    async def body(coordinator):
        deployments.bus.emit(**crash_event(deployment))
        await asyncio.sleep(0.02)  # scheduled, still sleeping out the backoff
        ctx.settings.auto_restart_crashed_deployments = False
        await asyncio.sleep(0.4)
        assert deployments.launched == [], "toggling off mid-backoff must cancel the retry"
        assert attempted_events(ctx) == []

    run(_run_coordinator(ctx, body))


def test_auto_restart_disabled_never_schedules_anything(monkeypatch):
    monkeypatch.setattr(restart_module, "RESTART_BACKOFF_S", FAST_BACKOFF)
    ctx, deployments = build_ctx(auto_restart=False)
    deployment = crashed_deployment()
    deployments.deployments.append(deployment)

    async def body(coordinator):
        deployments.bus.emit(**crash_event(deployment))
        await asyncio.sleep(0.3)
        assert deployments.launched == []
        assert attempted_events(ctx) == []
        assert deployment.served_name not in coordinator._state

    run(_run_coordinator(ctx, body))


def test_two_overlapping_crash_events_do_not_double_schedule(monkeypatch):
    monkeypatch.setattr(restart_module, "RESTART_BACKOFF_S", (0.1, 0.1, 0.1))
    ctx, deployments = build_ctx()
    deployment = crashed_deployment()
    deployments.deployments.append(deployment)

    async def body(coordinator):
        # A flapping health probe firing BACKEND_LOST twice before the first
        # attempt has even started sleeping out its backoff.
        deployments.bus.emit(**crash_event(deployment))
        deployments.bus.emit(**crash_event(deployment))
        for _ in range(200):
            if deployments.launched:
                break
            await asyncio.sleep(0.01)
        assert len(deployments.launched) == 1, "two crash events launched two retries"
        assert len(attempted_events(ctx)) == 1

    run(_run_coordinator(ctx, body))
