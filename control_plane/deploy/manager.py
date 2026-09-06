"""The deployment manager.

Turns an approved plan into a running backend, tracks it, and tears it down.
Owns the lifecycle FSM, the health and memory watch, and reconciliation
across a control-plane restart.

Three rules this file exists to enforce:

* Agent D's fit verdict gates every launch. WONT_FIT never produces a launch
  attempt, and the refusal carries D's reason unedited -- it is more specific
  than anything written here.
* Illegal transitions raise. A manager that silently corrects one ships a
  deployment record that lies about what is running.
* Memory pressure sheds load, it never kills. Shedding is recoverable;
  killing a loaded 120B model is twenty minutes of reload.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterable

from control_plane.contracts import (
    Deployment,
    DeploymentState as S,
    FitResult,
    ModelShape,
    NodeState,
    ParallelismPlan,
    RegistryPort,
    Verdict,
)

from . import events as ev
from .events import EventBus
from .fsm import SERVING, TERMINAL, check as fsm_check
from .health import probe
from .sparkrun import LaunchError, SparkrunAdapter, default_served_name, is_oom
from .store import DeploymentStore

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 5.0
#: Two consecutive misses at a 5s poll puts a dead backend in FAILED inside
#: 15 seconds, which is the acceptance bar.
HEALTH_FAIL_THRESHOLD = 2
READY_TIMEOUT_S = 1800.0
READY_POLL_INTERVAL_S = 3.0
STOP_CONFIRM_TIMEOUT_S = 30.0


class LaunchRefused(RuntimeError):
    """The fit gate said no.

    ``str(exc)`` and ``exc.reason`` are Agent D's reason, character for
    character. Do not paraphrase it on the way to the user.
    """

    def __init__(self, fit: FitResult) -> None:
        super().__init__(fit.reason)
        self.reason = fit.reason
        self.fit = fit
        self.verdict = fit.verdict


class DuplicateDeployment(RuntimeError):
    """This model is already deployed on these nodes."""

    def __init__(self, existing: Deployment) -> None:
        super().__init__(
            "%s is already deployed on %s as %s (%s)"
            % (
                existing.served_name,
                ", ".join(existing.plan.node_ids),
                existing.deployment_id,
                existing.state.value,
            )
        )
        self.existing = existing


@dataclass
class _Record:
    """A deployment plus everything the watch loop needs to track it."""

    deployment: Deployment
    handle: dict[str, Any] = field(default_factory=dict)
    health_failures: int = 0
    node_severity: dict[str, str] = field(default_factory=dict)  # node_id -> ok|warning|critical
    unhealthy_nodes: set[str] = field(default_factory=set)

    @property
    def cluster_id(self) -> str | None:
        return self.handle.get("cluster_id")

    @property
    def hosts(self) -> list[str]:
        return list(self.handle.get("hosts") or [])

    def degraded_reasons(self) -> list[str]:
        reasons = [
            "node %s memory %s" % (node_id, sev)
            for node_id, sev in sorted(self.node_severity.items())
            if sev in ("warning", "critical")
        ]
        reasons += ["node %s unhealthy" % n for n in sorted(self.unhealthy_nodes)]
        return reasons

    def admitting(self) -> bool:
        """False once any node crosses critical. Agent G reads this."""
        return "critical" not in self.node_severity.values()


class DeploymentManager:
    """DeploymentPort over sparkrun."""

    def __init__(
        self,
        adapter: SparkrunAdapter | None = None,
        registry: RegistryPort | None = None,
        *,
        state_dir: Path | str = "/data",
        bus: EventBus | None = None,
        poll_interval_s: float = POLL_INTERVAL_S,
        health_fail_threshold: int = HEALTH_FAIL_THRESHOLD,
        ready_timeout_s: float = READY_TIMEOUT_S,
        ready_poll_interval_s: float = READY_POLL_INTERVAL_S,
        stop_confirm_timeout_s: float = STOP_CONFIRM_TIMEOUT_S,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] | None = None,
        probe_fn: Callable[..., tuple[bool, str | None]] = probe,
        autostart: bool = True,
    ) -> None:
        state_dir = Path(state_dir)
        self.adapter = adapter or SparkrunAdapter(registry, recipe_dir=state_dir / "recipes")
        self.registry = registry
        self.store = DeploymentStore(state_dir / "deployments")
        self.bus = bus or EventBus()
        self.poll_interval_s = poll_interval_s
        self.health_fail_threshold = health_fail_threshold
        self.ready_timeout_s = ready_timeout_s
        self.ready_poll_interval_s = ready_poll_interval_s
        self.stop_confirm_timeout_s = stop_confirm_timeout_s
        self._clock = clock
        self._id_factory = id_factory or (lambda: "d-%s" % uuid.uuid4().hex[:8])
        self._probe = probe_fn

        self._lock = threading.RLock()
        self._records: dict[str, _Record] = {}
        self._stop_event = threading.Event()
        self._watch_thread: threading.Thread | None = None
        self._workers: list[threading.Thread] = []
        self._autostart = autostart

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
    ) -> Deployment:
        """Refuse, render, run, then watch for readiness.

        Returns as soon as the deployment is LAUNCHING. A 120B model takes
        minutes to load; blocking the caller for that is not an option, so
        readiness is settled on a worker thread and announced as a
        state_changed event.
        """
        if fit.verdict is Verdict.WONT_FIT:
            # Nothing is started. Not a subprocess, not a recipe file.
            self.bus.emit(
                ev.LAUNCH_REFUSED,
                served_name=served_name or default_served_name(shape),
                model_id=shape.model_id,
                verdict=fit.verdict.value,
                reason=fit.reason,  # Agent D's words, verbatim
                limiting_term=fit.limiting_term,
                max_context_that_fits=fit.max_context_that_fits,
                node_ids=list(plan.node_ids),
            )
            raise LaunchRefused(fit)

        self.adapter.require()
        name = served_name or default_served_name(shape)

        with self._lock:
            existing = self._find_conflict(name, plan.node_ids)
            if existing is not None:
                raise DuplicateDeployment(existing)

            deployment = Deployment(
                deployment_id=self._id_factory(),
                served_name=name,
                shape=shape,
                plan=plan,
                fit=fit,
                runtime=runtime,
                state=S.PLANNED,
                backend_url=None,
                context_length=ctx,
                max_concurrent_seqs=max_seqs,
                started_at=None,
                last_error=None,
            )
            port = self._allocate_port()
            record = _Record(deployment=deployment, handle={"port": port})
            self._records[deployment.deployment_id] = record
            self._transition(record, S.LAUNCHING)

        worker = threading.Thread(
            target=self._launch_worker,
            args=(record, port),
            name="sparkplane-launch-%s" % deployment.deployment_id,
            daemon=True,
        )
        self._workers.append(worker)
        worker.start()
        self.start()
        return deployment

    def stop(self, deployment_id: str) -> None:
        """Tear down through sparkrun and wait for confirmation.

        A stop that does not confirm inside the budget escalates: the record
        goes to STOPPED because we are no longer tracking it, but last_error
        names exactly what was left behind on which hosts, and a
        stop_escalated event goes out so a human sees it.
        """
        with self._lock:
            record = self._records.get(deployment_id)
            if record is None:
                raise KeyError("no such deployment: %s" % deployment_id)
            if record.deployment.state in TERMINAL:
                return
            self._transition(record, S.STOPPING)
            cluster_id = record.cluster_id
            hosts = record.hosts

        left_behind: str | None = None
        if cluster_id:
            ok, output = self.adapter.stop(cluster_id, hosts=hosts)
            deadline = self._clock() + self.stop_confirm_timeout_s
            confirmed = ok and not self.adapter.is_running(cluster_id, hosts=hosts)
            while not confirmed and self._clock() < deadline:
                time.sleep(1.0)
                confirmed = not self.adapter.is_running(cluster_id, hosts=hosts)
            if not confirmed:
                left_behind = (
                    "sparkrun stop did not confirm within %.0fs. Workload %s may still be "
                    "running on %s. Check with: sparkrun cluster check-job %s\n%s"
                    % (
                        self.stop_confirm_timeout_s,
                        cluster_id,
                        ", ".join(hosts) or "its hosts",
                        cluster_id,
                        output.strip(),
                    )
                )
                self.bus.emit(
                    ev.STOP_ESCALATED,
                    deployment_id=deployment_id,
                    served_name=record.deployment.served_name,
                    cluster_id=cluster_id,
                    hosts=hosts,
                    detail=left_behind,
                )

        with self._lock:
            if left_behind:
                record.deployment.last_error = left_behind
            self._transition(record, S.STOPPED)

    def list(self) -> list[Deployment]:
        with self._lock:
            return [r.deployment for r in self._records.values()]

    def get(self, deployment_id: str) -> Deployment | None:
        with self._lock:
            record = self._records.get(deployment_id)
            return record.deployment if record else None

    # -- the rest of Agent F's surface ------------------------------------

    def render_command(
        self,
        plan: ParallelismPlan,
        shape: ModelShape,
        runtime: str,
        ctx: int,
        max_seqs: int,
        **kwargs: Any,
    ) -> list[str]:
        """Pure. Runs nothing. Safe for the UI to display before committing."""
        return self.adapter.render_command(plan, shape, runtime, ctx, max_seqs, **kwargs)

    async def events(self) -> AsyncIterator[dict]:
        """State changes and OOM warnings. ``async for e in mgr.events():``"""
        async for event in self.bus.subscribe():
            yield event

    def reconcile(self) -> list[Deployment]:
        """Adopt what is still running, retire what is not.

        Called on startup. Reconciliation is rehydration, not transition: the
        state a record comes back in is chosen from evidence, and the FSM
        governs everything after that. This is the only place a state is set
        without an FSM check, and it happens once per record.
        """
        adopted: list[Deployment] = []
        for deployment, handle in self.store.load_all():
            record = _Record(deployment=deployment, handle=handle)
            resolved, reason = self._evidence(record)
            deployment.state = resolved
            if reason:
                deployment.last_error = reason
            with self._lock:
                self._records[deployment.deployment_id] = record
            if resolved in TERMINAL:
                self.store.save(deployment, handle)
            else:
                self.store.save(deployment, handle)
                adopted.append(deployment)
            if resolved is S.LAUNCHING:
                # Container is up but not answering yet: finish waiting for it.
                self._spawn_ready_waiter(record)
            elif resolved is S.STOPPING:
                # A teardown was in flight when we went down. Finish it, on a
                # worker so one wedged host cannot stall the whole startup.
                self._spawn_stop_finisher(record)
            self.bus.emit(
                ev.RECONCILED,
                deployment_id=deployment.deployment_id,
                served_name=deployment.served_name,
                state=resolved.value,
                adopted=resolved not in TERMINAL,
                backend_url=deployment.backend_url,
                detail=reason,
            )
        if adopted:
            self.start()
        return adopted

    # -- lifecycle of the manager itself ----------------------------------

    def start(self) -> None:
        """Start the health and memory watch. Idempotent."""
        if not self._autostart:
            return
        with self._lock:
            if self._watch_thread is not None and self._watch_thread.is_alive():
                return
            self._stop_event.clear()
            self._watch_thread = threading.Thread(
                target=self._watch_loop, name="sparkplane-watch", daemon=True
            )
            self._watch_thread.start()

    def close(self, timeout: float = 5.0) -> None:
        """Stop watching. Does not stop deployments; sparkrun owns those."""
        self._stop_event.set()
        thread = self._watch_thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._watch_thread = None
        for worker in list(self._workers):
            worker.join(timeout=timeout)

    def __enter__(self) -> "DeploymentManager":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- launch worker ----------------------------------------------------

    def _launch_worker(self, record: _Record, port: int) -> None:
        deployment = record.deployment
        try:
            result = self.adapter.launch(
                deployment.plan,
                deployment.shape,
                deployment.runtime,
                deployment.context_length,
                deployment.max_concurrent_seqs,
                served_name=deployment.served_name,
                port=port,
            )
        except LaunchError as exc:
            self._fail_launch(record, exc)
            return
        except Exception as exc:  # sparkrun missing, permissions, anything else
            self._fail_launch(record, LaunchError(str(exc), raw=""))
            return

        with self._lock:
            record.handle.update(
                {
                    "cluster_id": result.cluster_id,
                    "hosts": result.hosts,
                    "head_host": result.head_host,
                    "port": result.port,
                    "recipe_path": str(result.recipe_path),
                    "argv": result.argv,
                }
            )
            deployment.backend_url = result.backend_url
            self.store.save(deployment, record.handle)

        self._wait_for_ready(record)

    def _spawn_stop_finisher(self, record: _Record) -> None:
        def finish() -> None:
            try:
                self.stop(record.deployment.deployment_id)
            except Exception:
                logger.exception(
                    "could not finish the in-flight stop of %s",
                    record.deployment.deployment_id,
                )

        thread = threading.Thread(
            target=finish,
            name="sparkplane-stop-%s" % record.deployment.deployment_id,
            daemon=True,
        )
        self._workers.append(thread)
        thread.start()

    def _spawn_ready_waiter(self, record: _Record) -> None:
        thread = threading.Thread(
            target=self._wait_for_ready,
            args=(record,),
            name="sparkplane-ready-%s" % record.deployment.deployment_id,
            daemon=True,
        )
        self._workers.append(thread)
        thread.start()

    def _wait_for_ready(self, record: _Record) -> None:
        deployment = record.deployment
        deadline = self._clock() + self.ready_timeout_s
        url = deployment.backend_url or ""
        last_reason = "no backend url"
        while self._clock() < deadline and not self._stop_event.is_set():
            if deployment.state is not S.LAUNCHING:
                return  # stopped or failed out from under us
            healthy, reason = self._probe(url)
            if healthy:
                with self._lock:
                    deployment.started_at = self._clock()
                    deployment.last_error = None
                    self._transition(record, S.READY)
                return
            last_reason = reason or last_reason
            # The container dying during load is the common failure. Catch it
            # here rather than waiting out the full ready timeout.
            if record.cluster_id and not self.adapter.is_running(
                record.cluster_id, hosts=record.hosts, timeout=10.0
            ):
                self._fail_launch(
                    record,
                    LaunchError(
                        "backend exited during startup: %s" % last_reason, raw=""
                    ),
                    post_mortem=True,
                )
                return
            if self._stop_event.wait(self.ready_poll_interval_s):
                return
        if deployment.state is S.LAUNCHING:
            self._fail_launch(
                record,
                LaunchError(
                    "backend did not answer within %.0fs: %s"
                    % (self.ready_timeout_s, last_reason),
                    raw="",
                ),
                post_mortem=True,
            )

    def _fail_launch(
        self, record: _Record, exc: LaunchError, *, post_mortem: bool = False
    ) -> None:
        deployment = record.deployment
        detail = str(exc)
        if exc.raw:
            detail = "%s\n--- sparkrun output ---\n%s" % (detail, exc.raw.strip())
        with self._lock:
            deployment.last_error = detail
            if deployment.state in (S.LAUNCHING, S.READY, S.DEGRADED):
                self._transition(record, S.FAILED, reason=str(exc))
        self.bus.emit(
            ev.LAUNCH_FAILED,
            deployment_id=deployment.deployment_id,
            served_name=deployment.served_name,
            reason=str(exc),
            oom=exc.oom,
        )
        if exc.oom:
            self._emit_fit_miss(record, detail)
        elif post_mortem:
            self._post_mortem(record)

    def _post_mortem(self, record: _Record) -> None:
        """Pull the container log tail and look for an OOM signature.

        Runs after the FAILED transition, never before it, so a slow SSH does
        not delay the state change the gateway is waiting on.
        """
        cluster_id = record.cluster_id
        if not cluster_id:
            return
        tail = self.adapter.logs(cluster_id, hosts=record.hosts, tail=200)
        if not tail:
            return
        with self._lock:
            record.deployment.last_error = "%s\n--- backend log tail ---\n%s" % (
                record.deployment.last_error or "",
                tail.strip(),
            )
        if is_oom(tail):
            self._emit_fit_miss(record, tail)

    def _emit_fit_miss(self, record: _Record, detail: str) -> None:
        """The most valuable telemetry this system produces.

        A launch that OOMs despite passing the fit gate means Agent D's
        estimate was low. Emitting the full breakdown next to the actual
        failure is what makes the next estimate better.
        """
        deployment = record.deployment
        breakdown = deployment.fit.breakdown
        self.bus.emit(
            ev.FIT_MISS,
            deployment_id=deployment.deployment_id,
            served_name=deployment.served_name,
            model_id=deployment.shape.model_id,
            dtype=deployment.shape.dtype,
            runtime=deployment.runtime,
            context_length=deployment.context_length,
            max_concurrent_seqs=deployment.max_concurrent_seqs,
            plan={
                "kind": deployment.plan.kind.value,
                "tensor_parallel": deployment.plan.tensor_parallel,
                "pipeline_parallel": deployment.plan.pipeline_parallel,
                "expert_parallel": deployment.plan.expert_parallel,
                "data_parallel": deployment.plan.data_parallel,
                "node_ids": list(deployment.plan.node_ids),
            },
            predicted_verdict=deployment.fit.verdict.value,
            predicted_reason=deployment.fit.reason,
            predicted_total=breakdown.total,
            predicted_breakdown={
                "weights": breakdown.weights,
                "kv_cache": breakdown.kv_cache,
                "activations": breakdown.activations,
                "comm_buffers": breakdown.comm_buffers,
                "replicated": breakdown.replicated,
                "framework_overhead": breakdown.framework_overhead,
            },
            usable_per_node=deployment.fit.usable_per_node,
            predicted_headroom=deployment.fit.headroom,
            actual_error=detail.strip()[-4000:],
        )
        logger.error(
            "fit miss: %s passed the gate with %d bytes predicted per node "
            "against %d usable, then failed with an out-of-memory error",
            deployment.served_name,
            breakdown.total,
            deployment.fit.usable_per_node,
        )

    # -- the watch loop ---------------------------------------------------

    def _watch_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("deployment watch tick failed")
            if self._stop_event.wait(self.poll_interval_s):
                return

    def tick(self) -> None:
        """One pass of the health and memory watch. Exposed for tests."""
        nodes = self._node_states()
        with self._lock:
            records = [
                r for r in self._records.values() if r.deployment.state in SERVING
            ]
        for record in records:
            self._check_memory(record, nodes)
            self._check_backend(record)
            self._settle_state(record)

    def _node_states(self) -> dict[str, NodeState]:
        if self.registry is None:
            return {}
        try:
            return {n.profile.node_id: n for n in self.registry.list_nodes()}
        except Exception:
            logger.exception("registry unavailable this tick")
            return {}

    def _check_memory(self, record: _Record, nodes: dict[str, NodeState]) -> None:
        deployment = record.deployment
        for node_id in deployment.plan.node_ids:
            state = nodes.get(node_id)
            if state is None:
                continue  # not in the registry yet; do not flap on startup

            was_unhealthy = node_id in record.unhealthy_nodes
            if not state.healthy:
                record.unhealthy_nodes.add(node_id)
                if not was_unhealthy:
                    self.bus.emit(
                        ev.NODE_UNHEALTHY,
                        deployment_id=deployment.deployment_id,
                        served_name=deployment.served_name,
                        node_id=node_id,
                    )
            else:
                record.unhealthy_nodes.discard(node_id)

            total = state.profile.total_memory or 1
            fraction = state.memory_used / total
            severity = (
                "critical"
                if fraction >= ev.MEMORY_CRITICAL_FRACTION
                else "warning"
                if fraction >= ev.MEMORY_WARN_FRACTION
                else "ok"
            )
            previous = record.node_severity.get(node_id, "ok")
            record.node_severity[node_id] = severity
            if severity == previous:
                continue

            common = {
                "deployment_id": deployment.deployment_id,
                "served_name": deployment.served_name,
                "node_id": node_id,
                "memory_used": state.memory_used,
                "total_memory": state.profile.total_memory,
                "memory_used_pct": round(fraction * 100.0, 1),
            }
            if severity == "critical":
                # Agent G stops admitting. We do not kill: shedding load is
                # recoverable, killing a loaded model is not.
                self.bus.emit(
                    ev.MEMORY_CRITICAL,
                    severity="critical",
                    admitting=False,
                    threshold_pct=ev.MEMORY_CRITICAL_FRACTION * 100.0,
                    **common,
                )
            elif severity == "warning":
                self.bus.emit(
                    ev.MEMORY_WARNING,
                    severity="warning",
                    admitting=record.admitting(),
                    threshold_pct=ev.MEMORY_WARN_FRACTION * 100.0,
                    **common,
                )
            else:
                self.bus.emit(
                    ev.MEMORY_CLEARED,
                    severity="ok",
                    admitting=record.admitting(),
                    **common,
                )

    def _check_backend(self, record: _Record) -> None:
        deployment = record.deployment
        if not deployment.backend_url:
            return
        healthy, reason = self._probe(deployment.backend_url)
        if healthy:
            record.health_failures = 0
            return
        record.health_failures += 1
        if record.health_failures < self.health_fail_threshold:
            return

        detail = "backend stopped answering: %s" % (reason or "unknown")
        self.bus.emit(
            ev.BACKEND_LOST,
            deployment_id=deployment.deployment_id,
            served_name=deployment.served_name,
            backend_url=deployment.backend_url,
            reason=detail,
            consecutive_failures=record.health_failures,
        )
        with self._lock:
            deployment.last_error = detail
            self._transition(record, S.FAILED, reason=detail)
        # Post-mortem after the transition, on its own thread: an SSH to a
        # wedged host must not hold up the next deployment's health check.
        threading.Thread(
            target=self._post_mortem,
            args=(record,),
            name="sparkplane-postmortem-%s" % deployment.deployment_id,
            daemon=True,
        ).start()

    def _settle_state(self, record: _Record) -> None:
        """READY <-> DEGRADED, according to what the watch just found."""
        deployment = record.deployment
        if deployment.state not in SERVING:
            return
        reasons = record.degraded_reasons()
        with self._lock:
            if reasons and deployment.state is S.READY:
                self._transition(record, S.DEGRADED, reason="; ".join(reasons))
            elif not reasons and deployment.state is S.DEGRADED:
                self._transition(record, S.READY, reason="pressure cleared")

    # -- helpers ----------------------------------------------------------

    def _transition(self, record: _Record, target: S, *, reason: str | None = None) -> None:
        """The only legal way to change a deployment's state."""
        deployment = record.deployment
        source = deployment.state
        fsm_check(deployment.deployment_id, source, target)
        if source is target:
            return
        deployment.state = target
        self.store.save(deployment, record.handle)
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
        logger.info(
            "deployment %s %s -> %s%s",
            deployment.deployment_id,
            source.value,
            target.value,
            " (%s)" % reason if reason else "",
        )

    def _find_conflict(self, served_name: str, node_ids: Iterable[str]) -> Deployment | None:
        wanted = set(node_ids)
        for record in self._records.values():
            d = record.deployment
            if d.state in TERMINAL:
                continue
            if d.served_name == served_name and set(d.plan.node_ids) & wanted:
                return d
        return None

    def _allocate_port(self) -> int:
        taken = {
            r.handle.get("port")
            for r in self._records.values()
            if r.deployment.state not in TERMINAL
        }
        port = self.adapter.base_port
        while port in taken:
            port += 1
        return port

    def _evidence(self, record: _Record) -> tuple[S, str | None]:
        """What state does the world say this persisted record is in?"""
        deployment = record.deployment
        if deployment.state in TERMINAL:
            return deployment.state, None

        url = deployment.backend_url
        if url:
            healthy, _ = self._probe(url)
            if healthy:
                if deployment.state is S.STOPPING:
                    # A stop we never finished. Finish it.
                    return S.STOPPING, "stop was in flight when the control plane restarted"
                return S.READY, None

        cluster_id = record.cluster_id
        if cluster_id and self.adapter.is_running(cluster_id, hosts=record.hosts):
            if deployment.state is S.STOPPING:
                return S.STOPPING, "stop was in flight when the control plane restarted"
            # Container up, backend silent: still loading, or just restarted.
            return S.LAUNCHING, "adopted mid-startup after a control plane restart"

        if deployment.state is S.LAUNCHING:
            return S.FAILED, "launch did not survive the control plane restart"
        return S.STOPPED, "backend was gone when the control plane restarted"
