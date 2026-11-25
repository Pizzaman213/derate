"""Where a LAN or tailnet address is, from what this machine's tailscaled knows.

No IP database can place ``100.67.68.35`` (a tailnet address) or
``192.168.0.75`` (a LAN one): they are not on the internet. But when this
machine runs Tailscale, the local daemon knows three things that can:

* this machine's own public address -- ``Self.Addrs``, its STUN-discovered
  endpoints -- which the database places at city level. A request from
  loopback, the LAN, or this machine's own tailnet address came from THIS
  network, so it is placed there (``source="coordinator"``).
* a peer's direct endpoint (``CurAddr``) when Tailscale reaches it without a
  relay: a public one is placed by the database like any address; a private
  one means the peer is on a network this machine reaches directly, which is
  placed as this machine's.
* a relayed peer's home relay (``Relay``) -- the DERP region with the lowest
  latency to it. That is a REGION, hundreds of kilometres wide, and it is
  labelled so (``source="relay"``).

All of it is where the device is NOW, at read time: a tailnet address follows
its device, unlike a public one, so a laptop's old requests move with it. The
labels say "now". Asked through ``tailscale status --json`` -- the local
daemon, nothing leaves the machine -- at most once a minute, and never when
``DERATE_GEOIP_TAILSCALE=0``. Without Tailscale this is simply absent and
those requests stay unplaced, as before.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from control_plane.telemetry.geoip import Location, _address, classify

log = logging.getLogger(__name__)

#: Tailscale's address ranges: CGNAT space for IPv4, its own ULA prefix for IPv6.
TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
#: How long one ``tailscale status`` answer is used.
REFRESH_S = 60.0
_TIMEOUT_S = 3.0

#: Tailscale's DERP regions by the code ``tailscale status`` reports as a
#: peer's ``Relay``, and the city the region's servers are in -- the list
#: ``tailscale debug derp-map`` printed on 1.104. A code missing here places
#: nothing rather than guessing.
DERP_REGIONS: dict[str, tuple[str, float, float]] = {
    "ams": ("Amsterdam", 52.37, 4.90),
    "bom": ("Mumbai", 19.08, 72.88),
    "dbi": ("Dubai", 25.20, 55.27),
    "den": ("Denver", 39.74, -104.99),
    "dfw": ("Dallas", 32.78, -96.80),
    "fra": ("Frankfurt", 50.11, 8.68),
    "hel": ("Helsinki", 60.17, 24.94),
    "hkg": ("Hong Kong", 22.32, 114.17),
    "hnl": ("Honolulu", 21.31, -157.86),
    "iad": ("Ashburn", 39.04, -77.49),
    "jnb": ("Johannesburg", -26.20, 28.05),
    "lax": ("Los Angeles", 34.05, -118.24),
    "lhr": ("London", 51.51, -0.13),
    "mad": ("Madrid", 40.42, -3.70),
    "mia": ("Miami", 25.76, -80.19),
    "nai": ("Nairobi", -1.29, 36.82),
    "nue": ("Nuremberg", 49.45, 11.08),
    "nyc": ("New York City", 40.71, -74.01),
    "ord": ("Chicago", 41.88, -87.63),
    "par": ("Paris", 48.86, 2.35),
    "sao": ("São Paulo", -23.55, -46.63),
    "sea": ("Seattle", 47.61, -122.33),
    "sfo": ("San Francisco", 37.77, -122.42),
    "sin": ("Singapore", 1.35, 103.82),
    "syd": ("Sydney", -33.87, 151.21),
    "tok": ("Tokyo", 35.68, 139.69),
    "tor": ("Toronto", 43.65, -79.38),
    "waw": ("Warsaw", 52.23, 21.01),
}


@dataclass(frozen=True)
class Peer:
    host: str
    #: The address Tailscale reaches it at directly; None when relayed or idle.
    endpoint: str | None
    #: Its home DERP region code.
    relay: str | None


@dataclass(frozen=True)
class Snapshot:
    #: This machine's own tailnet addresses.
    own_ips: frozenset[str]
    #: This machine's public address, from its own STUN endpoints.
    public_ip: str | None
    #: Peers by each of their tailnet addresses.
    peers: dict[str, Peer]


def _endpoint_ip(endpoint: str) -> str | None:
    """``1.2.3.4:41641`` or ``[2001:db8::1]:41641`` -> the address."""
    addr = _address(endpoint.rsplit(":", 1)[0].strip("[]") if ":" in endpoint else endpoint)
    return str(addr) if addr is not None else None


def parse_status(raw: dict[str, Any]) -> Snapshot:
    """``tailscale status --json`` as a :class:`Snapshot`."""
    me = raw.get("Self") or {}
    public = None
    for ep in me.get("Addrs") or []:
        ip = _endpoint_ip(str(ep))
        if ip and classify(ip) == "public":
            public = ip
            break
    peers: dict[str, Peer] = {}
    for p in (raw.get("Peer") or {}).values():
        peer = Peer(
            host=str(p.get("HostName") or p.get("DNSName") or "a tailnet device"),
            endpoint=_endpoint_ip(p["CurAddr"]) if p.get("CurAddr") else None,
            relay=p.get("Relay") or None,
        )
        for ip in p.get("TailscaleIPs") or []:
            peers[str(ip)] = peer
    return Snapshot(own_ips=frozenset(map(str, me.get("TailscaleIPs") or [])), public_ip=public, peers=peers)


def in_tailnet(ip: str) -> bool:
    addr = _address(ip)
    return addr is not None and (addr in TAILNET_V4 or addr in TAILNET_V6)


def _place(loc: Location) -> str:
    return ", ".join(s for s in (loc.city, loc.region, loc.country) if s) or "an unnamed place"


def locate_local(
    ip: str, snap: Snapshot | None, lookup: Callable[[str], Location | None]
) -> Location | None:
    """A LAN, loopback or tailnet address placed through *snap*; None when it
    says nothing. *lookup* is the IP database."""
    if snap is None or _address(ip) is None:
        return None
    ip = str(_address(ip))

    def here(who: str) -> Location | None:
        loc = lookup(snap.public_ip) if snap.public_ip else None
        if loc is None:
            return None
        return replace(loc, label=f"{who} ({_place(loc)})", source="coordinator")

    if ip in snap.own_ips:
        return here("this coordinator")
    if not in_tailnet(ip):
        return here("this coordinator's network")
    peer = snap.peers.get(ip)
    if peer is None:
        return None
    if peer.endpoint:
        if classify(peer.endpoint) == "public":
            loc = lookup(peer.endpoint)
            return replace(loc, label=f"{peer.host} (now in {_place(loc)})") if loc else None
        return here(f"{peer.host}, now on this coordinator's network")
    region = DERP_REGIONS.get(peer.relay or "")
    if region is None:
        return None
    name, lat, lon = region
    return Location(
        lat=lat, lon=lon, city=name, region=None, country=None,
        label=f"{peer.host} (now near the {name} relay)", source="relay",
    )


def _status(binary: str) -> str:
    return subprocess.run(
        [binary, "status", "--json"], capture_output=True, text=True, timeout=_TIMEOUT_S, check=True
    ).stdout


class Tailnet:
    """This machine's tailscaled, asked at most once per :data:`REFRESH_S`.
    Off (``binary`` None) when Tailscale is not installed or switched off."""

    def __init__(self, binary: str | None) -> None:
        self.binary = binary
        self.snapshot: Snapshot | None = None
        self.error: str | None = None
        self._asked = float("-inf")

    @classmethod
    def from_path(cls, enabled: bool) -> Tailnet:
        return cls(shutil.which("tailscale") if enabled else None)

    async def refresh(self) -> None:
        """Ask again when the last answer is stale. Never raises: a failure is
        :attr:`error`, and the last good snapshot is kept."""
        if self.binary is None or time.monotonic() - self._asked < REFRESH_S:
            return
        self._asked = time.monotonic()
        try:
            self.snapshot = parse_status(json.loads(await asyncio.to_thread(_status, self.binary)))
            self.error = None
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
            if self.error is None:
                log.warning("tailscale status failed, tailnet addresses stay unplaced: %s", exc)
            self.error = str(exc) or type(exc).__name__
