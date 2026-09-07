"""The downloaded weights, and getting rid of them.

This is the other half of the disk question. `storage.py` says how full a
filesystem is; this says what is filling it, because on a machine that serves
models the answer is almost always the model weights and nothing else. On the
box this was written against the HuggingFace cache held 894 GiB across 51
repositories, and a single one of them -- `openai/gpt-oss-120b` -- was 182 GiB.

**The control plane does not download these and never will.** `resolver/hf.py`
reads metadata and byte ranges, never shards. The weights are pulled by the
vLLM or SGLang container `sparkrun` starts, which mounts the host's HF cache
and sets `HF_HOME` to it. That is the whole reason this module can exist: the
runtime and the node agent are looking at the same directory on the same host,
so what the runtime downloaded, the agent can measure and delete.

Three things here are load-bearing.

**Blobs are the bytes.** The cache stores every file once under `blobs/` and
builds `snapshots/<sha>/` out of symlinks into it. Walking the snapshots would
count the same weights once per revision; walking only `blobs/` counts them
once, which is what the disk actually holds.

**A repository is matched by folder name, not by a decoded id.** The cache
encodes `org/name` as `models--org--name`, and decoding that back is ambiguous
whenever a name contains a double hyphen. Every decision that matters -- is
this repository in use, is this the one to delete -- compares the encoded form,
which is exact. Decoding is done once, for display, and is allowed to be
best-effort because nothing depends on it.

**Deleting is bounded to the cache.** The folder to remove arrives as a URL
path segment, so it is validated as a single name, required to start with the
cache's own prefix, and the resolved path is required to sit directly inside
the resolved cache directory. Any one of those alone would probably do; a
delete of a directory tree gets all three.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

#: The cache's own naming convention: ``org/name`` becomes ``models--org--name``.
FOLDER_PREFIX = "models--"


def cache_dir() -> Path | None:
    """Where this node's downloaded weights live, or None if nothing looked.

    Resolved in the order the hub library itself uses, so the agent measures
    the directory the runtime actually writes to rather than a default nobody
    configured. ``DERATE_HF_CACHE`` comes first as the deployment's own
    override, for a container that mounts the cache somewhere of its choosing.
    """
    explicit = os.environ.get("DERATE_HF_CACHE")
    if explicit:
        return Path(explicit)
    hub = os.environ.get("HUGGINGFACE_HUB_CACHE")
    if hub:
        return Path(hub)
    hf_home = os.environ.get("HF_HOME")
    if hf_home:
        return Path(hf_home) / "hub"
    default = Path.home() / ".cache" / "huggingface" / "hub"
    return default if default.is_dir() else None


def folder_for(repo_id: str) -> str:
    """The cache folder a repository id maps to. The exact inverse is not
    needed anywhere; this direction is, and it is unambiguous."""
    return FOLDER_PREFIX + repo_id.strip().replace("/", "--")


def repo_from_folder(folder: str) -> str:
    """Best-effort display id for a cache folder.

    Ambiguous by construction -- ``models--a--b--c`` could be ``a/b--c`` or
    ``a--b/c`` -- so the first separator is taken as the split. Nothing
    decides anything on this value; it is what a person reads.
    """
    if not folder.startswith(FOLDER_PREFIX):
        return folder
    rest = folder[len(FOLDER_PREFIX) :]
    org, sep, name = rest.partition("--")
    return f"{org}/{name}" if sep else org


def _blob_bytes(repo: Path) -> tuple[int, int, float | None]:
    """(bytes, blob count, newest mtime) for one repository.

    Only ``blobs/`` is measured, and it is a flat directory, so this is one
    ``scandir`` per repository however many revisions are cached. Measured at
    6 ms for 51 repositories holding 894 GiB.
    """
    blobs = repo / "blobs"
    if not blobs.is_dir():
        return 0, 0, None
    total = 0
    count = 0
    newest: float | None = None
    try:
        with os.scandir(blobs) as entries:
            for entry in entries:
                try:
                    # lstat, so a symlinked blob counts as the link it is and
                    # its target is not attributed here twice.
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                total += stat.st_size
                count += 1
                if newest is None or stat.st_mtime > newest:
                    newest = stat.st_mtime
    except OSError:
        return 0, 0, None
    return total, count, newest


def _revisions(repo: Path) -> list[str]:
    snapshots = repo / "snapshots"
    if not snapshots.is_dir():
        return []
    try:
        return sorted(d.name for d in snapshots.iterdir() if d.is_dir())
    except OSError:
        return []


def scan(directory: Path | str | None = None) -> dict:
    """Every cached repository on this node, largest first.

    Returns ``available: False`` with a reason when there is no cache to read,
    which is the ordinary case for a container that was not given the mount --
    and is a different answer from a cache that is empty.
    """
    path = Path(directory) if directory is not None else cache_dir()
    if path is None:
        return {
            "available": False,
            "path": None,
            "repos": [],
            "total_bytes": None,
            "reason": (
                "No model cache was found on this node. Nothing has been "
                "downloaded here, or the cache is not mounted into this "
                "container."
            ),
        }
    if not path.is_dir():
        return {
            "available": False,
            "path": str(path),
            "repos": [],
            "total_bytes": None,
            "reason": (
                f"{path} is not a directory on this node, so what has been "
                "downloaded here cannot be read."
            ),
        }

    repos: list[dict] = []
    try:
        entries = sorted(path.iterdir())
    except OSError as exc:
        return {
            "available": False,
            "path": str(path),
            "repos": [],
            "total_bytes": None,
            "reason": f"{path} could not be read: {exc.strerror or exc}.",
        }

    for entry in entries:
        if not entry.name.startswith(FOLDER_PREFIX) or not entry.is_dir():
            continue
        if entry.is_symlink():
            continue
        size, blob_count, newest = _blob_bytes(entry)
        repos.append(
            {
                "folder": entry.name,
                "repo_id": repo_from_folder(entry.name),
                "bytes": size,
                "blob_count": blob_count,
                "revisions": _revisions(entry),
                "last_modified": newest,
            }
        )

    repos.sort(key=lambda r: -r["bytes"])
    return {
        "available": True,
        "path": str(path),
        "repos": repos,
        "total_bytes": sum(r["bytes"] for r in repos),
        "reason": None,
        "measured_at": time.time(),
    }


class DeleteRefused(Exception):
    """A delete that must not happen, with the sentence saying why."""


def resolve_target(folder: str, directory: Path | str | None = None) -> Path:
    """The directory a delete may touch, or raise.

    Every guard is here rather than at the route, so no future caller can
    reach the removal without passing them.
    """
    path = Path(directory) if directory is not None else cache_dir()
    if path is None or not path.is_dir():
        raise DeleteRefused("There is no model cache on this node to delete from.")

    # One path segment. Not a traversal, not nested, not empty.
    if not folder or folder != Path(folder).name or folder in (".", ".."):
        raise DeleteRefused(f"{folder!r} is not a single cache folder name.")
    if not folder.startswith(FOLDER_PREFIX):
        raise DeleteRefused(
            f"{folder!r} is not a model cache folder; those start with "
            f"{FOLDER_PREFIX!r}."
        )

    target = path / folder
    # Resolved, and required to sit directly inside the resolved cache, so a
    # symlink planted in the cache cannot redirect the removal elsewhere.
    try:
        resolved = target.resolve(strict=True)
        root = path.resolve(strict=True)
    except OSError:
        raise DeleteRefused(
            f"{folder!r} is not in the model cache on this node."
        ) from None
    if resolved.parent != root:
        raise DeleteRefused(f"{folder!r} is not directly inside the model cache.")
    if not resolved.is_dir():
        raise DeleteRefused(f"{folder!r} is not a directory.")
    return resolved


def delete(folder: str, directory: Path | str | None = None) -> dict:
    """Remove one cached repository. Returns what it actually reclaimed.

    The size is measured before the removal and the directory's absence is
    confirmed after it, because "the call did not raise" and "the bytes came
    back" are different claims and only the second one is worth reporting.
    """
    target = resolve_target(folder, directory)
    size, _, _ = _blob_bytes(target)
    try:
        shutil.rmtree(target)
    except OSError as exc:
        raise DeleteRefused(
            f"{folder!r} could not be removed: {exc.strerror or exc}."
        ) from None
    return {
        "deleted": not target.exists(),
        "folder": folder,
        "repo_id": repo_from_folder(folder),
        "bytes_freed": size,
    }
