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

from control_plane.procmatch import matches_deployment

from . import states

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
) -> list[dict]:
    """Annotate each process dict with the deployment it belongs to, if any.

    ``handles`` is ``DeploymentManager.handles()`` or None. None means the
    deployment port in use does not expose launch handles (the stubs do not),
    and every process is then reported unattributed -- degrade, never refuse.
    """
    handles = handles or {}
    here = [d for d in deployments if node_id in (getattr(d.plan, "node_ids", None) or [])]

    out: list[dict] = []
    for proc in processes:
        command = proc.get("command") or proc.get("name") or ""
        matched = None
        for dep in here:
            handle = handles.get(dep.deployment_id) or {}
            if matches_deployment(
                command, handle.get("cluster_id"), handle.get("port")
            ):
                matched = dep
                break

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


def find(processes: list[dict], pid: int) -> dict | None:
    return next((p for p in processes if int(p.get("pid", -1)) == pid), None)
