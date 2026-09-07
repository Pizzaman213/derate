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
    Modality,
    ModelShape,
    NodeState,
    ParallelismPlan,
    RegistryPort,
    Verdict,
)
from control_plane.procmatch import matches_deployment

from . import events as ev
from .events import EventBus
from .fsm import SERVING, TERMINAL, check as fsm_check
from .health import HEALTH_PATHS, probe
from .recipes import check_recipe_identifiers
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
#: Per-request budget when talking to a node agent during a forced stop.
#: Long enough to cover the agent's own SIGTERM grace plus SIGKILL wait.
AGENT_KILL_TIMEOUT_S = 25.0

#: M-15: the acceptance bar above assumed an instant probe. health.probe()
#: tries up to len(HEALTH_PATHS) URLs, each able to block for its own
#: timeout -- a HANGING (not merely refused) health endpoint can blow the bar
#: even though the threshold*poll_interval arithmetic still looks fine on
#: paper. We derive the per-path probe timeout every tick from the poll
#: cycle: with n = health_fail_threshold misses needed, the watch loop's own
#: wait contributes n*poll_interval_s before the bound is reached, which
#: leaves (DEATH_BOUND_S - n*poll_interval_s) of probing time per miss;
#: dividing that by the URLs probe() tries per deployment yields a per-path
#: timeout, and PROBE_OVERHEAD_S is shaved off first so the non-probe work
#: tick() does every pass (a registry list, a store save per settled record)
#: has room too -- the bound is meant to be an upper bound, not a value the
#: arithmetic lands on exactly with no slack.
#:
#: watched_count does NOT divide this budget. It used to: an earlier version
#: divided the per-tick probe budget by the number of SERVING deployments,
#: on the theory that tick() probes them one at a time so their timeouts sum.
#: MIN_PROBE_TIMEOUT_S then floored that per-deployment share, which meant
#: past watched_count = budget_per_tick / MIN_PROBE_TIMEOUT_S (6 deployments
#: at the shipped constants) the floor won and the *sum* over all deployments
#: grew without bound -- 16s at 6 watched deployments, 30s at 20, worse than
#: the ~22s this fix was meant to close. tick() instead probes every
#: deployment's backend concurrently (see _probe_backends) so watched_count
#: no longer lengthens a tick at all: the whole probing phase costs about
#: one deployment's worth of probing, however many are watched, up to
#: MAX_PROBE_WORKERS.
#:
#: One honest limit even with that fix: a probe's *timeout* argument bounds
#: each individual socket operation (connect, one recv), not the call's
#: total wall time -- a "slow drip" responder that sends a trickle of bytes
#: just inside the timeout window, or a blocking DNS lookup, can in
#: principle keep resetting it forever. _probe_backends puts a hard deadline
#: on *joining* each probe thread for exactly this reason: that stops the
#: tick from waiting past the intended budget, though the thread itself is
#: only reaped once its underlying socket call eventually does return --
#: it is a daemon thread so a still-stuck one cannot block process exit.
DEATH_BOUND_S = 15.0
#: Never so small a real, merely-slow-but-alive backend starts flapping.
MIN_PROBE_TIMEOUT_S = 0.25
#: Headroom subtracted from the per-tick probe budget for the non-probe work
#: tick() does every pass -- see DEATH_BOUND_S above.
PROBE_OVERHEAD_S = 0.3
#: Caps the thread pool _probe_backends spins up per tick. Past this many
#: concurrently-watched deployments, probing partially serializes again and
#: DEATH_BOUND_S is no longer provable by construction -- an accepted,
#: documented limit rather than an unbounded thread pool, since a single
#: control plane watching this many deployments at once is already far
#: outside today's expected scale.
MAX_PROBE_WORKERS = 64


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
        # Rotation cursor for _probe_backends when the fleet exceeds
        # MAX_PROBE_WORKERS: which window of SERVING deployments gets probed
        # this tick. Watch-thread only; no lock needed.
        self._probe_offset = 0
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
        modality: Modality = Modality.TEXT,
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
        # M-22: run the same model_id/served_name safety check synthesize()
        # runs, but here -- synchronously, before any record or launch
        # worker thread exists. Without this, a hostile (or merely
        # malformed) model_id or served_name still produced a LAUNCHING
        # deployment: the launch worker only discovers the ValueError when
        # it calls synthesize() on its own thread, several steps later, and
        # reports it as an ordinary launch failure rather than refusing the
        # request up front the way the WONT_FIT check above does.
        check_recipe_identifiers(shape.model_id, name)

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
                modality=modality,
            )
            port = self._allocate_port()
            record = _Record(deployment=deployment, handle={"port": port})
            self._records[deployment.deployment_id] = record
            self._transition(record, S.LAUNCHING)

        worker = threading.Thread(
            target=self._launch_worker,
            args=(record, port),
            name="derate-launch-%s" % deployment.deployment_id,
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
            launching = record.deployment.state is S.LAUNCHING
            if not launching:
                self._transition(record, S.STOPPING)
            cluster_id = record.cluster_id
            hosts = record.hosts

        if launching:
            # The lifecycle has no LAUNCHING -> STOPPING edge; a launch is not
            # cancellable in the model. But leaving the container running
            # because the diagram has no arrow for it would orphan a workload,
            # which is worse than any labelling question. So tear it down and
            # land in FAILED, which is legal, with a last_error that says this
            # was a request rather than a crash.
            if cluster_id:
                self.adapter.stop(cluster_id, hosts=hosts)
            with self._lock:
                if record.deployment.state is S.LAUNCHING:
                    record.deployment.last_error = (
                        "stop requested while the backend was still loading; "
                        "the launch was torn down"
                    )
                    self._transition(
                        record, S.FAILED, reason="stopped during launch"
                    )
            return

        left_behind: str | None = None
        kill_detail: str | None = None
        if cluster_id:
            ok, output = self.adapter.stop(cluster_id, hosts=hosts)
            deadline = self._clock() + self.stop_confirm_timeout_s
            # M-16: is_running() is three-valued. "confirmed" means we have
            # positive evidence of a stop -- an explicit False -- not merely
            # the absence of a True. An unknown (sparkrun could not answer)
            # must keep us in the retry loop rather than declaring victory.
            confirmed = ok and self.adapter.is_running(cluster_id, hosts=hosts) is False
            while not confirmed and self._clock() < deadline:
                time.sleep(1.0)
                confirmed = self.adapter.is_running(cluster_id, hosts=hosts) is False
            if not confirmed:
                # Second tier. sparkrun has said what it can and the workload is
                # still holding the pool; the node agents are the only thing left
                # that can reach the processes directly. This fires only on an
                # explicit operator stop -- the rule at the top of this file, that
                # memory pressure never kills, is about the watch loop and still
                # stands.
                killed, kill_detail = self._kill_through_agents(record, cluster_id)
                if killed:
                    confirmed = self.adapter.is_running(cluster_id, hosts=hosts) is not True

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
                if kill_detail:
                    left_behind += "\n\nForce kill: %s" % kill_detail
                self.bus.emit(
                    ev.STOP_ESCALATED,
                    deployment_id=deployment_id,
                    served_name=record.deployment.served_name,
                    cluster_id=cluster_id,
                    hosts=hosts,
                    detail=left_behind,
                )
            elif kill_detail:
                # It did stop, but not gracefully. An operator who is about to
                # relaunch needs to know a SIGKILL happened here: an abrupt
                # teardown is where a half-written cache or a wedged driver
                # comes from, and this is the only record that it did.
                left_behind = (
                    "sparkrun stop did not confirm within %.0fs, so the backend "
                    "was killed on its nodes. %s"
                    % (self.stop_confirm_timeout_s, kill_detail)
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

    def _kill_through_agents(
        self, record: _Record, cluster_id: str | None
    ) -> tuple[bool, str | None]:
        """Last resort: signal this deployment's processes on their own nodes.

        Returns (killed_anything, detail). Every failure mode here is answered
        with a sentence rather than an exception -- this runs inside a stop the
        operator already asked for, and a traceback would replace a partial
        result with none at all.

        Reached only from :meth:`stop`, after ``sparkrun stop`` has had its full
        confirmation budget. ``registry`` is None in unit tests and stub wiring,
        which skips the tier entirely and leaves the old behaviour exactly as it
        was.
        """
        if self.registry is None:
            return False, None
        node_ids = list(record.deployment.plan.node_ids or [])
        if not node_ids:
            return False, None

        urls_fn = getattr(self.registry, "agent_urls", None)
        token_fn = getattr(self.registry, "cluster_token", None)
        if not callable(urls_fn) or not callable(token_fn):
            return False, None
        try:
            # include_local matters: on a single-Spark cluster the workload is
            # on the coordinator's own node, and excluding it would make this
            # tier a no-op in the most common topology there is.
            urls = urls_fn(include_local=True)
            token = token_fn()
        except Exception:
            logger.exception("could not reach the registry for a force kill")
            return False, None
        if not token:
            return False, "no cluster token is available, so the node agents could not be asked"

        port = record.handle.get("port")
        notes: list[str] = []
        killed = False
        import httpx

        for node_id in node_ids:
            url = urls.get(node_id)
            if not url:
                notes.append("%s: no agent URL" % node_id)
                continue
            base = url.rstrip("/")
            try:
                with httpx.Client(timeout=AGENT_KILL_TIMEOUT_S) as client:
                    res = client.get("%s/agent/processes" % base)
                    res.raise_for_status()
                    processes = res.json().get("processes") or []
            except Exception as exc:
                notes.append("%s: could not read processes (%s)" % (node_id, exc))
                continue

            targets = [
                p
                for p in processes
                if matches_deployment(p.get("command") or "", cluster_id, port)
            ]
            if not targets:
                notes.append("%s: nothing matched this deployment" % node_id)
                continue

            for proc in targets:
                pid = proc.get("pid")
                try:
                    with httpx.Client(timeout=AGENT_KILL_TIMEOUT_S) as client:
                        res = client.post(
                            "%s/agent/processes/%s/kill" % (base, pid),
                            headers={"X-Derate-Token": token},
                        )
                except Exception as exc:
                    notes.append("%s: kill of pid %s failed (%s)" % (node_id, pid, exc))
                    continue
                if res.status_code >= 400:
                    notes.append(
                        "%s: pid %s was refused (%s)" % (node_id, pid, res.text.strip()[:200])
                    )
                    continue
                killed = True
                try:
                    notes.append("%s: %s" % (node_id, res.json().get("detail", "killed")))
                except Exception:
                    notes.append("%s: pid %s killed" % (node_id, pid))

        return killed, "; ".join(notes) if notes else None

    def list(self) -> list[Deployment]:
        with self._lock:
            return [r.deployment for r in self._records.values()]

    def get(self, deployment_id: str) -> Deployment | None:
        with self._lock:
            record = self._records.get(deployment_id)
            return record.deployment if record else None

    def handles(self) -> dict[str, dict]:
        """The sparkrun handle behind each record, for callers that must match
        a running process back to a deployment.

        Deliberately not on ``DeploymentPort``: section 4.7's protocol is
        frozen at launch/stop/list/get, and the gateway reaches this through a
        ``getattr`` so a port without it (the stubs) degrades to "nothing is
        attributable" rather than failing. Only the fields an outside caller
        can act on -- we do not hand out the recipe path.
        """
        with self._lock:
            return {
                deployment_id: {
                    "cluster_id": record.handle.get("cluster_id"),
                    "port": record.handle.get("port"),
                    "hosts": list(record.handle.get("hosts") or []),
                }
                for deployment_id, record in self._records.items()
            }

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
        removed = self.store.purge_expired()
        if removed:
            logger.info(
                "garbage collected %d terminal deployment record(s) past the retention window",
                removed,
            )
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
                target=self._watch_loop, name="derate-watch", daemon=True
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
            name="derate-stop-%s" % record.deployment.deployment_id,
            daemon=True,
        )
        self._workers.append(thread)
        thread.start()

    def _spawn_ready_waiter(self, record: _Record) -> None:
        thread = threading.Thread(
            target=self._wait_for_ready,
            args=(record,),
            name="derate-ready-%s" % record.deployment.deployment_id,
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
            # M-16: only a definite False is death. None (sparkrun could not
            # answer -- e.g. check-job timed out on a wedged host) is a miss
            # at most; fall through and keep polling instead of failing the
            # launch out from under a host that may still come back.
            if record.cluster_id and self.adapter.is_running(
                record.cluster_id, hosts=record.hosts, timeout=10.0
            ) is False:
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
        probe_timeout = self._backend_probe_timeout(len(records))
        # M-15: probe every deployment's backend concurrently, not serially
        # in this loop. Gathering results first keeps every state mutation
        # below (memory severity, health_failures, transitions) on this one
        # thread under the usual lock discipline -- only the network I/O
        # runs off of it. See _probe_backends and the module comment by
        # DEATH_BOUND_S for why this is what keeps the bound independent of
        # how many deployments are watched.
        probe_results = self._probe_backends(records, probe_timeout)
        for record in records:
            self._check_memory(record, nodes)
            self._apply_backend_probe(record, probe_results.get(record.deployment.deployment_id))
            self._settle_state(record)

    def _backend_probe_timeout(self, watched_count: int) -> float:
        """Per-path health-probe timeout that keeps DEATH_BOUND_S honest.

        See the module-level comment by DEATH_BOUND_S for the derivation.
        *watched_count* is accepted (and used by _probe_backends to size its
        thread pool) but deliberately does not shrink this timeout: probing
        happens concurrently across deployments now, so the number watched
        no longer lengthens a tick, and dividing the budget by it would only
        make individual probes flap on real, merely-slow backends for no
        bound-safety reason.
        """
        misses = max(1, self.health_fail_threshold)
        budget_per_tick = DEATH_BOUND_S / misses - self.poll_interval_s - PROBE_OVERHEAD_S
        per_path = budget_per_tick / max(1, len(HEALTH_PATHS))
        return max(MIN_PROBE_TIMEOUT_S, per_path)

    def _probe_backends(
        self, records: list[_Record], probe_timeout: float
    ) -> dict[str, tuple[bool, str | None]]:
        """Probe every record's backend_url concurrently.

        Serial probing here is exactly what let a hang on one deployment
        push every deployment probed after it later and later within the
        same tick, which is why the old per-deployment timeout had to shrink
        as watched_count grew -- see the module comment by DEATH_BOUND_S.
        Run concurrently, the whole phase costs about one deployment's worth
        of probing (~probe_timeout * len(HEALTH_PATHS)) no matter how many
        are watched, up to MAX_PROBE_WORKERS threads at once.

        Plain daemon threads rather than ThreadPoolExecutor deliberately:
        a pool's context manager (and plain .shutdown()) default to
        wait=True, which blocks on a genuinely-stuck worker regardless of
        any per-future timeout passed to .result() -- exactly the hang this
        method exists to bound. A daemon thread that is still stuck when we
        give up on it cannot block this method, tick(), or process exit; it
        is reaped whenever its underlying socket call eventually returns.

        Each thread also gets a hard join deadline on top of *probe_timeout*
        itself: the timeout a probe is given bounds each individual socket
        operation, not the call's total wall time, so without this a
        slow-drip responder could in principle keep resetting it and never
        return, even though no single recv() ever exceeded its own timeout.
        Hitting the hard deadline reports that record as unhealthy for this
        tick and moves on.
        """
        targets = [r for r in records if r.deployment.backend_url]
        if not targets:
            return {}
        hard_deadline = probe_timeout * max(1, len(HEALTH_PATHS))
        results: dict[str, tuple[bool, str | None]] = {}
        results_lock = threading.Lock()

        def run(record: _Record) -> None:
            try:
                outcome = self._probe(record.deployment.backend_url, timeout=probe_timeout)
            except Exception as exc:  # a probe_fn must never crash the watch thread
                outcome = (False, "probe raised %s: %s" % (type(exc).__name__, exc))
            with results_lock:
                results[record.deployment.deployment_id] = outcome

        # Past the worker cap, rotate which window gets probed each tick so
        # no deployment is *permanently* unprobed: every backend is reached
        # within ceil(n / MAX_PROBE_WORKERS) ticks, stretching the death
        # bound by that factor rather than making it infinite for the tail.
        # Deployments outside this tick's window get no result, which
        # _apply_backend_probe treats as "nothing learned this tick".
        cap = max(1, MAX_PROBE_WORKERS)
        if len(targets) > cap:
            n = len(targets)
            start = self._probe_offset % n
            targets = (targets[start:] + targets[:start])[:cap]
            self._probe_offset = (start + cap) % n

        threads = []
        for record in targets:
            thread = threading.Thread(
                target=run,
                args=(record,),
                name="derate-probe-%s" % record.deployment.deployment_id,
                daemon=True,
            )
            thread.start()
            threads.append((thread, record))

        # One ABSOLUTE deadline for the whole phase, not a fresh allowance
        # per thread: joining serially with a per-thread timeout would let
        # k concurrent hangs cost k * hard_deadline in a single tick.
        deadline = time.monotonic() + hard_deadline
        for thread, record in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
            with results_lock:
                if record.deployment.deployment_id not in results:
                    results[record.deployment.deployment_id] = (
                        False,
                        "probe did not return within %.2fs (possible slow-drip "
                        "response or a blocked hostname lookup)" % hard_deadline,
                    )
        return results

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

            # M-13: this must share Agent G's admission-controller denominator
            # (gateway/admission.py), not the nameplate total. addressable_memory
            # is the GPU-reachable ceiling the fit calculator's usable_memory()
            # budget was computed against; total_memory is bytes the GPU can
            # never actually address. On GB10, addressable (119.7 GiB) is ~7%
            # below total (128 GiB), so dividing by total under-reads pressure
            # by that much and the two 95% watches would trip at different
            # real occupancy -- one denominator per fraction, shared everywhere
            # a fraction of "how full is this GPU" is computed.
            total = state.profile.addressable_memory or 1
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
                # Same denominator the pct was computed against (M-13): the
                # GPU-reachable ceiling, not the nameplate total.
                "addressable_memory": state.profile.addressable_memory,
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

    def _apply_backend_probe(
        self, record: _Record, result: tuple[bool, str | None] | None
    ) -> None:
        """React to a backend probe _probe_backends already gathered.

        Split from the probe itself (see tick() and _probe_backends) so the
        network call can run concurrently across deployments while every
        state mutation here still happens on the manager's single tick
        thread, under the same lock discipline as before -- concurrency was
        added to the I/O that used to make deployments wait on each other,
        not to the state transitions.
        """
        deployment = record.deployment
        if result is None:
            return  # no backend_url yet (or nothing to probe this tick)
        healthy, reason = result
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
            name="derate-postmortem-%s" % deployment.deployment_id,
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
        running = self.adapter.is_running(cluster_id, hosts=record.hosts) if cluster_id else False
        if running:
            if deployment.state is S.STOPPING:
                return S.STOPPING, "stop was in flight when the control plane restarted"
            # Container up, backend silent: still loading, or just restarted.
            return S.LAUNCHING, "adopted mid-startup after a control plane restart"
        if running is None:
            # M-16: sparkrun could not confirm liveness either way (e.g.
            # check-job timed out on a wedged host). That is absence of
            # evidence, not evidence of death -- unlike a definite False it
            # must not retire what may still be a live deployment. Adopt as
            # LAUNCHING so a ready-waiter keeps asking instead of declaring
            # it gone on the strength of one unanswered check.
            if deployment.state is S.STOPPING:
                return S.STOPPING, "stop was in flight when the control plane restarted"
            return (
                S.LAUNCHING,
                "sparkrun could not confirm liveness after a control plane "
                "restart (check-job did not answer); treating as still starting",
            )

        if deployment.state is S.LAUNCHING:
            return S.FAILED, "launch did not survive the control plane restart"
        return S.STOPPED, "backend was gone when the control plane restarted"
