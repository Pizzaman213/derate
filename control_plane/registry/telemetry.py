"""Live telemetry: 1 Hz sampling and a 300-sample ring per node.

Two things matter here. The poll must never block on a wedged nvidia-smi, so
every call is an async subprocess under a timeout and the process is killed if
it overruns. And the ring is bounded by construction (a deque with maxlen), so
a generator running for days holds 300 samples per node and not one more.

GB10 needs two fallbacks, because unified memory breaks the usual accounting in
two separate ways.

First, a real DGX Spark reports ``[N/A]`` for every aggregate FB memory field:
memory.used, memory.total, memory.free and memory.reserved. There is no discrete
framebuffer to describe. The unified pool is read from /proc/meminfo instead.
Returning zero would tell the UI the node is idle while it is actually full.

Second, per-process GPU memory *does* work on GB10 even though the aggregate
does not: ``--query-compute-apps=used_gpu_memory`` reports real numbers. That is
the only way to tell how much of the pool is models and how much is the
operating system, and the difference is the whole planning question on this
part. A Spark with 68 GiB of model resident and 25 GiB of desktop has 27 GiB
left, not the 107 GiB that ``usable_memory(0.90)`` implies.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from control_plane.contracts import DEFAULT_GUARDRAIL, DeviceClass, NodeProfile

from .config import HOST_MEMORY_RESERVE, TELEMETRY_RING_SAMPLES
from .probe import _num

log = logging.getLogger(__name__)

TELEMETRY_QUERY = "memory.used,memory.total,power.draw,temperature.gpu,utilization.gpu"
# Per-process GPU memory. Works on GB10 where the aggregate fields do not.
COMPUTE_APPS_QUERY = "pid,used_gpu_memory"
TELEMETRY_TIMEOUT_S = 2.0
MEMINFO = Path("/proc/meminfo")


@dataclass(frozen=True)
class TelemetrySample:
    """One second of a node.

    ``memory_used`` is always "how much of the pool this node plans against is
    spent". On a discrete card that is VRAM. On GB10 it is the unified pool,
    operating system included, because the OS is spending the same bytes the
    model wants.

    ``gpu_memory_used`` is the share of that attributable to GPU contexts. On a
    discrete card the two are the same number. On GB10 the gap between them is
    the operating system, and it is what makes the nameplate misleading.
    """

    ts: float
    memory_used: int  # bytes, from the pool this node plans against
    memory_total: int  # bytes, as reported; 0 when the device will not say
    power_watts: float
    temperature_c: float
    utilization_pct: float
    gpu_memory_used: int = 0  # bytes attributable to GPU contexts
    gpu_process_count: int = 0
    host_memory_total: int = 0  # unified pool, GB10 only
    host_memory_available: int = 0  # kernel's own "allocatable without swapping"
    swap_used: int = 0

    @property
    def host_memory_used(self) -> int:
        """Pool bytes spent on something other than a GPU context."""
        if not self.host_memory_total:
            return 0
        return max(0, self.memory_used - self.gpu_memory_used)

    def as_dict(self) -> dict:
        return {
            "ts": self.ts,
            "memory_used": self.memory_used,
            "memory_total": self.memory_total,
            "power_w": round(self.power_watts, 1),
            "temp_c": round(self.temperature_c, 1),
            "util_pct": round(self.utilization_pct, 1),
            "gpu_memory_used": self.gpu_memory_used,
            "gpu_process_count": self.gpu_process_count,
            "host_memory_total": self.host_memory_total,
            "host_memory_available": self.host_memory_available,
            "swap_used": self.swap_used,
        }


async def run_nvidia_smi_async(
    query: str,
    timeout: float = TELEMETRY_TIMEOUT_S,
    flag: str = "--query-gpu",
) -> list[list[str]] | None:
    """Async nvidia-smi query under a hard timeout. Returns None on any failure.

    ``flag`` selects the query family: --query-gpu for per-device fields,
    --query-compute-apps for per-process ones. They are not interchangeable and
    passing a compute-apps field list to --query-gpu is simply rejected.

    A slow nvidia-smi is killed rather than awaited. The poll loop keeps its
    cadence and the previous sample stays on screen, which is the correct
    failure: stale numbers beat a stalled UI.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi",
            f"{flag}={query}",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as exc:
        log.debug("nvidia-smi unavailable: %s", exc)
        return None

    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        log.warning("nvidia-smi exceeded %.1fs, killing it", timeout)
        try:
            proc.kill()
            await proc.wait()
        except (ProcessLookupError, OSError):
            pass
        return None

    if proc.returncode != 0:
        return None
    rows = [
        [cell.strip() for cell in line.split(",")]
        for line in stdout.decode(errors="replace").splitlines()
        if line.strip()
    ]
    return rows or None


