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
import os
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from control_plane.contracts import (
    DEFAULT_GUARDRAIL,
    DeviceClass,
    GpuProcess,
    NodeProfile,
)

from . import hostfacts
from .config import HOST_MEMORY_RESERVE, TELEMETRY_RING_SAMPLES, cpu_host_reserve
from .probe import _NOT_A_NUMBER, _num

log = logging.getLogger(__name__)

#: The five fields every driver has known for years. Kept as its own constant
#: because it is the fallback, not merely the old value -- see
#: ``_clock_fields_ok`` below.
TELEMETRY_QUERY_BASE = (
    "memory.used,memory.total,power.draw,temperature.gpu,utilization.gpu"
)
#: ...and the three that say whether the clocks were being held down. They ride
#: the call that already runs once a second, so they cost no extra process.
TELEMETRY_QUERY = TELEMETRY_QUERY_BASE + (
    ",clocks_event_reasons.active,clocks.sm,clocks.max.sm"
)
#: How many cells ``TELEMETRY_QUERY`` returns, and how many the fallback does.
TELEMETRY_CELLS = 8
TELEMETRY_CELLS_BASE = 5

#: The bits that mean the clocks were held BELOW what was asked for. NVML also
#: reports GpuIdle (0x1), ApplicationsClocksSetting (0x2), SyncBoost (0x10) and
#: DisplayClockSetting (0x100); none of those is a derate. Including GpuIdle
#: would light the badge on every quiet node in the fleet, which is most of them
#: most of the time. Store the raw mask, derive the predicate from this in one
#: place, and never test ``bits != 0``.
CLOCK_SW_POWER_CAP = 0x4
CLOCK_HW_SLOWDOWN = 0x8
CLOCK_SW_THERMAL = 0x20
CLOCK_HW_THERMAL = 0x40
CLOCK_HW_POWER_BRAKE = 0x80
CLOCK_THROTTLE_MASK = (
    CLOCK_SW_POWER_CAP
    | CLOCK_HW_SLOWDOWN
    | CLOCK_SW_THERMAL
    | CLOCK_HW_THERMAL
    | CLOCK_HW_POWER_BRAKE
)
#: The name of each bit, for the reason line an event carries. Ordered so the
#: hardware ones read last, because they are the serious ones.
CLOCK_THROTTLE_NAMES = (
    (CLOCK_SW_POWER_CAP, "sw power cap"),
    (CLOCK_SW_THERMAL, "sw thermal"),
    (CLOCK_HW_SLOWDOWN, "hw slowdown"),
    (CLOCK_HW_THERMAL, "hw thermal"),
    (CLOCK_HW_POWER_BRAKE, "hw power brake"),
)
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
VMSTAT = Path("/proc/vmstat")
#: Pressure Stall Information. Optional in the kernel (CONFIG_PSI) and absent
#: off Linux, so its absence is remembered rather than retried once a second.
PRESSURE_MEMORY = Path("/proc/pressure/memory")
THERMAL_ZONES = Path("/sys/class/thermal")
PROC_STAT = Path("/proc/stat")
# A zone reporting outside this range is handing back a sentinel rather than a
# temperature. Unpopulated sensors read 0 or -274 on plenty of boards.
TEMP_MIN_C = 1.0
TEMP_MAX_C = 150.0
# How long the first CPU reading waits for something to difference against.
# /proc/stat is cumulative, so one reading carries no rate at all.
CPU_FIRST_DELTA_S = 0.12
# Bytes per page, for turning /proc/vmstat's page counts into a rate that is
# comparable with swap_used. NOT 4096: this is aarch64, 16K and 64K kernels
# exist, and hardcoding the x86 value understates paging by 4x or 16x on a
# machine nobody would think to check.
try:
    PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
except (AttributeError, ValueError, OSError):  # pragma: no cover - non-POSIX
    PAGE_SIZE = 4096


def _round_or_none(value: float | None, places: int) -> float | None:
    """Round for the wire, but keep None as None rather than rounding it to 0.0."""
    return None if value is None else round(value, places)


