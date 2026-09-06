"""Hardware and topology contracts. 00-architecture.md section 4.1.

Day-0 file. Transcribed from the architecture doc, not designed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .constants import DEFAULT_GUARDRAIL


class DeviceClass(str, Enum):
    GB10 = "gb10"  # DGX Spark, unified memory
    DISCRETE = "discrete"  # RTX 3090 and similar
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class NodeProfile:
    node_id: str
    hostname: str
    address: str  # management IP
    device_class: DeviceClass
    gpu_name: str
    gpu_count: int
    total_memory: int  # bytes
    addressable_memory: int  # bytes reachable by the GPU
    memory_bandwidth_gbps: float
    compute_capability: str
    driver_version: str

    def usable_memory(self, guardrail: float = DEFAULT_GUARDRAIL) -> int:
        return int(self.addressable_memory * guardrail)


@dataclass
class NodeState:
    profile: NodeProfile
    healthy: bool
    last_seen: float  # unix ts
    memory_used: int  # bytes, live
    power_watts: float
    temperature_c: float
    utilization_pct: float


@dataclass(frozen=True)
class LinkMeasurement:
    src: str  # node_id
    dst: str  # node_id
    all_reduce_gbps: float  # what governs tensor parallel
    sendrecv_gbps: float  # what governs pipeline handoff and KV transfer
    latency_us: float
    gpudirect_rdma: bool
    measured_at: float
    method: str  # "nccl-tests" | "ib_write_bw" | "manual"