@dataclass(frozen=True)
class HostMemory:
    """The unified pool as the kernel sees it."""

    total: int
    available: int
    swap_used: int

    @property
    def used(self) -> int:
        return max(0, self.total - self.available)


def read_host_memory() -> HostMemory | None:
    """The unified pool, from /proc/meminfo.

    MemAvailable, not MemFree: page cache is reclaimable, and counting it as
    spent would read as permanently full. MemAvailable is also the kernel's own
    estimate of what can be allocated without swapping, which is exactly the
    question a fit check is asking on this hardware.
    """
    try:
        text = MEMINFO.read_text()
    except OSError:
        return None
    fields: dict[str, int] = {}
    wanted = {"MemTotal:", "MemAvailable:", "SwapTotal:", "SwapFree:"}
    for line in text.splitlines():
        key = line.split(":")[0] + ":"
        if key in wanted:
            value = _num(line.split()[1])
            if value is not None:
                fields[key] = int(value) * 1024
    if "MemTotal:" not in fields or "MemAvailable:" not in fields:
        return None
    swap_used = max(0, fields.get("SwapTotal:", 0) - fields.get("SwapFree:", 0))
    return HostMemory(
        total=fields["MemTotal:"],
        available=fields["MemAvailable:"],
        swap_used=swap_used,
    )


def read_unified_memory() -> tuple[int, int] | None:
    """(used, total) bytes of the unified pool. Kept for callers that want the pair."""
    host = read_host_memory()
    return (host.used, host.total) if host else None


async def read_compute_apps(
    rows: list[list[str]] | None = None,
) -> tuple[int, int] | None:
    """(bytes, process_count) of GPU memory held by compute contexts.

    This is the one memory number nvidia-smi will still give you on GB10, and
    it is what separates "68 GiB of model" from "25 GiB of desktop". Graphics
    contexts (Xorg, the shell) are not covered by this query; they are tens of
    MiB and they are accounted for in the pool figure regardless.
    """
    if rows is None:
        rows = await run_nvidia_smi_async(
            COMPUTE_APPS_QUERY, flag="--query-compute-apps"
        )
    if rows is None:
        # No compute contexts at all is an empty list, not a failure; only a
        # broken or absent nvidia-smi gets here.
        return None
    total = 0
    count = 0
    for row in rows:
        if len(row) < 2:
            continue
        mib = _num(row[1])
        if mib is None:
            continue
        total += int(mib) * 1024**2
        count += 1
    return total, count


async def read_telemetry(
    profile: NodeProfile,
    now: float | None = None,
    rows: list[list[str]] | None = None,
    apps_rows: list[list[str]] | None = None,
) -> TelemetrySample | None:
    """Sample the local GPUs. None when nothing could be read.

    Aggregation across multiple GPUs: memory and power sum, temperature takes
    the hottest, utilisation averages. A node is one planning unit, so it needs
    one number per metric.
    """
    ts = time.time() if now is None else now
    if rows is None:
        rows = await run_nvidia_smi_async(TELEMETRY_QUERY)
    if not rows:
        return None

    used_mib: list[float] = []
    total_mib: list[float] = []
    power: list[float] = []
    temps: list[float] = []
    utils: list[float] = []

    for row in rows:
        cells = (row + [""] * 5)[:5]
        for cell, sink in zip(cells, (used_mib, total_mib, power, temps, utils)):
            value = _num(cell)
            if value is not None:
                sink.append(value)

    memory_used = int(sum(used_mib)) * 1024**2 if used_mib else 0
    memory_total = int(sum(total_mib)) * 1024**2 if total_mib else 0

    gpu_memory_used = memory_used
    gpu_process_count = 0
    host_total = host_available = swap_used = 0

    is_unified = profile.device_class is DeviceClass.GB10
    if is_unified or not used_mib:
        apps = await read_compute_apps(rows=apps_rows)
        if apps is not None:
            gpu_memory_used, gpu_process_count = apps

    if is_unified:
        host = read_host_memory()
        if host is not None:
            host_total = host.total
            host_available = host.available
            swap_used = host.swap_used
            # The pool is what this node plans against, operating system
            # included: the OS is spending the same bytes the model wants.
            memory_used = host.used
            memory_total = memory_total or host.total
        elif not used_mib:
            memory_used = gpu_memory_used

    return TelemetrySample(
        ts=ts,
        memory_used=memory_used,
        memory_total=memory_total,
        power_watts=sum(power) if power else 0.0,
        temperature_c=max(temps) if temps else 0.0,
        utilization_pct=(sum(utils) / len(utils)) if utils else 0.0,
        gpu_memory_used=gpu_memory_used,
        gpu_process_count=gpu_process_count,
        host_memory_total=host_total,
        host_memory_available=host_available,
        swap_used=swap_used,
    )