def _hexnum(cell: str) -> int | None:
    """Parse one nvidia-smi integer cell that may be hexadecimal.

    ``probe.py::_num`` is ``float(cell)`` and raises on ``0x0000000000000000``,
    returning None -- so reusing it here would ship a field that is silently
    always unknown. The clock-event mask is printed as hex and nothing else in
    the query is, which is why this is its own parser rather than a widening of
    that one.
    """
    cell = cell.strip()
    if not cell or cell.lower() in _NOT_A_NUMBER:
        return None
    try:
        return int(cell, 16) if cell.lower().startswith("0x") else int(cell)
    except ValueError:
        return None


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
    # Everything below defaults to None, never 0. On a machine with no GPU there
    # is no throttle state and no SM clock, and 0 there would read as "measured,
    # and it is fine" -- the same reason serde.power_reading refuses to report a
    # GPU-less board as drawing 0 W. None means nobody looked.
    clock_throttle_bits: int | None = None  # raw NVML mask; see CLOCK_THROTTLE_MASK
    sm_clock_mhz: int | None = None
    sm_clock_max_mhz: int | None = None
    swap_in_bps: float | None = None
    swap_out_bps: float | None = None
    major_faults_per_s: float | None = None
    memory_pressure_pct: float | None = None  # /proc/pressure/memory, full avg10

    @property
    def throttled(self) -> bool | None:
        """Whether the clocks were being held BELOW what was asked for.

        Tri-state on purpose. ``None`` is "no reading", which is a CPU-only node
        or a driver too old for the field -- not a node that is running free.
        Tests the mask rather than ``bits != 0``, because NVML also sets a bit
        for an idle GPU and one for an operator-set clock limit, and neither is
        a derate.
        """
        if self.clock_throttle_bits is None:
            return None
        return bool(self.clock_throttle_bits & CLOCK_THROTTLE_MASK)

    @property
    def throttle_reasons(self) -> tuple[str, ...]:
        """The bits that were set, named. Empty when none were or none is known."""
        bits = self.clock_throttle_bits or 0
        return tuple(name for bit, name in CLOCK_THROTTLE_NAMES if bits & bit)

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
            "clock_throttle_bits": self.clock_throttle_bits,
            "sm_clock_mhz": self.sm_clock_mhz,
            "sm_clock_max_mhz": self.sm_clock_max_mhz,
            "swap_in_bps": _round_or_none(self.swap_in_bps, 1),
            "swap_out_bps": _round_or_none(self.swap_out_bps, 1),
            "major_faults_per_s": _round_or_none(self.major_faults_per_s, 2),
            "memory_pressure_pct": _round_or_none(self.memory_pressure_pct, 2),
        }


