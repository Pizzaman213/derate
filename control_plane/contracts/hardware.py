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

    def describe(self) -> str:
        """``node_id (gpu_name)``, or bare ``node_id`` when there is no GPU.

        Three separate modules build this string for reason lines, and all
        three used to print an empty ``()`` for a machine the probe found no
        GPU on -- ``unknown_profile()`` zeroes ``gpu_name`` rather than
        inventing a placeholder. Empty parentheses read as a missing value in a
        sentence that is otherwise about hardware, so the parentheses go away
        with the name.
        """
        return f"{self.node_id} ({self.gpu_name})" if self.gpu_name else self.node_id


@dataclass
class NodeState:
    profile: NodeProfile
    healthy: bool
    last_seen: float  # unix ts
    memory_used: int  # bytes, live
    power_watts: float
    temperature_c: float
    utilization_pct: float
    # Live denominator for memory_used, from the sample rather than the probe.
    # A machine with no GPU has profile.total_memory 0 -- there is no GPU memory
    # to describe -- but it does have host RAM, and with no total to divide by
    # its memory readout is a permanent em dash. Additive to section 4.1, and
    # deliberately NOT folded into profile.total_memory: that field is summed
    # into cluster-wide totals, where host RAM no model can reach does not
    # belong. 0 means nothing has been sampled yet.
    memory_total: int = 0  # bytes, live
    # unix ts of the last applied telemetry sample; 0.0 when there has never
    # been one. Deliberately not ``last_seen``: the health loop refreshes that
    # on every answered /agent/health, so a node whose nvidia-smi is gone --
    # a container started without --gpus, say -- keeps a fresh last_seen while
    # the four live readings below are frozen at whatever they last were. Two
    # different questions ("is it reachable" / "is this number current") need
    # two different timestamps, or the UI shows an hour-old wattage as live.
    sample_ts: float = 0.0
    # Whether this is the machine the coordinator is itself running on. Set by
    # Registry.enroll_local and by nothing else, so it is false on every node
    # that arrived over the network -- including on a worker's own copy of its
    # own state, which is correct: the question is "is this the coordinator's
    # host", not "is this me".
    #
    # On NodeState rather than NodeProfile, for the reason `label` is: the
    # profile is the hardware as probed, and a re-probe must not carry a fact
    # about which process happens to be serving. It is also not derivable from
    # the profile at all -- move the coordinator to another machine and the
    # hardware is unchanged while this flips.
    is_local: bool = False
    # Which build of derate this node is running, from its /agent/health. "" on
    # a node that has not answered yet, or one whose build predates the field.
    #
    # Not on NodeProfile, and the distinction is the entire point: the profile
    # is what the hardware IS, and this is what was doing the looking. They
    # were conflated by absence until a Raspberry Pi running an old image
    # reported itself as unidentified hardware, and the roster had no way to
    # say "the machine is fine, the software is stale".
    build: str = ""


@dataclass(frozen=True)
class GpuProcess:
    """One compute context holding GPU memory, as nvidia-smi reports it.

    Additive to section 4.1, and deliberately not a field on ``TelemetrySample``:
    this is read on demand when an operator opens a node, never on the 5s poll,
    so the durable journal does not carry a process list nobody reads.

    ``command`` and ``user`` are best-effort from /proc and are None when the
    entry could not be read -- a container without ``--pid=host`` sees neither
    these processes nor their /proc entries, and inventing a name for one would
    be worse than admitting we could not look.
    """

    pid: int
    name: str  # basename, for display
    command: str | None  # full /proc cmdline, truncated
    user: str | None
    gpu_memory: int  # bytes

    def as_dict(self) -> dict:
        return {
            "pid": self.pid,
            "name": self.name,
            "command": self.command,
            "user": self.user,
            "gpu_memory": self.gpu_memory,
        }


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