def allocatable_bytes(
    profile: NodeProfile,
    sample: TelemetrySample | None,
    guardrail: float = DEFAULT_GUARDRAIL,
    host_reserve: int = HOST_MEMORY_RESERVE,
) -> int:
    """How much memory a new model could actually get on this node, right now.

    ``NodeProfile.usable_memory`` answers a different and static question: what
    this hardware could ever spend, with nothing else running. That is the right
    ceiling and the wrong number to launch against, because on GB10 it overstates
    what is available by whatever the operating system is currently holding.

    Discrete cards keep the simple answer: VRAM is its own pool and host RAM does
    not constrain it.

    Unified parts take the smaller of two limits. The GPU cannot reach past its
    addressable slice, and the pool cannot surrender more than the kernel says is
    allocatable without swapping. Swapping a model is not slow, it is fatal, so
    the second limit is the one that usually binds.
    """
    ceiling = profile.usable_memory(guardrail)
    if sample is None:
        return ceiling

    gpu_headroom = max(0, ceiling - sample.memory_used)
    if profile.device_class is not DeviceClass.GB10:
        return gpu_headroom
    if sample.host_memory_available <= 0:
        return gpu_headroom

    # MemAvailable already accounts for what the GPU is holding, so this is not
    # double counting: it is the same headroom seen from the pool's side.
    host_headroom = max(0, sample.host_memory_available - host_reserve)
    return min(gpu_headroom, host_headroom)


@dataclass
class RingBuffer:
    """Bounded history for one node. 300 samples at 1 Hz is 5 minutes."""

    maxlen: int = TELEMETRY_RING_SAMPLES
    _samples: deque[TelemetrySample] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._samples = deque(maxlen=self.maxlen)

    def add(self, sample: TelemetrySample) -> None:
        self._samples.append(sample)

    @property
    def latest(self) -> TelemetrySample | None:
        return self._samples[-1] if self._samples else None

    def window(self, seconds: float, now: float | None = None) -> list[TelemetrySample]:
        now = time.time() if now is None else now
        cutoff = now - seconds
        return [s for s in self._samples if s.ts >= cutoff]

    def __len__(self) -> int:
        return len(self._samples)


class TelemetryStore:
    """One ring per node. Buffers are dropped when a node is removed.

    Not when it goes unhealthy: an unhealthy node keeps its last known numbers
    so the UI can grey them and say what happened, rather than showing zeros.
    """

    def __init__(self, maxlen: int = TELEMETRY_RING_SAMPLES) -> None:
        self._maxlen = maxlen
        self._rings: dict[str, RingBuffer] = {}

    def record(self, node_id: str, sample: TelemetrySample) -> None:
        ring = self._rings.get(node_id)
        if ring is None:
            ring = self._rings[node_id] = RingBuffer(maxlen=self._maxlen)
        ring.add(sample)

    def latest(self, node_id: str) -> TelemetrySample | None:
        ring = self._rings.get(node_id)
        return ring.latest if ring else None

    def history(
        self, node_id: str, seconds: float = 60.0, now: float | None = None
    ) -> list[dict]:
        ring = self._rings.get(node_id)
        if ring is None:
            return []
        return [s.as_dict() for s in ring.window(seconds, now=now)]

    def drop(self, node_id: str) -> None:
        self._rings.pop(node_id, None)

    def size(self, node_id: str) -> int:
        ring = self._rings.get(node_id)
        return len(ring) if ring else 0

    def node_ids(self) -> list[str]:
        return list(self._rings)
