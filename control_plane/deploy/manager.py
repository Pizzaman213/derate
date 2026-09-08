"""The deployment manager.

Turns an approved plan into a running backend, tracks it, and tears it down.
Owns the lifecycle FSM, the health and memory watch, and reconciliation
across a control-plane restart.

Four rules this file exists to enforce:

* The fit calculator's verdict gates every launch. WONT_FIT never produces a
  launch attempt, and the refusal carries its reason unedited -- it is more
  specific than anything written here.
* One node runs one copy of a model. A node here is one GPU, so a second copy
  of the same model on it is not a second machine's worth of throughput: it is
  the same weights loaded twice out of one pool of unified memory that the fit
  gate budgeted for one of them.
* Illegal transitions raise. A manager that silently corrects one ships a
  deployment record that lies about what is running.
* Memory pressure sheds load, it never kills. Shedding is recoverable;
  killing a loaded 120B model is twenty minutes of reload.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
import uuid
from collections import deque
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
from .health import HEALTH_PATHS, probe, wrong_model
from .progress import LaunchProgress, advance as advance_progress, from_launcher, from_runtime_log
from .recipes import check_extra_args_safe, check_recipe_identifiers
from .sparkrun import LaunchError, SparkrunAdapter, default_served_name, is_oom
from .utilization import utilization_for
from .store import DeploymentStore

from control_plane.paths import data_dir

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 5.0
#: Two consecutive misses at a 5s poll puts a dead backend in FAILED inside
#: 15 seconds, which is the acceptance bar.
HEALTH_FAIL_THRESHOLD = 2
READY_TIMEOUT_S = 1800.0
READY_POLL_INTERVAL_S = 3.0
#: How many readiness polls between `sparkrun cluster check-job` calls.
#:
#: Every one of them is a subprocess, and off-host an SSH round trip to the
#: node that is busy loading the model. At a 3s poll that was some six hundred
#: of them across a long launch, serialized with the probe and the sleep, to
#: catch a case the runtime's own words already cover on every pass
#: (`progress.py::_RUNTIME_FATAL`). What is left for this check is the rarer
#: one -- the container itself going away -- which is worth noticing in
#: fifteen seconds rather than paying for every three.
#:
#: The first pass always checks, so a launch into a container that is already
#: gone still fails at once, and a test with a millisecond poll still sees it.
READY_CHECK_JOB_EVERY = 5
#: Lines of existing log to take before following. Everything after this
#: arrives as it is printed, so this number only has to cover the gap between
#: the container starting and the subscription -- but the gap is where a
#: launch that failed early already said why.
#:
#: It was 60, which was wrong for exactly that reason: a real failed launch on
#: this box ended `RuntimeError: Engine core initialization failed. See root
#: cause above.`, and the root cause above it was a `ValueError` about GPU
#: memory some 40 lines further back, past the tail. A traceback that tells
#: you to look above is worthless if the buffer starts below it. This is one
#: transfer per launch, not a poll -- the cost of the larger window is paid
#: once.
LOG_TAIL_LINES = 300
#: How long to wait before starting the follower again after it dies. The
#: container may not be exec-able the instant sparkrun returns.
LOG_STREAM_RETRY_S = 6.0
#: How many times. A runtime whose log cannot be followed at all is a caption
#: this launch does without, not a subprocess started every few seconds for
#: half an hour.
LOG_STREAM_ATTEMPTS = 5
#: How long the post-mortem waits for a log tail it is only going to read
#: once. See _post_mortem: the call follows, so this is when to stop listening
#: rather than how long the answer takes to arrive.
POST_MORTEM_READ_S = 10.0
#: Lines of launcher output and backend log kept per deployment, for the sheet
#: that shows them. Enough to hold a whole vLLM startup -- the log that matters
#: is the one from the launch that is going wrong -- and small enough that a
#: fleet of them is not a memory decision anybody has to think about.
LOG_BUFFER_LINES = 500
#: Where a failed launch's log is written, under ``state_dir`` beside the
#: deployment records, because it is evidence about a deployment rather than
#: something this process said about itself.
#:
#: The buffer above is per deployment and a retry is a NEW deployment with its
#: own, so a relaunch does not overwrite it -- what loses it is this process
#: restarting. The runtime's own copy is worse off: a solo launch writes to
#: /tmp/sparkrun_serve.log inside a container the relaunch reuses by name, so
#: attempt N's traceback is truncated away by attempt N+1. Between the two,
#: the reason a launch failed survived only as long as the coordinator did.
#:
#: Which mattered on the first real launch from a containerized node: three
#: attempts, the first two `RuntimeError: Engine core initialization failed.
#: See root cause above.` -- and the root cause above, `ValueError: No
#: available memory for the cache blocks`, was in a buffer nobody had a reason
#: to open and a file the next attempt had already overwritten.
#: How often a launch waiting for a node to finish starting something else
#: re-checks. Short: the thing it is waiting for takes minutes, and the cost of
#: asking is a set lookup.
STARTING_GATE_POLL_S = 0.5
FAILED_LOG_DIR = "failed-launches"
#: How many of them to keep. A box that fails a lot is exactly the box you
#: want the last few from, and 500 lines each is not a disk decision.
FAILED_LOGS_KEPT = 50
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


def _bar_desc(line: str) -> str | None:
    """The description in front of a tqdm bar, or None if this is not one.

    `Fetching 16 files:  19%|...` and `Loading safetensors checkpoint shards:
    45%|...` are the same bar redrawn, frame after frame; everything before
    the first colon is what identifies which bar it is.
    """
    if "%|" not in line:
        return None
    head, sep, _ = line.partition(":")
    return head if sep else None


class LaunchRefused(RuntimeError):
    """The fit gate said no.

    ``str(exc)`` and ``exc.reason`` are the fit calculator's reason, character
    for character. Do not paraphrase it on the way to the user.
    """

    def __init__(self, fit: FitResult) -> None:
        super().__init__(fit.reason)
        self.reason = fit.reason
        self.fit = fit
        self.verdict = fit.verdict


def port_is_free(port: int) -> bool:
    """Nothing is listening on this port HERE, on this machine.

    Bind rather than connect: a listener on 127.0.0.1 and one on a LAN address
    are both in the way, and only a bind on 0.0.0.0 sees both. SO_REUSEADDR so
    a socket in TIME_WAIT -- the remains of a deployment we already stopped --
    does not read as occupied.

    Local, and that is the honest limit of it: a plan places on a node that is
    usually not this one, and nothing here can see that machine's ports. It
    closes the case that actually happened -- the coordinator handing out 8100
    while a llama-server on the coordinator already held it -- and leaves the
    rest to the identity probe, which catches a stranger wherever it runs.

    Injectable through `port_free_fn` for the same reason `probe_fn` and
    `clock` are: a test that asks the real machine which ports are taken is a
    test whose answer depends on what else is running on the developer's box.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


