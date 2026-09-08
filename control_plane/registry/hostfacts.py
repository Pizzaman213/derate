"""Host facts for a machine with no /proc.

The readers in ``telemetry.py`` are ``/proc/meminfo``, ``/proc/stat`` and
``/sys/class/thermal``, and each already returns ``None`` when the file is not
there. On macOS and Windows all three return ``None`` together, which makes
``read_host_sample`` return ``None``, which means such a node joins the roster,
reports healthy, and shows nothing at all. That is the shape of failure this
codebase most consistently refuses: nothing looks broken.

This module is what those readers fall through *to*. Three deliberate
properties:

**It is reached by evidence, not by identity.** Nothing here or in
``telemetry.py`` asks ``sys.platform``. The trigger is that ``/proc/meminfo``
could not be read, which is the fact the caller actually needs. A name test
would be wrong inside a Linux container on a Mac, under WSL, and on a hardened
``/proc`` -- all cases where the Linux readers still work and should still win.

**Linux never gets here**, so the reasoning encoded in the ``/proc`` readers --
MemAvailable rather than MemFree, the hottest thermal zone rather than a mean,
the differenced ``/proc/stat`` sample -- is untouched, and no existing test
changes.

**Absence degrades.** psutil is a declared dependency, but if it is missing
this returns ``None`` and the machine reads exactly as it does today rather
than worse.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def _psutil():
    """psutil, or None. Imported lazily so this module is importable without it."""
    try:
        import psutil
    except Exception as exc:  # pragma: no cover - exercised only where it is absent
        log.debug("psutil unavailable, host facts stay unknown: %s", exc)
        return None
    return psutil


def host_memory() -> tuple[int, int, int] | None:
    """``(total, available, swap_used)`` in bytes, or None.

    ``available`` rather than free, matching the ``MemAvailable`` choice the
    Linux reader documents: page cache is reclaimable, and counting it as spent
    would read as a permanently full machine. psutil computes the same quantity
    per platform, which is the reason to use it rather than to divide a
    ``vm_stat`` page count here and get it subtly wrong.
    """
    ps = _psutil()
    if ps is None:
        return None
    try:
        vm = ps.virtual_memory()
        swap = ps.swap_memory()
    except Exception as exc:
        log.debug("psutil could not read memory: %s", exc)
        return None
    return int(vm.total), int(vm.available), int(swap.used)


def cpu_utilization() -> float | None:
    """Busy percentage since the previous call, or None.

    ``interval=None`` is non-blocking and differences against psutil's own
    previous reading, which is the same contract ``read_cpu_utilization`` has
    with its ``_last_cpu_times`` global -- including that the first call after
    start has nothing to difference against and reports 0.0.
    """
    ps = _psutil()
    if ps is None:
        return None
    try:
        return float(ps.cpu_percent(interval=None))
    except Exception as exc:
        log.debug("psutil could not read cpu: %s", exc)
        return None


def temperature() -> float | None:
    """Always None, on purpose.

    ``psutil.sensors_temperatures`` is Linux-only. macOS needs root for
    ``powermetrics`` and the SMC otherwise; Windows exposes motherboard
    thermal zones through WMI that are not the GPU. There is no portable
    reading here, and the honest answer is the one the gateway already renders
    as an em dash. A motherboard sensor reported as a board temperature would
    be a number nobody could tell from a measurement.

    Present as a function rather than absent so the fallthrough in
    ``telemetry.read_host_temperature`` reads the same as the other two.
    """
    return None
