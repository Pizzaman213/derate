"""Auto-restart a crashed deployment.

A deployment that crashes lands in `S.FAILED` (deploy/manager.py) and, before
this module, stayed there: nothing relaunched it. This subscribes to the same
STATE_CHANGED stream `app.py`'s `_consume_deployment_events` already reads
(`deps.deployments.events()`, a second independent consumer on the same
fan-out bus -- see `deploy/events.py::EventBus.subscribe`, built for exactly
this) and, when a deployment crashes rather than being stopped by an operator,
relaunches it with fresh planning, up to `MAX_RESTART_ATTEMPTS` with backoff.

Deliberately entirely in the gateway layer, never touching `deploy/manager.py`:
the restart *decision* is a gateway policy (settings, planning, routing), and
`deploy/` may not import back up into it. Distinguishing a crash from an
operator-requested stop needs no help from manager.py either -- `stop()`
racing a LAUNCHING deployment tags that one case with a literal, first-party
`reason="stopped during launch"` on the very same STATE_CHANGED event, which
is both necessary and sufficient to exclude it.

State is in-memory only, per `served_name` (never persisted, matching the
deploy manager's own reconcile state, which also does not survive a
control-plane restart): a crash that outlives this process starts a fresh
budget rather than resuming a stale count.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from functools import partial

from control_plane.contracts import Deployment, Verdict

from .deps import GatewayContext
from .internal_api import _plan_and_fit, _sharding_refusal

log = logging.getLogger("gateway.restart")

#: Three tries, not more: a permanently broken model (bad weights, a recipe
#: that can never launch) must stop looping and surface to a human rather than
#: retry forever.
MAX_RESTART_ATTEMPTS = 3
#: Delay before attempts 1, 2, 3 respectively -- not delay *between* failures.
#: Three tries span ~105s, giving a flaky node or driver real time to settle
#: rather than exhausting the budget in under 30s.
RESTART_BACKOFF_S: tuple[float, ...] = (5.0, 20.0, 80.0)

#: manager.py::stop() tags the one FAILED transition that is an operator's
#: own request -- tearing a launch down mid-flight, which the FSM has no
#: LAUNCHING -> STOPPING edge for -- with this exact, first-party reason.
#: Every other FAILED reason (a fatal runtime marker, an exited container, a
#: ready-timeout, a health probe that stopped answering) is a genuine crash.
OPERATOR_STOP_REASON = "stopped during launch"


@dataclass
class _RestartState:
    attempts: int = 0
    in_flight: bool = False
    task: "asyncio.Task | None" = field(default=None, repr=False)


async def _attempt_relaunch(
    ctx: GatewayContext, deployment: Deployment
) -> tuple[str, Deployment | None, str | None]:
    """One restart attempt. Mirrors internal_api.py's create_deployment route
    (resolve -> plan -> fit -> shard check -> launch) against a payload
    reconstructed from the crashed record, minus everything that route asks a
    human for: no dtype override, no forced node/degree placement (so the
    planner re-picks from currently-healthy nodes rather than the possibly-dead
    one that caused the crash), no live-memory or mixed-hardware override --
    if the fresh fit needs one of those, this attempt fails and the budget
    counts down, rather than a background process silently re-granting a risk
    a human accepted once.

    Returns (outcome, new_deployment_or_none, detail). outcome is one of
    "launched", "conflict" (someone already relaunched it -- not a failure),
    "wont_fit", "runtime_unsupported", "sharding_unsupported", or "error".
    """
    payload = {
        "model_id": deployment.shape.model_id,
        "context": deployment.context_length,
        "concurrency": deployment.max_concurrent_seqs,
        "runtime": deployment.runtime,
    }
    try:
        out = await _plan_and_fit(ctx, payload)
    except Exception as exc:
        return "error", None, str(exc)

    fit = out.fit
    if fit is None:
        return "error", None, "the fit calculator is not wired"

    runtime = deployment.runtime
    supported_by = getattr(ctx.deps.resolver, "supported_by", None)
    if callable(supported_by):
        ok, reason = await asyncio.to_thread(supported_by, out.shape, runtime)
        if not ok:
            return "runtime_unsupported", None, reason

    sharding_problem = _sharding_refusal(
        runtime,
        out.plan.tensor_parallel,
        out.plan.pipeline_parallel,
        out.plan.expert_parallel,
        out.plan.data_parallel,
    )
    if sharding_problem:
        return "sharding_unsupported", None, sharding_problem

    if fit.verdict is Verdict.WONT_FIT:
        return "wont_fit", None, fit.reason

    live = out.fit_live
    if live is not None and live.verdict is Verdict.WONT_FIT:
        return "wont_fit", None, live.reason
    gating_fit = live if live is not None else fit

    try:
        # No served_name kwarg: DeploymentPort does not guarantee one (only
        # modality/extra_args/custom_command are part of the contract), and
        # it is redundant here anyway -- served_name is never
        # operator-supplied, only ever default_served_name(shape), so
        # passing the same shape through re-derives the identical name.
        new_deployment = await asyncio.to_thread(
            partial(
                ctx.deps.deployments.launch,
                out.shape,
                out.plan,
                gating_fit,
                runtime,
                out.context_length,
                out.concurrency,
                modality=out.modality,
                extra_args=deployment.extra_args,
                custom_command=deployment.custom_command,
            )
        )
    except Exception as exc:
        existing = getattr(exc, "existing", None)
        if getattr(existing, "deployment_id", None):
            return "conflict", existing, str(exc)
        return "error", None, str(exc)

    rebuild = getattr(ctx.router, "rebuild", None)
    if callable(rebuild):
        rebuild(force_scores=True)
    return "launched", new_deployment, None


class RestartCoordinator:
    """Watches for a crashed deployment and relaunches it, bounded and backed off.

    Same start()/stop() lifecycle shape as AdmissionController/Router/
    CircuitBreaker, wired into app.py's lifespan alongside them.
    """

    def __init__(self, ctx: GatewayContext) -> None:
        self._ctx = ctx
        self._state: dict[str, _RestartState] = {}
        self._consumer: asyncio.Task | None = None

    async def start(self) -> None:
        events_fn = getattr(self._ctx.deps.deployments, "events", None)
        if callable(events_fn):
            self._consumer = asyncio.create_task(self._consume(events_fn))

    async def stop(self) -> None:
        if self._consumer is not None:
            self._consumer.cancel()
            try:
                await self._consumer
            except asyncio.CancelledError:
                pass
            self._consumer = None
        for state in self._state.values():
            if state.task is not None:
                state.task.cancel()
        self._state.clear()

    async def _consume(self, events_fn) -> None:
        try:
            async for event in events_fn():
                self._handle(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("restart coordinator event stream ended unexpectedly")

    def _handle(self, event: dict) -> None:
        served_name = event.get("served_name")
        to = event.get("to")
        if not served_name or to is None:
            return
        if to == "ready":
            state = self._state.pop(served_name, None)
            if state is not None and state.task is not None:
                state.task.cancel()
            return
        if to != "failed":
            return
        if event.get("reason") == OPERATOR_STOP_REASON:
            return
        if not self._ctx.settings.auto_restart_crashed_deployments:
            return
        deployment_id = event.get("deployment_id")
        if not deployment_id:
            return
        existing = self._state.get(served_name)
        if existing is not None and existing.in_flight:
            # A flapping health probe can fire this twice for one deployment;
            # one retry loop per served_name at a time.
            return
        state = _RestartState(in_flight=True)
        self._state[served_name] = state
        state.task = asyncio.create_task(
            self._retry_loop(served_name, deployment_id, state)
        )

    async def _retry_loop(
        self, served_name: str, deployment_id: str, state: _RestartState
    ) -> None:
        detail: str | None = None
        try:
            while state.attempts < MAX_RESTART_ATTEMPTS:
                if not self._ctx.settings.auto_restart_crashed_deployments:
                    return
                delay = RESTART_BACKOFF_S[
                    min(state.attempts, len(RESTART_BACKOFF_S) - 1)
                ]
                await asyncio.sleep(delay)
                # Read fresh, so toggling the setting off during the backoff
                # cancels this retry rather than letting it fire anyway.
                if not self._ctx.settings.auto_restart_crashed_deployments:
                    return
                deployment = self._ctx.deps.deployments.get(deployment_id)
                if deployment is None:
                    return
                state.attempts += 1
                outcome, new_deployment, detail = await _attempt_relaunch(
                    self._ctx, deployment
                )
                self._emit_attempted(
                    served_name, deployment_id, state.attempts, outcome,
                    new_deployment, detail,
                )
                if outcome in ("launched", "conflict"):
                    return
            self._emit_exhausted(served_name, deployment_id, state.attempts, detail)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "restart attempt for %s (%s) failed unexpectedly",
                served_name, deployment_id,
            )
        finally:
            state.in_flight = False

    def _emit_attempted(
        self, served_name, previous_deployment_id, attempt, outcome,
        new_deployment, detail,
    ) -> None:
        events = getattr(self._ctx, "events", None)
        if events is None:
            return
        events.restart_attempted(
            served_name=served_name,
            previous_deployment_id=previous_deployment_id,
            attempt=attempt,
            max_attempts=MAX_RESTART_ATTEMPTS,
            outcome=outcome,
            new_deployment_id=getattr(new_deployment, "deployment_id", None),
            reason=detail,
        )

    def _emit_exhausted(
        self, served_name, previous_deployment_id, attempts, last_error
    ) -> None:
        events = getattr(self._ctx, "events", None)
        if events is None:
            return
        events.restart_exhausted(
            served_name=served_name,
            previous_deployment_id=previous_deployment_id,
            attempts=attempts,
            max_attempts=MAX_RESTART_ATTEMPTS,
            last_error=last_error,
        )