class DuplicateDeployment(RuntimeError):
    """A node in this plan is already running this model, or the cluster is
    already serving this name.

    ``clash`` says which rule was hit -- ``"model"`` or ``"served_name"`` --
    and ``str(exc)`` says it in the sentence, because this refusal renders
    verbatim like every other one here. Both name the deployment in the way
    and what to do about it: a refusal that does not is a dead end.
    """

    def __init__(self, existing: Deployment, *, clash: str = "served_name") -> None:
        nodes = ", ".join(existing.plan.node_ids)
        where = "%s as %s (%s)" % (nodes, existing.deployment_id, existing.state.value)
        if clash == "model":
            message = (
                "%s is already deployed on %s. A node runs one copy of a "
                "model: a second copy shares that node's GPU and its unified "
                "memory with the first, which the fit gate budgeted for one "
                "deployment. Stop %s, or place this launch on nodes that are "
                "not already running it."
                % (existing.shape.model_id, where, existing.deployment_id)
            )
        else:
            message = (
                "%s is already deployed on %s. A served name is one "
                "deployment across the whole cluster: it is what clients pass "
                "as the model, what the gateway routes on, and what the floor "
                "captions a band with, so a second one answering to it leaves "
                "two deployments to stop and no way to tell which one served "
                "a reply. Stop %s, or serve this one under a different name -- "
                "a second replica is a different name, not the same one twice."
                % (existing.served_name, where, existing.deployment_id)
            )
        super().__init__(message)
        self.existing = existing
        self.clash = clash


@dataclass
class _Record:
    """A deployment plus everything the watch loop needs to track it."""

    deployment: Deployment
    handle: dict[str, Any] = field(default_factory=dict)
    health_failures: int = 0
    node_severity: dict[str, str] = field(default_factory=dict)  # node_id -> ok|warning|critical
    unhealthy_nodes: set[str] = field(default_factory=set)
    #: What this launch is doing right now, when it is still launching.
    #:
    #: In memory and nowhere else. `Deployment` is a frozen contract
    #: (00-architecture.md section 4.6) and this is neither a fact about the
    #: deployment nor worth surviving a restart: after one, the phase is
    #: whatever the backend's log says next, which is more current than
    #: anything that could have been persisted.
    progress: LaunchProgress | None = None
    #: The launcher's output and the backend's log, in the order they arrived,
    #: kept so a person can read what a launch actually did.
    #:
    #: Free: these are the same lines the progress classifier is already being
    #: handed. Asking for them again would not be -- `sparkrun logs` follows,
    #: so a "fetch the tail" request cannot return until it is cut off, which
    #: is no way to serve a screen. Bounded, because a startup log is not a
    #: durable record and this one lives in the coordinator's memory.
    log_lines: deque[str] = field(default_factory=lambda: deque(maxlen=LOG_BUFFER_LINES))

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
        """False once any node crosses critical. The gateway reads this."""
        return "critical" not in self.node_severity.values()


