"""Day-0 stub. Returns the fixture nodes so downstream agents are never blocked.

Deleted at integration. Everything here is static: no probing, no HTTP, no
background loops. It exists so that D, E, F, G and H can import a RegistryPort
and get valid contract types out of it within the first hour.

The profiles come from ``tests/fixtures`` when that package is importable, so
the stub cannot drift from the frozen fixtures. Inside the container image,
where tests are not shipped, it falls back to an inline copy.
"""

from __future__ import annotations

import asyncio
import time
from typing import AsyncIterator

from control_plane.contracts import (
    GB10_ADDRESSABLE,
    GB10_MEM_BANDWIDTH,
    GB10_TOTAL_MEMORY,
    DeviceClass,
    NodeProfile,
    NodeState,
)

from .config import ROLE_COORDINATOR, TELEMETRY_INTERVAL_S
from .errors import NodeNotFound
from .serde import memory_used_pct
from .telemetry import TelemetryStore, TelemetrySample

STUB_TOKEN = "stub-token-not-a-real-cluster"
STUB_CLUSTER_ID = "c-stub"

_FALLBACK = (
    NodeProfile(
        node_id="spark-01",
        hostname="spark-01",
        address="192.168.11.13",
        device_class=DeviceClass.GB10,
        gpu_name="NVIDIA GB10",
        gpu_count=1,
        total_memory=GB10_TOTAL_MEMORY,
        addressable_memory=GB10_ADDRESSABLE,
        memory_bandwidth_gbps=GB10_MEM_BANDWIDTH,
        compute_capability="12.1",
        driver_version="580.95.05",
    ),
    NodeProfile(
        node_id="spark-02",
        hostname="spark-02",
        address="192.168.11.14",
        device_class=DeviceClass.GB10,
        gpu_name="NVIDIA GB10",
        gpu_count=1,
        total_memory=GB10_TOTAL_MEMORY,
        addressable_memory=GB10_ADDRESSABLE,
        memory_bandwidth_gbps=GB10_MEM_BANDWIDTH,
        compute_capability="12.1",
        driver_version="580.95.05",
    ),
    NodeProfile(
        node_id="ws-3090",
        hostname="workstation",
        address="192.168.11.20",
        device_class=DeviceClass.DISCRETE,
        gpu_name="NVIDIA GeForce RTX 3090",
        gpu_count=1,
        total_memory=24 * 1024**3,
        addressable_memory=23 * 1024**3,
        memory_bandwidth_gbps=936.0,
        compute_capability="8.6",
        driver_version="580.95.05",
    ),
)

# Plausible steady state, matching the topology example in the architecture:
# two Sparks busy on a shared deployment, a 3090 mostly idle.
_TELEMETRY: dict[str, tuple[float, float, float, float]] = {
    # node_id: (memory_used_fraction, power_w, temp_c, util_pct)
    "spark-01": (0.78, 71.0, 62.0, 94.0),
    "spark-02": (0.76, 68.0, 60.0, 91.0),
    "ws-3090": (0.31, 210.0, 68.0, 22.0),
}


def fixture_profiles() -> tuple[NodeProfile, ...]:
    try:
        from tests.fixtures import SPARK_01, SPARK_02, WS_3090

        return (SPARK_01, SPARK_02, WS_3090)
    except Exception:
        return _FALLBACK


class StubRegistry:
    """A RegistryPort that always answers, and never touches the network."""

    def __init__(self, profiles: tuple[NodeProfile, ...] | None = None) -> None:
        self._profiles = profiles or fixture_profiles()
        now = time.time()
        self._members: dict[str, NodeState] = {}
        self._telemetry = TelemetryStore()
        for profile in self._profiles:
            fraction, power, temp, util = _TELEMETRY.get(
                profile.node_id, (0.5, 100.0, 55.0, 40.0)
            )
            used = int(profile.addressable_memory * fraction)
            self._members[profile.node_id] = NodeState(
                profile=profile,
                healthy=True,
                last_seen=now,
                memory_used=used,
                power_watts=power,
                temperature_c=temp,
                utilization_pct=util,
                sample_ts=now,
            )
            # Seed a full ring so history() answers on the first call.
            for i in range(60):
                self._telemetry.record(
                    profile.node_id,
                    TelemetrySample(
                        ts=now - (59 - i),
                        memory_used=used,
                        memory_total=profile.total_memory,
                        power_watts=power,
                        temperature_c=temp,
                        utilization_pct=util,
                    ),
                )

    # RegistryPort
    def list_nodes(self) -> list[NodeState]:
        return list(self._members.values())

    def get_node(self, node_id: str) -> NodeState | None:
        return self._members.get(node_id)

    def healthy_nodes(self) -> list[NodeState]:
        return [s for s in self._members.values() if s.healthy]

    # Beyond the port
    async def add_node(self, address: str) -> NodeState:
        raise NotImplementedError("stub registry cannot probe an address")

    def admit(self, node_id: str) -> NodeState:
        state = self._members.get(node_id)
        if state is None:
            raise NodeNotFound(node_id)
        return state

    def remove_node(self, node_id: str) -> None:
        self._members.pop(node_id, None)
        self._telemetry.drop(node_id)

    def candidates(self) -> list[dict]:
        return []

    async def handle_join(self, token, profile, agent_url) -> dict:
        raise NotImplementedError("stub registry does not accept joins")

    def role(self) -> str:
        return ROLE_COORDINATOR

    def cluster_token(self) -> str:
        return STUB_TOKEN

    def cluster_id(self) -> str:
        return STUB_CLUSTER_ID

    def history(self, node_id: str, seconds: int = 60) -> list[dict]:
        return self._telemetry.history(node_id, seconds)

    def snapshot(self) -> dict:
        nodes = []
        total_power = 0.0
        for state in self._members.values():
            total_power += state.power_watts
            nodes.append(
                {
                    "node_id": state.profile.node_id,
                    "power_w": state.power_watts,
                    "temp_c": state.temperature_c,
                    "memory_used_pct": memory_used_pct(state),
                    "util_pct": state.utilization_pct,
                    "healthy": state.healthy,
                    "last_seen": state.last_seen,
                    "sample_ts": state.sample_ts or None,
                }
            )
        return {
            "ts": time.time(),
            "cluster": {
                "total_power_w": round(total_power, 1),
                "node_count": len(self._members),
                "healthy_nodes": len(self._members),
                "candidates": 0,
            },
            "nodes": nodes,
        }

    async def snapshots(self, interval: float = TELEMETRY_INTERVAL_S) -> AsyncIterator[dict]:
        while True:
            yield self.snapshot()
            await asyncio.sleep(interval)
