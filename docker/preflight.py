#!/usr/bin/env python3
"""Startup preflight for the derate node container.

One check matters here: are we on host networking?

mDNS is multicast and does not cross a Docker bridge, and the node agent has
to see the real interfaces to report the ConnectX-7 topology. A bridged
container comes up looking perfectly healthy and simply never discovers
anything, which is the worst possible failure: silent, and indistinguishable
from "there is no second node". So we detect it and refuse to start.

Detection, in order of confidence:

1. Not in a container at all -> nothing to check.
2. A real hardware interface is visible (has a /sys/class/net/<if>/device
   symlink) -> we are in the host's network namespace. Host networking.
3. docker0 / br-* / virbr* is visible -> only the host sees those bridges.
   Host networking.
4. Otherwise every non-loopback interface is a veth (ifindex != iflink),
   which is exactly what a bridged container gets. Bridge networking.

DERATE_ALLOW_BRIDGE=1 downgrades the refusal to a loud warning. It is
unsupported and discovery will not work; it exists so somebody debugging in
a constrained CI sandbox is not stuck.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

NET = Path("/sys/class/net")
HOST_BRIDGE_PREFIXES = ("docker", "br-", "virbr", "cni", "flannel")

BRIDGE_MESSAGE = """\
================================================================================
derate refuses to start: this container is on bridge networking.

    Host networking is required. mDNS is multicast and does not cross a
    Docker bridge, so this container would start cleanly, report itself
    healthy, and never discover another node. The node agent also needs to
    see the real interfaces to report the ConnectX-7 topology.

Start it again with --network host:

    docker run --network host -v derate:/data ghcr.io/pizzaman213/derate/node

With compose, the service needs:

    network_mode: host

Interfaces visible in this namespace: %s

To override anyway (unsupported, discovery will not work):
    DERATE_ALLOW_BRIDGE=1
================================================================================"""


def in_container() -> bool:
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return True
    if os.environ.get("container"):
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text()
    except OSError:
        return False
    return any(marker in cgroup for marker in ("docker", "containerd", "kubepods", "podman"))


def interfaces() -> list[str]:
    try:
        return sorted(p.name for p in NET.iterdir() if p.name != "lo")
    except OSError:
        return []


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def is_veth(name: str) -> bool:
    """A veth's iflink points at its peer in another namespace."""
    ifindex = _read_int(NET / name / "ifindex")
    iflink = _read_int(NET / name / "iflink")
    if ifindex is None or iflink is None:
        return False
    return ifindex != iflink


def on_host_network() -> tuple[bool, str]:
    """(host_networking, why)."""
    names = interfaces()
    if not names:
        return False, "no network interfaces are visible at all"

    hardware = [n for n in names if (NET / n / "device").exists()]
    if hardware:
        return True, "hardware interfaces visible: %s" % ", ".join(hardware)

    bridges = [n for n in names if n.startswith(HOST_BRIDGE_PREFIXES)]
    if bridges:
        return True, "host bridges visible: %s" % ", ".join(bridges)

    veths = [n for n in names if is_veth(n)]
    if veths and len(veths) == len(names):
        return False, "every interface is a veth pair: %s" % ", ".join(veths)

    # Unusual: no hardware, no host bridge, not all veth. Do not block on a
    # case we do not understand, but say so.
    return True, "could not classify %s; assuming host networking" % ", ".join(names)


def check(stream=sys.stderr) -> bool:
    """True if it is safe to start. Prints the reason when it is not."""
    if not in_container():
        return True

    host_net, why = on_host_network()
    if host_net:
        print("[derate] host networking confirmed (%s)" % why, file=stream)
        return True

    if os.environ.get("DERATE_ALLOW_BRIDGE") in ("1", "true", "yes"):
        print(
            "[derate] WARNING: bridge networking detected (%s). "
            "DERATE_ALLOW_BRIDGE is set, continuing anyway. mDNS discovery "
            "will not work and this node will not find or be found by any other."
            % why,
            file=stream,
        )
        return True

    print(BRIDGE_MESSAGE % (", ".join(interfaces()) or "none"), file=stream)
    return False


if __name__ == "__main__":
    sys.exit(0 if check() else 1)
