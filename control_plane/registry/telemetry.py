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

from control_plane.contracts import (
    DEFAULT_GUARDRAIL,
    DeviceClass,
    GpuProcess,
    NodeProfile,
)

from .config import HOST_MEMORY_RESERVE, TELEMETRY_RING_SAMPLES
from .probe import _num

log = logging.getLogger(__name__)

TELEMETRY_QUERY = "memory.used,memory.total,power.draw,temperature.gpu,utilization.gpu"
# Per-process GPU memory. Works on GB10 where the aggregate fields do not.
COMPUTE_APPS_QUERY = "pid,used_gpu_memory"
# The same family with the name attached. Deliberately a second constant:
# COMPUTE_APPS_QUERY feeds the 5s poll and its column order is load-bearing
# for gpu_memory_used, so it is not extended in place.
PROCESS_QUERY = "pid,process_name,used_gpu_memory"
# Long enough that a vLLM serve line keeps its --port, which is what the
# coordinator attributes a process to a deployment by. Truncating below
# that would silently turn a managed backend into an unattributed one.
CMDLINE_MAX = 2000
TELEMETRY_TIMEOUT_S = 2.0
MEMINFO = Path("/proc/meminfo")
THERMAL_ZONES = Path("/sys/class/thermal")
PROC_STAT = Path("/proc/stat")
# A zone reporting outside this range is handing back a sentinel rather than a
# temperature. Unpopulated sensors read 0 or -274 on plenty of boards.
TEMP_MIN_C = 1.0
TEMP_MAX_C = 150.0
# How long the first CPU reading waits for something to difference against.
# /proc/stat is cumulative, so one reading carries no rate at all.
CPU_FIRST_DELTA_S = 0.12


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
    allow_empty: bool = False,
) -> list[list[str]] | None:
    """Async nvidia-smi query under a hard timeout. Returns None on any failure.

    ``flag`` selects the query family: --query-gpu for per-device fields,
    --query-compute-apps for per-process ones. They are not interchangeable and
    passing a compute-apps field list to --query-gpu is simply rejected.

    ``allow_empty`` distinguishes "ran fine, printed nothing" from "failed". An
    empty --query-gpu is a broken probe; an empty --query-compute-apps is an
    idle GPU, which is a real answer and must not be read as a failure.

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
    if rows:
        return rows
    return [] if allow_empty else None


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


def read_host_temperature() -> float | None:
    """The hottest thermal zone in degrees C, or None when there are none.

    Same aggregation rule as the GPU path: a node is one planning unit, so it
    reports its hottest sensor rather than an average that hides a hot one. A
    machine exposing no /sys/class/thermal at all returns None, which is
    reported as unknown -- never as a cold 0, which reads like a measurement.
    """
    try:
        zones = sorted(THERMAL_ZONES.glob("thermal_zone*/temp"))
    except OSError:
        return None
    readings: list[float] = []
    for zone in zones:
        try:
            milli = _num(zone.read_text())
        except OSError:
            continue
        if milli is None:
            continue
        celsius = milli / 1000.0
        if TEMP_MIN_C <= celsius <= TEMP_MAX_C:
            readings.append(celsius)
    return max(readings) if readings else None


@dataclass(frozen=True)
class _CpuTimes:
    """The aggregate cpu line of /proc/stat, split into busy and idle."""

    busy: int
    total: int


def read_cpu_times() -> _CpuTimes | None:
    try:
        first = PROC_STAT.read_text().split("\n", 1)[0]
    except OSError:
        return None
    parts = first.split()
    if len(parts) < 5 or parts[0] != "cpu":
        return None
    try:
        values = [int(cell) for cell in parts[1:]]
    except ValueError:
        return None
    # Fields 3 and 4 are idle and iowait; everything else is the CPU doing
    # something. iowait counts as idle because the CPU is available.
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    return _CpuTimes(busy=max(0, total - idle), total=total)


_last_cpu_times: _CpuTimes | None = None


async def read_cpu_utilization() -> float | None:
    """Busy percentage of the whole CPU since the previous call.

    /proc/stat is cumulative, so a single reading carries no rate. Every poll
    after the first differences against the one before it, which at the sample
    cadence is exactly the window the UI draws. The first call has nothing to
    difference against and takes its own short delta rather than reporting a
    fabricated 0%.
    """
    global _last_cpu_times
    current = read_cpu_times()
    if current is None:
        return None
    previous = _last_cpu_times
    _last_cpu_times = current
    if previous is None or current.total <= previous.total:
        # Nothing to difference against: either the first call ever, or one
        # soon enough after the last that the counters have not ticked. Take a
        # short delta of our own rather than reporting a fabricated 0%.
        await asyncio.sleep(CPU_FIRST_DELTA_S)
        previous, current = current, read_cpu_times()
        if current is None:
            return None
        _last_cpu_times = current
    span = current.total - previous.total
    if span <= 0:
        return None
    busy = current.busy - previous.busy
    return max(0.0, min(100.0, busy / span * 100.0))


async def read_host_sample(ts: float) -> TelemetrySample | None:
    """A sample for a machine with no GPU at all.

    Every figure here is a host fact -- /proc/meminfo, /sys/class/thermal,
    /proc/stat -- so it works on any Linux box, a Raspberry Pi included. It is
    built only for a node the probe found no GPU on, and that is also a node
    the planner will not place work on: this makes such a node legible in the
    roster, it does not make it eligible. ``addressable_memory`` stays 0 on the
    profile, so every fit against it still refuses.

    ``power_watts`` stays 0.0: a board with no GPU has no GPU power draw to
    read and there is no portable host equivalent. The gateway renders it as
    unknown rather than as zero watts.
    """
    host = read_host_memory()
    temperature = read_host_temperature()
    utilization = await read_cpu_utilization()
    if host is None and temperature is None and utilization is None:
        return None
    return TelemetrySample(
        ts=ts,
        memory_used=host.used if host else 0,
        memory_total=host.total if host else 0,
        power_watts=0.0,
        temperature_c=temperature or 0.0,
        utilization_pct=utilization or 0.0,
        host_memory_total=host.total if host else 0,
        host_memory_available=host.available if host else 0,
        swap_used=host.swap_used if host else 0,
    )


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
            COMPUTE_APPS_QUERY, flag="--query-compute-apps", allow_empty=True
        )
    if rows is None:
        # Only a broken or absent nvidia-smi gets here. An idle GPU returns [].
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


def _proc_cmdline(pid: int) -> str | None:
    """/proc/<pid>/cmdline, NUL-joined and truncated. None when unreadable.

    Unreadable is the normal case in a container without ``--pid=host``, and it
    is also what a process that exited between the nvidia-smi read and this one
    looks like. Neither is an error worth raising over a best-effort field.
    """
    try:
        raw = Path("/proc/%d/cmdline" % pid).read_bytes()
    except (OSError, ValueError):
        return None
    text = raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    if not text:
        return None
    return text[:CMDLINE_MAX]


def _proc_user(pid: int) -> str | None:
    """Owner of /proc/<pid>, by name when we can resolve it, else uid."""
    try:
        uid = Path("/proc/%d" % pid).stat().st_uid
    except (OSError, ValueError):
        return None
    try:
        import pwd

        return pwd.getpwuid(uid).pw_name
    except (KeyError, ImportError):
        return str(uid)


async def read_gpu_processes(
    rows: list[list[str]] | None = None,
) -> list[GpuProcess] | None:
    """Every compute context holding GPU memory, named.

    Three-valued like :func:`read_compute_apps`, and for the same reason: None
    means nvidia-smi could not be asked, ``[]`` means it answered and the GPU is
    idle. Collapsing those two would report an unreadable driver as an empty
    machine, which is the one answer an operator must not be given before
    deciding nothing is holding the memory.
    """
    if rows is None:
        rows = await run_nvidia_smi_async(
            PROCESS_QUERY, flag="--query-compute-apps", allow_empty=True
        )
    if rows is None:
        return None
    out: list[GpuProcess] = []
    for row in rows:
        if len(row) < 3:
            continue
        pid = _num(row[0])
        mib = _num(row[2])
        if pid is None or mib is None:
            continue
        pid = int(pid)
        full = row[1].strip()
        out.append(
            GpuProcess(
                pid=pid,
                name=full.rsplit("/", 1)[-1] or full,
                command=_proc_cmdline(pid) or (full or None),
                user=_proc_user(pid),
                gpu_memory=int(mib) * 1024**2,
            )
        )
    out.sort(key=lambda p: p.gpu_memory, reverse=True)
    return out


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
        # A machine with no GPU at all is a different thing from a GPU node
        # whose nvidia-smi timed out. The first has host facts worth reporting
        # and would otherwise sit in the roster at a permanent 0W/0C/0%, which
        # reads as broken telemetry rather than as absent hardware. The second
        # must keep its last sample: host RAM appearing where VRAM belongs
        # would be a wrong number, which is worse than a stale one.
        if profile.gpu_count == 0:
            return await read_host_sample(ts)
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