class DeploymentManager:
    """DeploymentPort over sparkrun."""

    def __init__(
        self,
        adapter: SparkrunAdapter | None = None,
        registry: RegistryPort | None = None,
        *,
        state_dir: Path | str | None = None,
        bus: EventBus | None = None,
        poll_interval_s: float = POLL_INTERVAL_S,
        health_fail_threshold: int = HEALTH_FAIL_THRESHOLD,
        ready_timeout_s: float = READY_TIMEOUT_S,
        ready_poll_interval_s: float = READY_POLL_INTERVAL_S,
        stop_confirm_timeout_s: float = STOP_CONFIRM_TIMEOUT_S,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] | None = None,
        probe_fn: Callable[..., tuple[bool, str | None]] = probe,
        port_free_fn: Callable[[int], bool] = port_is_free,
        autostart: bool = True,
    ) -> None:
        state_dir = Path(state_dir) if state_dir is not None else data_dir()
        self.adapter = adapter or SparkrunAdapter(registry, recipe_dir=state_dir / "recipes")
        self.registry = registry
        self.store = DeploymentStore(state_dir / "deployments")
        self.failed_log_dir = state_dir / FAILED_LOG_DIR
        #: Nodes with an engine between `docker run` and ready. See
        #: _hold_starting: two vLLM startups on one node bill each other's
        #: allocations to their own memory budget, so they are serialised.
        self._starting: set[str] = set()
        self._starting_cv = threading.Condition()
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
        self._port_free = port_free_fn

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
        extra_args: tuple[str, ...] = (),
        custom_command: tuple[str, ...] = (),
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
                reason=fit.reason,  # the fit calculator's words, verbatim
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
        # Same reasoning, for the caller's own extra CLI tokens: refuse before
        # any record exists rather than discovering an unsafe token only when
        # the launch worker calls synthesize() on its own thread.
        check_extra_args_safe(extra_args)
        check_extra_args_safe(custom_command, field="custom_command")
        if extra_args and custom_command:
            raise ValueError(
                "extra_args and custom_command are mutually exclusive: extra_args "
                "appends to the generated serve command, custom_command replaces "
                "it, and a request cannot mean both at once"
            )

        with self._lock:
            conflict = self._find_conflict(name, shape.model_id, plan.node_ids)
            if conflict is not None:
                existing, clash = conflict
                raise DuplicateDeployment(existing, clash=clash)

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
                extra_args=tuple(extra_args),
                custom_command=tuple(custom_command),
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

        with httpx.Client(timeout=AGENT_KILL_TIMEOUT_S) as client:
            for node_id in node_ids:
                url = urls.get(node_id)
                if not url:
                    notes.append("%s: no agent URL" % node_id)
                    continue
                base = url.rstrip("/")
                try:
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

    def progress(self) -> dict[str, dict[str, object]]:
        """What each still-launching deployment is doing, by deployment id.

        Reached through a ``getattr`` from the gateway for the same reason
        ``handles`` is: section 4.7's DeploymentPort is frozen at
        launch/stop/list/get, and a port without this (the stubs) must degrade
        to "no phase reported" rather than break the activity endpoint.

        Only LAUNCHING records. A deployment that is serving is described by
        its state, and a phase left over from how it got there would be a
        stale sentence rendered as a live one.
        """
        with self._lock:
            return {
                deployment_id: record.progress.as_dict()
                for deployment_id, record in self._records.items()
                if record.progress is not None
                and record.deployment.state is S.LAUNCHING
            }

    def _archive_log(self, record: _Record) -> None:
        """Write this failed launch's log somewhere the next attempt cannot reach.

        Called on the way into FAILED, so the lines are still in the buffer.
        The file is self-contained -- what was being launched, what killed it,
        then every line -- because the person opening it has a deployment id
        and a question, not this process's memory.

        Never raises. An unwritable state directory costs the evidence, which
        is bad; failing the launch that is already failing, to report it, is
        worse, and it would be reported as the launch's own error.
        """
        deployment = record.deployment
        with self._lock:
            lines = list(record.log_lines)
            last_error = deployment.last_error or ""
        if not lines and not last_error:
            return
        header = [
            "deployment: %s" % deployment.deployment_id,
            "model:      %s" % deployment.shape.model_id,
            "served as:  %s" % deployment.served_name,
            "runtime:    %s" % deployment.runtime,
            "nodes:      %s" % ", ".join(deployment.plan.node_ids),
            "",
            "--- why it failed ---",
            last_error.strip(),
            "",
            "--- the launch, as it was printed ---",
        ]
        try:
            self.failed_log_dir.mkdir(parents=True, exist_ok=True)
            path = self.failed_log_dir / ("%s.log" % deployment.deployment_id)
            path.write_text("\n".join(header + lines) + "\n")
            self._prune_failed_logs()
        except Exception:
            logger.warning(
                "could not keep the log for %s", deployment.deployment_id, exc_info=True
            )

    def _prune_failed_logs(self) -> None:
        """Newest FAILED_LOGS_KEPT, by the time each was written."""
        kept = sorted(
            self.failed_log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        for stale in kept[FAILED_LOGS_KEPT:]:
            stale.unlink(missing_ok=True)

    def _archived_log(self, deployment_id: str, *, limit: int) -> list[str]:
        """The kept lines for *deployment_id*, or none. Never raises."""
        path = self.failed_log_dir / ("%s.log" % deployment_id)
        try:
            return path.read_text().splitlines()[-limit:]
        except FileNotFoundError:
            return []
        except Exception:
            logger.warning("could not read the kept log %s", path, exc_info=True)
            return []

    def log_tail(self, deployment_id: str, *, limit: int = LOG_BUFFER_LINES) -> dict[str, Any]:
        """What this deployment's launcher and backend have said.

        Three sources, and the caller is told which one it got:

        ``"buffer"`` -- the lines the manager already streamed while the
        launch was in flight, served straight out of memory. Free, live, and
        safe to poll.

        ``"archive"`` -- the file written on the way into FAILED, for a
        launch that failed before this process last restarted. The buffer is
        memory and the runtime's own log is truncated by the next attempt, so
        for a failure older than the coordinator this is the only copy.

        ``"read"`` -- a bounded `sparkrun logs` for a deployment we are no
        longer following. That command tails and follows, so it does not
        return on its own; the tail arrives at once and the call is cut off.
        Costly enough that it belongs behind a person clicking something, not
        behind a timer, which is what ``source`` is for.

        Reached through a ``getattr`` from the gateway like ``handles`` and
        ``progress``: §4.7's DeploymentPort is frozen, and a port without this
        answers "no log" rather than 500.
        """
        with self._lock:
            record = self._records.get(deployment_id)
            if record is None:
                # No record and possibly still a kept log: the records are
                # swept after a week and the file outlives nothing, but the
                # order of the two is not this method's business.
                kept = self._archived_log(deployment_id, limit=limit)
                if kept:
                    return {"lines": kept, "source": "archive", "cluster_id": None}
                return {"lines": [], "source": "none", "cluster_id": None}
            lines = list(record.log_lines)[-limit:]
            cluster_id = record.cluster_id
            hosts = record.hosts
            # Only while we are actually following it. The buffer stops at the
            # READY transition, so for a serving deployment it is a frozen
            # snapshot of how it started -- and answering "what is my backend
            # logging" with a recording of twenty minutes ago is worse than
            # spending a bounded read on the truth. A deployment that has
            # finished badly is the exception: its buffer holds the death, and
            # its container is usually gone, so a live read would come back
            # empty and lose the one thing worth keeping.
            following = record.deployment.state in (S.PLANNED, S.LAUNCHING)
            terminal = record.deployment.state in TERMINAL
        if lines and (following or terminal):
            return {"lines": lines, "source": "buffer", "cluster_id": cluster_id}
        # The buffer is memory, so a coordinator that has restarted since the
        # failure has the record and none of the lines. `sparkrun logs` below
        # cannot help there either -- the container is gone, or the relaunch
        # truncated its log -- so the file written on the way into FAILED is
        # the only copy left, and it is named as its own source rather than
        # passed off as the live read it is not.
        kept = self._archived_log(deployment_id, limit=limit)
        if kept:
            return {"lines": kept, "source": "archive", "cluster_id": cluster_id}
        if not cluster_id:
            return {"lines": [], "source": "none", "cluster_id": None}
        try:
            tail = self.adapter.logs(
                cluster_id, hosts=hosts, tail=limit, timeout=POST_MORTEM_READ_S
            )
        except Exception:
            logger.debug("could not read the log for %s", deployment_id, exc_info=True)
            return {"lines": [], "source": "none", "cluster_id": cluster_id}
        return {
            "lines": [ln for ln in (tail or "").splitlines() if ln.strip()][-limit:],
            "source": "read",
            "cluster_id": cluster_id,
        }

    # -- the rest of the deployment manager's surface ---------------------

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

    def _note_progress(self, record: _Record, reading: LaunchProgress | None) -> None:
        """Fold a reading into the record, forwards only. See progress.advance."""
        if reading is None:
            return
        with self._lock:
            if record.deployment.state is not S.LAUNCHING:
                return  # it has settled; a late log line is not a phase any more
            record.progress = advance_progress(record.progress, reading)

    def _note_line(self, record: _Record, line: str, read: Callable[[str], Any]) -> None:
        """Keep the line, and read a phase out of it.

        One entry point for both halves of a launch -- the launcher's own
        output and the backend's log -- because they are one story to whoever
        is reading it, and they never overlap in time.

        Progress-bar frames collapse onto the previous line rather than
        stacking up. tqdm redraws several times a second, so a download would
        otherwise fill the whole buffer with its own bar and push out the lines
        that say what happened; keeping the newest frame is both what a reader
        wants and what stops a log from being nothing but a bar.
        """
        with self._lock:
            buffer = record.log_lines
            bar = _bar_desc(line)
            if bar is not None and buffer and _bar_desc(buffer[-1]) == bar:
                buffer[-1] = line
            else:
                buffer.append(line)
        self._note_progress(record, read(line))

    def _launch_utilization(self, record: _Record) -> float | None:
        """What share of the device this launch should ask the runtime for.

        The flag is a claim on the whole GPU, not a limit: the runtime refuses
        to start unless that share is free. A constant therefore asks for the
        same slice of the machine whatever is being launched, and on a node
        with neighbours it fails every time -- see deploy/utilization.py, which
        was written against three launches that died this way in a row, one of
        them a 0.5B model the fit gate had passed at 1.8 GiB into 24.0 GiB.

        The denominator is the node's LIVE total, because that is the number
        the runtime itself divides by; `profile.total_memory` is the probe's
        figure and does not always agree. Nothing to divide by, or no node to
        ask, returns None and the adapter's own default stands -- the same way
        every other live reading in this system degrades rather than blocks.
        """
        node_ids = list(record.deployment.plan.node_ids)
        if not node_ids or self.registry is None:
            return None
        needed = record.deployment.fit.breakdown.total
        smallest: float | None = None
        for node_id in node_ids:
            try:
                state = self.registry.get_node(node_id)
            except Exception:
                logger.debug("could not read %s for a utilization", node_id, exc_info=True)
                state = None
            if state is None or not getattr(state, "memory_total", 0):
                return None
            free = max(0, state.memory_total - (state.memory_used or 0))
            share = utilization_for(
                needed_bytes=needed,
                device_total_bytes=state.memory_total,
                free_bytes=free,
            )
            # The plan runs on every node at once, and one flag covers them
            # all: the tightest node decides, or the launch starts on the
            # others and is refused on that one.
            smallest = share if smallest is None else min(smallest, share)
        return smallest

    def _hold_starting(self, node_ids: tuple[str, ...], deadline: float) -> bool:
        """Wait until no other engine is starting on any of *node_ids*.

        **Two vLLM startups on one device corrupt each other's arithmetic.**
        The engine sizes its KV cache as

            requested   = device total x gpu_memory_utilization
            consumed    = free memory before the model - free memory after profiling
            available   = requested - consumed - transient peak

        and `consumed` is the *device's* free-memory drop, not this process's
        allocation. vLLM says so itself, above that subtraction: "we assume
        that the other processes using the same GPU did not change their
        memory usage during the profiling." A neighbour that allocates inside
        that window is charged to whoever is profiling, and when the total goes
        negative the engine dies with `No available memory for the cache
        blocks` -- advice to raise gpu_memory_utilization for a model that
        wanted 1.8 GiB and was billed for somebody else's 16.

        That is what happened on the first launch from a containerized node
        here: three attempts of the same 0.5B with the same command, the first
        two failing at two minutes each because an 8B was loading its weights
        inside their profiling window, the third succeeding once the device was
        quiet. Nothing about the model, the plan or the flags differed.

        So a node starts one engine at a time. It costs the wall-clock of
        launching two models at once, which was never real: the second one died
        and retried into the same race.

        Returns False if *deadline* passes first, which the caller turns into a
        refusal rather than a launch into a busy profiler.

        The lift is deliberately not here: see `_launch_worker` for why the
        real answer is `--kv-cache-memory`, which this vLLM already takes.
        """
        with self._starting_cv:
            while any(node in self._starting for node in node_ids):
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return False
                self._starting_cv.wait(min(remaining, STARTING_GATE_POLL_S))
            self._starting.update(node_ids)
            return True

    def _release_starting(self, node_ids: tuple[str, ...]) -> None:
        """Let the next launch on these nodes begin. Idempotent."""
        with self._starting_cv:
            self._starting.difference_update(node_ids)
            self._starting_cv.notify_all()

    def _launch_worker(self, record: _Record, port: int) -> None:
        deployment = record.deployment
        # One clock for the whole of LAUNCHING. `adapter.launch` carries its
        # own launch_timeout_s and `_wait_for_ready` used to start a second,
        # independent one after it returned: the two ran back to back and
        # nothing bounded the sum, so a deployment could sit in LAUNCHING for
        # twice READY_TIMEOUT_S while every message about it -- the refusal
        # below included -- quoted the single figure. The adapter's timeout
        # stays the bound on that one call; this deadline is the bound on the
        # state, which is the number a person is actually waiting out.
        deadline = self._clock() + self.ready_timeout_s

        # One engine at a time per node, for the whole of its startup -- see
        # _hold_starting for the arithmetic that makes two of them fatal to
        # each other.
        #
        # The lift, when it is wanted: this vLLM takes `--kv-cache-memory`,
        # and `determine_available_memory` returns that figure directly
        # ("skipped memory profiling ... does not respect the
        # gpu_memory_utilization config") instead of deriving one from the
        # device's free-memory delta. The fit gate already computes exactly
        # that number -- `fit.breakdown.kv_cache` -- so passing it makes a
        # launch's budget independent of what its neighbours are doing, and
        # this gate can come off. It is a change to the recipe template and to
        # what the runtimes accept, so it is not the hotfix.
        nodes = tuple(sorted(set(deployment.plan.node_ids)))
        if not self._hold_starting(nodes, deadline):
            self._fail_launch(
                record,
                LaunchError(
                    "another model is still starting on %s. This one waited "
                    "%.0fs for the node and stopped rather than starting a "
                    "second engine inside the first one's memory profiling, "
                    "which is a launch that dies after minutes of work and "
                    "blames its own gpu_memory_utilization."
                    % (", ".join(nodes), self.ready_timeout_s),
                    raw="",
                ),
            )
            return
        try:
            self._launch_and_wait(record, port, deadline)
        finally:
            self._release_starting(nodes)

    def _launch_and_wait(self, record: _Record, port: int, deadline: float) -> None:
        """The launch itself, with the node held. Split out so the release in
        `_launch_worker` covers every way this returns."""
        deployment = record.deployment
        # After the gate, not before: this reads what the device has free, and
        # the answer is worthless while another engine is mid-profile.
        share = self._launch_utilization(record)
        if share is not None:
            logger.info(
                "launching %s at %.2f of the device (%.1f GiB is what the plan "
                "budgeted per node)",
                deployment.served_name,
                share,
                deployment.fit.breakdown.total / 1024**3,
            )
        try:
            result = self.adapter.launch(
                deployment.plan,
                deployment.shape,
                deployment.runtime,
                deployment.context_length,
                deployment.max_concurrent_seqs,
                served_name=deployment.served_name,
                port=port,
                gpu_memory_utilization=share,
                # Everything before the container exists is inside this call:
                # the image pull and the weights. Read it as it is printed or
                # it is not readable at all -- the output is in a pipe until
                # the process exits, which on a cold launch is the twenty
                # minutes somebody is sitting through.
                on_output=lambda line: self._note_line(record, line, from_launcher),
                extra_args=deployment.extra_args,
                custom_command=deployment.custom_command,
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

        self._wait_for_ready(record, deadline=deadline)

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

    def _follow_launch_log(self, record: _Record) -> Any:
        """Subscribe to the backend's own account of what it is doing.

        The container log is the only place the load onto the GPU is visible,
        and the runtime narrates itself in there -- the shard loader even
        counts its own shards. This module has always had the call and used it
        only for a post-mortem after a launch died, which is the one moment
        the information no longer helps anybody.

        A subscription rather than a poll because `sparkrun logs` follows: it
        is a `tail -f` and does not return, so every "read" would cost a whole
        timeout and still see one snapshot.
        """
        cluster_id = record.cluster_id
        if not cluster_id:
            return None
        stream = getattr(self.adapter, "stream_logs", None)
        if not callable(stream):
            return None
        try:
            return stream(
                cluster_id,
                hosts=record.hosts,
                tail=LOG_TAIL_LINES,
                on_line=lambda line: self._note_line(record, line, from_runtime_log),
            )
        except Exception:
            logger.debug("could not follow the launch log", exc_info=True)
            return None

    def _wait_for_ready(self, record: _Record, *, deadline: float | None = None) -> None:
        deployment = record.deployment
        # A caller that has already spent part of the launch budget hands in
        # what is left of it (see _launch_worker), which is what keeps the
        # "did not answer within %.0fs" below true rather than half the story.
        # Adopting a deployment at reconcile has spent none of it, so the
        # default is the whole window.
        if deadline is None:
            deadline = self._clock() + self.ready_timeout_s
        url = deployment.backend_url or ""
        last_reason = "no backend url"
        # The container exists, so sparkrun's half of the story is over. Say
        # so before the first log read: the runtime spends its first seconds
        # importing torch without printing anything we know how to read, and
        # leaving the phase on "Preparing the machine" through that would
        # report the step that just finished as the one in progress.
        self._note_progress(
            record,
            LaunchProgress(
                phase="loading",
                status="the backend container is up; waiting for it to load the model",
                source="derate",
            ),
        )
        stream: Any = None
        attempts = 0
        polls = 0
        next_attempt = self._clock()
        try:
            while self._clock() < deadline and not self._stop_event.is_set():
                if deployment.state is not S.LAUNCHING:
                    return  # stopped or failed out from under us
                # Subscribe, or subscribe again if the follower died -- the
                # container is not always exec-able the instant sparkrun
                # returns. Bounded, and before the probe because starting it
                # does not block: a log that cannot be followed at all costs
                # this launch a caption, not a subprocess every few seconds
                # for the next half hour.
                if (
                    (stream is None or not stream.alive)
                    and attempts < LOG_STREAM_ATTEMPTS
                    and self._clock() >= next_attempt
                ):
                    if stream is not None:
                        stream.close()
                    stream = self._follow_launch_log(record)
                    attempts += 1
                    next_attempt = self._clock() + LOG_STREAM_RETRY_S
                # `expect_model`: a 200 on this port is not proof the port is
                # ours. `_allocate_port` cannot see what a machine is already
                # running, and a stranger adopted here becomes a READY
                # deployment that routing sends real traffic to.
                healthy, reason = self._probe(
                    url,
                    timeout=self._ready_probe_timeout(),
                    expect_model=deployment.served_name,
                )
                if healthy:
                    with self._lock:
                        deployment.started_at = self._clock()
                        deployment.last_error = None
                        self._transition(record, S.READY)
                    return
                last_reason = reason or last_reason
                # The runtime saying it died. A solo launch execs the serve
                # command inside a container that sleeps forever, so the
                # container check below is False only when the whole workload
                # is gone -- an engine that failed to start leaves the
                # container up, the port refusing, and this loop waiting out
                # the full ready timeout for a process that has already
                # exited. The reason is the runtime's own line, which names
                # what to change; "backend did not answer within 1800s" does
                # not.
                died = record.progress
                if died is not None and died.fatal:
                    self._fail_launch(
                        record,
                        LaunchError(
                            "the backend died during startup: %s" % died.status,
                            raw="",
                            oom=is_oom(died.status),
                        ),
                        post_mortem=True,
                    )
                    return
                # The container dying during load is the common failure. Catch
                # it here rather than waiting out the full ready timeout.
                # M-16: only a definite False is death. None (sparkrun could
                # not answer -- e.g. check-job timed out on a wedged host) is a
                # miss at most; fall through and keep polling instead of
                # failing the launch out from under a host that may still come
                # back. Every READY_CHECK_JOB_EVERY passes rather than every
                # one: it is a subprocess, and the engine dying -- the case
                # this used to be relied on for -- is caught above from the
                # runtime's own words on every pass.
                if (
                    record.cluster_id
                    and polls % READY_CHECK_JOB_EVERY == 0
                    and self.adapter.is_running(
                        record.cluster_id, hosts=record.hosts, timeout=10.0
                    )
                    is False
                ):
                    self._fail_launch(
                        record,
                        LaunchError(
                            "backend exited during startup: %s" % last_reason, raw=""
                        ),
                        post_mortem=True,
                    )
                    return
                polls += 1
                if self._stop_event.wait(self.ready_poll_interval_s):
                    return
        finally:
            # Every exit from that loop, including the two returns that are
            # the normal ones. A follower left running holds a `docker exec`
            # session open on the node for as long as the container lives.
            if stream is not None:
                stream.close()
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
        # Last, so the file carries whatever the post-mortem added.
        self._archive_log(record)

    def _post_mortem(self, record: _Record) -> None:
        """Pull the container log tail and look for an OOM signature.

        Runs after the FAILED transition, never before it, so a slow SSH does
        not delay the state change the gateway is waiting on.

        Bounded, because `sparkrun logs` follows: it is `tail -f` inside the
        container, so against a container that is still up -- an engine that
        died inside one that sleeps forever -- this call does not return on
        its own. The tail arrives in the first moment and the rest of the wait
        buys nothing, so it is cut short deliberately and the partial output
        is the whole answer, not a truncated one.
        """
        cluster_id = record.cluster_id
        if not cluster_id:
            return
        tail = self.adapter.logs(
            cluster_id, hosts=record.hosts, tail=200, timeout=POST_MORTEM_READ_S
        )
        if not tail:
            return
        with self._lock:
            record.deployment.last_error = "%s\n--- backend log tail ---\n%s" % (
                record.deployment.last_error or "",
                tail.strip(),
            )
        if is_oom(tail):
            self._emit_fit_miss(record, tail)

    def _post_mortem_and_keep(self, record: _Record) -> None:
        """The post-mortem, then the log. One thread, in that order.

        `finally`, because a post-mortem that raises -- an SSH to a host that
        has gone away -- is exactly when the lines already in the buffer are
        the only account of what happened.
        """
        try:
            self._post_mortem(record)
        finally:
            self._archive_log(record)

    def _emit_fit_miss(self, record: _Record, detail: str) -> None:
        """The most valuable telemetry this system produces.

        A launch that OOMs despite passing the fit gate means the fit
        calculator's estimate was low. Emitting the full breakdown next to the
        actual failure is what makes the next estimate better.
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

    def _ready_probe_timeout(self) -> float:
        """Per-path budget for the probe inside the readiness loop.

        This was the one probe in the file with no timeout of its own, so it
        took health.probe's 3.0s default across as many as three URLs --
        `/v1/models` first, then both HEALTH_PATHS -- and a bound-but-hanging
        port could spend 9s inside a loop whose caller asked to poll every 3.
        Nothing here has to be caught inside DEATH_BOUND_S the way the watch
        loop does, so the budget is simply the poll interval: an iteration
        costs about what the caller said it was willing to wait between them.
        """
        # +1 for the `/v1/models` identity read, which is tried before the
        # health paths rather than being one of them.
        per_path = self.ready_poll_interval_s / (len(HEALTH_PATHS) + 1)
        return max(MIN_PROBE_TIMEOUT_S, per_path)

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
                outcome = self._probe(
                    record.deployment.backend_url,
                    timeout=probe_timeout,
                    expect_model=record.deployment.served_name,
                )
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

            # M-13: this must share the gateway's admission-controller denominator
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
                # The gateway stops admitting. We do not kill: shedding load
                # is recoverable, killing a loaded model is not.
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
        # wedged host must not hold up the next deployment's health check. The
        # log is kept once that has run, on the same thread and in that order,
        # so the file carries whatever the post-mortem found. A backend that
        # dies while serving is relaunched by the restart coordinator, so this
        # log has exactly the same short life as a failed launch's.
        threading.Thread(
            target=self._post_mortem_and_keep,
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
        if target in TERMINAL:
            # Here, not only in reconcile(): a week's retention swept once per
            # restart cannot hold against a retry loop that writes a record
            # every twenty seconds, and the pile it leaves is what a browser
            # then has to download. Costs a directory listing while the count
            # is under the cap.
            try:
                self.store.purge_surplus()
            except Exception:
                logger.warning("could not sweep terminal records", exc_info=True)
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

    def _find_conflict(
        self, served_name: str, model_id: str, node_ids: Iterable[str]
    ) -> tuple[Deployment, str] | None:
        """The live deployment standing in this launch's way, and which rule.

        Two rules, and they are scoped differently on purpose.

        The MODEL rule is scoped to the nodes this plan names: a node may not
        run the same model twice, because a second copy shares that node's GPU
        and unified memory with the first and the fit gate budgeted for one.
        Node granularity is the whole of it. A GB10 is one GPU per node and
        ``ParallelismPlan`` places by node id -- there is no finer unit to
        spend -- so "the same GPU" and "the same node" are one sentence here.
        A node that ever carries two separately assignable GPUs is a change
        this check has to learn about; nothing else in the manager would
        notice.

        The SERVED NAME rule is cluster-wide, and is checked before the node
        filter for exactly that reason. A served name is not a property of a
        node; it is the identity clients pass as ``"model"``, the key the
        gateway routes on and the caption the cluster floor draws. Scoping it
        by node let one name be launched twice on different machines, which
        put two bands with one caption on the floor and gave an operator two
        deployments to stop and no way to tell which one an answer came from.
        Replication is still available and still what the planner recommends
        when the spare machines are worth more as a second replica -- it just
        has to be asked for under its own name.

        The model rule is reported first when both apply, because telling an
        operator to pick another name for a copy that should not exist at all
        sends them the wrong way.

        Terminal deployments hold nothing. A FAILED or STOPPED record is
        history, and refusing a relaunch against one would make a node
        unusable until somebody pruned the store.
        """
        wanted = set(node_ids)
        name_clash: Deployment | None = None
        for record in self._records.values():
            d = record.deployment
            if d.state in TERMINAL:
                continue
            # Ahead of the node filter: this rule does not care where the
            # other deployment sits, only that it already answers to the name.
            if d.served_name == served_name and name_clash is None:
                name_clash = d
            if not set(d.plan.node_ids) & wanted:
                continue
            if d.shape.model_id == model_id:
                return d, "model"
        return (name_clash, "served_name") if name_clash is not None else None

    # How far past the base port to look before giving up and handing one out
    # anyway. A bound rather than an open loop: the caller is holding the
    # manager lock, and a machine where two hundred consecutive ports are
    # occupied has a problem this function is not going to solve.
    _PORT_SEARCH = 200

    def _allocate_port(self) -> int:
        taken = {
            r.handle.get("port")
            for r in self._records.values()
            if r.deployment.state not in TERMINAL
        }
        base = self.adapter.base_port
        port = base
        while port in taken:
            port += 1
        # Our own records are not the whole truth about a port. The counter
        # above knows only what THIS control plane started, so a runtime
        # somebody else left running -- or one of ours orphaned by a restart
        # that forgot it -- was invisible, and the port got handed out on top
        # of it. The launch then adopted the stranger as its own backend.
        first = port
        while port < first + self._PORT_SEARCH:
            if port not in taken and self._port_free(port):
                return port
            port += 1
        # Nothing free in the window. Hand out the counter's answer rather
        # than refusing the launch: this check is an improvement on a guess,
        # not a gate, and the identity probe is what actually protects the
        # deployment.
        logger.warning(
            "no free port between %d and %d; falling back to %d",
            first, first + self._PORT_SEARCH, first,
        )
        return first

    def _evidence(self, record: _Record) -> tuple[S, str | None]:
        """What state does the world say this persisted record is in?"""
        deployment = record.deployment
        if deployment.state in TERMINAL:
            return deployment.state, None

        url = deployment.backend_url
        if url:
            # The restart path is where the adoption actually happened: the
            # port outlived the control plane, something else took it, and
            # this call handed the record straight to READY. The reason is
            # kept now instead of discarded, because "a stranger is on your
            # port" is the one thing here worth reporting.
            healthy, why = self._probe(url, expect_model=deployment.served_name)
            if healthy:
                if deployment.state is S.STOPPING:
                    # A stop we never finished. Finish it.
                    return S.STOPPING, "stop was in flight when the control plane restarted"
                return S.READY, None
            if wrong_model(why):
                # Decisive, and the one case where the url itself is the fault
                # rather than the backend behind it. Retiring the record is
                # what breaks the loop: left LAUNCHING it keeps this url, the
                # ready-waiter keeps finding a stranger that answers, and a
                # restart re-adopts it. FAILED with the probe's own sentence
                # sends the operator to the server actually holding the port.
                return S.FAILED, why

        cluster_id = record.cluster_id
        # No cluster id is UNKNOWN, not dead. `is_running` is three-valued and
        # says so in as many words -- "None is absence of evidence, not evidence
        # of death; callers must treat it as unknown, never as False" -- and a
        # record with no handle yet is the youngest one there is: the launch was
        # seconds old when the control plane went down, which makes it the most
        # likely to still be starting, not the least.
        #
        # Read as False it took the FAILED branch below and reported "launch did
        # not survive the control plane restart" about a sparkrun process that
        # was alive and still loading. The control plane then forgot the port it
        # was on, so nothing would ever stop it: an orphan holding GPU memory
        # that only shows up in nvidia-smi, on a machine whose aggregate memory
        # reads N/A. Observed on the live box, which is why this is one token.
        running = (
            self.adapter.is_running(cluster_id, hosts=record.hosts)
            if cluster_id
            else None
        )
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
