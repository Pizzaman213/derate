"""The sparkrun adapter.

sparkrun owns fabric setup, the SSH mesh, ConnectX-7 subnet configuration and
multi-runtime launch. We render a plan into an invocation, run it, and parse
the result back. We do not launch vLLM by hand and we do not reimplement any
of the above.

We also ignore sparkrun's fit estimate completely. `sparkrun run` prints a
"VRAM Estimation" block ending in "DGX Spark fit: YES"; it is advisory, it
does not block, and its utilization figures are known to be wrong. The fit
calculator's FitResult is the only verdict that gates a launch. This module
never parses that block.

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
from typing import Any, Callable, Mapping, Sequence

from control_plane.contracts import (
    DEFAULT_GUARDRAIL,
    ModelShape,
    ParallelismPlan,
    RegistryPort,
    SpeculativeSpec,
)

from .flags import KNOBS_BY_NAME, RuntimeSpec, runtime_spec, sparkrun_binary
from .recipes import RecipeSpec, materialize, synthesize

from control_plane.paths import data_dir

logger = logging.getLogger(__name__)

#: How long to wait for a killed sparkrun to flush its pipes before giving up
#: on its partial output. It has had SIGKILL; this is a drain, not a wait.
_KILL_DRAIN_S = 5.0

#: Asking docker which container a cluster id is in is a local socket call on
#: the snapshot path, so it gets a short leash of its own.
_DOCKER_PS_TIMEOUT_S = 5.0


def _docker_binary() -> str:
    """Resolved rather than assumed, the same way `SparkrunAdapter.binary` is.

    Falls back to the bare name so a PATH that resolves it at exec time still
    works -- and so the failure, if there is one, is an OSError the callers
    above already treat as "no reading".
    """
    return shutil.which("docker") or "docker"

#: `Cluster:   sparkrun_c02f6a8db07f` in `sparkrun run` output. This is the
#: handle for stop, logs, and liveness checks.
#:
#: The trailing segments are not decoration. sparkrun 0.2.40 printed one hex
#: run, 0.3.8 prints two (`sparkrun_6579cd9ba54b79f5_1a209ee6d1e8`), and an
#: expression anchored after the first one matched neither -- so a launch that
#: had genuinely worked, containers up on both hosts, came back as "sparkrun
#: exited 0 but printed no cluster id; cannot track this workload" and was
#: recorded FAILED with the workload still running. A handle this cannot read
#: is a workload nothing can stop, so the shape is kept deliberately loose:
#: any number of underscore-joined hex runs.
CLUSTER_ID_RE = re.compile(
    r"^\s*Cluster:\s+(sparkrun_[0-9a-f]{6,}(?:_[0-9a-f]{6,})*)\s*$", re.MULTILINE
)
#: `  Head:    10.0.0.1`
HEAD_HOST_RE = re.compile(r"^\s*Head:\s+(\S+)\s*$", re.MULTILINE)
#: Solo launches print no Head line, only the target.
TARGET_HOST_RE = re.compile(r"^\s*Target:\s+(\S+)\s*$", re.MULTILINE)

#: Signatures that mean the runtime ran out of memory rather than failing for
#: some ordinary reason. Used to tag the calibration event the fit calculator wants.
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
    # this the launch was tagged an ordinary failure and the fit calculator
    # learned nothing from the one case it is calibrated by.
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
        binary: str | None = None,
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
        # None, not the constant: an explicit binary is the caller's and wins,
        # and everything else asks the environment now rather than at import.
        self.binary = binary if binary is not None else sparkrun_binary()
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
        kv_cache_memory_bytes: int | None = None,
        speculative: SpeculativeSpec | None = None,
        extra_args: tuple[str, ...] = (),
        custom_command: tuple[str, ...] = (),
        enforce_eager: bool = False,
        cudagraph_capture_sizes: tuple[int, ...] | None = None,
        kv_dtype: str | None = None,
        quantization: str | None = None,
        nccl_env: Mapping[str, str] | None = None,
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
            # No adapter-level default, unlike the share above: this one is a
            # byte count the fit gate computed for this model at this context,
            # and there is no sensible constant to fall back to. Absent, the
            # runtime sizes its own cache exactly as it did before.
            kv_cache_memory_bytes=kv_cache_memory_bytes,
            # Also no adapter-level default, and for a stronger reason than the
            # byte count above: this one is not a number the adapter could
            # sensibly guess at all. It is a decision somebody made on the model
            # screen, priced by the fit gate against this checkpoint.
            speculative=speculative,
            recipe_dir=self.recipe_dir,
            extra_args=extra_args,
            custom_command=custom_command,
            enforce_eager=enforce_eager,
            cudagraph_capture_sizes=cudagraph_capture_sizes,
            kv_dtype=kv_dtype,
            quantization=quantization,
            nccl_env=nccl_env,
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
        kv_cache_memory_bytes: int | None = None,
        speculative: SpeculativeSpec | None = None,
        extra_args: tuple[str, ...] = (),
        enforce_eager: bool = False,
        cudagraph_capture_sizes: tuple[int, ...] | None = None,
        kv_dtype: str | None = None,
        quantization: str | None = None,
        nccl_env: Mapping[str, str] | None = None,
    ) -> list[str]:
        """The exact argv that will run. Pure: no subprocess, no filesystem.

        The UI shows this to the user before they commit, so it must be the
        real thing, including --no-follow. Deterministic for a given plan,
        shape, runtime, context, concurrency, port and extra_args.
        """
        spec: RuntimeSpec = runtime_spec(runtime)
        name = served_name or default_served_name(shape)
        chosen_port = port if port is not None else self.base_port
        recipe = recipe or self.recipe_for(
            plan, shape, runtime, ctx, max_seqs, served_name=name, port=chosen_port,
            gpu_memory_utilization=gpu_memory_utilization,
            kv_cache_memory_bytes=kv_cache_memory_bytes,
            speculative=speculative, extra_args=extra_args,
            enforce_eager=enforce_eager, cudagraph_capture_sizes=cudagraph_capture_sizes,
            kv_dtype=kv_dtype,
            quantization=quantization,
            nccl_env=nccl_env,
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
        kv_cache_memory_bytes: int | None = None,
        speculative: SpeculativeSpec | None = None,
        on_output: Callable[[str], None] | None = None,
        extra_args: tuple[str, ...] = (),
        custom_command: tuple[str, ...] = (),
        enforce_eager: bool = False,
        cudagraph_capture_sizes: tuple[int, ...] | None = None,
        kv_dtype: str | None = None,
        quantization: str | None = None,
        nccl_env: Mapping[str, str] | None = None,
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
            gpu_memory_utilization=gpu_memory_utilization,
            kv_cache_memory_bytes=kv_cache_memory_bytes,
            speculative=speculative, extra_args=extra_args,
            custom_command=custom_command,
            enforce_eager=enforce_eager, cudagraph_capture_sizes=cudagraph_capture_sizes,
            kv_dtype=kv_dtype,
            quantization=quantization,
            nccl_env=nccl_env,
        )
        materialize(recipe)
        argv = self.render_command(
            plan, shape, runtime, ctx, max_seqs,
            served_name=name, port=chosen_port, recipe=recipe,
            gpu_memory_utilization=gpu_memory_utilization,
            kv_cache_memory_bytes=kv_cache_memory_bytes,
            speculative=speculative,
            enforce_eager=enforce_eager, cudagraph_capture_sizes=cudagraph_capture_sizes,
            kv_dtype=kv_dtype,
            quantization=quantization,
            nccl_env=nccl_env,
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

    #: sparkrun's own serve-log path inside a solo container. A literal, like
    #: every other sparkrun fact this module reads: `sparkrun logs` itself
    #: runs `docker exec <container> tail -f --lines N` against exactly this
    #: file (orchestration/ssh.py), and manager.py's own comment names it too.
    SERVE_LOG_PATH = "/tmp/sparkrun_serve.log"

    def log_snapshot(
        self,
        cluster_id: str,
        *,
        hosts: Sequence[str] | None = None,
        tail: int = 200,
        timeout: float = 10.0,
    ) -> str:
        """A bounded, NON-FOLLOWING read of the same log `logs()` follows.

        `sparkrun logs` has no no-follow mode -- `--tail` is documented as
        "number of log lines before following" -- so every call to it is a
        subscription. CLAUDE.md says so in as many words: that command "is
        subscribed to, never polled". A caller that wants one snapshot every
        minute is polling it, and on a LOCAL docker socket each poll strands a
        `tail -f` inside the container for as long as the container lives:
        killing the client does not reach a process dockerd started. Measured
        here before this existed, one container held 1195 of them and another
        2994, one per minute since launch.

        So the snapshot path does not go through `sparkrun logs` at all. It
        reads the same file with a `tail -n` that exits on its own, which is
        what a snapshot always wanted. Remote hosts still fall back to
        `logs()`: over ssh the strand does not happen, because ssh tears the
        remote command down with its client.
        """
        name = self._local_container(cluster_id)
        if name is None:
            return self.logs(cluster_id, hosts=hosts, tail=tail, timeout=timeout)
        argv = [
            _docker_binary(), "exec", name,
            "tail", "-n", str(tail), self.SERVE_LOG_PATH,
        ]
        try:
            proc = self._run(argv, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            return _decode(exc.stdout)
        except OSError:
            logger.debug("could not read the serve log for %s", cluster_id, exc_info=True)
            return ""
        # A non-zero exit is an absent file or a container that went away
        # mid-read. Both are "no log", never a raise: the caller is keeping
        # evidence, not depending on it.
        return proc.stdout if proc.returncode == 0 else ""

    def _local_container(self, cluster_id: str) -> str | None:
        """The container this cluster id is running in ON THIS MACHINE, or None.

        None means "not here, or docker could not be asked" -- absence of
        evidence, and the caller falls back to sparkrun rather than treating
        it as absence of a container.
        """
        try:
            proc = self._run(
                [
                    _docker_binary(), "ps",
                    "--filter", "name=^%s_" % cluster_id,
                    "--format", "{{.Names}}",
                ],
                timeout=_DOCKER_PS_TIMEOUT_S,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        if proc.returncode != 0:
            return None
        names = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        return names[0] if names else None

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
        """`subprocess.run`, except a timeout kills the whole process GROUP.

        This was `subprocess.run(..., timeout=)` and that is a leak, not a
        style question. `sparkrun logs` is a CLI in front of a `docker exec
        ... tail -f`, so it never returns and this call ALWAYS times out --
        and `subprocess.run`'s own timeout kills the CLI it started and
        nothing underneath it. Over ssh that is harmless, because ssh tears
        the remote command down when its client dies. Against a LOCAL docker
        socket -- which is how a containerized node reaches its own
        deployments -- the exec'd process outlives the client and runs for as
        long as the container does.

        Measured on this box before the fix: 1195 of one container's 1208
        processes were `tail -f --lines 200 /tmp/sparkrun_serve.log`, exactly
        one per minute since launch, because `_snapshot_serving_log` calls
        `logs()` every `SERVING_LOG_SNAPSHOT_S`. Another was at 2994, being
        watched by two coordinators. `LogStream.close` already guarded this
        for the streaming path and says why; every other caller of this method
        did not, and `logs()` is the one that runs on a timer.

        `hasattr` rather than a platform name, for the reason the rest of this
        codebase gives: `os.killpg` is the thing being asked about, so ask
        about it.
        """
        group = hasattr(os, "killpg") and hasattr(os, "getpgid") and hasattr(os, "setsid")
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.env,
            start_new_session=group,
        )
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._kill_group(proc, group=group)
            try:
                # Drain whatever it managed to write. Callers of `logs()` read
                # exactly this partial output and it is the whole point of the
                # call, so losing it here would trade one bug for another.
                out, err = proc.communicate(timeout=_KILL_DRAIN_S)
            except subprocess.TimeoutExpired:  # pragma: no cover - it was killed
                out, err = "", ""
            raise subprocess.TimeoutExpired(
                argv, timeout, output=out, stderr=err
            ) from None
        return subprocess.CompletedProcess(argv, proc.returncode, out, err)

    @staticmethod
    def _kill_group(proc: subprocess.Popen, *, group: bool) -> None:
        """SIGKILL the process group, falling back to the process itself."""
        if group:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                return
            except OSError:
                logger.debug("could not kill the sparkrun process group", exc_info=True)
        try:
            proc.kill()
        except OSError:  # pragma: no cover - already gone
            pass

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
