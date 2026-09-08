"""mDNS advertisement and browsing over the management LAN.

Advertises ``_derate._tcp.local.`` with role, cluster_id and node_id in TXT,
so a browsing node can tell a coordinator from a worker before it tries to join.

zeroconf is a soft import. The package must stay importable, and every unit test
must stay runnable, on a machine without it; discovery then degrades to "found
nothing", which is the same path as an empty network and is already handled.

This is management-LAN discovery only. ConnectX-7 subnets, the SSH mesh, and
fabric setup belong to sparkrun and NVIDIA Sync, not here.
"""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass

from .config import MDNS_BROWSE_SECONDS, MDNS_SERVICE_TYPE

log = logging.getLogger(__name__)

try:
    from zeroconf import ServiceBrowser, ServiceInfo, ServiceListener, Zeroconf

    HAVE_ZEROCONF = True
except ImportError:  # pragma: no cover - exercised by absence, not by tests
    HAVE_ZEROCONF = False
    ServiceBrowser = ServiceInfo = ServiceListener = Zeroconf = None  # type: ignore

ZEROCONF_MISSING = (
    "zeroconf is not installed, so mDNS discovery is disabled. Nodes will not "
    "find each other automatically. Install it, or use DERATE_JOIN=<addr>."
)


@dataclass(frozen=True)
class DiscoveredPeer:
    node_id: str
    role: str
    cluster_id: str
    address: str
    port: int

    @property
    def agent_url(self) -> str:
        return f"http://{self.address}:{self.port}"

    @property
    def is_coordinator(self) -> bool:
        return self.role == "coordinator"


def _txt(props: dict, key: str) -> str:
    """TXT values arrive as bytes. Missing keys are empty, never None."""
    value = props.get(key.encode()) or props.get(key)
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value) if value is not None else ""


class Advertiser:
    """Registers this node on mDNS for its process lifetime.

    Withdraws on stop. A stale advertisement outlives the process otherwise,
    and the next node to browse would try to join a coordinator that is gone.
    """

    def __init__(
        self,
        node_id: str,
        role: str,
        cluster_id: str,
        address: str,
        port: int,
        service_type: str = MDNS_SERVICE_TYPE,
    ) -> None:
        self.node_id = node_id
        self.role = role
        self.cluster_id = cluster_id
        self.address = address
        self.port = port
        self.service_type = service_type
        self._zc = None
        self._info = None

    @property
    def active(self) -> bool:
        return self._info is not None

    def start(self) -> bool:
        """Register. False when zeroconf is unavailable or registration fails."""
        if not HAVE_ZEROCONF:
            log.warning(ZEROCONF_MISSING)
            return False
        if self._info is not None:
            return True
        try:
            info = ServiceInfo(
                self.service_type,
                f"{self.node_id}.{self.service_type}",
                addresses=[socket.inet_aton(self.address)],
                port=self.port,
                properties={
                    "role": self.role,
                    "cluster_id": self.cluster_id,
                    "node_id": self.node_id,
                },
                server=f"{self.node_id}.local.",
            )
            self._zc = Zeroconf()
            self._zc.register_service(info)
            self._info = info
            log.info(
                "advertising %s as role=%s on %s:%s",
                self.node_id,
                self.role,
                self.address,
                self.port,
            )
            return True
        except Exception as exc:
            log.warning("mDNS advertisement failed: %s", exc)
            self.stop()
            return False

    def readvertise(self, address: str, role: str | None = None) -> bool:
        """Re-register at a new address. False when nothing needed doing.

        The record is built once, in ``start()``, from the address this node had
        at boot. A machine that changes IP -- a new DHCP lease, a move to another
        subnet, a second interface winning the default route -- therefore kept
        advertising an address it no longer answers on, and the next node to
        browse would try to join a coordinator that is not there. Withdrawing and
        re-registering is the only way: ``start()`` returns early while ``_info``
        is set, so calling it again is a no-op by itself.
        """
        if address == self.address and (role is None or role == self.role):
            return False
        log.info(
            "re-advertising %s at %s (was %s)", self.node_id, address, self.address
        )
        was_active = self.active
        self.stop()
        self.address = address
        if role is not None:
            self.role = role
        # Only re-register if we were registered: an advertiser that never
        # started (no zeroconf, or a failed bind) must not be started by a
        # change of address it was not announcing in the first place.
        return self.start() if was_active else False

    def stop(self) -> None:
        if self._zc is not None:
            try:
                if self._info is not None:
                    self._zc.unregister_service(self._info)
                self._zc.close()
            except Exception as exc:  # shutdown must not raise
                log.debug("mDNS withdrawal failed: %s", exc)
        self._zc = None
        self._info = None


if HAVE_ZEROCONF:

    class _Collector(ServiceListener):  # type: ignore[misc]
        def __init__(self) -> None:
            self.peers: dict[str, DiscoveredPeer] = {}

        def _record(self, zc, service_type: str, name: str) -> None:
            try:
                info = zc.get_service_info(service_type, name, timeout=1500)
            except Exception:
                return
            if info is None:
                return
            addresses = info.parsed_addresses() if hasattr(info, "parsed_addresses") else []
            if not addresses:
                return
            props = info.properties or {}
            node_id = _txt(props, "node_id") or name.split(".")[0]
            self.peers[node_id] = DiscoveredPeer(
                node_id=node_id,
                role=_txt(props, "role") or "unknown",
                cluster_id=_txt(props, "cluster_id"),
                address=addresses[0],
                port=int(info.port or 0),
            )

        def add_service(self, zc, service_type, name):
            self._record(zc, service_type, name)

        def update_service(self, zc, service_type, name):
            self._record(zc, service_type, name)

        def remove_service(self, zc, service_type, name):
            pass


def browse(
    seconds: float = MDNS_BROWSE_SECONDS,
    exclude_node_id: str | None = None,
    service_type: str = MDNS_SERVICE_TYPE,
) -> list[DiscoveredPeer]:
    """Browse for peers. Blocking, bounded by ``seconds``. Never raises.

    Returns [] when zeroconf is missing or the network is empty. The caller
    cannot tell those apart, and does not need to: both mean "no coordinator",
    and the answer to that is to become one.
    """
    if not HAVE_ZEROCONF:
        log.warning(ZEROCONF_MISSING)
        return []

    import time as _time

    zc = None
    try:
        zc = Zeroconf()
        collector = _Collector()
        ServiceBrowser(zc, service_type, collector)
        _time.sleep(seconds)
        peers = list(collector.peers.values())
    except Exception as exc:
        log.warning("mDNS browse failed: %s", exc)
        return []
    finally:
        if zc is not None:
            try:
                zc.close()
            except Exception:
                pass
    if exclude_node_id:
        peers = [p for p in peers if p.node_id != exclude_node_id]
    return peers


async def browse_async(
    seconds: float = MDNS_BROWSE_SECONDS,
    exclude_node_id: str | None = None,
    service_type: str = MDNS_SERVICE_TYPE,
) -> list[DiscoveredPeer]:
    """browse() off the event loop, so a 3 s browse does not stall the agent."""
    import asyncio

    return await asyncio.to_thread(browse, seconds, exclude_node_id, service_type)
