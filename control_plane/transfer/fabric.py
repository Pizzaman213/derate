"""Which links a node has, and which of them a copy should use.

Every physical interface with carrier is a candidate, whatever its vendor:
`/sys/class/net/<if>/device` separates a NIC from a bridge or a veth,
`.../speed` says how fast it is, and `.../device/infiniband/` says it can do
RDMA (RoCE or InfiniBand). Read from `/sys/class/net` because that is what a
host-network agent container sees.

Two kinds of address make a path. An IPv4 or global IPv6 address pairs with a
remote one in the same subnet. An IPv6 link-local address (`fe80::`) pairs
with any remote link-local one, because a direct cable is a link whether or
not anyone gave it a subnet -- which is what lets two Sparks copy over their
RoCE ports with no netplan at all. Link-local pairings cannot be judged from
the addresses, so `candidate_paths` lists them and the sender keeps the ones
that answer a connect.

The fastest tier wins (`fastest_tier`): with two 200 Gb/s rails and a 10 Gb/s
LAN, the rails carry the copy and the LAN carries nothing.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Sequence
from dataclasses import dataclass

from control_plane.links.runner import CommandRunner, SubprocessRunner

_INET_RE = re.compile(r"\binet\s+(\d{1,3}(?:\.\d{1,3}){3})/(\d{1,2})\b")
_INET6_RE = re.compile(r"\binet6\s+([0-9a-fA-F:]+)/(\d{1,3})\b")


@dataclass(frozen=True)
class LinkAddress:
    iface: str
    address: str  # IPv4, or IPv6 without a scope
    prefix: int
    speed_mbps: int | None = None
    rdma: bool = False

    @property
    def link_local(self) -> bool:
        return self.address.lower().startswith("fe80:")


@dataclass(frozen=True)
class LinkPath:
    """One way to reach the receiver. `dial` is what to connect to (with a
    `%iface` scope for link-local); `source` the local address to bind, or None."""

    local_iface: str
    remote_iface: str
    source: str | None
    dial: str
    speed_mbps: int | None
    rdma: bool

    @property
    def route(self) -> str:
        return f"{self.source or self.local_iface}->{self.dial}"


def link_addresses(runner: CommandRunner | None = None) -> tuple[LinkAddress, ...]:
    """Every address on a physical interface with carrier, sorted. Never raises."""
    runner = runner or SubprocessRunner()
    out: list[LinkAddress] = []
    try:
        devices = runner.glob("/sys/class/net/*/device")
    except Exception:  # noqa: BLE001 - discovery degrades to "no links"
        return ()
    for device in devices:
        try:
            iface = device.split("/")[4]
            if (runner.read_text(f"/sys/class/net/{iface}/carrier") or "").strip() != "1":
                continue
            speed = _speed(runner.read_text(f"/sys/class/net/{iface}/speed"))
            rdma = bool(runner.glob(f"/sys/class/net/{iface}/device/infiniband/*"))
            res = runner.run(["ip", "-o", "addr", "show", "dev", iface], timeout=10)
        except Exception:  # noqa: BLE001
            continue
        if not res.ok:
            continue
        for m in _INET_RE.finditer(res.stdout):
            out.append(LinkAddress(iface, m.group(1), int(m.group(2)), speed, rdma))
        for m in _INET6_RE.finditer(res.stdout):
            out.append(LinkAddress(iface, m.group(1).lower(), int(m.group(2)), speed, rdma))
    return tuple(sorted(out, key=lambda a: (a.iface, a.address)))


def _speed(text: str | None) -> int | None:
    try:
        value = int((text or "").strip())
    except ValueError:
        return None
    return value if value > 0 else None


def _slower(a: int | None, b: int | None) -> int | None:
    known = [s for s in (a, b) if s]
    return min(known) if known else None


def candidate_paths(local: Sequence[LinkAddress], remote: Sequence[LinkAddress]) -> list[LinkPath]:
    """Every pairing worth trying, at most one per (local iface, remote iface),
    fastest first. Subnet matches come before link-local guesses."""
    out: dict[tuple[str, str], LinkPath] = {}

    def add(mine: LinkAddress, theirs: LinkAddress, source: str | None, dial: str) -> None:
        out.setdefault((mine.iface, theirs.iface), LinkPath(
            mine.iface, theirs.iface, source, dial,
            _slower(mine.speed_mbps, theirs.speed_mbps), mine.rdma and theirs.rdma,
        ))

    routed = [a for a in local if not a.link_local]
    for theirs in remote:
        if theirs.link_local:
            continue
        try:
            addr = ipaddress.ip_address(theirs.address)
        except ValueError:
            continue
        for mine in routed:
            try:
                net = ipaddress.ip_interface(f"{mine.address}/{mine.prefix}").network
            except ValueError:
                continue
            # Every local interface in that subnet, not the first: two LAN
            # ports in one subnet are exactly the case speed has to decide.
            if addr.version == net.version and addr in net:
                add(mine, theirs, mine.address, theirs.address)

    # Link-local: any local one may reach any remote one. Try the diagonal
    # first (sorted by interface name, i-th with i-th) so two rails pair rail
    # with rail and each PCIe link carries half.
    local_ll = sorted((a for a in local if a.link_local), key=lambda a: a.iface)
    remote_ll = sorted((a for a in remote if a.link_local), key=lambda a: a.iface)
    for i, theirs in enumerate(remote_ll):
        for j in range(len(local_ll)):
            mine = local_ll[(i + j) % len(local_ll)]
            add(mine, theirs, None, f"{theirs.address}%{mine.iface}")

    return sorted(out.values(), key=lambda p: (-(p.speed_mbps or 0), not p.rdma))


def fastest_tier(reachable: Sequence[LinkPath]) -> list[LinkPath]:
    """The paths a copy should use: each local and each remote interface at
    most once, then only the fastest speed among them."""
    used_local: set[str] = set()
    used_remote: set[str] = set()
    chosen: list[LinkPath] = []
    for p in reachable:
        if p.local_iface in used_local or p.remote_iface in used_remote:
            continue
        used_local.add(p.local_iface)
        used_remote.add(p.remote_iface)
        chosen.append(p)
    if not chosen:
        return []
    top = max((p.speed_mbps or 0) for p in chosen)
    return [p for p in chosen if (p.speed_mbps or 0) == top]


def describe(paths: Sequence[LinkPath]) -> str:
    """'RoCE, 2 × 200 Gb/s' / 'enP7s7, 10 Gb/s' -- what a person reads."""
    if not paths:
        return "the management network"
    name = "RoCE" if all(p.rdma for p in paths) else ", ".join(sorted({p.local_iface for p in paths}))
    speed = paths[0].speed_mbps
    rate = f"{speed / 1000:g} Gb/s" if speed else "unknown speed"
    return f"{name}, {len(paths)} × {rate}" if len(paths) > 1 else f"{name}, {rate}"
