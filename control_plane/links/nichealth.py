"""How each fabric port is configured, and what to change between two ends.

The link ladder answers "how fast is this pair". This answers "why is it not
faster": the settings on each RDMA-capable port that decide what a copy or a
collective can get out of the wire. Two halves:

* `read_nic_health` runs on a node agent (`GET /agent/links/health`). For every
  physical interface with an RDMA device (`/sys/class/net/<if>/device/
  infiniband/*`) it reads carrier, speed and MTU from sysfs, ring sizes
  (`ethtool -g`), the `rx_cqe_compress` private flag (`ethtool
  --show-priv-flags`), four receive counters (`ethtool -S`) and the IPv4
  addresses (`ip -o -4 addr`). A missing `ethtool` leaves those fields None
  with a reason; nothing here raises.

* `compare_health` runs on the coordinator over the two payloads of a pair and
  returns findings, each with the exact command or netplan stanza that fixes
  it. Only ports with carrier are judged: an unplugged cage has no peer.

Rails are paired across the two ends by a shared IPv4 subnet first, then by
interface name (both Sparks name their ports identically), then in sorted
order. The last rule is an assumption about symmetric cabling, which is why the
first two are tried before it.

The drop counters are cumulative since the driver loaded. A nonzero value says
drops happened at some point, not that they are happening now, and every
sentence that names one says so.

Settings say nothing about whether a rail carries traffic, so the coordinator
also has one end of each pair dial the other's copy receiver from every rail
(`probe_plan` / `probe_rails`, below under "reach"); a rail with a link that
reached nothing is a warning of its own.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field

from control_plane.transfer.protocol import TRANSFER_PORT

from .runner import CommandRunner, SubprocessRunner

# The receive counters `ethtool -S` reports on mlx5 that mean "a packet that
# arrived was lost". `link_down_events_phy` is read too, but it counts the link
# going down -- at least once at every boot -- so it is reported, never judged.
DROP_COUNTERS = ("rx_out_of_buffer", "rx_discards_phy", "rx_crc_errors_phy")
COUNTERS = (*DROP_COUNTERS, "link_down_events_phy")

# Where a node's ethtool lives on the HOST, for the udev line that makes a ring
# or flag survive a reboot. Debian and Ubuntu both install it here.
_HOST_ETHTOOL = "/usr/sbin/ethtool"
_UDEV_FILE = "/etc/udev/rules.d/70-derate-fabric.rules"

# What a fabric port should carry when neither end has any IPv4 on it. The same
# plan the two Sparks here use: one /24 per rail, .1 and .2 at the ends.
_FALLBACK_RAIL_NET = "10.100.{rail}.0/24"
_FALLBACK_FIRST_RAIL = 152

_INET_RE = re.compile(r"\binet\s+(\d{1,3}(?:\.\d{1,3}){3}/\d{1,2})\b")
_INET6_LL_RE = re.compile(r"\binet6\s+(fe80:[0-9a-fA-F:]*)/\d{1,3}\b")
_RING_RE = re.compile(r"^\s*(RX|TX):\s*(\S+)\s*$")
_FLAG_RE = re.compile(r"^\s*rx_cqe_compress\s*:\s*(on|off)\s*$", re.MULTILINE)
_COUNTER_RE = re.compile(r"^\s*([A-Za-z0-9_]+):\s*(\d+)\s*$", re.MULTILINE)

_NO_ETHTOOL = (
    "ethtool is not installed on this node, so ring sizes, the rx_cqe_compress "
    "flag and the drop counters cannot be read."
)


@dataclass(frozen=True)
class RailHealth:
    iface: str
    rdma_device: str | None
    carrier: bool
    speed_mbps: int | None
    mtu: int | None
    ring_rx: int | None = None
    ring_tx: int | None = None
    ring_rx_max: int | None = None
    ring_tx_max: int | None = None
    rx_cqe_compress: bool | None = None
    # Only counters the driver reported; an absent name was not read.
    counters: dict[str, int] = field(default_factory=dict)
    # "A.B.C.D/P", in the order `ip` printed them.
    ipv4: tuple[str, ...] = ()
    # One sentence per group of fields left None, saying why.
    unread: tuple[str, ...] = ()
    # IPv6 link-local addresses ("fe80::..."), no prefix and no scope: the
    # scope is the PROBING end's interface, never this one's.
    ipv6_link_local: tuple[str, ...] = ()

    def to_json(self) -> dict:
        d = asdict(self)
        d["ipv4"] = list(self.ipv4)
        d["unread"] = list(self.unread)
        d["ipv6_link_local"] = list(self.ipv6_link_local)
        return d

    @classmethod
    def from_json(cls, d: Mapping) -> RailHealth:
        def _int(v: object) -> int | None:
            return v if isinstance(v, int) and not isinstance(v, bool) else None

        counters = d.get("counters") or {}
        return cls(
            iface=str(d.get("iface") or ""),
            rdma_device=d.get("rdma_device") if isinstance(d.get("rdma_device"), str) else None,
            carrier=d.get("carrier") is True,
            speed_mbps=_int(d.get("speed_mbps")),
            mtu=_int(d.get("mtu")),
            ring_rx=_int(d.get("ring_rx")),
            ring_tx=_int(d.get("ring_tx")),
            ring_rx_max=_int(d.get("ring_rx_max")),
            ring_tx_max=_int(d.get("ring_tx_max")),
            rx_cqe_compress=d.get("rx_cqe_compress") if isinstance(d.get("rx_cqe_compress"), bool) else None,
            counters={k: v for k, v in counters.items() if isinstance(k, str) and _int(v) is not None}
            if isinstance(counters, Mapping)
            else {},
            ipv4=tuple(a for a in (d.get("ipv4") or ()) if isinstance(a, str)),
            unread=tuple(s for s in (d.get("unread") or ()) if isinstance(s, str)),
            ipv6_link_local=tuple(
                a for a in (d.get("ipv6_link_local") or ()) if isinstance(a, str)
            ),
        )


# --------------------------------------------------------------------- reader


def read_nic_health(runner: CommandRunner | None = None) -> dict:
    """This node's RDMA-capable ports, as the agent route returns them.

    `{"interfaces": [RailHealth.to_json()...], "ethtool": bool}`. Never raises.
    """
    runner = runner or SubprocessRunner()
    have_ethtool = runner.which("ethtool") is not None
    rails = [_read_rail(runner, iface, dev, have_ethtool) for iface, dev in _rdma_ifaces(runner)]
    return {"interfaces": [r.to_json() for r in rails], "ethtool": have_ethtool}


def _rdma_ifaces(runner: CommandRunner) -> list[tuple[str, str]]:
    found: dict[str, str] = {}
    for path in runner.glob("/sys/class/net/*/device/infiniband/*"):
        parts = path.split("/")
        if len(parts) < 7:
            continue
        found.setdefault(parts[4], parts[-1])
    return sorted(found.items())


def _read_rail(runner: CommandRunner, iface: str, dev: str, have_ethtool: bool) -> RailHealth:
    base = f"/sys/class/net/{iface}"
    carrier = _strip(runner.read_text(f"{base}/carrier")) == "1"
    speed = _as_int(runner.read_text(f"{base}/speed"))
    mtu = _as_int(runner.read_text(f"{base}/mtu"))
    unread: list[str] = []

    ipv4: tuple[str, ...] = ()
    res = runner.run(["ip", "-o", "-4", "addr", "show", "dev", iface], timeout=10)
    if res.ok:
        ipv4 = tuple(_INET_RE.findall(res.stdout))
    else:
        unread.append(f"`ip -4 addr` failed on {iface}, so its IPv4 addresses are unknown.")
    ipv6_ll: tuple[str, ...] = ()
    res = runner.run(["ip", "-o", "-6", "addr", "show", "dev", iface, "scope", "link"], timeout=10)
    if res.ok:
        ipv6_ll = tuple(_INET6_LL_RE.findall(res.stdout))
    else:
        unread.append(
            f"`ip -6 addr` failed on {iface}, so its link-local address is unknown "
            "and the rail is probed over IPv4 only."
        )

    ring: dict[str, int | None] = {}
    flag: bool | None = None
    counters: dict[str, int] = {}
    if not have_ethtool:
        unread.append(_NO_ETHTOOL)
    else:
        res = runner.run(["ethtool", "-g", iface], timeout=10)
        if res.ok:
            ring = parse_rings(res.stdout)
        else:
            unread.append(f"`ethtool -g {iface}` failed, so its ring sizes are unknown.")
        res = runner.run(["ethtool", "--show-priv-flags", iface], timeout=10)
        if res.ok:
            flag = parse_cqe_compress(res.stdout)
        if flag is None:
            unread.append(f"`ethtool --show-priv-flags {iface}` reported no rx_cqe_compress flag.")
        res = runner.run(["ethtool", "-S", iface], timeout=10)
        if res.ok:
            counters = parse_counters(res.stdout)
        else:
            unread.append(f"`ethtool -S {iface}` failed, so its drop counters are unknown.")

    return RailHealth(
        iface=iface,
        rdma_device=dev,
        carrier=carrier,
        # sysfs says -1 for a port with no link.
        speed_mbps=speed if speed is not None and speed > 0 else None,
        mtu=mtu,
        ring_rx=ring.get("rx"),
        ring_tx=ring.get("tx"),
        ring_rx_max=ring.get("rx_max"),
        ring_tx_max=ring.get("tx_max"),
        rx_cqe_compress=flag,
        counters=counters,
        ipv4=ipv4,
        unread=tuple(unread),
        ipv6_link_local=ipv6_ll,
    )


def parse_rings(text: str) -> dict[str, int | None]:
    """`ethtool -g`: the RX/TX lines under "Pre-set maximums" and "Current"."""
    out: dict[str, int | None] = {}
    section = None
    for line in text.splitlines():
        low = line.strip().lower()
        if low.startswith("pre-set maximums"):
            section = "_max"
            continue
        if low.startswith("current hardware settings"):
            section = ""
            continue
        m = _RING_RE.match(line)
        if m and section is not None:
            out[f"{m.group(1).lower()}{section}"] = _as_int(m.group(2))
    return out


def parse_cqe_compress(text: str) -> bool | None:
    m = _FLAG_RE.search(text)
    return None if m is None else m.group(1) == "on"


def parse_counters(text: str) -> dict[str, int]:
    return {k: int(v) for k, v in _COUNTER_RE.findall(text) if k in COUNTERS}


def _strip(text: str | None) -> str | None:
    return text.strip() if text is not None else None


def _as_int(text: str | None) -> int | None:
    try:
        return int((text or "").strip())
    except ValueError:
        return None


# ------------------------------------------------------------------- findings


@dataclass(frozen=True)
class Finding:
    severity: str  # "warning" | "info"
    node_id: str
    iface: str
    sentence: str
    fix: str

    def to_json(self) -> dict:
        return asdict(self)


def compare_health(a_id: str, a: Sequence[RailHealth], b_id: str, b: Sequence[RailHealth]) -> list[Finding]:
    """Everything worth changing on the fabric between *a_id* and *b_id*.

    Warnings first (MTU, missing IPv4 subnet, drops), then the two settings
    that cost throughput without losing anything (rings, CQE compression).
    """
    a_up = [r for r in a if r.carrier]
    b_up = [r for r in b if r.carrier]
    if not a_up or not b_up:
        return []
    pairs = pair_rails(a_up, b_up)
    out: list[Finding] = []
    out += _mtu_findings(a_id, b_id, pairs)
    out += _subnet_findings(a_id, b_id, pairs)
    for node_id, rails in ((a_id, a_up), (b_id, b_up)):
        for r in rails:
            out += _drop_findings(node_id, r)
    for node_id, rails in ((a_id, a_up), (b_id, b_up)):
        for r in rails:
            out += _ring_findings(node_id, r)
            out += _cqe_findings(node_id, r)
    return out


def pair_rails(a: Sequence[RailHealth], b: Sequence[RailHealth]) -> list[tuple[RailHealth, RailHealth]]:
    """Which port on *a* is cabled to which on *b*, as well as can be told."""
    pairs: list[tuple[RailHealth, RailHealth]] = []
    left = sorted(a, key=lambda r: r.iface)
    right = sorted(b, key=lambda r: r.iface)
    for ra in list(left):
        rb = next((x for x in right if _shared_subnet(ra, x)), None)
        if rb is not None:
            pairs.append((ra, rb))
            left.remove(ra)
            right.remove(rb)
    for ra in list(left):
        rb = next((x for x in right if x.iface == ra.iface), None)
        if rb is not None:
            pairs.append((ra, rb))
            left.remove(ra)
            right.remove(rb)
    pairs.extend(zip(left, right))
    return pairs


def _networks(r: RailHealth) -> list[ipaddress.IPv4Interface]:
    out = []
    for a in r.ipv4:
        try:
            out.append(ipaddress.IPv4Interface(a))
        except ValueError:
            continue
    return out


def _shared_subnet(x: RailHealth, y: RailHealth) -> bool:
    return any(
        ix.network == iy.network and ix.ip != iy.ip for ix in _networks(x) for iy in _networks(y)
    )


def _routable(r: RailHealth) -> list[ipaddress.IPv4Interface]:
    """Addresses an operator assigned: not 169.254/16, which NetworkManager
    hands out on its own and which no peer is configured to expect."""
    return [i for i in _networks(r) if not i.ip.is_link_local]


def _netplan(iface_lines: dict[str, list[str]], ifaces_hint: Sequence[str]) -> str:
    pattern = "|".join(ifaces_hint)
    lines = [
        f"# In the netplan file that already defines these ports (sudo grep -lE '{pattern}' "
        "/etc/netplan/*.yaml) -- never a second file, netplan merges addresses across files.",
        "# Then: sudo netplan try --timeout 120",
        "network:",
        "  ethernets:",
    ]
    for iface, body in iface_lines.items():
        lines.append(f"    {iface}:")
        lines.extend(f"      {b}" for b in body)
    return "\n".join(lines)


def _mtu_findings(a_id: str, b_id: str, pairs) -> list[Finding]:
    out = []
    for ra, rb in pairs:
        if ra.mtu is None or rb.mtu is None or ra.mtu == rb.mtu:
            continue
        (low_id, low), (high_id, high) = sorted(((a_id, ra), (b_id, rb)), key=lambda t: t[1].mtu)
        out.append(
            Finding(
                severity="warning",
                node_id=low_id,
                iface=low.iface,
                sentence=(
                    f"{low.iface} on {low_id} has MTU {low.mtu} and {high.iface} on {high_id} "
                    f"has {high.mtu}. Every frame across this rail is held to the smaller "
                    f"size, so raise {low_id}'s to {high.mtu}."
                ),
                fix=_netplan({low.iface: [f"mtu: {high.mtu}"]}, [low.iface]),
            )
        )
    return out


def _host_in(net: ipaddress.IPv4Network, taken: set) -> ipaddress.IPv4Address | None:
    for host in net.hosts():
        if host not in taken:
            return host
    return None


def _subnet_findings(a_id: str, b_id: str, pairs) -> list[Finding]:
    unshared = [(ra, rb) for ra, rb in pairs if not _shared_subnet(ra, rb)]
    if not unshared:
        return []
    shared = len(pairs) - len(unshared)
    # node_id -> {iface: address/prefix to give it}
    changes: dict[str, dict[str, str]] = {}
    for i, (ra, rb) in enumerate(unshared):
        a_nets, b_nets = _routable(ra), _routable(rb)
        if b_nets:
            target, iface, peer = a_id, ra.iface, b_nets[0]
        elif a_nets:
            target, iface, peer = b_id, rb.iface, a_nets[0]
        else:
            net = ipaddress.IPv4Network(_FALLBACK_RAIL_NET.format(rail=_FALLBACK_FIRST_RAIL + i))
            hosts = net.hosts()
            changes.setdefault(a_id, {})[ra.iface] = f"{next(hosts)}/{net.prefixlen}"
            changes.setdefault(b_id, {})[rb.iface] = f"{next(hosts)}/{net.prefixlen}"
            continue
        host = _host_in(peer.network, {peer.ip})
        if host is not None:
            changes.setdefault(target, {})[iface] = f"{host}/{peer.network.prefixlen}"
    out = []
    for node_id, ifaces in changes.items():
        names = sorted(ifaces)
        listed = " and ".join(names)
        if shared == 0:
            sentence = (
                f"No IPv4 subnet is shared between {a_id} and {b_id} on their fabric ports. "
                "Copies still work over IPv6 link-local, but NCCL's bootstrap and sparkrun's "
                f"fabric detection need IPv4 there, so they fall back to the management network. "
                f"Give {listed} on {node_id} an address in its peer's subnet."
            )
        else:
            sentence = (
                f"{listed} on {node_id} shares no IPv4 subnet with its peer, so only {shared} of "
                f"{len(pairs)} rails carry NCCL and sparkrun traffic. Copies still use it over "
                "IPv6 link-local."
            )
        out.append(
            Finding(
                severity="warning",
                node_id=node_id,
                iface=",".join(names),
                sentence=sentence,
                fix=_netplan({n: [f"addresses: [{ifaces[n]}]"] for n in names}, names),
            )
        )
    return out


def _drop_findings(node_id: str, r: RailHealth) -> list[Finding]:
    out = []
    for name in DROP_COUNTERS:
        value = r.counters.get(name)
        if not value:
            continue
        out.append(
            Finding(
                severity="warning",
                node_id=node_id,
                iface=r.iface,
                sentence=(
                    f"{name} on {r.iface} on {node_id} is {value:,}. The counter runs since "
                    "boot, so packets were dropped at some point, not necessarily now; "
                    "watch it during a copy to see whether it still climbs."
                ),
                fix=f"watch -d -n1 \"ethtool -S {r.iface} | grep -E '{name}'\"",
            )
        )
    return out


def _udev(iface: str, args: str) -> str:
    return (
        f"# To keep it across reboots, one line in {_UDEV_FILE}:\n"
        f'ACTION=="add", SUBSYSTEM=="net", KERNEL=="{iface}", RUN+="{_HOST_ETHTOOL} {args}"'
    )


def _ring_findings(node_id: str, r: RailHealth) -> list[Finding]:
    cur = (r.ring_rx, r.ring_tx)
    top = (r.ring_rx_max, r.ring_tx_max)
    if None in cur or None in top or (cur[0] >= top[0] and cur[1] >= top[1]):
        return []
    args = f"-G {r.iface} rx {top[0]} tx {top[1]}"
    return [
        Finding(
            severity="info",
            node_id=node_id,
            iface=r.iface,
            sentence=(
                f"{r.iface} on {node_id} has rings of {cur[0]} receive and {cur[1]} transmit "
                f"descriptors of a possible {top[0]} and {top[1]}. A burst longer than the "
                "ring is dropped and counted as rx_out_of_buffer."
            ),
            fix=f"sudo ethtool {args}\n{_udev(r.iface, args)}",
        )
    ]


def _cqe_findings(node_id: str, r: RailHealth) -> list[Finding]:
    if r.rx_cqe_compress is not False:
        return []
    args = f"--set-priv-flags {r.iface} rx_cqe_compress on"
    return [
        Finding(
            severity="info",
            node_id=node_id,
            iface=r.iface,
            sentence=(
                f"rx_cqe_compress is off on {r.iface} on {node_id}. With it on, the NIC "
                "reports several received packets per completion write, which saves PCIe "
                "bandwidth at high packet rates."
            ),
            fix=f"sudo ethtool {args}\n{_udev(r.iface, args)}",
        )
    ]


# ---------------------------------------------------------------------- reach
#
# A port can say "carrier, 200 Gb/s, MTU 9000" and still carry nothing: the
# exo "ghost node" failure, where every link reads up and every ping fails. So
# one end of each pair opens a TCP connection to the other end's copy receiver
# (control_plane/transfer, port 47101) from each of its rails, toward each of
# the far end's rail addresses: IPv4 only where the two share a subnet (that is
# what makes the kernel send it out of THAT rail -- the source address is bound
# to the rail's own), and IPv6 link-local with the probing rail as the scope,
# which pins the egress interface without any address plan at all.
#
# A refused connection is not a failure of the RAIL: the far kernel's RST came
# back over it. It says nothing listens on 47101 (an agent from before the
# receiver, or one that could not open the port), which is reported once, as
# info. Only a rail where no target answered at all is a warning.

RECEIVER_PORT = TRANSFER_PORT
PROBE_TIMEOUT_S = 1.5
MAX_PROBE_TARGETS = 16
# The only IPv4 a probe will dial: RFC 1918 and 169.254/16. A fabric rail is
# never on a public address, and refusing one keeps the agent route from being
# a scanner pointed anywhere the caller likes.
_PROBE_NETS_V4 = tuple(
    ipaddress.IPv4Network(n)
    for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16")
)


class ProbeRefused(ValueError):
    """A probe target this node will not dial; the message is one sentence."""


@dataclass(frozen=True)
class ProbeTarget:
    iface: str  # the PROBING node's rail
    address: str  # "10.100.152.1", or "fe80::1%enp1s0f0np0" scoped to *iface*
    port: int = RECEIVER_PORT

    def to_json(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ProbeResult:
    iface: str
    target: str
    ok: bool  # connected: the receiver is listening there
    reached: bool  # the far host answered at all: connected, or refused
    ms: float | None  # connect (or refusal) round trip; None when unanswered
    error: str | None

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: Mapping) -> ProbeResult:
        ms = d.get("ms")
        return cls(
            iface=str(d.get("iface") or ""),
            target=str(d.get("target") or ""),
            ok=d.get("ok") is True,
            reached=d.get("reached") is True,
            ms=float(ms) if isinstance(ms, (int, float)) and not isinstance(ms, bool) else None,
            error=d.get("error") if isinstance(d.get("error"), str) else None,
        )


def probe_plan(local: Sequence[RailHealth], remote: Sequence[RailHealth]) -> list[ProbeTarget]:
    """Every (local rail, remote address) pair worth one connect, capped.

    IPv4 only where the local rail holds an address in the remote address's
    subnet; IPv6 link-local from every local rail that has one to every remote
    link-local address. Only ports with carrier, on both ends.
    """
    out: list[ProbeTarget] = []
    remote_up = [r for r in remote if r.carrier]
    for ra in sorted((r for r in local if r.carrier), key=lambda r: r.iface):
        nets = _networks(ra)
        for rb in remote_up:
            for ib in _networks(rb):
                if any(ib.ip in ia.network and ib.ip != ia.ip for ia in nets):
                    out.append(ProbeTarget(ra.iface, str(ib.ip)))
            if ra.ipv6_link_local:
                out.extend(ProbeTarget(ra.iface, f"{ll}%{ra.iface}") for ll in rb.ipv6_link_local)
    unique = list(dict.fromkeys(out))
    return unique[:MAX_PROBE_TARGETS]


def validate_probe_targets(raw: object, local_ifaces: set[str]) -> list[ProbeTarget]:
    """The agent route's body, checked. Raises ProbeRefused with one sentence.

    Bounded on every axis: at most MAX_PROBE_TARGETS, only this node's own
    RDMA ports as the source, only the copy receiver's port, and only private
    or link-local addresses -- never a host on the internet.
    """
    if not isinstance(raw, list) or not raw:
        raise ProbeRefused('Body must be {"targets": [{"iface", "address", "port"}, ...]}.')
    if len(raw) > MAX_PROBE_TARGETS:
        raise ProbeRefused(f"At most {MAX_PROBE_TARGETS} targets per probe; got {len(raw)}.")
    out: list[ProbeTarget] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ProbeRefused("Each target must be an object with iface, address and port.")
        iface, address, port = item.get("iface"), item.get("address"), item.get("port")
        if not isinstance(iface, str) or iface not in local_ifaces:
            raise ProbeRefused(f"{iface!r} is not an RDMA port on this node.")
        if port != RECEIVER_PORT or isinstance(port, bool):
            raise ProbeRefused(f"The probe only dials the copy receiver's port, {RECEIVER_PORT}.")
        if not isinstance(address, str):
            raise ProbeRefused("Each target needs an address.")
        host, _, scope = address.partition("%")
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            raise ProbeRefused(f"{address!r} is not an IP address.") from None
        if ip.version == 4:
            if scope or not any(ip in n for n in _PROBE_NETS_V4):
                raise ProbeRefused(
                    f"{address} is not a private or link-local address; the probe only "
                    "dials fabric addresses."
                )
        elif not ip.is_link_local or scope != iface:
            raise ProbeRefused(
                f"{address} is not an IPv6 link-local address scoped to {iface}; the probe "
                "only dials fabric addresses."
            )
        out.append(ProbeTarget(iface, address, RECEIVER_PORT))
    return out


def probe_rails(
    targets: Sequence[ProbeTarget],
    local: Sequence[RailHealth],
    timeout: float = PROBE_TIMEOUT_S,
) -> list[ProbeResult]:
    """One TCP connect per target, all at once, each at most *timeout*.

    The connection is closed as soon as it opens: the receiver reads EOF
    before its handshake and drops it. Never raises.
    """
    if not targets:
        return []
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        return list(pool.map(lambda t: _probe_one(t, local, timeout), targets))


def _probe_one(t: ProbeTarget, local: Sequence[RailHealth], timeout: float) -> ProbeResult:
    host, _, scope = t.address.partition("%")
    source: tuple | None = None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return ProbeResult(t.iface, t.address, False, False, None, "not an IP address")
    if ip.version == 4:
        # Bind to the rail's own address in the target's subnet: that is what
        # sends the SYN out of this rail rather than whichever the route picks.
        rail = next((r for r in local if r.iface == t.iface), None)
        mine = next(
            (i for i in (_networks(rail) if rail else []) if ip in i.network and i.ip != ip), None
        )
        if mine is None:
            return ProbeResult(
                t.iface, t.address, False, False, None,
                f"{t.iface} has no IPv4 address in {ip}'s subnet",
            )
        family, sockaddr, source = socket.AF_INET, (host, t.port), (str(mine.ip), 0)
    else:
        try:
            info = socket.getaddrinfo(t.address, t.port, socket.AF_INET6, socket.SOCK_STREAM)
        except OSError as exc:
            return ProbeResult(t.iface, t.address, False, False, None, _os_error(exc))
        family, sockaddr = socket.AF_INET6, info[0][4]
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    started = time.perf_counter()
    try:
        if source is not None:
            sock.bind(source)
        sock.connect(sockaddr)
        return ProbeResult(t.iface, t.address, True, True, _ms(started), None)
    except ConnectionRefusedError:
        return ProbeResult(
            t.iface, t.address, False, True, _ms(started),
            f"connection refused: nothing is listening on port {t.port}",
        )
    except (TimeoutError, socket.timeout):
        return ProbeResult(t.iface, t.address, False, False, None, f"no answer in {timeout:g} s")
    except OSError as exc:
        return ProbeResult(t.iface, t.address, False, False, None, _os_error(exc))
    finally:
        sock.close()


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


def _os_error(exc: OSError) -> str:
    return exc.strerror or str(exc) or type(exc).__name__


def local_rails(runner: CommandRunner | None = None) -> list[RailHealth]:
    """This node's rails with only what a probe needs: carrier and addresses.
    `read_nic_health` without the three ethtool queries per port."""
    runner = runner or SubprocessRunner()
    return [_read_rail(runner, iface, dev, False) for iface, dev in _rdma_ifaces(runner)]


@dataclass(frozen=True)
class RailReach:
    """What one end of a pair saw when it dialled the other from each rail.

    *unavailable* is the sentence when the probing end could not be asked
    (an agent too old to have the route); *results* is then empty."""

    prober: str
    other: str
    results: tuple[ProbeResult, ...] = ()
    unavailable: str | None = None

    def findings(self) -> list[Finding]:
        if self.unavailable is not None:
            return [Finding("info", self.prober, "", self.unavailable, "")]
        out: list[Finding] = []
        by_iface: dict[str, list[ProbeResult]] = {}
        for r in self.results:
            by_iface.setdefault(r.iface, []).append(r)
        for iface, rows in by_iface.items():
            if any(r.reached for r in rows):
                continue
            why = "; ".join(dict.fromkeys(f"{r.target}: {r.error}" for r in rows))
            out.append(
                Finding(
                    "warning", self.prober, iface,
                    f"{iface} on {self.prober} has a link but carried nothing to "
                    f"{self.other} ({why}).",
                    _ping(rows[0]),
                )
            )
        if any(r.reached for r in self.results) and not any(r.ok for r in self.results):
            out.append(
                Finding(
                    "info", self.other, "",
                    f"Nothing on {self.other} is listening on port {RECEIVER_PORT}, so its "
                    "rails answer but a copy cannot land there: its agent runs a build "
                    "without the copy receiver, or could not open the port. Upgrade that "
                    "node, or read its log for 'transfer receiver not started'.",
                    f"ss -ltn 'sport = :{RECEIVER_PORT}'",
                )
            )
        return out


def _ping(r: ProbeResult) -> str:
    if "%" in r.target:
        return f"ping -c 3 {r.target}"
    return f"ping -c 3 -I {r.iface} {r.target}"



# --------------------------------------------------------------------- report


def link_health_report(
    pairs: Sequence[tuple[str, str]],
    health: Mapping[str, Mapping],
    unreadable: Mapping[str, str],
    measured_at: float,
    reach: Mapping[tuple[str, str], RailReach] | None = None,
) -> dict:
    """The `/api/links/health` body.

    *health* maps node_id to its agent's payload; *unreadable* maps node_id to
    the sentence saying why there is none. A pair where either end has no port
    with carrier has no fabric to judge and is left out; a pair with an
    unreadable end is left out too, and the reason is listed once per node.
    *reach* maps a pair to what its rails carried (`RailReach`); a pair with no
    entry was not probed and gets an empty `reach` list and no reach findings.
    """
    links = []
    for a, b in pairs:
        if a not in health or b not in health:
            continue
        ra = rails_from(health[a])
        rb = rails_from(health[b])
        if not any(r.carrier for r in ra) or not any(r.carrier for r in rb):
            continue
        findings = compare_health(a, ra, b, rb)
        rows: list[dict] = []
        got = (reach or {}).get((a, b))
        if got is not None:
            findings += got.findings()
            rows = [
                {"node_id": got.prober, "iface": r.iface, "target": r.target,
                 "ok": r.ok, "ms": r.ms, "error": r.error}
                for r in got.results
            ]
        links.append(
            {"a": a, "b": b, "findings": [f.to_json() for f in findings], "reach": rows}
        )
    return {
        "links": links,
        "unreadable": [{"node_id": n, "reason": unreadable[n]} for n in sorted(unreadable)],
        "measured_at": measured_at,
    }


def rails_from(payload: Mapping) -> list[RailHealth]:
    """The rails in one agent's `read_nic_health` payload."""
    rows = payload.get("interfaces") or []
    return [RailHealth.from_json(r) for r in rows if isinstance(r, Mapping)]
