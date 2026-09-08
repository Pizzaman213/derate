"""The hardware probe. Turns a machine into a NodeProfile.

Never raises. An unprobeable node is a node the planner will skip, not a crash,
so every failure path here returns a profile with ``device_class=UNKNOWN`` and
zeroed memory rather than propagating an exception.

On GB10 the memory numbers come from constants, not from nvidia-smi. This is not
caution, it is measurement: a real DGX Spark reports ``[N/A]`` for every FB
memory field, because the memory is unified with the host and there is no
discrete framebuffer to report. Reading the nameplate 128 GiB would also be
wrong for planning; 119.7 GiB is the GPU-reachable slice.
"""

from __future__ import annotations

import logging
import re
import socket
import subprocess
from pathlib import Path

from control_plane.contracts import DeviceClass, NodeProfile
from control_plane.contracts import (
    GB10_ADDRESSABLE,
    GB10_MEM_BANDWIDTH,
    GB10_TOTAL_MEMORY,
)

from .config import DISCRETE_MEMORY_RESERVE

log = logging.getLogger(__name__)

PROBE_QUERY = "name,memory.total,driver_version,compute_cap"
# Older drivers do not know compute_cap. Falling back keeps a working node
# working instead of demoting it to UNKNOWN over one missing field.
PROBE_QUERY_FALLBACK = "name,memory.total,driver_version"
PROBE_TIMEOUT_S = 5.0

# GB/s. Keyed on a substring of the reported model name. Deliberately short:
# a wrong bandwidth silently corrupts every plan that reads it, so an unknown
# card returns 0.0 and the planner treats it as unmeasured.
MEMORY_BANDWIDTH_GBPS: dict[str, float] = {
    "h100": 3350.0,
    "a100": 2039.0,
    "4090": 1008.0,
    "3090": 936.0,
    "a6000": 768.0,
}

# What nvidia-smi prints instead of a number when it has none.
_NOT_A_NUMBER = {"", "n/a", "[n/a]", "[not supported]", "[unknown error]", "unknown"}

# Where the kernel still admits to an NVIDIA card when nvidia-smi will not run.
# Both are readable from inside a container started WITHOUT --gpus: the proc
# entry belongs to the loaded driver and the PCI bus is the host's. That is the
# whole point of them -- see nvidia_hardware_present.
NVIDIA_PROC = Path("/proc/driver/nvidia/version")
PCI_DEVICES = Path("/sys/bus/pci/devices")
NVIDIA_PCI_VENDOR = "0x10de"


def _num(cell: str) -> float | None:
    """Parse one nvidia-smi cell, or None when it is not a number."""
    cell = cell.strip()
    if cell.lower() in _NOT_A_NUMBER:
        return None
    try:
        return float(cell)
    except ValueError:
        return None