async def run_nvidia_smi_async(
    query: str,
    timeout: float = TELEMETRY_TIMEOUT_S,
    flag: str = "--query-gpu",
    allow_empty: bool = False,
    note: "Callable[[str], None] | None" = None,
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

    ``note`` is called with WHY, when there is a why. Four quite different
    failures collapsed into one ``None`` here, and the caller kept its previous
    sample either way -- so a wedged driver and an idle GPU produced the same
    flat line, and nothing anywhere recorded which one it had been. The values
    are ``not_found``, ``timeout``, ``exit_nonzero`` and ``empty``.
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
        if note:
            note("not_found")
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
        if note:
            note("timeout")
        return None

    if proc.returncode != 0:
        if note:
            note("exit_nonzero")
        return None
    rows = [
        [cell.strip() for cell in line.split(",")]
        for line in stdout.decode(errors="replace").splitlines()
        if line.strip()
    ]
    if rows:
        return rows
    if allow_empty:
        return []
    if note:
        note("empty")
    return None


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
        # No /proc. Not necessarily "no reading" -- see registry/hostfacts.py.
        # The trigger is the missing file, not the operating system's name, so
        # a Linux container on a Mac still reads the line above and wins.
        facts = hostfacts.host_memory()
        if facts is None:
            return None
        total, available, swap_used = facts
        return HostMemory(total=total, available=available, swap_used=swap_used)
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
        return hostfacts.temperature()
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
        # No /proc/stat. psutil keeps its own previous reading, so it has the
        # same difference-against-last-call contract as the block below --
        # including that the first call has nothing to difference against.
        return hostfacts.cpu_utilization()
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


@dataclass(frozen=True)
class _Paging:
    """The cumulative page counters, and the moment they were read."""

    ts: float
    swap_in: int
    swap_out: int
    major_faults: int


@dataclass(frozen=True)
class PagingRates:
    """How hard the machine is paging, right now."""

    swap_in_bps: float
    swap_out_bps: float
    major_faults_per_s: float


_last_paging: _Paging | None = None
#: Remembered across calls: /proc/pressure/memory is optional in the kernel
#: (CONFIG_PSI) and absent off Linux, and this runs once a second. Retrying and
#: logging a missing file 86,400 times a day is its own outage.
_pressure_available: bool | None = None


def _read_vmstat(now: float) -> _Paging | None:
    """The three counters that say whether the pool is actually thrashing."""
    try:
        text = VMSTAT.read_text()
    except OSError:
        return None
    wanted = {"pswpin": 0, "pswpout": 0, "pgmajfault": 0}
    seen = 0
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if key in wanted:
            try:
                wanted[key] = int(value)
            except ValueError:
                return None
            seen += 1
            if seen == len(wanted):
                break
    if seen < len(wanted):
        return None
    return _Paging(
        ts=now,
        swap_in=wanted["pswpin"],
        swap_out=wanted["pswpout"],
        major_faults=wanted["pgmajfault"],
    )


def read_paging(now: float | None = None) -> PagingRates | None:
    """Paging RATE since the previous call, or None when there is no answer yet.

    ``swap_used`` is a level, and a level cannot tell a settled pool of cold
    pages from a machine actively tearing itself apart -- measured on the box
    this was written for, 4.5 GiB of swap in use at 0 pages/s out. The reserve
    in ``HOST_MEMORY_RESERVE`` exists to prevent the second case and nothing
    measured whether it worked.

    Differenced here, on the machine that owns the counter, for the same reason
    ``read_cpu_utilization`` differences /proc/stat here: the archive admits
    late and out-of-order rows and deletes its oldest raw day, so it has no
    guarantee it holds the row immediately before any other. A consumer-side
    subtraction across a retention hole would invent a spike the size of the
    hole -- the failure the ``gaps`` table exists to prevent, with the sign
    flipped.

    Unlike that function this one does NOT take its own short delta on the first
    call. There the cost buys a first CPU reading that would otherwise show a
    fabricated 0%; here it would be paid at every agent start to fill one second
    of one chart, and None already says the right thing.
    """
    global _last_paging
    now = time.time() if now is None else now
    current = _read_vmstat(now)
    if current is None:
        return None
    previous, _last_paging = _last_paging, current
    if previous is None:
        return None
    span = current.ts - previous.ts
    if span <= 0:
        return None
    if (
        current.swap_in < previous.swap_in
        or current.swap_out < previous.swap_out
        or current.major_faults < previous.major_faults
    ):
        # The machine rebooted, or a counter wrapped. Either way the delta is
        # not a rate. Report nothing and let the next call difference against
        # the reading we just stored.
        return None
    return PagingRates(
        swap_in_bps=(current.swap_in - previous.swap_in) * PAGE_SIZE / span,
        swap_out_bps=(current.swap_out - previous.swap_out) * PAGE_SIZE / span,
        major_faults_per_s=(current.major_faults - previous.major_faults) / span,
    )


def read_memory_pressure() -> float | None:
    """Percent of the last 10s in which EVERY runnable task was stalled on memory.

    The ``full`` line, not ``some``. ``some`` fires whenever any single task
    waits on reclaim and is nonzero on a healthy machine constantly; ``full``
    means nothing ran at all, which is the number that predicts the collapse.

    ``avg10`` is a kernel-maintained 10-second average sampled at 1 Hz, so
    consecutive samples overlap. That is deliberate: it makes the rollup MAX
    read as "the worst 10-second window overlapping this bucket", which is what
    an alert should fire on.
    """
    global _pressure_available
    if _pressure_available is False:
        return None
    try:
        text = PRESSURE_MEMORY.read_text()
    except OSError:
        _pressure_available = False
        return None
    _pressure_available = True
    for line in text.splitlines():
        if not line.startswith("full "):
            continue
        for field in line.split()[1:]:
            key, _, value = field.partition("=")
            if key == "avg10":
                try:
                    return float(value)
                except ValueError:
                    return None
    return None


def _paging_fields() -> dict[str, float | None]:
    """The four host paging figures, as kwargs. Host facts, so both samplers use them."""
    rates = read_paging()
    return {
        "swap_in_bps": rates.swap_in_bps if rates else None,
        "swap_out_bps": rates.swap_out_bps if rates else None,
        "major_faults_per_s": rates.major_faults_per_s if rates else None,
        "memory_pressure_pct": read_memory_pressure(),
    }


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
        # A machine with no GPU still swaps, and swapping is the same signal
        # there. The three clock fields stay None: there is no GPU to derate.
        **_paging_fields(),
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


#: Tri-state. None = not yet established, True = this driver answers the clock
#: fields, False = it does not and we stopped asking.
_clock_fields_ok: bool | None = None


async def _query_gpus(
    note: "Callable[[str], None] | None" = None,
) -> tuple[list[list[str]] | None, bool]:
    """One --query-gpu, with the clock fields if this driver knows them.

    Returns ``(rows, had_clocks)``.

    An unknown field does not degrade -- it fails the WHOLE query. Verified:
    ``--query-gpu=power.draw,temperature.gpu,not_a_real_field`` prints
    ``Field "not_a_real_field" is not a valid field to query.`` and exits 2 with
    no data at all. Without the fallback below, a driver too old for
    ``clocks_event_reasons.active`` would therefore lose a GPU node ALL of its
    telemetry rather than three fields of it, and the node would sit in the
    roster at a permanent 0W/0C/0% while nvidia-smi worked perfectly from a
    shell. ``probe.py::PROBE_QUERY_FALLBACK`` exists for exactly this reason and
    exactly this shape, one query earlier in the same process.

    The answer is remembered, so the cost is one extra subprocess per process
    rather than one per second.
    """
    global _clock_fields_ok
    if _clock_fields_ok is not False:
        rows = await run_nvidia_smi_async(TELEMETRY_QUERY, note=note)
        if rows:
            _clock_fields_ok = True
            return rows, True
        if _clock_fields_ok is True:
            # It worked before, so this is a timeout or a wedged driver rather
            # than an unknown field. Do not downgrade on a transient.
            return rows, True
    rows = await run_nvidia_smi_async(TELEMETRY_QUERY_BASE, note=note)
    if rows and _clock_fields_ok is None:
        log.info(
            "nvidia-smi does not know the clock-event fields; "
            "telemetry will not report GPU throttling on this driver"
        )
        _clock_fields_ok = False
    return rows, False


async def read_telemetry(
    profile: NodeProfile,
    now: float | None = None,
    rows: list[list[str]] | None = None,
    apps_rows: list[list[str]] | None = None,
    note: "Callable[[str], None] | None" = None,
) -> TelemetrySample | None:
    """Sample the local GPUs. None when nothing could be read.

    Aggregation across multiple GPUs: memory and power sum, temperature takes
    the hottest, utilisation averages. A node is one planning unit, so it needs
    one number per metric.
    """
    ts = time.time() if now is None else now
    had_clocks = True
    if rows is None:
        rows, had_clocks = await _query_gpus(note=note)
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
    paging = _paging_fields()

    used_mib: list[float] = []
    total_mib: list[float] = []
    power: list[float] = []
    temps: list[float] = []
    utils: list[float] = []

    # The clock triple has to stay ROW-PAIRED while everything else is sinked
    # per column: on a mixed node the interesting GPU is the one being held down
    # hardest, and a mean of two different parts describes neither. Tracked as
    # (ratio, sm, max) and the minimum ratio wins.
    throttle_bits: int | None = None
    worst_clock: tuple[float, int, int] | None = None

    for row in rows:
        cells = (row + [""] * TELEMETRY_CELLS)[:TELEMETRY_CELLS]
        for cell, sink in zip(cells[:5], (used_mib, total_mib, power, temps, utils)):
            value = _num(cell)
            if value is not None:
                sink.append(value)
        if not had_clocks:
            continue
        bits = _hexnum(cells[5])
        if bits is not None:
            # Union across GPUs: the node is one planning unit, and any GPU
            # being held down is the node being held down. The bitmask
            # analogue of taking the hottest sensor.
            throttle_bits = bits if throttle_bits is None else throttle_bits | bits
        sm = _hexnum(cells[6])
        sm_max = _hexnum(cells[7])
        if sm is not None and sm_max:
            ratio = sm / sm_max
            if worst_clock is None or ratio < worst_clock[0]:
                worst_clock = (ratio, sm, sm_max)

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
        clock_throttle_bits=throttle_bits,
        sm_clock_mhz=worst_clock[1] if worst_clock else None,
        sm_clock_max_mhz=worst_clock[2] if worst_clock else None,
        **paging,
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

    A machine with no GPU has a third answer, and it is the reason this
    function needed a new branch rather than a new field. ``usable_memory`` is
    ``addressable_memory * guardrail``, which is 0 for a CPU node by
    construction and deliberately so -- ``addressable_memory`` means "bytes
    reachable by the GPU" and there is no GPU. That 0 used to end the story:
    every fit against such a node refused, which was correct while no runtime
    here could run on one.

    The ``llamacpp`` runtime can, and it spends host RAM. So a CPU node's
    budget is MemAvailable less a reserve -- the same shape as the GB10 line
    below, with a reserve sized for the hardware (``config.cpu_host_reserve``;
    8 GiB is ~6% of a Spark's pool and the whole of a small Pi's).

    Note what this does NOT do: it never reads a static ceiling. A GPU node
    with no sample falls back to its nameplate, because a nameplate is a real
    fact about a device that exists. A CPU node has no such number -- its
    ceiling is 0 -- so an unsampled CPU node returns 0 and the live path drops
    it, which is the honest answer. "Live memory degrades to static" is the
    rule everywhere else in this project; here there is nothing to degrade to,
    and the refusal says so rather than inventing a budget.
    """
    ceiling = profile.usable_memory(guardrail)

    if profile.device_class is DeviceClass.CPU:
        if sample is None or sample.host_memory_available <= 0:
            return 0
        return max(
            0, sample.host_memory_available - cpu_host_reserve(sample.host_memory_available)
        )

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


def allocatable_bytes_or_none(
    profile: NodeProfile,
    sample: TelemetrySample | None,
    guardrail: float = DEFAULT_GUARDRAIL,
    host_reserve: int = HOST_MEMORY_RESERVE,
) -> int | None:
    """:func:`allocatable_bytes`, with "nobody has measured this" left sayable.

    ``allocatable_bytes`` answers an unsampled node with its static ceiling.
    That is the right default for a display and the wrong one for a gate: the
    caller cannot tell a measurement from a nameplate, and the fit gate goes on
    to narrate it as "allocatable right now". On a part whose pool is shared
    with an operating system -- and, on the box this was written for, with a
    llama-server holding 68 GiB of it -- the ceiling is not merely optimistic.
    It is the one figure guaranteed to be wrong, and it is the spec-sheet
    number this project exists to refuse to plan against.

    ``None`` instead lets the live path drop the node, so the fit gate falls
    back to the ceiling *and says that is what it did* -- the branch
    ``FitCalculator._budget`` has always carried and nothing could reach,
    because the ceiling arrived as a number rather than as an absence.
    """
    if sample is None:
        return None
    return allocatable_bytes(profile, sample, guardrail, host_reserve)


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
