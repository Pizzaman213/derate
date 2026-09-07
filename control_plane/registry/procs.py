"""Ending a compute context that is holding GPU memory.

The node agent's only mutating capability, and the only channel in the system
that can touch a workload sparkrun did not launch. A leftover ``llama-server``,
a driver orphaned by a killed load run, a vLLM the coordinator lost track of
across a restart -- all of them hold the pool the fit gate plans against, and
none of them are addressable by ``sparkrun stop``.

Three rules keep this a narrow verb rather than a remote-exec hole:

1. **Only PIDs nvidia-smi currently reports as holding GPU memory.** The list is
   re-read at kill time, not taken from the caller. That bounds the blast radius
   to exactly "things occupying the GPU", and it is checked here rather than at
   the HTTP layer so no future route can skip it.
2. **Never PID 1, never ourselves, never an ancestor of ourselves.** Killing our
   own process tree would take the agent down with the workload and leave the
   node unreachable to say what happened.
3. **The caller must hold the cluster token.** Enforced in ``agent.py``, because
   that is where the request and its headers are.

Success means the memory came back, not that the signal was delivered. A
process can exit while the driver still holds its context, and reporting that
as a reclaim would hand the operator a number they would then plan against.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from dataclasses import dataclass
from pathlib import Path

from control_plane.contracts import GpuProcess

from .telemetry import read_gpu_processes

log = logging.getLogger(__name__)

# SIGTERM first. A vLLM or llama-server given ten seconds unmaps its weights and
# tears the CUDA context down cleanly; SIGKILL leaves that to the driver, which
# usually works and occasionally does not.
KILL_GRACE_S = 10.0
# After SIGKILL there is nothing gentler left to try, so this is only how long
# we are willing to keep the operator waiting before answering honestly.
KILL_FORCE_S = 5.0
POLL_INTERVAL_S = 0.5

PROTECTED_PIDS = frozenset({0, 1})


class KillRefused(Exception):
    """The kill was not attempted. ``code`` selects the HTTP status upstream."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class KillResult:
    pid: int
    name: str
    signal_sent: str  # "SIGTERM" | "SIGKILL"
    exited: bool
    gpu_memory_before: int
    gpu_memory_reclaimed: int
    detail: str

    def as_dict(self) -> dict:
        return {
            "pid": self.pid,
            "name": self.name,
            "signal_sent": self.signal_sent,
            "exited": self.exited,
            "gpu_memory_before": self.gpu_memory_before,
            "gpu_memory_reclaimed": self.gpu_memory_reclaimed,
            "detail": self.detail,
        }


def _ppid(pid: int) -> int | None:
    """Parent of *pid* from /proc/<pid>/stat, or None when unreadable.

    Field 4 of stat, and it must be found by scanning back from the last ``)``:
    field 2 is the executable name in parentheses and may itself contain spaces
    and parentheses, so splitting the line from the left is wrong for exactly
    the processes most likely to be interesting.
    """
    try:
        line = Path("/proc/%d/stat" % pid).read_text()
    except (OSError, ValueError):
        return None
    close = line.rfind(")")
    if close == -1:
        return None
    parts = line[close + 1 :].split()
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def _ancestry(pid: int, limit: int = 64) -> set[int]:
    """*pid* and every parent above it, bounded against a cyclic /proc read."""
    seen: set[int] = set()
    current: int | None = pid
    while current is not None and current > 0 and current not in seen:
        seen.add(current)
        if len(seen) >= limit:
            break
        current = _ppid(current)
    return seen


def _state(pid: int) -> str | None:
    """The single-letter state from /proc/<pid>/stat, or None if unreadable.

    Parsed from the right of the last ``)`` for the same reason as :func:`_ppid`:
    field 2 is ``(comm)`` and may contain spaces and parentheses of its own.
    """
    try:
        line = Path("/proc/%d/stat" % pid).read_text()
    except (OSError, ValueError):
        return None
    close = line.rfind(")")
    if close == -1:
        return None
    parts = line[close + 1 :].split()
    return parts[0] if parts else None


def _alive(pid: int) -> bool:
    """Is *pid* still running?

    A zombie is not. ``os.kill(pid, 0)`` succeeds against one -- the entry
    survives until the parent reaps it -- so signalling alone would report a
    process that has already exited and released its GPU context as still
    running, and we would escalate to SIGKILL against a corpse and then report
    the memory as never reclaimed. Whether the parent has got round to
    ``wait()`` is not a fact about the GPU.
    """
    if _state(pid) == "Z":
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Running, we simply may not signal it. Reporting it dead here would
        # end the wait loop early and claim a reclaim that never happened.
        return True
    except OSError:
        return False
    return True


