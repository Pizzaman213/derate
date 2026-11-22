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

**Extra storage lives beneath the HuggingFace root, or it cannot be served.**
A launched container mounts exactly one folder, the cache root
(``~/.cache/huggingface`` -> ``/cache/huggingface``), and the recipe format has
no ``volumes:`` key, so a disk mounted anywhere else is invisible to the
runtime. ``DERATE_EXTRA_CACHE_DIRS`` names folders beneath that root (docker's
``-v`` is a recursive bind, so a disk mounted at ``<root>/extra-ssd`` is
inside the container too); anything else is refused with a sentence. A model
placed on one lives at ``<extra>/models--X`` and ``hub/models--X`` is a
RELATIVE symlink to it -- an absolute one names a host path the container does
not have. sparkrun's ``rsync -a ... <src>/models--X/ <host>:<hub>/models--X``
writes through such a link rather than replacing it, because rsync enters the
destination folder named on its command line. That is derate's own link, and
`linked_folder` is the only thing that recognises one: scan measures through
it, delete removes the folder and the link, and any other symlink keeps being
ignored by the scan and refused by the delete.
"""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Sequence
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


#: Colon-separated folders beneath the HuggingFace root a copy may land in when
#: the hub's own filesystem is short. See `extra_cache_dirs`.
EXTRA_DIRS_ENV = "DERATE_EXTRA_CACHE_DIRS"


def hf_root(cache: Path | str) -> Path:
    """The folder a launched container mounts: the hub cache's parent (``HF_HOME``)."""
    return Path(cache).parent


def extra_cache_dirs(
    cache: Path | str | None = None, raw: str | None = None
) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    """(usable extra folders, resolved, in the order given; one sentence per refused entry).

    An entry is usable only when it is an existing directory that resolves
    strictly beneath the HuggingFace root and outside the hub cache itself. A
    folder that resolves anywhere else -- including through a symlink planted
    beneath the root -- would hold weights no launched container can read.
    """
    text = os.environ.get(EXTRA_DIRS_ENV, "") if raw is None else raw
    entries = [e.strip() for e in text.split(":") if e.strip()]
    if not entries:
        return (), ()
    hub = Path(cache) if cache is not None else cache_dir()
    if hub is None:
        return (), (f"{EXTRA_DIRS_ENV} is set, but this node has no model cache, so none of it is used.",)
    root = hf_root(hub)
    try:
        root_r = root.resolve(strict=True)
    except OSError:
        return (), (f"{EXTRA_DIRS_ENV} is set, but {root} does not exist, so none of it is used.",)
    hub_r = hub.resolve()
    dirs: list[Path] = []
    refused: list[str] = []
    for entry in entries:
        path = Path(entry)
        if not path.is_absolute():
            refused.append(f"{entry} in {EXTRA_DIRS_ENV} is not an absolute path, so it is not used.")
            continue
        try:
            real = path.resolve(strict=True)
        except OSError:
            refused.append(f"{entry} in {EXTRA_DIRS_ENV} does not exist on this node, so it is not used.")
            continue
        if not real.is_dir():
            refused.append(f"{entry} in {EXTRA_DIRS_ENV} is not a directory, so it is not used.")
            continue
        if real == root_r or not real.is_relative_to(root_r):
            refused.append(
                f"{entry} in {EXTRA_DIRS_ENV} is not beneath {root}, the only folder a launched "
                f"model container mounts, so weights there could not be served; mount the disk "
                f"beneath it instead (for example at {root}/extra-ssd)."
            )
            continue
        if real == hub_r or real.is_relative_to(hub_r):
            refused.append(
                f"{entry} in {EXTRA_DIRS_ENV} is the hub cache or inside it, so it adds no room; "
                f"use a folder beside {hub.name}/ instead."
            )
            continue
        if real not in dirs:
            dirs.append(real)
    return tuple(dirs), tuple(refused)


