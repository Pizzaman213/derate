"""Filesystem capacity and what this product is spending it on.

Read on demand, never sampled. Disk moves slowly and a 1 Hz trace of it would
cost the durable journal two more columns per node per second to record a
number that changes hourly. That is the same call ``GpuProcess`` already makes
in contracts/hardware.py -- read when an operator opens the screen, never on
the poll -- and it is why nothing in this module touches ``TelemetrySample``,
``NodeProfile``, or the archive schema.

Two things here are less obvious than they look.

**Several paths are usually one filesystem.** The data root, the resolver cache
and the sparkrun cache are all under ``/data`` in the container, and on a
development box they may all be under ``/``. Reporting them as three
filesystems reports the same bytes three times: a 3.7 TB disk becomes 7.5 TB
used. They are grouped by ``st_dev``, so a filesystem is counted once no matter
how many of our paths live on it.

**A path we cannot read reports nothing, not zero.** Free space is the number
an operator decides against, and a confident 0 is worse than an admitted gap --
0 bytes free reads as an emergency, and 0 bytes used reads as an empty disk.
Unreadable paths come back in ``unreadable`` with the reason.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Iterable

#: Severity thresholds for a filesystem, as percentages of capacity used.
#: Shipped in the payload rather than left to the client, exactly as
#: capacity_api's memory report ships memory_warn_pct/memory_critical_pct: two
#: copies of a threshold is two answers, and the one that disagrees with the
#: server is the one an operator acts on.
#:
#: Lower than the 90/95 used for memory, deliberately. Memory is reclaimed the
#: moment a process exits; a full disk stops a model download partway and
#: leaves the partial behind, so the warning has to arrive while there is still
#: room to act on it.
DISK_WARN_PCT = 85.0
DISK_CRITICAL_PCT = 95.0

#: Ceilings on a directory walk. The estate is small by construction -- one
#: SQLite file per stream, one JSON per deployment -- but ``/data`` is a mount
#: an operator can put anything under, and this runs inside a request.
_WALK_MAX_DEPTH = 6
_WALK_MAX_ENTRIES = 20000

#: The data root's contents, in the order they are worth reading. Every entry
#: is owned by a module in this repo; nothing here guesses at a path somebody
#: else created. ``dir`` walks, ``file`` stats.
#:
#: Deliberately not including /data/sparkrun-cache: entrypoint.sh symlinks it
#: to $HOME/.cache/sparkrun, and following it would attribute another tool's
#: bytes to ours. Its filesystem is still measured, because the symlink target
#: is one of the probe paths.
ESTATE: tuple[tuple[str, str, str, str], ...] = (
    ("archive", "Telemetry archive", "telemetry/archive.db", "file"),
    ("journal", "Telemetry journal", "telemetry/journal.db", "file"),
    ("resolver_cache", "Resolver shape cache", "cache/resolver", "dir"),
    ("deployments", "Deployment records", "deployments", "dir"),
    ("recipes", "Sparkrun recipes", "recipes", "dir"),
    ("registry", "Roster", "registry.json", "file"),
    ("cluster", "Cluster identity", "cluster.json", "file"),
    ("enrollments", "Enrollment tokens", "enrollments.json", "file"),
    ("links", "Link measurements", "links.json", "file"),
    ("settings", "Settings", "settings.json", "file"),
    ("models", "Model registry", "models.db", "file"),
    ("providers", "Provider config", "providers.json", "file"),
    ("secrets", "Provider keys", "secrets.json", "file"),
)


def severity(used_pct: float) -> str:
    """``ok``/``warn``/``critical`` for a percentage of capacity used."""
    if used_pct >= DISK_CRITICAL_PCT:
        return "critical"
    if used_pct >= DISK_WARN_PCT:
        return "warn"
    return "ok"


def read_filesystems(
    paths: Iterable[Path | str],
) -> tuple[list[dict], list[dict]]:
    """Capacity for the filesystems behind *paths*, one entry per device.

    Returns ``(filesystems, unreadable)``. A path that does not exist or cannot
    be stat'd contributes to the second list and to no filesystem at all --
    see the module docstring on why it does not contribute a zero.

    ``mount_paths`` carries every one of our paths that landed on that device,
    so an operator can see that the archive and the model cache share a disk
    without us having to name the mount point.
    """
    by_device: dict[int, dict] = {}
    unreadable: list[dict] = []

    for raw in paths:
        path = Path(raw)
        try:
            usage = shutil.disk_usage(path)
            device = os.stat(path).st_dev
        except OSError as exc:
            unreadable.append({"path": str(path), "reason": _reason(path, exc)})
            continue

        entry = by_device.get(device)
        if entry is not None:
            # Same filesystem, already measured. Record the path and move on;
            # re-adding the bytes is the double-count this grouping exists for.
            if str(path) not in entry["mount_paths"]:
                entry["mount_paths"].append(str(path))
            continue

        # Percentage against what we can actually allocate into, not against
        # the raw device size. A filesystem reserves blocks for root -- 190 GiB
        # on the 3.7 TB NVMe this was written against -- and they are neither
        # ours to use nor free. df computes used/(used+avail) for exactly this
        # reason, and a storage tab that disagrees with df by four points is a
        # storage tab an operator stops believing.
        capacity = usage.used + usage.free
        used_pct = round(100.0 * usage.used / capacity, 1) if capacity else 0.0
        by_device[device] = {
            "device": device,
            "mount_paths": [str(path)],
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "reserved": max(0, usage.total - capacity),
            "used_pct": used_pct,
            "warn_pct": DISK_WARN_PCT,
            "critical_pct": DISK_CRITICAL_PCT,
            "severity": severity(used_pct),
        }

    filesystems = sorted(by_device.values(), key=lambda f: f["mount_paths"][0])
    return filesystems, unreadable


def path_bytes(path: Path) -> int | None:
    """Bytes held by a file or directory, or None if it could not be read.

    Directories are walked to ``_WALK_MAX_DEPTH`` and ``_WALK_MAX_ENTRIES``,
    counting apparent size and never following symlinks. A partial walk still
    returns what it counted: a floor on a directory's size is useful, and the
    alternative -- refusing to answer because the tree was large -- tells an
    operator nothing at all.
    """
    try:
        if path.is_symlink():
            # A symlink's bytes belong to whatever it points at, which is
            # somebody else's accounting. /data/sparkrun-cache is one.
            return None
        if path.is_file():
            return path.stat().st_size
        if not path.is_dir():
            return None
    except OSError:
        return None

    total = 0
    seen = 0
    root_depth = len(path.parts)
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        if len(Path(dirpath).parts) - root_depth >= _WALK_MAX_DEPTH:
            dirnames[:] = []
        for name in filenames:
            seen += 1
            if seen > _WALK_MAX_ENTRIES:
                return total
            try:
                stat = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            # lstat, so a symlink counts as the link and not its target --
            # otherwise a tree of links to one big file reports it many times.
            total += stat.st_size
    return total


def read_estate(root: Path | str) -> list[dict]:
    """What this product is storing under *root*, itemised.

    Every known component gets a row whether or not it exists, so a reader can
    tell "no deployments yet" from "this build does not write deployments".
    ``bytes`` is None for a component that is absent or unreadable, and the
    distinction is carried by ``exists``.
    """
    base = Path(root)
    out: list[dict] = []
    for key, label, rel, kind in ESTATE:
        path = base / rel
        exists = path.exists()
        out.append(
            {
                "key": key,
                "label": label,
                "path": str(path),
                "kind": kind,
                "exists": exists,
                "bytes": path_bytes(path) if exists else None,
            }
        )
    return out


def probe_paths(root: Path | str) -> list[Path]:
    """The paths worth measuring capacity for on this node.

    The data root always. The resolver cache when it has been pointed somewhere
    else (DERATE_CACHE_DIR), and the sparkrun cache through its symlink,
    because on a real install those are where the bytes actually go and neither
    is guaranteed to share a device with /data. Duplicates are harmless -- they
    collapse in read_filesystems -- so this errs towards looking.
    """
    base = Path(root)
    paths = [base]

    cache_dir = os.environ.get("DERATE_CACHE_DIR")
    if cache_dir:
        paths.append(Path(cache_dir))

    sparkrun = base / "sparkrun-cache"
    if sparkrun.exists():
        # Resolved, so we measure the filesystem it actually lands on rather
        # than the one holding the link.
        try:
            paths.append(sparkrun.resolve())
        except OSError:
            pass
    return paths


def storage_payload(node_id: str, root: Path | str) -> dict:
    """This node's disk picture: capacity, and our share of it."""
    filesystems, unreadable = read_filesystems(probe_paths(root))
    return {
        "node_id": node_id,
        "root": str(root),
        "filesystems": filesystems,
        "estate": read_estate(root),
        "unreadable": unreadable,
        "measured_at": time.time(),
    }


def _reason(path: Path, exc: OSError) -> str:
    """One sentence, in the product's voice, for a path we could not read."""
    if isinstance(exc, FileNotFoundError):
        return f"{path} does not exist on this node."
    if isinstance(exc, PermissionError):
        return f"{path} could not be read: permission denied."
    return f"{path} could not be read: {exc.strerror or exc}."