def guard(pid: int) -> None:
    """Refuse the kills that would take the node down with the workload."""
    if pid in PROTECTED_PIDS:
        raise KillRefused(
            "protected_pid",
            "PID %d is the init process. Refusing." % pid,
        )
    if pid == os.getpid():
        raise KillRefused(
            "protected_pid",
            "PID %d is the node agent itself. Killing it would take the node "
            "offline without freeing the model." % pid,
        )
    if pid in _ancestry(os.getpid()):
        raise KillRefused(
            "protected_pid",
            "PID %d is an ancestor of the node agent. Killing it would take "
            "the agent down with it." % pid,
        )


async def kill_gpu_process(
    pid: int,
    *,
    grace_s: float = KILL_GRACE_S,
    force_s: float = KILL_FORCE_S,
) -> KillResult:
    """SIGTERM, wait, SIGKILL, and report whether the memory actually came back.

    Raises :class:`KillRefused` when the PID is not holding GPU memory or is
    protected, and never signals anything in that case.
    """
    processes = await read_gpu_processes()
    if processes is None:
        raise KillRefused(
            "gpu_unreadable",
            "nvidia-smi did not answer, so there is no way to confirm what "
            "PID %d is holding. Refusing to signal it." % pid,
        )
    target = next((p for p in processes if p.pid == pid), None)
    if target is None:
        raise KillRefused(
            "not_a_gpu_process",
            "PID %d is not holding GPU memory. Only compute contexts on this "
            "node can be killed from here." % pid,
        )
    guard(pid)

    before = target.gpu_memory
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return await _settle(target, "SIGTERM", before, exited=True)
    except PermissionError as exc:
        raise KillRefused(
            "kill_not_permitted",
            "The node agent may not signal PID %d (owned by %s). It is running "
            "as a user that cannot reach that process: %s"
            % (pid, target.user or "another user", exc),
        ) from exc

    released = await _await_release(pid, grace_s)
    if released:
        return await _settle(target, "SIGTERM", before, exited=True)

    log.warning("pid %d survived SIGTERM after %.0fs; escalating", pid, grace_s)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError as exc:
        raise KillRefused(
            "kill_not_permitted",
            "SIGTERM did not end PID %d and the node agent may not SIGKILL it: "
            "%s" % (pid, exc),
        ) from exc
    exited = await _await_release(pid, force_s)
    return await _settle(target, "SIGKILL", before, exited=exited)


async def _await_release(pid: int, budget_s: float) -> bool:
    """True once the process is gone *and* its context has left the GPU."""
    deadline = asyncio.get_running_loop().time() + budget_s
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(POLL_INTERVAL_S)
        if _alive(pid):
            continue
        processes = await read_gpu_processes()
        # None means we could not check. Keep waiting rather than declaring a
        # reclaim we have no evidence for.
        if processes is not None and all(p.pid != pid for p in processes):
            return True
    return False


async def _settle(
    target: GpuProcess, sent: str, before: int, *, exited: bool
) -> KillResult:
    processes = await read_gpu_processes()
    still = None
    if processes is not None:
        still = next((p for p in processes if p.pid == target.pid), None)
    reclaimed = before - (still.gpu_memory if still else 0)
    if still is None and processes is not None:
        detail = "%s ended %s (pid %d); %.1f GiB released." % (
            sent,
            target.name,
            target.pid,
            reclaimed / 1024**3,
        )
    elif processes is None:
        detail = (
            "%s was sent to %s (pid %d), but nvidia-smi stopped answering, so "
            "the release could not be confirmed." % (sent, target.name, target.pid)
        )
        reclaimed = 0
    else:
        detail = (
            "%s was sent to %s (pid %d) and it is still holding %.1f GiB. The "
            "driver has not released the context."
            % (sent, target.name, target.pid, still.gpu_memory / 1024**3)
        )
    return KillResult(
        pid=target.pid,
        name=target.name,
        signal_sent=sent,
        exited=exited and still is None,
        gpu_memory_before=before,
        gpu_memory_reclaimed=max(0, reclaimed),
        detail=detail,
    )
