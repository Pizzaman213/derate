"""Network facts the node agent needs before it can do anything useful.

Two jobs: find our management address, and refuse to start on a bridge network.
mDNS does not cross a Docker bridge, so a bridged container would discover
nothing, forever, silently. Failing at startup with the fix in the message is
the whole point.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
from pathlib import Path

from ..netliteral import is_numeric_host_literal
from .errors import BridgeNetworkError

log = logging.getLogger(__name__)

# A DNS name: labels of letters, digits and inner hyphens. Never starts with
# "-", so an address can never be read as an ssh option.
HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)


def valid_host(value: str) -> bool:
    """An IP literal or a hostname: the only things safe as an ssh target."""
    if not value or value.startswith("-"):
        return False
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return bool(HOSTNAME_RE.match(value))


_LINK_LOCAL = (ipaddress.ip_network("169.254.0.0/16"), ipaddress.ip_network("fe80::/10"))


def is_peer_address(host: str) -> bool:
    """Whether *host* can name another machine on the network.

    An IP literal is refused when it is loopback, link-local (which holds the
    cloud metadata address), multicast, unspecified or reserved; a hostname
    passes iff :func:`valid_host`. Loopback is refused because every caller
    means "a different machine" -- an agent URL that names the coordinator's
    own loopback is an SSRF into it, not a peer. No DNS resolution: that
    would make the answer depend on whoever runs the resolver.
    """
    if not host or is_numeric_host_literal(host):
        return False
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        name = host.lower().rstrip(".")
        if name == "localhost" or name.endswith(".localhost"):
            return False
        return valid_host(host)
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return False
    return not any(ip in net for net in _LINK_LOCAL if net.version == ip.version)


def _as_ip(value: str):
    try:
        ip = ipaddress.ip_address(value.strip("[]"))
    except ValueError:
        return None
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped or ip


def same_host(a: str | None, b: str | None) -> bool:
    """Whether two host strings name the same machine, without DNS.

    IP literals compare as addresses (so ``::ffff:10.0.0.5`` is ``10.0.0.5``
    and IPv6 spellings agree); anything else compares case-insensitively.
    """
    if not a or not b:
        return False
    ia, ib = _as_ip(a), _as_ip(b)
    if ia is not None or ib is not None:
        return ia is not None and ia == ib
    return a.lower().rstrip(".") == b.lower().rstrip(".")


def is_local_address(host: str | None) -> bool:
    """Whether *host* is an address of this machine: loopback, or an IP
    literal one of our interfaces holds (binding to it succeeds). No DNS and
    no packets -- a bind to port 0 asks the kernel and nothing else."""
    ip = _as_ip(host or "")
    if ip is None:
        return False
    if ip.is_loopback:
        return True
    family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.bind((str(ip), 0))
        return True
    except OSError:
        return False


BRIDGE_MESSAGE = (
    "Bridge networking detected. mDNS discovery cannot cross a Docker bridge, "
    "so this node would never find or be found by a coordinator.\n"
    "Start the container with host networking:\n"
    "    docker run --network host -v derate:/data ghcr.io/pizzaman213/derate/node\n"
    "If you are certain this is wrong, set DERATE_ALLOW_BRIDGE=1 to skip "
    "this check. Discovery will not work; use DERATE_JOIN=<addr> instead."
)

# Interfaces a default-bridge container sees. If a container sees these and
# nothing else, it is bridged: with --network host it would see the host's real
# interfaces (docker0, enp*, wl*) as well.
_BRIDGE_ONLY_INTERFACES = {
    "eth0",
    "tunl0",
    "sit0",
    "ip6tnl0",
    "gre0",
    "gretap0",
    "erspan0",
}


def list_interfaces() -> list[str]:
    try:
        return sorted(os.listdir("/sys/class/net"))
    except OSError:
        return []


def in_container() -> bool:
    """True when we are inside a container runtime."""
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text()
    except OSError:
        return False
    return any(m in cgroup for m in ("docker", "containerd", "kubepods", "libpod"))


def detect_bridge_networking(
    interfaces: list[str] | None = None, container: bool | None = None
) -> bool:
    """True when we appear to be on a container bridge network.

    Both arguments are injection points for tests. On a host (not a container)
    this is always False: bare-metal has no bridge to be trapped behind.
    """
    container = in_container() if container is None else container
    if not container:
        return False
    interfaces = list_interfaces() if interfaces is None else interfaces
    non_loopback = {i for i in interfaces if i != "lo"}
    if not non_loopback:
        # No interfaces at all is a different problem, and not one this check
        # should claim to have diagnosed.
        return False
    return non_loopback <= _BRIDGE_ONLY_INTERFACES


def require_host_networking(
    allow_bridge: bool = False,
    interfaces: list[str] | None = None,
    container: bool | None = None,
) -> None:
    """Raise BridgeNetworkError unless we can actually do mDNS."""
    if allow_bridge:
        log.warning("DERATE_ALLOW_BRIDGE set: skipping the host-network check")
        return
    if detect_bridge_networking(interfaces=interfaces, container=container):
        raise BridgeNetworkError(BRIDGE_MESSAGE)


def primary_address() -> str:
    """The address other nodes should reach us on.

    Opens a UDP socket toward a public address to learn which local interface
    the routing table would pick. No packets are sent and nothing is contacted.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return str(sock.getsockname()[0])
    except OSError:
        pass
    finally:
        sock.close()
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


def normalize_agent_url(address: str, default_port: int) -> str:
    """Accept 'host', 'host:port', or a full URL and return a base URL.

    Manual add is a form a human types into. It should not care about scheme.
    """
    address = address.strip().rstrip("/")
    if not address:
        raise ValueError("empty address")
    if address.startswith(("http://", "https://")):
        return address
    if ":" in address and not address.count(":") > 1:  # host:port, not IPv6
        return f"http://{address}"
    return f"http://{address}:{default_port}"
