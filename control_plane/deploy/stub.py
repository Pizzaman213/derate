"""Day-0 in-memory deployment manager.

Fakes the lifecycle with timers so Agent G can build routing before sparkrun
is wired: LAUNCHING for three seconds, then READY with a fake backend URL.

It is a stub in exactly one respect -- nothing is launched. Everything else
is the real behaviour: the same FSM, the same refusal on WONT_FIT carrying
Agent D's reason verbatim, the same event names and payloads on the same bus.
A stub that returns a shape the real thing does not is worse than no stub, so
this one is deliberately boring.

Deleted at integration.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, AsyncIterator, Callable

from control_plane.contracts import (
    Deployment,
    DeploymentState as S,
    FitResult,
    Modality,
    ModelShape,
    ParallelismPlan,
    Verdict,
)

from . import events as ev
from .events import EventBus
from .fsm import SERVING, TERMINAL, check as fsm_check
from .manager import LaunchRefused, _Record
from .sparkrun import SparkrunAdapter, default_served_name

LAUNCH_SECONDS = 3.0


class StubDeploymentManager:
    """Same surface as DeploymentManager, no sparkrun, no subprocesses."""

    def __init__(
        self,
        *,
        launch_seconds: float = LAUNCH_SECONDS,
        base_port: int = 8100,
        host: str = "127.0.0.1",
        bus: EventBus | None = None,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.launch_seconds = launch_seconds
        self.base_port = base_port
        self.host = host
        self.bus = bus or EventBus()
        self._clock = clock
        self._id_factory = id_factory or (lambda: "d-%s" % uuid.uuid4().hex[:8])
        self._lock = threading.RLock()
        self._records: dict[str, _Record] = {}
        self._timers: list[threading.Timer] = []
        # Rendering is real: the stub shows the command the real manager
        # would run, so the UI is not developed against a fiction.
        self._adapter = SparkrunAdapter(recipe_dir="/tmp/derate-stub-recipes")

    # -- DeploymentPort ---------------------------------------------------

    def launch(
        self,
        shape: ModelShape,
        plan: ParallelismPlan,
        fit: FitResult,
        runtime: str,
        ctx: int,
        max_seqs: int,
        *,
        served_name: str | None = None,
        modality: Modality = Modality.TEXT,
        extra_args: tuple[str, ...] = (),
        custom_command: tuple[str, ...] = (),
    ) -> Deployment:
        name = served_name or default_served_name(shape)
        if fit.verdict is Verdict.WONT_FIT:
            self.bus.emit(
                ev.LAUNCH_REFUSED,
                served_name=name,
                model_id=shape.model_id,
                verdict=fit.verdict.value,
                reason=fit.reason,
                limiting_term=fit.limiting_term,
                max_context_that_fits=fit.max_context_that_fits,
                node_ids=list(plan.node_ids),
            )
            raise LaunchRefused(fit)

        with self._lock:
            port = self.base_port + len(self._records)
            deployment = Deployment(
                deployment_id=self._id_factory(),
                served_name=name,
                shape=shape,
                plan=plan,
                fit=fit,
                runtime=runtime,
                state=S.PLANNED,
                backend_url="http://%s:%d/v1" % (self.host, port),
                context_length=ctx,
                max_concurrent_seqs=max_seqs,
                started_at=None,
                last_error=None,
                modality=modality,
                extra_args=tuple(extra_args),
                custom_command=tuple(custom_command),
            )
            record = _Record(deployment=deployment, handle={"port": port})
            self._records[deployment.deployment_id] = record
            self._transition(record, S.LAUNCHING)

        timer = threading.Timer(self.launch_seconds, self._become_ready, args=(record,))
        timer.daemon = True
        self._timers.append(timer)
        timer.start()
        return deployment

    def stop(self, deployment_id: str) -> None:
        with self._lock:
            record = self._records.get(deployment_id)
            if record is None:
                raise KeyError("no such deployment: %s" % deployment_id)
            if record.deployment.state in TERMINAL:
                return
            if record.deployment.state is S.LAUNCHING:
                # Same as the real manager: no LAUNCHING -> STOPPING edge
                # exists, so a cancelled launch lands in FAILED saying so.
                record.deployment.last_error = (
                    "stop requested while the backend was still loading; "
                    "the launch was torn down"
                )
                self._transition(record, S.FAILED, reason="stopped during launch")
                return
            self._transition(record, S.STOPPING)
            self._transition(record, S.STOPPED)

    def list(self) -> list[Deployment]:
        with self._lock:
            return [r.deployment for r in self._records.values()]

    def get(self, deployment_id: str) -> Deployment | None:
        with self._lock:
            record = self._records.get(deployment_id)
            return record.deployment if record else None

    # -- rest of Agent F's surface ----------------------------------------

    def render_command(
        self,
        plan: ParallelismPlan,
        shape: ModelShape,
        runtime: str,
        ctx: int,
        max_seqs: int,
        **kwargs: Any,
    ) -> list[str]:
        return self._adapter.render_command(plan, shape, runtime, ctx, max_seqs, **kwargs)

    async def events(self) -> AsyncIterator[dict]:
        async for event in self.bus.subscribe():
            yield event

    def reconcile(self) -> list[Deployment]:
        """Nothing is persisted, so nothing is adopted."""
        return []

    def close(self, timeout: float = 5.0) -> None:
        for timer in self._timers:
            timer.cancel()
        self._timers.clear()

    def __enter__(self) -> "StubDeploymentManager":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- test hooks -------------------------------------------------------
    # Agent G needs to exercise admission control and failover before there
    # is a real cluster to do it on.

    def simulate_memory(self, deployment_id: str, node_id: str, used_pct: float) -> None:
        """Drive a node's memory percentage and emit what the real watch would."""
        record = self._require(deployment_id)
        fraction = used_pct / 100.0
        severity = (
            "critical"
            if fraction >= ev.MEMORY_CRITICAL_FRACTION
            else "warning"
            if fraction >= ev.MEMORY_WARN_FRACTION
            else "ok"
        )
        previous = record.node_severity.get(node_id, "ok")
        record.node_severity[node_id] = severity
        if severity != previous:
            event_type = {
                "critical": ev.MEMORY_CRITICAL,
                "warning": ev.MEMORY_WARNING,
                "ok": ev.MEMORY_CLEARED,
            }[severity]
            self.bus.emit(
                event_type,
                deployment_id=deployment_id,
                served_name=record.deployment.served_name,
                node_id=node_id,
                memory_used_pct=round(used_pct, 1),
                severity=severity,
                admitting=record.admitting(),
            )
        self._settle(record)

    def simulate_node_unhealthy(self, deployment_id: str, node_id: str, unhealthy: bool = True) -> None:
        record = self._require(deployment_id)
        if unhealthy:
            if node_id not in record.unhealthy_nodes:
                record.unhealthy_nodes.add(node_id)
                self.bus.emit(
                    ev.NODE_UNHEALTHY,
                    deployment_id=deployment_id,
                    served_name=record.deployment.served_name,
                    node_id=node_id,
                )
        else:
            record.unhealthy_nodes.discard(node_id)
        self._settle(record)

    def simulate_backend_death(self, deployment_id: str, reason: str = "backend exited") -> None:
        record = self._require(deployment_id)
        with self._lock:
            record.deployment.last_error = reason
            self.bus.emit(
                ev.BACKEND_LOST,
                deployment_id=deployment_id,
                served_name=record.deployment.served_name,
                backend_url=record.deployment.backend_url,
                reason=reason,
            )
            self._transition(record, S.FAILED, reason=reason)

    def admitting(self, deployment_id: str) -> bool:
        return self._require(deployment_id).admitting()

    # -- plumbing ---------------------------------------------------------

    def _require(self, deployment_id: str) -> _Record:
        with self._lock:
            record = self._records.get(deployment_id)
        if record is None:
            raise KeyError("no such deployment: %s" % deployment_id)
        return record

    def _become_ready(self, record: _Record) -> None:
        with self._lock:
            if record.deployment.state is not S.LAUNCHING:
                return
            record.deployment.started_at = self._clock()
            self._transition(record, S.READY)

    def _settle(self, record: _Record) -> None:
        with self._lock:
            if record.deployment.state not in SERVING:
                return
            reasons = record.degraded_reasons()
            if reasons and record.deployment.state is S.READY:
                self._transition(record, S.DEGRADED, reason="; ".join(reasons))
            elif not reasons and record.deployment.state is S.DEGRADED:
                self._transition(record, S.READY, reason="pressure cleared")

    def _transition(self, record: _Record, target: S, *, reason: str | None = None) -> None:
        deployment = record.deployment
        source = deployment.state
        fsm_check(deployment.deployment_id, source, target)
        if source is target:
            return
        deployment.state = target
        self.bus.emit(
            ev.STATE_CHANGED,
            deployment_id=deployment.deployment_id,
            served_name=deployment.served_name,
            **{"from": source.value, "to": target.value},
            backend_url=deployment.backend_url,
            admitting=record.admitting() and target in SERVING,
            reason=reason,
            last_error=deployment.last_error,
        )
