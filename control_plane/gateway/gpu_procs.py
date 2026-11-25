"""Attributing a resident GPU process to the deployment that owns it.

A pure helper, like ``livefit`` and ``ui_detail``: no routes, no state. It
answers one question -- of the compute contexts a node agent just reported,
which ones did we launch?

This matters because the answer decides whether a Kill button appears. Killing
a backend the router is still dispatching to would leave the deployment record
claiming READY for two health polls while the process is gone, and the operator
who did it would have no way to connect the 502s to their own click. So a
process we can name a deployment for is not killable from here at all: the
deployment's own Stop is the correct verb, and it drains first.

Attribution is by evidence on the command line -- the deployment's sparkrun
cluster id, or the port the manager allocated for it -- because that is what we
actually have. We never recorded the backend's PID; sparkrun launches it on the
far side of an SSH hop and only ever hands back a cluster id. An unmatched
process is reported unattributed rather than guessed at, which is the safe
direction: the worst case is a Kill button on something we launched, guarded by
a confirm dialog, rather than a missing one on a stray holding the whole pool.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from control_plane.procmatch import matches_deployment

from . import states

log = logging.getLogger(__name__)

# Terminal deployments no longer own anything. A process still running under a
# STOPPED record is exactly the orphan this feature exists to clear, so it must
# come back killable. The complement is formed in ``states`` rather than here,
# so this module and the router cannot disagree about which states are over.
_LIVE_STATES = states.LIVE


def attribute(
    processes: list[dict],
    deployments: list,
    handles: dict | None,
    node_id: str,
    lineage: dict[int, list[str]] | None = None,
) -> list[dict]:
    """Annotate each process dict with the deployment it belongs to, if any.

    ``handles`` is ``DeploymentManager.handles()`` or None. None means the
    deployment port in use does not expose launch handles (the stubs do not),
    and every process is then reported unattributed -- degrade, never refuse.

    ``lineage`` maps a pid to more evidence than its own command line: its
    container's name and its ancestors' command lines (``/agent/containers``).
    vLLM's EngineCore renames its argv to ``VLLM::EngineCore`` and holds nearly
    all of a deployment's memory, so without it derate's own engine reads as a
    stray -- with a Kill button.
    """
    handles = handles or {}
    # Live records first: sparkrun's cluster id hashes model, hosts and port, and a
    # terminal (stopped or failed) record's port is reused, so a relaunch shares its old record's handle.
    here = sorted(_placed_here(deployments, node_id), key=lambda d: d.state not in _LIVE_STATES)
    lineage = lineage or {}

    out: list[dict] = []
    for proc in processes:
        own = proc.get("command") or proc.get("name") or ""
        evidence = [own, *lineage.get(_pid(proc), [])]
        # A cluster id names one launch and is tried first, across every
        # deployment, over the process's own command line and its lineage. A
        # port is a number any flag can hold (`--max-model-len 8192`), and a
        # stray container may serve on that same port on its own network, so a
        # port is matched only on the process's own command line.
        matched = next((d for d in here if _hit(evidence, handles, d, False)), None) or next(
            (d for d in here if _hit([own], handles, d, True)), None
        )
        row = dict(proc)
        if matched is None:
            row.update(
                deployment_id=None,
                served_name=None,
                deployment_state=None,
                killable=True,
                not_killable_reason=None,
            )
        else:
            live = matched.state in _LIVE_STATES
            row.update(
                deployment_id=matched.deployment_id,
                served_name=matched.served_name,
                deployment_state=matched.state.value,
                killable=not live,
                not_killable_reason=(
                    "This process is serving %s (%s). Stop the deployment "
                    "instead -- it drains in-flight requests first."
                    % (matched.served_name, matched.deployment_id)
                    if live
                    else None
                ),
            )
        out.append(row)
    return out


def _hit(evidence: list[str], handles: dict, dep, by_port: bool) -> bool:
    handle = handles.get(dep.deployment_id) or {}
    cid, port = (None, handle.get("port")) if by_port else (handle.get("cluster_id"), None)
    return any(matches_deployment(command, cid, port) for command in evidence)


def _pid(proc: dict) -> int:
    """A row's pid, or -1 for a row the agent sent without a usable one."""
    try:
        return int(proc.get("pid", -1))
    except (TypeError, ValueError):
        return -1


def _placed_here(deployments: list, node_id: str) -> list:
    return [d for d in deployments if node_id in (getattr(d.plan, "node_ids", None) or [])]


async def agent_containers(agent_url: str, token: str, timeout: float) -> dict:
    """``GET /agent/containers`` on one node: its GPU processes' container
    names and ancestor command lines (``registry/containers.py``)."""
    import httpx  # lazily, as every other agent read in the gateway does

    async with httpx.AsyncClient(timeout=timeout) as client:
        res = await client.get(
            f"{agent_url.rstrip('/')}/agent/containers", headers={"X-Derate-Token": token}
        )
        res.raise_for_status()
        return res.json()


async def attribute_with_lineage(
    processes: list[dict],
    deployments: list,
    handles: dict | None,
    node_id: str,
    read_containers: Callable[[], Awaitable[dict]],
) -> list[dict]:
    """``attribute``, reading ``/agent/containers`` only when it can change the
    answer: some process is unattributed AND a deployment here has a launch
    cluster id -- live, or terminal, since an orphaned EngineCore of a stopped
    record is exactly what needs its lineage to be named. That read costs the
    node a second nvidia-smi and a ``docker ps``, so a machine derate never
    launched on never pays it. A failed read keeps the command-line answer --
    degrade, never refuse."""
    rows = attribute(processes, deployments, handles, node_id)
    placed = any(
        ((handles or {}).get(d.deployment_id) or {}).get("cluster_id")
        for d in _placed_here(deployments, node_id)
    )
    if not placed or all(r["deployment_id"] is not None for r in rows):
        return rows
    try:
        # Parsed inside the guard: a malformed payload is a failed read, not a 500.
        lineage = {
            int(c["pid"]): [str(c.get("container_name") or ""), *map(str, c.get("ancestor_commands") or [])]
            for c in (await read_containers()).get("containers") or []
            if c.get("pid") is not None
        }
    except Exception:
        log.debug("container lineage read failed for %s", node_id, exc_info=True)
        return rows
    return attribute(processes, deployments, handles, node_id, lineage)


def find(processes: list[dict], pid: int) -> dict | None:
    return next((p for p in processes if _pid(p) == pid), None)
