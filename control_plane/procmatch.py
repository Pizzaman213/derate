"""Deciding whether a running process belongs to a deployment.

Layer-free on purpose. Two callers need this predicate and they sit on opposite
sides of ``DeploymentPort``: the gateway annotates a node agent's process list
before drawing a Kill button, and the deployment manager picks which PIDs to
signal when ``sparkrun stop`` will not confirm. Neither may import the other, so
the rule lives here rather than being written twice and drifting.

We match on the command line because it is the only evidence we have. sparkrun
launches the backend across an SSH hop and hands back a cluster id, never a PID,
so there is nothing recorded to compare against directly.
"""

from __future__ import annotations

import re


def port_in(command: str, port: int) -> bool:
    """True when *command* uses *port* as a number rather than a substring.

    ``--port 81000`` is not a match for 8100, and neither is ``810``. Getting
    this wrong in the permissive direction attributes a stray to a deployment
    and hides the Kill button on the thing that is actually holding the pool.
    """
    if not command or not port:
        return False
    return re.search(r"(?<!\d)%d(?!\d)" % int(port), command) is not None


def matches_deployment(
    command: str, cluster_id: str | None, port: int | None
) -> bool:
    """Does *command* look like the backend of a deployment with this handle?"""
    if not command:
        return False
    if cluster_id and cluster_id in command:
        return True
    return bool(port) and port_in(command, int(port))
