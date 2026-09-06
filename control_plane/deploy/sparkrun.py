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
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from control_plane.contracts import (
    DEFAULT_GUARDRAIL,
    ModelShape,
    ParallelismPlan,
    RegistryPort,
)

from .flags import KNOBS_BY_NAME, SPARKRUN_BIN, RuntimeSpec, runtime_spec
from .recipes import RecipeSpec, materialize, synthesize

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
)

NOT_INSTALLED = (
    "sparkrun is not installed. The control plane launches every backend "
    "through sparkrun and will not launch vLLM or SGLang by hand.\n"
    "Install it with:  uv tool install sparkrun\n"
    "Then re-run this launch. If sparkrun is installed somewhere unusual, "
    "point SPARKPLANE_SPARKRUN_BIN at it."
)


class SparkrunNotInstalled(RuntimeError):
    pass


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
        recipe_dir: Path | str = "/data/recipes",
        binary: str = SPARKRUN_BIN,
        base_port: int = 8100,
        gpu_memory_utilization: float = DEFAULT_GUARDRAIL,
        launch_timeout_s: float = 1800.0,
        stop_timeout_s: float = 30.0,
        env: dict[str, str] | None = None,
    ) -> None:
        self.registry = registry
        self.recipe_dir = Path(recipe_dir)
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
    ) -> RecipeSpec:
        return synthesize(
            shape,
            plan,
            runtime,
            ctx,
            max_seqs,
            served_name or default_served_name(shape),
            port=port if port is not None else self.base_port,
            gpu_memory_utilization=self.gpu_memory_utilization,
            recipe_dir=self.recipe_dir,
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
    ) -> list[str]:
        """The exact argv that will run. Pure: no subprocess, no filesystem.

        Agent H shows this to the user before they commit, so it must be the
        real thing, including --no-follow. Deterministic for a given plan,
        shape, runtime, context, concurrency and port.
        """
        spec: RuntimeSpec = runtime_spec(runtime)
        name = served_name or default_served_name(shape)
        chosen_port = port if port is not None else self.base_port
        recipe = recipe or self.recipe_for(
            plan, shape, runtime, ctx, max_seqs, served_name=name, port=chosen_port
        )

        values: dict[str, Any] = {
            "hosts": self.hosts_for(plan.node_ids),
            "tensor_parallel": plan.tensor_parallel,
            "pipeline_parallel": plan.pipeline_parallel,
            "context_length": ctx,
            "served_name": name,
            "port": chosen_port,
            "gpu_memory_utilization": self.gpu_memory_utilization,
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
    ) -> LaunchResult:
        """Run the invocation and parse the handle back out.

        Raises LaunchError with the raw output preserved when sparkrun fails
        or prints something we cannot parse. Never returns a partial result:
        a launch we cannot address is a launch we cannot stop.
        """
        self.require()
        name = served_name or default_served_name(shape)
        chosen_port = port if port is not None else self.base_port
        recipe = self.recipe_for(
            plan, shape, runtime, ctx, max_seqs, served_name=name, port=chosen_port
        )
        materialize(recipe)
        argv = self.render_command(
            plan, shape, runtime, ctx, max_seqs,
            served_name=name, port=chosen_port, recipe=recipe,
        )
        hosts = self.hosts_for(plan.node_ids)

        logger.info("launching %s: %s", name, " ".join(argv))
        try:
            proc = self._run(argv, timeout=self.launch_timeout_s)
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

        Returns at least {"running": bool}. A sparkrun that cannot be reached
        reports not-running with the reason, rather than raising: reconcile
        must survive a sparkrun that is missing or broken.
        """
        if not self.available():
            return {"running": False, "cluster_id": cluster_id, "error": NOT_INSTALLED}
        argv = [self.binary, "cluster", "check-job", cluster_id, "--json"]
        if hosts:
            argv += KNOBS_BY_NAME["hosts"].emit(list(hosts))
        try:
            proc = self._run(argv, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"running": False, "cluster_id": cluster_id, "error": "check-job timed out"}
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
    ) -> bool:
        return bool(self.check_job(cluster_id, hosts=hosts, timeout=timeout).get("running"))

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
