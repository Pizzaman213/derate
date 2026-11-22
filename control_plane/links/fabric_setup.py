#!/usr/bin/env python3
"""Put this machine's RDMA rails into the state the fabric needs, and keep them there.

Runs as root ON THE HOST, outside any container -- it changes interface
settings, which a container on host networking can read and must not own. So
it is standard library only and imports nothing from ``control_plane``:
``install.sh --fabric-setup`` copies this one file out of the node image into
``/usr/local/lib/derate/`` and a systemd timer runs it every 30 minutes.

What it asserts on every interface that has an RDMA device and carrier:

* MTU 9000 on both ends (a 1500 end caps a RoCE path well below the link);
* rx/tx rings at the NIC's maximum (1024 of 8192 is the GB10 default);
* ``rx_cqe_compress`` on;
* one fabric IPv4 address per rail, numbered automatically: rail ``i`` gets
  ``10.100.{152+i}.0/24`` and the host part is the last octet of this
  machine's management address, which is unique on a /24 LAN -- so every
  node arrives at a consistent plan without being told anything;
* with ``exclusive`` (the default), no OTHER IPv4 address on a rail. A stale
  ``10.100.0.2/30`` or a ``169.254/16`` link-local address on a rail is what
  NCCL's bootstrap and RoCE GID selection pick when they should not.

It re-asserts rather than configuring once because NVIDIA Sync rewrites
netplan and NetworkManager re-applies connections; the timer is the same
answer exo's fabric service gives to the same problem.

``plan`` and ``plan_restore`` are pure: a ``HostState`` and a ``Config`` (or a
snapshot) in, a list of idempotent ``Action`` out, and nothing when the rails
already match. Everything that touches the machine is in ``read_state`` and
``execute``.

    fabric_setup.py --dry-run                 # print the plan; read-only, no root
    fabric_setup.py --apply --snapshot P      # record the originals once, then apply
    fabric_setup.py --restore P [--dry-run]   # put back exactly what P recorded
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import subprocess
import sys
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

SYS_NET = Path("/sys/class/net")
DEFAULT_CONFIG_PATH = "/etc/derate/fabric.json"
DEFAULT_SNAPSHOT_PATH = "/var/lib/derate/fabric-snapshot.json"
DEFAULT_MTU = 9000
DEFAULT_SUBNET_BASE = "10.100.152.0"
RAIL_PREFIX = 24
CQE_FLAG = "rx_cqe_compress"
SNAPSHOT_VERSION = 1

# (returncode, stdout, stderr). 127 means the binary is not installed.
Run = Callable[[Sequence[str]], "tuple[int, str, str]"]

# Everything below reaches a root argv, so it is checked before an Action exists.
IFACE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,15}$")
_SCOPES = frozenset({"global", "link", "host", "site"})
_MTU_RANGE = (576, 9216)


class Refused(Exception):
    """The plan cannot be made. ``sentence`` says why and what to change."""

    def __init__(self, sentence: str):
        super().__init__(sentence)
        self.sentence = sentence


@dataclass(frozen=True)
class Addr:
    address: str
    prefix: int
    scope: str = "global"
    noprefixroute: bool = False

    @property
    def cidr(self) -> str:
        return f"{self.address}/{self.prefix}"

    def same(self, other: "Addr") -> bool:
        return (self.address, self.prefix) == (other.address, other.prefix)

    def to_json(self) -> dict:
        return {"address": self.address, "prefix": self.prefix,
                "scope": self.scope, "noprefixroute": self.noprefixroute}

    @classmethod
    def from_json(cls, d: Mapping) -> "Addr":
        return cls(str(d["address"]), int(d["prefix"]), str(d.get("scope", "global")),
                   bool(d.get("noprefixroute", False)))

    def check(self) -> None:
        """Refuse anything that is not a plain IPv4 address, prefix and known scope."""
        try:
            ipaddress.IPv4Address(self.address)
        except (ipaddress.AddressValueError, ValueError):
            raise Refused(f"{self.address!r} is not an IPv4 address") from None
        if isinstance(self.prefix, bool) or not isinstance(self.prefix, int) \
                or not 0 <= self.prefix <= 32:
            raise Refused(f"{self.prefix!r} is not an IPv4 prefix length")
        if self.scope not in _SCOPES:
            raise Refused(f"{self.scope!r} is not an address scope")


def check_iface(name: object) -> str:
    if not isinstance(name, str) or not IFACE_RE.match(name):
        raise Refused(f"{name!r} is not an interface name")
    return name


@dataclass(frozen=True)
class Iface:
    name: str
    rdma: tuple[str, ...]
    carrier: bool
    pci: str = ""
    mtu: int | None = None
    ring_rx: int | None = None
    ring_tx: int | None = None
    ring_rx_max: int | None = None
    ring_tx_max: int | None = None
    cqe_compress: bool | None = None
    ipv4: tuple[Addr, ...] = ()


@dataclass(frozen=True)
class Management:
    iface: str
    address: str
    prefix: int


@dataclass(frozen=True)
class HostState:
    interfaces: tuple[Iface, ...]
    management: Management | None
    unread: tuple[str, ...] = ()


_CONFIG_KEYS = ("mtu", "ring", "rx_cqe_compress", "subnet_base", "exclusive", "addresses")


@dataclass(frozen=True)
class Config:
    mtu: int | None = DEFAULT_MTU
    ring: str | int | None = "max"          # "max", a count, or None to leave alone
    rx_cqe_compress: bool | None = True     # None leaves it alone
    subnet_base: str = DEFAULT_SUBNET_BASE  # rail i -> base + i in the third octet
    exclusive: bool = True
    addresses: Mapping[str, str] = field(default_factory=dict)  # iface -> "A/P" override

    @classmethod
    def from_json(cls, d: Mapping) -> "Config":
        if not isinstance(d, Mapping):
            raise Refused("the fabric config must be a JSON object")
        # An unknown key is refused rather than ignored: a typo would otherwise
        # read as configured while the defaults run.
        unknown = sorted(set(d) - set(_CONFIG_KEYS))
        if unknown:
            raise Refused(f"the fabric config has unknown key(s) {', '.join(unknown)}; "
                          f"the keys are {', '.join(_CONFIG_KEYS)}")
        cfg = cls(**{k: d[k] for k in _CONFIG_KEYS if k in d})
        if cfg.mtu is not None and (not isinstance(cfg.mtu, int) or isinstance(cfg.mtu, bool)
                                    or not 576 <= cfg.mtu <= 9216):
            raise Refused("mtu must be an integer from 576 to 9216, or null to leave it alone")
        if cfg.ring is not None and cfg.ring != "max" and (
                not isinstance(cfg.ring, int) or isinstance(cfg.ring, bool) or cfg.ring < 1):
            raise Refused('ring must be "max", a positive integer, or null to leave it alone')
        if cfg.rx_cqe_compress is not None and not isinstance(cfg.rx_cqe_compress, bool):
            raise Refused("rx_cqe_compress must be true, false, or null to leave it alone")
        if not isinstance(cfg.exclusive, bool):
            raise Refused("exclusive must be true or false")
        try:
            base = ipaddress.IPv4Address(cfg.subnet_base)
        except (ipaddress.AddressValueError, ValueError):
            raise Refused(f"subnet_base {cfg.subnet_base!r} is not an IPv4 address") from None
        if int(base) & 0xFF:
            raise Refused(f"subnet_base {cfg.subnet_base} must end in .0: rail i is the /24 "
                          f"{RAIL_PREFIX} bits up from it")
        if not isinstance(cfg.addresses, Mapping):
            raise Refused('addresses must be an object of interface name to "A.B.C.D/P"')
        for iface, cidr in cfg.addresses.items():
            check_iface(iface)
            try:
                ipaddress.IPv4Interface(str(cidr))
            except (ipaddress.AddressValueError, ValueError):
                raise Refused(f"addresses[{iface!r}] = {cidr!r} is not an IPv4 address "
                              f"with a prefix, like 10.100.152.7/24") from None
        return cfg

    def to_json(self) -> dict:
        return {"mtu": self.mtu, "ring": self.ring, "rx_cqe_compress": self.rx_cqe_compress,
                "subnet_base": self.subnet_base, "exclusive": self.exclusive,
                "addresses": dict(self.addresses)}

    def describe(self) -> str:
        """The config as the sentence an operator consents to."""
        parts = [f"MTU {self.mtu}" if self.mtu is not None else "MTU left alone"]
        if self.ring == "max":
            parts.append("ring buffers at the NIC's maximum")
        elif self.ring is None:
            parts.append("ring buffers left alone")
        else:
            parts.append(f"ring buffers at {self.ring} (capped at the NIC's maximum)")
        if self.rx_cqe_compress is None:
            parts.append(f"{CQE_FLAG} left alone")
        else:
            parts.append(f"{CQE_FLAG} {'on' if self.rx_cqe_compress else 'off'}")
        base = ipaddress.IPv4Address(self.subnet_base)
        parts.append(f"rail i numbered in {base}/{RAIL_PREFIX} + i with this machine's "
                     f"management octet as the host part")
        if self.addresses:
            parts.append("named addresses: " + ", ".join(
                f"{k} {v}" for k, v in sorted(self.addresses.items())))
        parts.append("exclusive: true -- every other IPv4 address on a rail is removed"
                     if self.exclusive else
                     "exclusive: false -- other IPv4 addresses on a rail are kept")
        return "; ".join(parts)


@dataclass(frozen=True)
class Action:
    kind: str     # "mtu" | "ring" | "cqe" | "addr_add" | "addr_del"
    iface: str
    value: object  # int | (rx, tx) | bool | Addr
    sentence: str

    def __post_init__(self) -> None:
        # The last gate before a root argv: whatever planned this action (sysfs,
        # the config, a snapshot file) must have handed over plain values.
        check_iface(self.iface)
        v = self.value
        if self.kind == "mtu":
            if isinstance(v, bool) or not isinstance(v, int) \
                    or not _MTU_RANGE[0] <= v <= _MTU_RANGE[1]:
                raise Refused(f"MTU {v!r} for {self.iface} is not an integer from "
                              f"{_MTU_RANGE[0]} to {_MTU_RANGE[1]}")
        elif self.kind == "ring":
            if not (isinstance(v, tuple) and len(v) == 2 and all(
                    isinstance(x, int) and not isinstance(x, bool) and x >= 1 for x in v)):
                raise Refused(f"ring sizes {v!r} for {self.iface} are not two positive integers")
        elif self.kind == "cqe":
            if not isinstance(v, bool):
                raise Refused(f"{CQE_FLAG} {v!r} for {self.iface} is not true or false")
        elif self.kind in ("addr_add", "addr_del"):
            if not isinstance(v, Addr):
                raise Refused(f"{v!r} for {self.iface} is not an address")
            v.check()
        else:
            raise Refused(f"unknown action kind {self.kind!r}")

    def argv(self) -> tuple[str, ...]:
        if self.kind == "mtu":
            return ("ip", "link", "set", "dev", self.iface, "mtu", str(self.value))
        if self.kind == "ring":
            rx, tx = self.value  # type: ignore[misc]
            return ("ethtool", "-G", self.iface, "rx", str(rx), "tx", str(tx))
        if self.kind == "cqe":
            return ("ethtool", "--set-priv-flags", self.iface, CQE_FLAG,
                    "on" if self.value else "off")
        addr: Addr = self.value  # type: ignore[assignment]
        if self.kind == "addr_add":
            argv = ["ip", "addr", "replace", addr.cidr, "brd", "+", "dev", self.iface]
            if addr.scope != "global":
                argv += ["scope", addr.scope]
            if addr.noprefixroute:
                argv.append("noprefixroute")
            return tuple(argv)
        if self.kind == "addr_del":
            return ("ip", "addr", "del", addr.cidr, "dev", self.iface)
        raise ValueError(f"unknown action kind {self.kind!r}")

    def command(self) -> str:
        return " ".join(self.argv())


# ---------------------------------------------------------------------------
# The planner (pure)
# ---------------------------------------------------------------------------


def rails(state: HostState) -> list[Iface]:
    """Every interface with an RDMA device and carrier, in rail order.

    Ordered by PCI address, then name. Not by the name alone: a byte sort puts
    ``enP2p1s0f0np0`` before ``enp1s0f0np0`` (``P`` < ``p``), the reverse of how
    the GB10's two PCIe x4 halves enumerate and of how the peers here were
    numbered by hand. A rail whose cable is pulled drops out and the ones after
    it renumber -- on a direct cable the far end loses carrier too, so both
    ends renumber alike.
    """
    up = [i for i in state.interfaces if i.rdma and i.carrier]
    return sorted(up, key=lambda i: (i.pci or "~", i.name.casefold(), i.name))


def host_octet(state: HostState) -> int:
    m = state.management
    where = f'the "addresses" key of the fabric config ({DEFAULT_CONFIG_PATH})'
    if m is None:
        raise Refused("this machine has no default route, so there is no management address "
                      f"to number its rails from; name each rail's address in {where}")
    net = ipaddress.IPv4Interface(f"{m.address}/{m.prefix}").network
    if m.prefix != 24:
        raise Refused(f"the management network is {net} on {m.iface}, not a /24, so the last "
                      f"octet of {m.address} is not guaranteed to be unique across the cluster; "
                      f"name each rail's address in {where}")
    octet = int(m.address.rsplit(".", 1)[1])
    if octet in (0, 255):
        raise Refused(f"the management address {m.address} ends in .{octet}, which cannot be a "
                      f"host's address on a rail /24; name each rail's address in {where}")
    return octet


def targets(state: HostState, config: Config) -> dict[str, Addr]:
    """The fabric address each rail must carry."""
    out: dict[str, Addr] = {}
    octet: int | None = None
    base = int(ipaddress.IPv4Address(config.subnet_base))
    for i, rail in enumerate(rails(state)):
        named = config.addresses.get(rail.name)
        if named:
            iface = ipaddress.IPv4Interface(named)
            out[rail.name] = Addr(str(iface.ip), iface.network.prefixlen)
            continue
        if octet is None:
            octet = host_octet(state)
        net_int = base + (i << 8)
        if net_int + 255 > 0xFFFFFFFF:
            raise Refused(f"subnet_base {config.subnet_base} has no room for rail {i}")
        out[rail.name] = Addr(str(ipaddress.IPv4Address(net_int + octet)), RAIL_PREFIX)
    return out


def _want_ring(config: Config, rail: Iface) -> tuple[int, int] | None:
    if config.ring is None or None in (rail.ring_rx, rail.ring_tx, rail.ring_rx_max,
                                       rail.ring_tx_max):
        return None
    if config.ring == "max":
        return rail.ring_rx_max, rail.ring_tx_max  # type: ignore[return-value]
    n = int(config.ring)
    return min(n, rail.ring_rx_max), min(n, rail.ring_tx_max)  # type: ignore[type-var]


def plan(state: HostState, config: Config) -> list[Action]:
    """What to run so every rail matches ``config``. Empty when it already does.

    Additions come before removals, so a rail never passes through a moment
    with no address on it.
    """
    want = targets(state, config)
    fabric_nets = [ipaddress.IPv4Interface(a.cidr).network for a in want.values()]
    mgmt = state.management.address if state.management else None
    changes: list[Action] = []
    removals: list[Action] = []
    for rail in rails(state):
        if config.mtu is not None and rail.mtu != config.mtu:
            changes.append(Action("mtu", rail.name, config.mtu,
                                  f"MTU {rail.mtu} -> {config.mtu}"))
        ring = _want_ring(config, rail)
        if ring is not None and ring != (rail.ring_rx, rail.ring_tx):
            changes.append(Action("ring", rail.name, ring,
                                  f"ring buffers {rail.ring_rx}/{rail.ring_tx} -> "
                                  f"{ring[0]}/{ring[1]} (rx/tx)"))
        if (config.rx_cqe_compress is not None and rail.cqe_compress is not None
                and rail.cqe_compress != config.rx_cqe_compress):
            changes.append(Action("cqe", rail.name, config.rx_cqe_compress,
                                  f"{CQE_FLAG} {'off -> on' if config.rx_cqe_compress else 'on -> off'}"))
        target = want[rail.name]
        if not any(a.same(target) for a in rail.ipv4):
            changes.append(Action("addr_add", rail.name, target,
                                  f"add fabric address {target.cidr}"))
        if not config.exclusive:
            continue
        for a in rail.ipv4:
            ip = ipaddress.IPv4Address(a.address)
            if a.address == mgmt or any(ip in net for net in fabric_nets):
                continue
            removals.append(Action("addr_del", rail.name, a,
                                   f"remove {a.cidr}: outside the fabric subnets "
                                   f"({', '.join(str(n) for n in fabric_nets)})"))
    return changes + removals


# ---------------------------------------------------------------------------
# Snapshot and restore (pure)
# ---------------------------------------------------------------------------


def _iface_record(rail: Iface) -> dict:
    return {"mtu": rail.mtu, "ring_rx": rail.ring_rx, "ring_tx": rail.ring_tx,
            CQE_FLAG: rail.cqe_compress, "ipv4": [a.to_json() for a in rail.ipv4]}


def update_snapshot(snapshot: Mapping | None, state: HostState,
                    actions: Sequence[Action]) -> dict:
    """The snapshot to hold before ``actions`` run.

    A rail's original settings are recorded the first time it is seen and
    never again -- a later run sees the state the first one made. The
    addresses this tool adds are accumulated, so ``--restore`` deletes exactly
    those and nothing NetworkManager or DHCP put there since.
    """
    snap = {"version": SNAPSHOT_VERSION, "interfaces": {}, "added": {}}
    if snapshot:
        snap["interfaces"] = {k: dict(v) for k, v in snapshot.get("interfaces", {}).items()}
        snap["added"] = {k: list(v) for k, v in snapshot.get("added", {}).items()}
    for rail in rails(state):
        snap["interfaces"].setdefault(rail.name, _iface_record(rail))
    for act in actions:
        if act.kind != "addr_add":
            continue
        cidr = act.value.cidr  # type: ignore[attr-defined]
        original = {Addr.from_json(a).cidr for a in
                    snap["interfaces"].get(act.iface, {}).get("ipv4", [])}
        added = snap["added"].setdefault(act.iface, [])
        if cidr not in original and cidr not in added:
            added.append(cidr)
    return snap


def plan_restore(state: HostState, snapshot: Mapping) -> list[Action]:
    """What to run to put every recorded interface back as the snapshot found it.

    The snapshot is a file; anything malformed in it is a ``Refused``, never an argv."""
    try:
        return _plan_restore(state, snapshot)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise Refused(f"the snapshot is malformed ({exc}); nothing was restored") from None


def _plan_restore(state: HostState, snapshot: Mapping) -> list[Action]:
    by_name = {i.name: i for i in state.interfaces}
    changes: list[Action] = []
    removals: list[Action] = []
    for name, rec in sorted(snapshot.get("interfaces", {}).items()):
        now = by_name.get(name)
        if now is None:
            continue
        mtu = rec.get("mtu")
        if mtu is not None and now.mtu != mtu:
            changes.append(Action("mtu", name, mtu, f"MTU {now.mtu} -> {mtu} (as recorded)"))
        rx, tx = rec.get("ring_rx"), rec.get("ring_tx")
        if None not in (rx, tx, now.ring_rx, now.ring_tx) and (now.ring_rx, now.ring_tx) != (rx, tx):
            changes.append(Action("ring", name, (rx, tx),
                                  f"ring buffers {now.ring_rx}/{now.ring_tx} -> {rx}/{tx} "
                                  f"(as recorded)"))
        cqe = rec.get(CQE_FLAG)
        if cqe is not None and now.cqe_compress is not None and now.cqe_compress != cqe:
            changes.append(Action("cqe", name, cqe,
                                  f"{CQE_FLAG} {'on' if cqe else 'off'} (as recorded)"))
        original = [Addr.from_json(a) for a in rec.get("ipv4", [])]
        for a in original:
            if not any(a.same(b) for b in now.ipv4):
                changes.append(Action("addr_add", name, a, f"put back {a.cidr}"))
        added = set(snapshot.get("added", {}).get(name, []))
        for a in now.ipv4:
            if a.cidr in added and not any(a.same(o) for o in original):
                removals.append(Action("addr_del", name, a,
                                       f"remove {a.cidr}: added by derate fabric setup"))
    return changes + removals


# ---------------------------------------------------------------------------
# Reading the machine
# ---------------------------------------------------------------------------


def run_command(argv: Sequence[str]) -> tuple[int, str, str]:
    try:
        p = subprocess.run(list(argv), capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: not installed"
    except subprocess.TimeoutExpired:
        return 124, "", f"{argv[0]} did not answer within 30s"
    return p.returncode, p.stdout, p.stderr


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def parse_ip_addr_json(text: str) -> tuple[Addr, ...]:
    out = []
    try:
        links = json.loads(text or "[]")
    except ValueError:
        return ()
    for link in links:
        for a in link.get("addr_info", []):
            if a.get("family") != "inet" or "local" not in a:
                continue
            out.append(Addr(a["local"], int(a.get("prefixlen", 32)),
                            a.get("scope", "global"), bool(a.get("noprefixroute", False))))
    return tuple(out)


def parse_rings(text: str) -> tuple[int | None, int | None, int | None, int | None]:
    """``ethtool -g`` -> (rx, tx, rx_max, tx_max)."""
    section = None
    vals: dict[tuple[str, str], int] = {}
    for line in text.splitlines():
        low = line.strip().lower()
        if low.startswith("pre-set maximums"):
            section = "max"
        elif low.startswith("current hardware settings"):
            section = "cur"
        elif section and ":" in line:
            key, _, val = line.partition(":")
            key, val = key.strip().upper(), val.strip()
            if key in ("RX", "TX") and val.isdigit():
                vals[(section, key)] = int(val)
    return (vals.get(("cur", "RX")), vals.get(("cur", "TX")),
            vals.get(("max", "RX")), vals.get(("max", "TX")))


def parse_priv_flag(text: str, flag: str = CQE_FLAG) -> bool | None:
    for line in text.splitlines():
        key, sep, val = line.partition(":")
        if sep and key.strip() == flag:
            return val.strip() == "on"
    return None


def read_management(run: Run) -> Management | None:
    rc, out, _ = run(["ip", "-j", "route", "show", "default"])
    if rc != 0:
        return None
    try:
        routes = [r for r in json.loads(out or "[]") if r.get("dev")]
    except ValueError:
        return None
    if not routes:
        return None
    route = min(routes, key=lambda r: r.get("metric", 0))
    dev = route["dev"]
    rc, out, _ = run(["ip", "-j", "addr", "show", "dev", dev])
    addrs = parse_ip_addr_json(out) if rc == 0 else ()
    src = route.get("prefsrc")
    chosen = next((a for a in addrs if a.address == src), addrs[0] if addrs else None)
    return Management(dev, chosen.address, chosen.prefix) if chosen else None


def read_state(run: Run = run_command, sys_net: Path = SYS_NET) -> HostState:
    unread: list[str] = []
    ifaces: list[Iface] = []
    ethtool_missing = False
    try:
        entries = sorted(sys_net.iterdir())
    except OSError:
        entries = []
    for d in entries:
        try:
            rdma = tuple(sorted(p.name for p in (d / "device" / "infiniband").iterdir()))
        except OSError:
            continue
        if not rdma:
            continue
        # Reading `carrier` on a downed interface fails with EINVAL; that is no carrier.
        carrier = _read(d / "carrier") == "1"
        try:
            pci = os.path.basename(os.path.realpath(d / "device"))
        except OSError:
            pci = ""
        if not carrier:
            ifaces.append(Iface(d.name, rdma, False, pci))
            continue
        mtu_text = _read(d / "mtu")
        mtu = int(mtu_text) if mtu_text and mtu_text.isdigit() else None
        rc, out, _ = run(["ip", "-j", "addr", "show", "dev", d.name])
        ipv4 = parse_ip_addr_json(out) if rc == 0 else ()
        rings: tuple[int | None, ...] = (None, None, None, None)
        cqe = None
        if not ethtool_missing:
            rc, out, _ = run(["ethtool", "-g", d.name])
            if rc == 127:
                ethtool_missing = True
                unread.append("ethtool is not installed, so ring buffers and "
                              f"{CQE_FLAG} are left alone")
            elif rc == 0:
                rings = parse_rings(out)
            if not ethtool_missing:
                rc, out, _ = run(["ethtool", "--show-priv-flags", d.name])
                cqe = parse_priv_flag(out) if rc == 0 else None
        ifaces.append(Iface(d.name, rdma, True, pci, mtu, *rings, cqe, ipv4))  # type: ignore[arg-type]
    return HostState(tuple(ifaces), read_management(run), tuple(unread))


# ---------------------------------------------------------------------------
# Applying, rendering, the command line
# ---------------------------------------------------------------------------


def execute(actions: Sequence[Action], run: Run = run_command,
            out: Callable[[str], None] = print) -> list[str]:
    """Run every action; one failure does not stop the rest. Returns the failures.

    Except: when an address could not be put on an interface, that interface's
    address REMOVALS are skipped (and reported as failures) -- deleting the old
    address after its replacement failed would leave the rail with no IPv4."""
    failures = []
    add_failed: set[str] = set()
    for act in actions:
        if act.kind == "addr_del" and act.iface in add_failed:
            line = (f"{act.iface}: {act.sentence} skipped (adding this interface's new "
                    f"address failed, so its old one is kept)")
            failures.append(line)
            out(f"  SKIPPED {line}")
            continue
        rc, _, err = run(list(act.argv()))
        if rc == 0:
            out(f"  ok      {act.iface}: {act.sentence}")
        else:
            if act.kind == "addr_add":
                add_failed.add(act.iface)
            why = (err or "").strip().splitlines()
            line = f"{act.iface}: {act.sentence} failed ({act.command()}: " \
                   f"{why[-1] if why else f'exit {rc}'})"
            failures.append(line)
            out(f"  FAILED  {line}")
    return failures


def render_actions(actions: Sequence[Action]) -> list[str]:
    if not actions:
        return ["nothing to change: every rail already matches"]
    lines = [f"{len(actions)} change(s):"]
    for act in actions:
        lines.append(f"  {act.iface}: {act.sentence}")
        lines.append(f"    $ {act.command()}")
    return lines


def render_plan(state: HostState, config: Config, actions: Sequence[Action]) -> list[str]:
    lines = [f"config: {config.describe()}"]
    m = state.management
    lines.append(f"management: {m.iface} {m.address}/{m.prefix}" if m else
                 "management: no default route")
    want = targets(state, config)
    up = rails(state)
    if not up:
        lines.append("rails: no interface with an RDMA device has carrier; nothing to do")
    for i, rail in enumerate(up):
        have = ", ".join(a.cidr for a in rail.ipv4) or "no IPv4"
        lines.append(f"rail {i}: {rail.name} ({', '.join(rail.rdma)}, {rail.pci or 'no PCI'}) "
                     f"MTU {rail.mtu}, rings {rail.ring_rx}/{rail.ring_tx} of "
                     f"{rail.ring_rx_max}/{rail.ring_tx_max}, {CQE_FLAG} "
                     f"{ {True: 'on', False: 'off', None: 'unread'}[rail.cqe_compress] }, "
                     f"{have} -> {want[rail.name].cidr}")
    down = [i.name for i in state.interfaces if i.rdma and not i.carrier]
    if down:
        lines.append(f"no carrier, left alone: {', '.join(down)}")
    lines.extend(state.unread)
    if any(a.kind in ("ring", "cqe") for a in actions):
        lines.append("note: ring and rx_cqe_compress changes reopen the NIC's queues, which "
                     "interrupts traffic on that rail for a moment; run with no model "
                     "spanning it")
    return lines + render_actions(actions)


def load_config(path: str) -> tuple[Config, str]:
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return Config(), f"no config at {path}; using the defaults"
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise Refused(f"{path} is not valid JSON: {exc}") from None
    return Config.from_json(data), f"config from {path}"


def _open_dir_nofollow(directory: str) -> int:
    """Walk from `/` to *directory* one component at a time with O_NOFOLLOW,
    creating missing components 0755. A symlink anywhere on the way is refused:
    resolving the path once and then trusting it is what lets an unprivileged
    user swap an ancestor for a link between the check and the write."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", flags)
    walked = ""
    try:
        for part in Path(os.path.abspath(directory)).parts[1:]:
            walked += "/" + part
            try:
                st = os.stat(part, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                os.mkdir(part, 0o755, dir_fd=fd)
                st = os.stat(part, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISLNK(st.st_mode):
                raise Refused(f"{walked} is a symlink; not writing beneath it")
            try:
                child = os.open(part, flags, dir_fd=fd)
            except OSError as exc:
                raise Refused(f"{walked} is not a plain directory ({exc.strerror}); "
                              "not writing beneath it") from exc
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def _write_json(path: str, data: Mapping, euid: Callable[[], int] = os.geteuid) -> None:
    """Write as root without following anything an unprivileged user could plant:
    every ancestor is opened without following a symlink, the final directory
    must be owned by us, the target must not be a symlink, and the bytes go to
    an O_EXCL temp file in that directory first, renamed relative to its fd."""
    p = Path(os.path.abspath(path))
    me = euid()
    dfd = _open_dir_nofollow(str(p.parent))
    try:
        if os.fstat(dfd).st_uid != me:
            raise Refused(f"{p.parent} is not owned by uid {me}; not writing {p.name}")
        try:
            st = os.stat(p.name, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            st = None
        if st is not None and stat.S_ISLNK(st.st_mode):
            raise Refused(f"{p} is a symlink; not writing through it")
        tmp = f".{p.name}.{secrets.token_hex(6)}.tmp"
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o644,
                     dir_fd=dfd)
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
                os.fchmod(fh.fileno(), 0o644)
            os.rename(tmp, p.name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except FileNotFoundError:
                pass
            raise
    finally:
        os.close(dfd)


def main(argv: Sequence[str] | None = None, *, run: Run = run_command,
         sys_net: Path = SYS_NET, euid: Callable[[], int] = os.geteuid) -> int:
    ap = argparse.ArgumentParser(
        description="Assert MTU, rings, rx_cqe_compress and fabric IPv4 on this machine's "
                    "RDMA rails.")
    ap.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    ap.add_argument("--dry-run", action="store_true", help="print the plan; change nothing")
    ap.add_argument("--apply", action="store_true", help="apply the plan (root)")
    ap.add_argument("--snapshot", metavar="PATH",
                    help="with --apply: record the original settings here before the first change")
    ap.add_argument("--restore", metavar="PATH",
                    help="put back what the snapshot at PATH recorded (with --dry-run: print it)")
    args = ap.parse_args(argv)
    if args.apply and (args.dry_run or args.restore):
        ap.error("--apply cannot be combined with --dry-run or --restore")
    if not (args.apply or args.dry_run or args.restore):
        ap.error("say --dry-run, --apply or --restore PATH")
    changing = not args.dry_run
    if changing and euid() != 0:
        print("changing interface settings needs root; --dry-run shows the plan without it",
              file=sys.stderr)
        return 2

    state = read_state(run, sys_net)

    if args.restore:
        try:
            snapshot = json.loads(Path(args.restore).read_text())
        except FileNotFoundError:
            print(f"no snapshot at {args.restore}; nothing to restore")
            return 0
        except ValueError as exc:
            print(f"{args.restore} is not valid JSON: {exc}", file=sys.stderr)
            return 1
        try:
            actions = plan_restore(state, snapshot)
        except Refused as exc:
            print(f"refused: {exc.sentence}", file=sys.stderr)
            return 1
        print(f"restore from {args.restore}" + (" (dry run: nothing is changed)" if not changing else ""))
        for line in render_actions(actions):
            print(line)
        if not changing:
            return 0
        failures = execute(actions, run)
        if failures:
            return 1
        # Restored in full: the record has done its job, and a later --apply
        # must snapshot the machine as it is then.
        Path(args.restore).unlink(missing_ok=True)
        return 0

    try:
        config, source = load_config(args.config)
        actions = plan(state, config)
        lines = render_plan(state, config, actions)
    except Refused as exc:
        print(f"refused: {exc.sentence}", file=sys.stderr)
        return 1
    print(f"derate fabric setup, {source}" + (" (dry run: nothing is changed)" if not changing else ""))
    for line in lines:
        print(line)
    if not changing:
        return 0
    if args.snapshot and actions:
        try:
            previous = json.loads(Path(args.snapshot).read_text())
        except FileNotFoundError:
            previous = None
        except ValueError as exc:
            print(f"{args.snapshot} is not valid JSON ({exc}); not changing anything without "
                  "a record to restore from", file=sys.stderr)
            return 1
        updated = update_snapshot(previous, state, actions)
        if updated != previous:
            try:
                _write_json(args.snapshot, updated)
            except (Refused, OSError) as exc:
                print(f"{getattr(exc, 'sentence', exc)}; not changing anything without a "
                      "record to restore from", file=sys.stderr)
                return 1
    return 1 if execute(actions, run) else 0


if __name__ == "__main__":
    sys.exit(main())