def run_nvidia_smi(
    query: str, timeout: float = PROBE_TIMEOUT_S
) -> list[list[str]] | None:
    """Run one --query-gpu and return rows of cells, or None on any failure.

    One row per GPU. Always bounded by a timeout: a wedged nvidia-smi must not
    become a wedged poll loop.
    """
    cmd = [
        "nvidia-smi",
        f"--query-gpu={query}",
        "--format=csv,noheader,nounits",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        log.debug("nvidia-smi %s failed: %s", query, exc)
        return None
    if proc.returncode != 0:
        log.debug("nvidia-smi %s exited %s: %s", query, proc.returncode, proc.stderr)
        return None
    rows = [
        [cell.strip() for cell in line.split(",")]
        for line in proc.stdout.splitlines()
        if line.strip()
    ]
    return rows or None


def run_sysctl(name: str, timeout: float = PROBE_TIMEOUT_S) -> str | None:
    """Read one sysctl key, or None when it is not there to read.

    Shaped like :func:`run_nvidia_smi` on purpose, and used the same way: as
    evidence rather than as a platform test. Linux has a ``sysctl`` binary but
    not ``machdep.cpu.brand_string``, so it exits non-zero and this returns
    None; Windows has no ``sysctl`` at all and raises FileNotFoundError, which
    is the same None. Nothing here asks what operating system this is.
    """
    try:
        proc = subprocess.run(
            ["sysctl", "-n", name],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        log.debug("sysctl %s failed: %s", name, exc)
        return None
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


def bandwidth_for(gpu_name: str) -> float:
    """Memory bandwidth in GB/s, or 0.0 when we do not know the card.

    0.0 means unknown and is checked for downstream. Guessing a number here
    would be indistinguishable from a measurement, which is worse than a gap.
    """
    name = gpu_name.lower()
    for key, gbps in MEMORY_BANDWIDTH_GBPS.items():
        if key in name:
            return gbps
    return 0.0


def nvidia_hardware_present(
    proc: Path = NVIDIA_PROC, pci: Path = PCI_DEVICES
) -> bool:
    """Whether there is NVIDIA hardware here that nvidia-smi merely did not report.

    This is the difference between *"we looked and there is no GPU on this
    machine"* and *"we could not look"*, and it is the only reason a GPU-less
    node can be identified at all rather than filed under UNKNOWN.

    Both sources survive the case that matters. A container started without
    ``--gpus`` has no nvidia-smi -- the container toolkit is what injects it --
    but it still gets a procfs carrying the loaded driver's own entry, and a
    read-only ``/sys`` showing the host's PCI bus. So the misconfigured DGX
    Spark that ``docker/README.md`` exists to warn about still probes as
    unidentified hardware, which is what keeps that warning findable, while a
    Raspberry Pi does not.

    Either source alone is enough and neither is required: a driver can be
    loaded with every card already handed to another container, and a card can
    sit on the bus with nothing bound to it. Both are read as evidence, never
    as a platform test, and any failure to read is simply not evidence.
    """
    try:
        if proc.exists():
            return True
    except OSError as exc:  # a hardened or absent /proc is not an answer
        log.debug("could not stat %s: %s", proc, exc)
    try:
        vendors = sorted(pci.glob("*/vendor"))
    except OSError as exc:
        log.debug("could not list %s: %s", pci, exc)
        return False
    for vendor in vendors:
        try:
            if vendor.read_text().strip().lower() == NVIDIA_PCI_VENDOR:
                return True
        except OSError:
            continue
    return False


def is_gb10(gpu_name: str) -> bool:
    return "gb10" in gpu_name.lower()


def slugify_hostname(hostname: str) -> str:
    """A stable, URL-safe node_id derived from the hostname."""
    slug = re.sub(r"[^a-zA-Z0-9-]+", "-", hostname.strip().lower()).strip("-")
    return slug or "node"


def unknown_profile(
    node_id: str, hostname: str, address: str, reason: str = ""
) -> NodeProfile:
    """The profile of a machine we could not read. Zeroed, never partial."""
    if reason:
        log.warning("node %s probed as UNKNOWN: %s", node_id, reason)
    return NodeProfile(
        node_id=node_id,
        hostname=hostname,
        address=address,
        device_class=DeviceClass.UNKNOWN,
        gpu_name="",
        gpu_count=0,
        total_memory=0,
        addressable_memory=0,
        memory_bandwidth_gbps=0.0,
        compute_capability="",
        driver_version="",
    )


def _probe_apple(
    node_id: str,
    hostname: str,
    address: str,
    sysctl=run_sysctl,
) -> NodeProfile | None:
    """An Apple Silicon machine, or None when this is not one.

    Reached only after nvidia-smi returned nothing, so it never competes with
    the NVIDIA path. The chip name is ``machdep.cpu.brand_string`` and the pool
    is ``hw.memsize``; both are real readings, and if either is missing this
    returns None and the caller falls through to ``unknown_profile`` exactly as
    before.

    Two fields are deliberately *not* what a reader might expect:

    ``gpu_count`` stays 0. It counts GPUs nvidia-smi reported, and a Mac has
    none. That is not pedantry -- it is the switch ``read_telemetry`` uses to
    decide that host facts *are* the reading rather than a fallback, and the
    switch ``serialize.power_reading``/``temp_reading`` use to report unknown
    rather than 0 W and 0 C. Setting it to 1 to mean "there is a GPU in here
    somewhere" makes a Mac report no telemetry at all, and a fabricated zero
    where a measurement belongs.

    ``addressable_memory`` stays 0, so every fit against this machine still
    refuses. macOS caps a single Metal allocation well below ``hw.memsize`` and
    there is no way to read that cap from here; inventing a fraction would be
    indistinguishable from having measured one. 0 already means something exact
    downstream -- "it can be a cluster member; it cannot be a serving node" --
    and that is the true sentence about a Mac. What a model served here can
    actually use is gated on the live host reading instead, which is the number
    the Ollama pull path already asks for.
    """
    brand = sysctl("machdep.cpu.brand_string")
    if not brand or not brand.startswith("Apple"):
        return None
    memsize = sysctl("hw.memsize")
    try:
        total_memory = int(memsize) if memsize else 0
    except ValueError:
        total_memory = 0
    return NodeProfile(
        node_id=node_id,
        hostname=hostname,
        address=address,
        device_class=DeviceClass.APPLE,
        gpu_name=brand,
        gpu_count=0,
        total_memory=total_memory,
        addressable_memory=0,
        # Unknown, and 0.0 is checked for downstream. The published figures for
        # these parts are marketing aggregates over the whole SoC, not a number
        # a plan should be derived from.
        memory_bandwidth_gbps=0.0,
        compute_capability="",
        driver_version="",
    )


def _probe_cpu(
    node_id: str,
    hostname: str,
    address: str,
    host_memory=None,
) -> NodeProfile | None:
    """A machine with no GPU on it, or None when we cannot say even that.

    Reached only after nvidia-smi returned nothing, ``_probe_apple`` said no,
    and ``nvidia_hardware_present`` found no card the probe was merely unable
    to read. What is left is a machine looked at from three directions and
    found to have no GPU -- a Raspberry Pi, a NAS, a spare x86 box -- and that
    is a fact rather than a gap. UNKNOWN reports it as a gap, and the
    coordinator then says it "cannot confirm this hardware is eligible to join
    the pool" about hardware it had in fact identified.

    The evidence required is one host memory reading. A machine that cannot say
    how much RAM it has is one we could not look at, whatever the reason, and
    it keeps the honest UNKNOWN.

    Every memory field stays 0, exactly as on a Mac and for the same reason:
    there is no GPU pool, so there is nothing for the planner or the fit gate
    to spend, and ``addressable_memory == 0`` already means the precise thing
    -- *"it can be a cluster member; it cannot carry a rank."* Host RAM
    deliberately does not go in ``total_memory``, which is summed into
    cluster-wide totals where memory no model can reach does not belong; the
    live ``NodeState.memory_total`` carries it, which is where the roster and
    the node inspector already read it from.

    ``gpu_name`` stays empty for the same reason ``gpu_count`` does: there is
    no GPU to name. ``NodeProfile.describe`` already drops the parentheses when
    it is empty, and the UI falls back to the device class, so the row reads
    "CPU only" rather than inventing a part number.
    """
    if host_memory is None:
        # Deferred: telemetry.py imports this module for `_num`.
        from .telemetry import read_host_memory

        host_memory = read_host_memory
    if host_memory() is None:
        return None
    return NodeProfile(
        node_id=node_id,
        hostname=hostname,
        address=address,
        device_class=DeviceClass.CPU,
        gpu_name="",
        gpu_count=0,
        total_memory=0,
        addressable_memory=0,
        memory_bandwidth_gbps=0.0,
        compute_capability="",
        driver_version="",
    )


def probe_local(
    node_id: str | None = None,
    hostname: str | None = None,
    address: str | None = None,
    rows: list[list[str]] | None = None,
    sysctl=run_sysctl,
    nvidia_present=nvidia_hardware_present,
    host_memory=None,
) -> NodeProfile:
    """Probe this machine's GPUs and return a NodeProfile.

    ``rows`` is an injection point for tests; production leaves it None and the
    probe shells out. This function does not raise.
    """
    hostname = hostname or socket.gethostname()
    node_id = node_id or slugify_hostname(hostname)
    if address is None:
        from .net import primary_address

        address = primary_address()

    try:
        if rows is None:
            rows = run_nvidia_smi(PROBE_QUERY)
            if rows is None:
                rows = run_nvidia_smi(PROBE_QUERY_FALLBACK)
        if not rows:
            # No NVIDIA GPU. Before calling this machine unidentified, ask
            # whether it is one we can identify another way -- "we could not
            # look" and "we looked and it is a Mac" are different facts, and
            # only the first should read as unrecognized hardware.
            apple = _probe_apple(node_id, hostname, address, sysctl)
            if apple is not None:
                return apple
            if nvidia_present():
                # A card is here and nvidia-smi did not answer for it. This is
                # the one shape that has to stay UNKNOWN, because it is a
                # misconfiguration -- almost always a container started without
                # --gpus -- and the roster saying "unrecognized" is how anybody
                # finds out. Calling it a CPU machine would be a wrong answer
                # delivered confidently, which is worse than the gap.
                return unknown_profile(
                    node_id,
                    hostname,
                    address,
                    "NVIDIA hardware is present but nvidia-smi did not answer; "
                    "the driver or the container toolkit is missing",
                )
            cpu = _probe_cpu(node_id, hostname, address, host_memory)
            if cpu is not None:
                return cpu
            return unknown_profile(
                node_id,
                hostname,
                address,
                "nvidia-smi absent or returned nothing, and this machine could "
                "not report its own memory either",
            )

        first = rows[0]
        gpu_name = first[0] if first else ""
        if not gpu_name:
            return unknown_profile(node_id, hostname, address, "no GPU name reported")

        gpu_count = len(rows)
        driver_version = first[2].strip() if len(first) > 2 else ""
        compute_capability = first[3].strip() if len(first) > 3 else ""

        if is_gb10(gpu_name):
            # Unified memory. nvidia-smi reports N/A for FB memory on this part;
            # the constants are the measured GPU-reachable slice.
            total_memory = GB10_TOTAL_MEMORY * gpu_count
            addressable_memory = GB10_ADDRESSABLE * gpu_count
            device_class = DeviceClass.GB10
            bandwidth = GB10_MEM_BANDWIDTH
        else:
            device_class = DeviceClass.DISCRETE
            per_gpu_mib = _num(first[1]) if len(first) > 1 else None
            if per_gpu_mib is None:
                # A discrete card that will not report its own VRAM is not
                # UNKNOWN hardware, it is hardware we cannot plan against.
                # Zero memory makes the planner skip it, which is the safe read.
                total_memory = 0
                addressable_memory = 0
                log.warning(
                    "node %s: %s reported no memory.total; treating as 0 usable",
                    node_id,
                    gpu_name,
                )
            else:
                per_gpu = int(per_gpu_mib) * 1024**2
                total_memory = per_gpu * gpu_count
                addressable_memory = (
                    max(0, per_gpu - DISCRETE_MEMORY_RESERVE) * gpu_count
                )
            bandwidth = bandwidth_for(gpu_name)

        return NodeProfile(
            node_id=node_id,
            hostname=hostname,
            address=address,
            device_class=device_class,
            gpu_name=gpu_name,
            gpu_count=gpu_count,
            total_memory=total_memory,
            addressable_memory=addressable_memory,
            memory_bandwidth_gbps=bandwidth,
            compute_capability=compute_capability,
            driver_version=driver_version,
        )
    except Exception as exc:  # never raise out of a probe
        return unknown_profile(node_id, hostname, address, f"unexpected: {exc!r}")