def linked_folder(
    cache: Path | str, folder: str, extras: Sequence[Path] | None = None
) -> Path | None:
    """Where ``cache/folder`` points when it is derate's own link into an extra
    folder, else None.

    Ours means: a relative link whose target, read lexically, is a folder of the
    same name directly inside a configured extra folder and is not itself a
    link. Lexical on purpose, so a link whose disk is not mounted is still
    recognised (and refused for that reason) rather than mistaken for a foreign one.
    """
    link = Path(cache) / folder
    if not link.is_symlink():
        return None
    try:
        text = os.readlink(link)
    except OSError:
        return None
    if os.path.isabs(text):
        return None
    dirs = extra_cache_dirs(cache)[0] if extras is None else tuple(extras)
    where = Path(os.path.normpath(os.path.join(Path(cache).resolve(), text)))
    if where.name != folder or where.parent not in dirs or where.is_symlink():
        return None
    return where


def link_text(cache: Path | str, target: Path) -> str:
    """The relative link ``cache/<name>`` must hold to reach *target*: relative so
    it resolves the same on the host and under the container's mount point."""
    return os.path.relpath(target, Path(cache).resolve())


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


def _blob_bytes(repo: Path, *, follow_symlinks: bool = False) -> tuple[int, int, float | None]:
    """(bytes, blob count, newest mtime) for one repository.

    Only ``blobs/`` is measured, and it is a flat directory, so this is one
    ``scandir`` per repository however many revisions are cached. Measured at
    6 ms for 51 repositories holding 894 GiB.

    ``follow_symlinks`` is for a progress reading of one repo: huggingface_hub
    1.x can land a finished blob as a link into a shared ``hub/blobs/`` store,
    and a download measured by lstat then reads ~0 bytes once it completes.
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
                    # lstat by default, so a symlinked blob counts as the link
                    # it is and its target is not attributed here twice.
                    stat = entry.stat(follow_symlinks=follow_symlinks)
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


def main_commit(repo: Path) -> str | None:
    """The commit `refs/main` names, or None when the folder has no such ref.

    A snapshot folder is named after a commit too, so "the node has the
    coordinator's revision" is this string being among the other's
    `_revisions`."""
    try:
        text = (repo / "refs" / "main").read_text().strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text or None


def revision(repo: Path) -> dict:
    """What a copy of this folder is pinned to: `{"commit", "snapshots"}`."""
    return {"commit": main_commit(repo), "snapshots": _revisions(repo)}


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

    extras = extra_cache_dirs(path)[0]
    for entry in entries:
        if not entry.name.startswith(FOLDER_PREFIX) or not entry.is_dir():
            continue
        if entry.is_symlink():
            # Only derate's own link into an extra folder is measured; any
            # other link would count bytes that live somewhere else.
            real = linked_folder(path, entry.name, extras)
            if real is None:
                continue
            size, blob_count, newest = _blob_bytes(real)
        else:
            size, blob_count, newest = _blob_bytes(entry)
        repos.append(
            {
                "folder": entry.name,
                "repo_id": repo_from_folder(entry.name),
                "bytes": size,
                "blob_count": blob_count,
                "revisions": _revisions(entry),
                "commit": main_commit(entry),
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
        # The one exception: derate's own link into a configured extra folder,
        # where the removal is bounded to that folder's direct child instead.
        if linked_folder(path, folder) != resolved:
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
    base = Path(directory) if directory is not None else cache_dir()
    link = Path(base) / folder  # resolve_target has refused a missing cache
    is_link = link.is_symlink()
    size, _, _ = _blob_bytes(target)
    try:
        shutil.rmtree(target)
        if is_link:
            link.unlink()
    except OSError as exc:
        raise DeleteRefused(
            f"{folder!r} could not be removed: {exc.strerror or exc}."
        ) from None
    return {
        "deleted": not target.exists() and not os.path.lexists(link),
        "folder": folder,
        "repo_id": repo_from_folder(folder),
        "bytes_freed": size,
    }
