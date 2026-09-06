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


def probe_local(
    node_id: str | None = None,
    hostname: str | None = None,
    address: str | None = None,
    rows: list[list[str]] | None = None,
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
            return unknown_profile(
                node_id, hostname, address, "nvidia-smi absent or returned nothing"
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
