"""The sparkrun adapter.

sparkrun owns fabric setup, the SSH mesh, ConnectX-7 subnet configuration and
multi-runtime launch. We render a plan into an invocation, run it, and parse
the result back. We do not launch vLLM by hand and we do not reimplement any
of the above.

We also ignore sparkrun's fit estimate completely. `sparkrun run` prints a
"VRAM Estimation" block ending in "DGX Spark fit: YES"; it is advisory, it
does not block, and its utilization figures are known to be wrong. Agent D's
FitResult is the only verdict that gates a launch. This module never parses
that block.

Verified against sparkrun 0.2.40.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from control_plane.contracts import (
    DEFAULT_GUARDRAIL,
    ModelShape,
    ParallelismPlan,
    RegistryPort,
)

from .flags import KNOBS_BY_NAME, SPARKRUN_BIN, RuntimeSpec, runtime_spec
from .recipes import RecipeSpec, materialize, synthesize

from control_plane.paths import data_dir

logger = logging.getLogger(__name__)

#: `Cluster:   sparkrun_c02f6a8db07f` in `sparkrun run` output. This is the
#: handle for stop, logs, and liveness checks.
CLUSTER_ID_RE = re.compile(r"^\s*Cluster:\s+(sparkrun_[0-9a-f]{6,})\s*$", re.MULTILINE)
#: `  Head:    10.0.0.1`
HEAD_HOST_RE = re.compile(r"^\s*Head:\s+(\S+)\s*$", re.MULTILINE)
#: Solo launches print no Head line, only the target.
TARGET_HOST_RE = re.compile(r"^\s*Target:\s+(\S+)\s*$", re.MULTILINE)

#: Signatures that mean the runtime ran out of memory rather than failing for
#: some ordinary reason. Used to tag the calibration event Agent D wants.
OOM_SIGNATURES = (
    "out of memory",
    "cuda error: out of memory",
    "torch.outofmemoryerror",
    "no available memory for the cache blocks",
    "cuda_error_out_of_memory",
    "killed process",  # the OOM killer
    "oom-kill",
    # vLLM refusing to start at all, which is a memory refusal that never says
    # "out of memory": "Free memory on device cuda:0 (49.56/121.69 GiB) on
    # startup is less than desired GPU memory utilization (0.9, 109.52 GiB)".
    # Read off a real failed launch on a GB10, and exactly the miss the fit
    # gate wants told about -- the static ceiling said the model fit, and less
    # than half the pool was actually free because the OS shares it. Without
    # this the launch was tagged an ordinary failure and Agent D learned
    # nothing from the one case it is calibrated by.
    "is less than desired gpu memory utilization",
)

NOT_INSTALLED = (
    "sparkrun is not installed. The control plane launches every backend "
    "through sparkrun and will not launch vLLM or SGLang by hand.\n"
    "Install it with:  uv tool install sparkrun\n"
    "Then re-run this launch. If sparkrun is installed somewhere unusual, "
    "point DERATE_SPARKRUN_BIN at it."
)


class SparkrunNotInstalled(RuntimeError):
    pass


class LogStream:
    """A running `sparkrun logs`, pumping lines to a callback until closed.

    One thread, doing nothing but moving bytes out of a pipe. It exists
    because the alternative -- reading the pipe from the thread that is also
    waiting for the backend to answer -- makes a launch's readiness wait as
    slow as its log reader, and the log reader is following a file that never
    ends.
    """

    def __init__(self, proc: "subprocess.Popen[str]", on_line: Callable[[str], None]) -> None:
        self._proc = proc
        self._reader = threading.Thread(
            target=self._pump, args=(on_line,), name="derate-log-stream", daemon=True
        )
        self._reader.start()

    def _pump(self, on_line: Callable[[str], None]) -> None:
        stdout = self._proc.stdout
        if stdout is None:  # pragma: no cover - Popen was asked for a pipe
            return
        # Universal newlines makes a lone `\r` a line ending, which is what
        # turns the shard loader's redrawn progress bar into one line per
        # frame instead of one line at the very end.
        for line in stdout:
            text = line.strip()
            if not text:
                continue
            try:
                on_line(text)
            except Exception:
                logger.debug("log stream callback failed", exc_info=True)

    @property
    def alive(self) -> bool:
        return self._proc.poll() is None

    def close(self) -> None:
        """Kill the follower and wait for its thread. Safe to call twice."""
        if self._proc.poll() is None:
            # The whole group: `sparkrun logs` is a CLI in front of a `docker
            # exec ... tail -f`, and killing only the parent leaves the exec
            # session open on the node. hasattr rather than a platform name,
            # like the rest of this codebase -- os.killpg is the thing being
            # asked about, so ask about it.
            killed = False
            if hasattr(os, "killpg") and hasattr(os, "getpgid"):
                try:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
                    killed = True
                except OSError:
                    logger.debug("could not kill the log stream group", exc_info=True)
            if not killed:
                self._proc.kill()
        try:
            self._proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:  # pragma: no cover - it was killed
            logger.debug("log stream did not exit after a kill")
        self._reader.join(timeout=5.0)


class LaunchError(RuntimeError):
    """A launch that did not produce a usable backend.

    Carries the raw sparkrun output so it can go into Deployment.last_error
    unedited; an unparseable launch is a failure whose evidence we keep.
    """

    def __init__(self, message: str, *, raw: str = "", oom: bool = False) -> None:
        super().__init__(message)
        self.raw = raw
        self.oom = oom


@dataclass(frozen=True)
class LaunchResult:
    cluster_id: str  # sparkrun_<hex>, the process/container handle
    head_host: str
    backend_url: str  # OpenAI-compatible base URL, ends in /v1
    port: int
    hosts: list[str]
    recipe_path: Path
    argv: list[str]
    raw: str = field(repr=False, default="")


def backend_origin(backend_url: str) -> str:
    """`http://h:8100/v1` -> `http://h:8100`. Health lives at the origin."""
    return backend_url[: -len("/v1")] if backend_url.endswith("/v1") else backend_url.rstrip("/")


def is_oom(text: str) -> bool:
    low = text.lower()
    return any(sig in low for sig in OOM_SIGNATURES)


class SparkrunAdapter:
    """Renders, runs, inspects and tears down sparkrun workloads.

    ``registry`` maps a plan's node_ids to the addresses sparkrun connects to.
    Everything else is configuration with a working default, because the whole
    pitch is that first run needs none.
    """

    def __init__(
        self,
        registry: RegistryPort | None = None,
        *,
        recipe_dir: Path | str | None = None,
        binary: str = SPARKRUN_BIN,
        base_port: int = 8100,
        gpu_memory_utilization: float = DEFAULT_GUARDRAIL,
        launch_timeout_s: float = 1800.0,
        stop_timeout_s: float = 30.0,
        env: dict[str, str] | None = None,
    ) -> None:
        self.registry = registry
        self.recipe_dir = (
            Path(recipe_dir) if recipe_dir is not None else data_dir() / "recipes"
        )
        self.binary = binary
        self.base_port = base_port
        self.gpu_memory_utilization = gpu_memory_utilization
        self.launch_timeout_s = launch_timeout_s
        self.stop_timeout_s = stop_timeout_s
        self.env = env

    # -- availability -----------------------------------------------------

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def require(self) -> None:
        if not self.available():
            raise SparkrunNotInstalled(NOT_INSTALLED)

    def version(self) -> str | None:
        if not self.available():
            return None
        proc = self._run([self.binary, "--version"], timeout=15.0)
        match = re.search(r"version\s+(\S+)", proc.stdout)
        return match.group(1) if match else proc.stdout.strip() or None

    # -- host resolution --------------------------------------------------

    def hosts_for(self, node_ids: Sequence[str]) -> list[str]:
        """node_id -> the address sparkrun SSHes to.

        Order is preserved: the planner decided which node is the pipeline
        head, and sparkrun treats the first host as the head node.
        """
        hosts: list[str] = []
        for node_id in node_ids:
            state = self.registry.get_node(node_id) if self.registry else None
            # A node_id that the registry does not know is used verbatim.
            # In practice node ids are hostnames, so this is the useful
            # fallback rather than an error that blocks the demo.
            hosts.append(state.profile.address if state else node_id)
        return hosts

    # -- rendering (pure) -------------------------------------------------

    def recipe_for(
        self,
        plan: ParallelismPlan,
        shape: ModelShape,
        runtime: str,
        ctx: int,
        max_seqs: int,
        *,
        served_name: str | None = None,
        port: int | None = None,
        gpu_memory_utilization: float | None = None,
        extra_args: tuple[str, ...] = (),
        custom_command: tuple[str, ...] = (),
    ) -> RecipeSpec:
        return synthesize(
            shape,
            plan,
            runtime,
            ctx,
            max_seqs,
            served_name or default_served_name(shape),
            port=port if port is not None else self.base_port,
            # Per launch when the caller worked one out, and the adapter's own
            # default when nobody did. See deploy/utilization.py: this flag is
            # a claim on the whole device rather than a limit, so a constant
            # asks for the same share of the machine for a 0.5B model as for a
            # 120B one -- and is refused outright on a machine that is busy.
            gpu_memory_utilization=(
                self.gpu_memory_utilization
                if gpu_memory_utilization is None
                else gpu_memory_utilization
            ),
            recipe_dir=self.recipe_dir,
            extra_args=extra_args,
            custom_command=custom_command,
        )

    def render_command(
        self,
        plan: ParallelismPlan,
        shape: ModelShape,
        runtime: str,
        ctx: int,
        max_seqs: int,
        *,
        served_name: str | None = None,
        port: int | None = None,
        recipe: RecipeSpec | None = None,
        gpu_memory_utilization: float | None = None,
        extra_args: tuple[str, ...] = (),
    ) -> list[str]:
        """The exact argv that will run. Pure: no subprocess, no filesystem.

        Agent H shows this to the user before they commit, so it must be the
        real thing, including --no-follow. Deterministic for a given plan,
        shape, runtime, context, concurrency, port and extra_args.
        """
        spec: RuntimeSpec = runtime_spec(runtime)
        name = served_name or default_served_name(shape)
        chosen_port = port if port is not None else self.base_port
        recipe = recipe or self.recipe_for(
            plan, shape, runtime, ctx, max_seqs, served_name=name, port=chosen_port,
            gpu_memory_utilization=gpu_memory_utilization, extra_args=extra_args,
        )

        values: dict[str, Any] = {
            "hosts": self.hosts_for(plan.node_ids),
            "tensor_parallel": plan.tensor_parallel,
            "pipeline_parallel": plan.pipeline_parallel,
            "context_length": ctx,
            "served_name": name,
            "port": chosen_port,
            # The same value the recipe was synthesized with, and this is the
            # copy that decides: the knob is emitted as a CLI override, which
            # wins over the recipe's default. Reading `self.` here while the
            # recipe said something else would put one number on screen in the
            # rendered command and run another. (The flag is spelled in
            # flags.py and nowhere else, including in this comment -- there is
            # a test.)
            "gpu_memory_utilization": (
                self.gpu_memory_utilization
                if gpu_memory_utilization is None
                else gpu_memory_utilization
            ),
            "max_concurrent_seqs": max_seqs,
            # Only sent when the planner actually asked for them; sparkrun
            # hashes non-default parallelism into the cluster_id, and sending
            # an explicit 1 would change the handle for no reason.
            "expert_parallel": plan.expert_parallel if plan.expert_parallel > 1 else None,
            "data_parallel": plan.data_parallel if plan.data_parallel > 1 else None,
        }

        argv = [self.binary, "run", str(recipe.path)]
        for knob_name, value in values.items():
            knob = KNOBS_BY_NAME[knob_name]
            key = spec.max_seqs_key if knob_name == "max_concurrent_seqs" else None
            argv += knob.emit(value, key)
        # Detach and return instead of streaming container logs forever.
        # sparkrun still does a 5s liveness check before exiting, which is
        # how a container that dies on startup surfaces as a non-zero rc.
        argv.append("--no-follow")
        return argv

    # -- launching --------------------------------------------------------

    def launch(
        self,
        plan: ParallelismPlan,
        shape: ModelShape,
        runtime: str,
        ctx: int,
        max_seqs: int,
        *,
        served_name: str | None = None,
        port: int | None = None,
        gpu_memory_utilization: float | None = None,
        on_output: Callable[[str], None] | None = None,
        extra_args: tuple[str, ...] = (),
        custom_command: tuple[str, ...] = (),
    ) -> LaunchResult:
        """Run the invocation and parse the handle back out.

        Raises LaunchError with the raw output preserved when sparkrun fails
        or prints something we cannot parse. Never returns a partial result:
        a launch we cannot address is a launch we cannot stop.

        *on_output* is called with each line as it is printed. Everything slow
        about a cold launch happens inside this one call -- the 24.4 GB image
        pull, then the weights -- and with the output merely captured, that
        whole window was a process holding a full pipe and a screen saying
        "launching". The callback is what lets a caller report it while it is
        happening; without one this runs exactly as it always did.
        """
        self.require()
        name = served_name or default_served_name(shape)
        chosen_port = port if port is not None else self.base_port
        recipe = self.recipe_for(
            plan, shape, runtime, ctx, max_seqs, served_name=name, port=chosen_port,
            gpu_memory_utilization=gpu_memory_utilization, extra_args=extra_args,
            custom_command=custom_command,
        )
        materialize(recipe)
        argv = self.render_command(
            plan, shape, runtime, ctx, max_seqs,
            served_name=name, port=chosen_port, recipe=recipe,
            gpu_memory_utilization=gpu_memory_utilization,
        )
        hosts = self.hosts_for(plan.node_ids)

        logger.info("launching %s: %s", name, " ".join(argv))
        try:
            if on_output is None:
                proc = self._run(argv, timeout=self.launch_timeout_s)
            else:
                proc = self._run_streamed(
                    argv, timeout=self.launch_timeout_s, on_line=on_output
                )
        except subprocess.TimeoutExpired as exc:
            raw = _decode(exc.stdout) + _decode(exc.stderr)
            raise LaunchError(
                "sparkrun did not return within %.0fs" % self.launch_timeout_s,
                raw=raw,
                oom=is_oom(raw),
            ) from exc

        raw = proc.stdout + proc.stderr
        if proc.returncode != 0:
            raise LaunchError(
                "sparkrun exited %d" % proc.returncode, raw=raw, oom=is_oom(raw)
            )

        match = CLUSTER_ID_RE.search(raw)
        if not match:
            # Exit code said success but we cannot address the workload.
            # Treat as a launch failure and keep the evidence.
            raise LaunchError(
                "sparkrun exited 0 but printed no cluster id; cannot track this workload",
                raw=raw,
                oom=is_oom(raw),
            )
        cluster_id = match.group(1)

        head_match = HEAD_HOST_RE.search(raw) or TARGET_HOST_RE.search(raw)
        head_host = head_match.group(1) if head_match else (hosts[0] if hosts else "")
        if not head_host:
            raise LaunchError(
                "sparkrun printed no head host and the plan named no nodes",
                raw=raw,
                oom=is_oom(raw),
            )

        return LaunchResult(
            cluster_id=cluster_id,
            head_host=head_host,
            backend_url="http://%s:%d/v1" % (head_host, chosen_port),
            port=chosen_port,
            hosts=hosts,
            recipe_path=recipe.path,
            argv=argv,
            raw=raw,
        )

    # -- inspection and teardown ------------------------------------------

    def check_job(
        self,
        cluster_id: str,
        *,
        hosts: Sequence[str] | None = None,
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        """`sparkrun cluster check-job --json`. The reconcile primitive.

        Returns at least {"running": bool | None}: True or False when
        sparkrun actually answered, None when it could not be asked at all
        (M-16). Reconcile must survive a sparkrun that is missing or broken
        rather than raising -- but "we could not check" is not the same
        fact as "confirmed not running", and callers (is_running(), and
        everything downstream of it) must not collapse the two. A sparkrun
        that is not installed or not on PATH right now is exactly the same
        kind of absent evidence as the TIMEOUT case just below: the control
        plane's own environment can temporarily lack the binary (a PATH not
        yet mounted, an image mid-upgrade) while a deployment it launched
        earlier, under a different environment, is still running --
        reporting a hard False here retired live deployments through this
        branch exactly as TIMEOUT used to through that one.
        """
        if not self.available():
            return {"running": None, "cluster_id": cluster_id, "error": NOT_INSTALLED}
        argv = [self.binary, "cluster", "check-job", cluster_id, "--json"]
        if hosts:
            argv += KNOBS_BY_NAME["hosts"].emit(list(hosts))
        try:
            proc = self._run(argv, timeout=timeout)
        except subprocess.TimeoutExpired:
            # M-16: a timeout means sparkrun could not answer, not that the
            # workload is confirmed absent. "running": False here used to
            # read as a definite negative to every caller, which let
            # reconcile() retire a deployment that might still be alive on a
            # wedged host. None is the honest "we do not know" -- callers
            # must not collapse it back into False.
            return {
                "running": None,
                "cluster_id": cluster_id,
                "error": "check-job timed out",
            }
        payload = _first_json_object(proc.stdout)
        if payload is None:
            # Exit code alone still answers the question: 0 = running.
            return {
                "running": proc.returncode == 0,
                "cluster_id": cluster_id,
                "error": (proc.stderr or proc.stdout).strip() or None,
            }
        payload.setdefault("cluster_id", cluster_id)
        return payload

    def is_running(
        self,
        cluster_id: str,
        *,
        hosts: Sequence[str] | None = None,
        timeout: float = 60.0,
    ) -> bool | None:
        """Three-valued: True/False when sparkrun answered, None when it
        could not be asked (M-16). None is absence of evidence, not evidence
        of death -- callers must treat it as "unknown", never as False."""
        running = self.check_job(cluster_id, hosts=hosts, timeout=timeout).get("running")
        return running if running is None else bool(running)

    def logs(
        self,
        cluster_id: str,
        *,
        hosts: Sequence[str] | None = None,
        tail: int = 200,
        timeout: float = 30.0,
    ) -> str:
        """Container log tail, for the post-mortem after a backend dies.

        Best effort: a host we can no longer reach returns an empty tail
        rather than raising, because the caller is already reporting a
        failure and a second one helps nobody.
        """
        if not self.available():
            return ""
        argv = [self.binary, "logs", cluster_id, "--tail", str(tail)]
        if hosts:
            argv += KNOBS_BY_NAME["hosts"].emit(list(hosts))
        try:
            proc = self._run(argv, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            return _decode(exc.stdout) + _decode(exc.stderr)
        except OSError:
            logger.debug("could not read logs for %s", cluster_id, exc_info=True)
            return ""
        return proc.stdout + proc.stderr

    def stream_logs(
        self,
        cluster_id: str,
        *,
        hosts: Sequence[str] | None = None,
        tail: int = 60,
        on_line: Callable[[str], None],
    ) -> "LogStream | None":
        """Follow the backend's log, line by line, until the stream is closed.

        `sparkrun logs` does not return: it runs `docker exec <container> tail
        -f --lines N /tmp/sparkrun_serve.log` (orchestration/ssh.py), because
        solo launches exec the serve command inside a sleeping container and
        its output goes to that file rather than to `docker logs`. So this is
        a subscription, not a request -- polling it would spend a whole
        timeout on every read and still only ever see a snapshot.

        Returns None when there is no sparkrun to ask. The caller must
        `close()` what it gets back; nothing here reaps it on its own.
        """
        if not self.available():
            return None
        argv = [self.binary, "logs", cluster_id, "--tail", str(tail)]
        if hosts:
            argv += KNOBS_BY_NAME["hosts"].emit(list(hosts))
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=self.env,
                # Its own process group, so closing this kills the `docker
                # exec` underneath rather than only the CLI in front of it --
                # an orphaned `tail -f` holds an exec session open on the node
                # for as long as the container lives. POSIX-only and ignored
                # elsewhere, which matches how `close` asks about killpg.
                start_new_session=True,
            )
        except OSError:
            logger.debug("could not start a log stream for %s", cluster_id, exc_info=True)
            return None
        return LogStream(proc, on_line)

    def stop(self, cluster_id: str, *, hosts: Sequence[str] | None = None) -> tuple[bool, str]:
        """Tear down through sparkrun. Returns (ok, output)."""
        self.require()
        argv = [self.binary, "stop", cluster_id]
        if hosts:
            argv += KNOBS_BY_NAME["hosts"].emit(list(hosts))
        try:
            proc = self._run(argv, timeout=self.stop_timeout_s)
        except subprocess.TimeoutExpired as exc:
            return False, "sparkrun stop did not return within %.0fs\n%s" % (
                self.stop_timeout_s,
                _decode(exc.stdout) + _decode(exc.stderr),
            )
        return proc.returncode == 0, proc.stdout + proc.stderr

    # -- plumbing ---------------------------------------------------------

    def _run(self, argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=self.env,
            check=False,
        )

    def _run_streamed(
        self,
        argv: list[str],
        *,
        timeout: float,
        on_line: Callable[[str], None],
    ) -> subprocess.CompletedProcess[str]:
        """`_run`, but the caller sees each line as it arrives.

        Same return value and the same TimeoutExpired-with-partial-output on
        the way out, because `launch` above parses and reports on both and
        must not learn which one ran.

        Three things are deliberate:

        **stderr is merged into stdout.** sparkrun's progress goes to one and
        its errors to the other, and reading two pipes from one thread
        deadlocks the moment either fills. `launch` concatenates them anyway
        before parsing, so the merge costs nothing and removes the deadlock.

        **A reader thread, not a readline loop.** `readline` blocks, so a
        deadline tested between lines is never reached by a launch that goes
        quiet -- which is exactly what a stalled pull looks like. The wait for
        the process carries the timeout; the thread only moves bytes.

        **The whole output is kept.** It is the evidence in a LaunchError and
        it holds the cluster id, without which a launch cannot be stopped. A
        cap here would be a cap on that, so there isn't one.
        """
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,  # line buffered: progress that arrives late is not progress
            env=self.env,
        )
        collected: list[str] = []

        def pump() -> None:
            assert proc.stdout is not None
            # Universal newlines makes a lone `\r` a line ending, so a docker
            # pull's redrawn progress arrives here as one line per frame
            # rather than one enormous line at the end.
            for line in proc.stdout:
                collected.append(line)
                text = line.strip()
                if not text:
                    continue
                try:
                    on_line(text)
                except Exception:
                    # Reporting progress must never be able to fail a launch.
                    logger.debug("launch output callback failed", exc_info=True)

        reader = threading.Thread(target=pump, name="derate-launch-output", daemon=True)
        reader.start()
        try:
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            reader.join(timeout=5.0)
            raise subprocess.TimeoutExpired(
                argv, timeout, output="".join(collected)
            ) from None
        # The process is gone, so the pipe is at EOF and this cannot hang; the
        # join is what guarantees the last lines are in `collected` before we
        # hand the output to a parser looking for the cluster id.
        reader.join(timeout=5.0)
        return subprocess.CompletedProcess(
            argv, returncode, stdout="".join(collected), stderr=""
        )


def default_served_name(shape: ModelShape) -> str:
    """`openai/gpt-oss-120b` -> `gpt-oss-120b`. What clients pass as "model"."""
    tail = shape.model_id.rsplit("/", 1)[-1]
    return tail.split(":", 1)[0] or shape.model_id


def _decode(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _first_json_object(text: str) -> dict[str, Any] | None:
    """Pull the JSON object out of output that may carry banner lines."""
    import json

    start = text.find("{")
    while start != -1:
        try:
            value, _ = json.JSONDecoder().raw_decode(text[start:])
        except ValueError:
            start = text.find("{", start + 1)
            continue
        if isinstance(value, dict):
            return value
        start = text.find("{", start + 1)
    return None
