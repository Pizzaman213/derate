"""Which QSFP cages are actually lit.

The GB10's two QSFP cages each hang off a PCIe Gen5 x4 link. One cable therefore
tops out near 100 Gb/s no matter what the port negotiated, so a pair cabled on a
single port measures roughly half of a pair cabled on both -- and the record has
to say which situation produced the number, or the next person reads a low
measurement as a driver problem.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .runner import CommandRunner

# The ConnectX driver. Used to tell fabric interfaces from the management NIC.
_FABRIC_DRIVERS = ("mlx5_core", "mlx4_core")

_STATE_RE = re.compile(r"^\s*\d+:\s*(\w+)")


@dataclass(frozen=True)
class PortInfo:
    name: str
    active: bool
    rate: str | None = None
    link_layer: str | None = None


@dataclass(frozen=True)
class PortStatus:
    ports: tuple[PortInfo, ...]
    inspected_on: str | None = None
    source: str = "none"

    @property
    def total(self) -> int:
        return len(self.ports)

    @property
    def active(self) -> int:
        return sum(1 for p in self.ports if p.active)

    @property
    def known(self) -> bool:
        return self.total > 0

    def note(self) -> str | None:
        """The sentence an operator needs, or nothing if there is none."""
        if not self.known:
            return None
        if self.total >= 2 and self.active == 1:
            return (
                f"only 1 of {self.total} QSFP ports is up; each cage is a PCIe Gen5 x4 "
                "link to the GB10, so a single cable caps near 100 Gb/s regardless of "
                "the 200GbE the port negotiates, and this figure is roughly half what "
                "both ports would give"
            )
        if self.active == 0:
            return f"no QSFP port is up on {self.inspected_on or 'the inspected node'}"
        if self.total >= 2 and self.active >= 2:
            return f"{self.active} of {self.total} QSFP ports up"
        return None


def inspect_ports(runner: CommandRunner, node_id: str | None = None) -> PortStatus:
    """Read local fabric port state from sysfs.

    Local, and deliberately so: port state is an observation about the machine
    doing the looking. The record names which node that was.
    """
    ports = _from_infiniband(runner)
    if ports:
        return PortStatus(ports=tuple(ports), inspected_on=node_id, source="sysfs-infiniband")
    ports = _from_netdev(runner)
    if ports:
        return PortStatus(ports=tuple(ports), inspected_on=node_id, source="sysfs-net")
    return PortStatus(ports=(), inspected_on=node_id, source="none")


def _from_infiniband(runner: CommandRunner) -> list[PortInfo]:
    out: list[PortInfo] = []
    for path in runner.glob("/sys/class/infiniband/*/ports/*"):
        parts = path.rstrip("/").split("/")
        if len(parts) < 2:
            continue
        device, port = parts[-3], parts[-1]
        state = _first_word(runner.read_text(f"{path}/state"))
        phys = _first_word(runner.read_text(f"{path}/phys_state"))
        if state is None and phys is None:
            continue
        out.append(
            PortInfo(
                name=f"{device}:{port}",
                # ACTIVE is the state where traffic flows. LinkUp alone means a
                # cable is seated but the port is not carrying.
                active=(state or "").upper() == "ACTIVE",
                rate=_clean(runner.read_text(f"{path}/rate")),
                link_layer=_clean(runner.read_text(f"{path}/link_layer")),
            )
        )
    return out


def _from_netdev(runner: CommandRunner) -> list[PortInfo]:
    """Fallback for a ConnectX in Ethernet mode with no ib device exposed."""
    out: list[PortInfo] = []
    for uevent in runner.glob("/sys/class/net/*/device/uevent"):
        text = runner.read_text(uevent) or ""
        if not any(f"DRIVER={d}" in text for d in _FABRIC_DRIVERS):
            continue
        iface = uevent.split("/")[4]
        carrier = _clean(runner.read_text(f"/sys/class/net/{iface}/carrier"))
        speed = _clean(runner.read_text(f"/sys/class/net/{iface}/speed"))
        out.append(
            PortInfo(
                name=iface,
                active=carrier == "1",
                rate=f"{speed} Mb/sec" if speed and speed != "-1" else None,
                link_layer="Ethernet",
            )
        )
    return out


def _first_word(text: str | None) -> str | None:
    """sysfs writes port state as `4: ACTIVE`; we want the word."""
    if text is None:
        return None
    m = _STATE_RE.match(text)
    if m:
        return m.group(1)
    return _clean(text)


def _clean(text: str | None) -> str | None:
    if text is None:
        return None
    stripped = text.strip()
    return stripped or None
