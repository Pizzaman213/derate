"""Which docker container, if any, owns a GPU process.

THIS agent's container runs with ``--pid=host`` (``procs.py`` documents why:
it is what makes GB10's per-process memory accounting work at all), so it
reads the host's PID namespace and ``nvidia-smi`` hands back host PIDs.
The launched model container does NOT share that namespace -- verified by
``docker inspect`` on a live sparkrun container, 2026-09-11: ``PidMode`` is
empty, and ``CAP_SYS_PTRACE`` is what it actually adds. It does not need to.
Every process in it is still visible from the host under a host PID, and that
is what makes this answerable without a second, container-aware probe:
``/proc/<pid>/cgroup`` names the container a host PID belongs to, cheaply and
without a docker call per process.

What it cannot say is the container's *name* -- cgroup paths carry docker's
internal id, not the name sparkrun gave it (``sparkrun_<hex>_<role>``, the
cluster id embedded in a name, not looked up any other way). That takes one
``docker ps`` call, shared across every process this reads in a round rather
than one call each.

Nor is the GPU-holding PID itself necessarily the process that was told the
launch flags. Confirmed live on this exact box: vLLM's own compute context is
held by a forked ``VLLM::EngineCore`` subprocess, which renames its argv on
the way up (``setproctitle``) and so reports a cmdline of literally
``VLLM::EngineCore`` -- the ``--port``/``--served-model-name``/etc. flags
live one level up, on the ``vllm serve`` process that forked it. This reads
every ancestor back to the container's own PID 1, bounded by the cgroup
itself: the walk stops the instant an ancestor's cgroup no longer matches
(the containerd shim that supervises the container from outside sits in its
own cgroup, one level above PID 1), so it can never wander into an unrelated
host process's command line.

Best-effort throughout, matching ``telemetry.py``'s nvidia-smi reads: a node
without a working docker socket answers None rather than raising, and a
process that is not in any container (there is no reason for one to be, but
nothing here assumes otherwise) is simply absent from the result rather than
reported with a null container -- there is nothing to adopt about it.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from .procs import _ppid
from .telemetry import read_gpu_processes

log = logging.getLogger(__name__)

#: A `docker ps` on an idle host answers in milliseconds; this bounds the
#: rare case of a wedged daemon rather than the common one.
DOCKER_TIMEOUT_S = 3.0

#: cgroup v2 unified hierarchy writes `.../docker-<id>.scope`; cgroup v1's
#: per-controller lines write `/docker/<id>`. Both name the same id, so one
#: pattern with an either/or separator reads both without knowing which this
#: host runs.
_CGROUP_ID_RE = re.compile(r"docker[-/]([0-9a-f]{64})")

#: /proc/<pid>/cmdline, NUL-joined and truncated. Same bound as
#: telemetry.py's own CMDLINE_MAX, kept local rather than imported: this
#: module reads a whole ancestor chain per process, telemetry.py reads one
#: PID nvidia-smi already named, and the two have no reason to change together.
_CMDLINE_MAX = 2000

#: How far up the ancestor chain to look before giving up on a container
#: whose process tree does not end in the cgroup boundary within a sane
#: depth. The real chain observed live is five hops (EngineCore -> vllm
#: serve -> sparkrun's wrapper script -> its env-export shell -> the
#: entrypoint's sleep-infinity shell); this is generous headroom above that,
#: not a tuned minimum.
_MAX_ANCESTOR_HOPS = 16


def _cgroup_container_id(pid: int) -> str | None:
    try:
        text = Path("/proc/%d/cgroup" % pid).read_text()
    except (OSError, ValueError):
        return None
    match = _CGROUP_ID_RE.search(text)
    return match.group(1) if match else None


def _cmdline(pid: int) -> str | None:
    try:
        raw = Path("/proc/%d/cmdline" % pid).read_bytes()
    except (OSError, ValueError):
        return None
    text = raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    return text[:_CMDLINE_MAX] if text else None


def _ancestor_commands(pid: int, container_id: str) -> list[str]:
    """*pid*'s own cmdline, then every ancestor's, while it is still inside
    *container_id*'s cgroup. First entry is *pid* itself."""
    out: list[str] = []
    seen: set[int] = set()
    current: int | None = pid
    while (
        current is not None
        and current > 0
        and current not in seen
        and len(seen) < _MAX_ANCESTOR_HOPS
    ):
        seen.add(current)
        if _cgroup_container_id(current) != container_id:
            break
        command = _cmdline(current)
        if command:
            out.append(command)
        current = _ppid(current)
    return out


async def _docker_ps_names() -> dict[str, str] | None:
    """container_id -> name, for every running container. None on any failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "ps",
            "--no-trunc",
            "--format",
            "{{.ID}}\t{{.Names}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError):
        return None
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=DOCKER_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    if proc.returncode != 0:
        return None
    out: dict[str, str] = {}
    for line in stdout.decode("utf-8", "replace").splitlines():
        container_id, _, name = line.partition("\t")
        if container_id and name:
            out[container_id] = name
    return out


async def read_container_membership() -> list[dict] | None:
    """Every GPU-holding process that belongs to a docker container.

    None when nvidia-smi or docker could not be asked at all -- the same
    three-valued shape as :func:`read_gpu_processes`, and for the same
    reason: a node this could not check must not read as a node with
    nothing running.
    """
    processes = await read_gpu_processes()
    if processes is None:
        return None
    names = await _docker_ps_names()
    if names is None:
        return None
    out: list[dict] = []
    for proc in processes:
        container_id = _cgroup_container_id(proc.pid)
        if container_id is None:
            continue
        name = names.get(container_id)
        if name is None:
            continue
        out.append(
            {
                "pid": proc.pid,
                "container_id": container_id,
                "container_name": name,
                # Not `proc.command`: that is nvidia-smi's own PID, which for
                # a vLLM deployment is the renamed EngineCore, not the server
                # that was actually told the launch flags. See the module
                # docstring. The caller tries each in order.
                "ancestor_commands": _ancestor_commands(proc.pid, container_id),
            }
        )
    return out
