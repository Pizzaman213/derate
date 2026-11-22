"""The copy protocol: a manifest over HTTP, then raw chunks over N TCP streams.

The manifest is opened on the receiving node's agent (`POST /agent/transfers`),
which answers with a session id, a one-session secret and the receiver's fabric
addresses. Each stream then connects to `TRANSFER_PORT`:

1. client: `MAGIC` + the 16 ASCII bytes of the session id
2. server: a 32-byte nonce, or `ACK_REFUSED` and close for an unknown session
3. client: `handshake_mac(secret, nonce)` (32 bytes)
4. server: `ACK_OK`, or close on a mismatch
5. loop -- client: `CHUNK_HEADER` (END_OF_STREAM closes), then `length` raw
   bytes; server: one ack byte.

Nothing on the wire is trusted for placement: every path comes from a manifest
validated here, so a chunk can only ever land inside the folder it names.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import posixpath
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAGIC = b"DRTX0001"
TRANSFER_PORT = 47101
CHUNK_BYTES = 64 << 20
RECV_BUF_BYTES = 4 << 20
DEFAULT_STREAMS = 8
MAX_STREAMS = 32
MAX_SESSIONS = 4
SESSION_IDLE_S = 300.0
FREE_SPACE_RESERVE = 10 << 30
PARTIAL_SUFFIX = ".derate-partial"
END_OF_STREAM = 0xFFFFFFFF
CHUNK_HEADER = struct.Struct("!IQQ")  # file_index, offset, length
ACK_OK, ACK_REFUSED = b"\x00", b"\x01"
FOLDER_RE = re.compile(r"^models--[A-Za-z0-9._-]+$")
# `trees/` is huggingface_hub's cached file listing (`trees/<sha>.json`); a
# fifth of the folders on these boxes have one.
TOP_LEVEL = ("blobs", "snapshots", "refs", ".no_exist", "trees")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")


class ManifestError(ValueError):
    """One sentence saying why a manifest cannot be used."""


@dataclass(frozen=True)
class FileEntry:
    path: str
    size: int
    mode: int
    mtime: float


@dataclass(frozen=True)
class LinkEntry:
    path: str
    target: str
    mtime: float


@dataclass(frozen=True)
class Manifest:
    folder: str
    files: tuple[FileEntry, ...]
    links: tuple[LinkEntry, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "folder": self.folder,
            "files": [
                {"path": f.path, "size": f.size, "mode": f.mode, "mtime": f.mtime} for f in self.files
            ],
            "links": [{"path": ln.path, "target": ln.target, "mtime": ln.mtime} for ln in self.links],
        }

    @classmethod
    def from_json(cls, d: Any) -> Manifest:
        if not isinstance(d, dict):
            raise ManifestError("the manifest must be a JSON object")
        folder = d.get("folder")
        if not isinstance(folder, str) or not FOLDER_RE.match(folder) or ".." in folder:
            raise ManifestError("folder must be a model cache folder, models--owner--name")
        raw_files = d.get("files", [])
        raw_links = d.get("links", [])
        if not isinstance(raw_files, list) or not isinstance(raw_links, list):
            raise ManifestError("files and links must be lists")
        seen: set[str] = set()
        files: list[FileEntry] = []
        for item in raw_files:
            if not isinstance(item, dict):
                raise ManifestError("every file entry must be an object")
            path = _checked_path(item.get("path"), seen)
            size = item.get("size")
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise ManifestError(f"{path} has no valid size")
            mode = item.get("mode", 0o644)
            if not isinstance(mode, int) or isinstance(mode, bool):
                raise ManifestError(f"{path} has no valid mode")
            files.append(FileEntry(path=path, size=size, mode=mode & 0o777, mtime=_mtime(item, path)))
        links: list[LinkEntry] = []
        for item in raw_links:
            if not isinstance(item, dict):
                raise ManifestError("every link entry must be an object")
            path = _checked_path(item.get("path"), seen)
            target = item.get("target")
            if not isinstance(target, str) or not target or "\x00" in target:
                raise ManifestError(f"{path} points outside the folder")
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
            if posixpath.isabs(target) or resolved.startswith("..") or resolved.startswith("/"):
                raise ManifestError(f"{path} points outside the folder")
            links.append(LinkEntry(path=path, target=target, mtime=_mtime(item, path)))
        return cls(folder=folder, files=tuple(files), links=tuple(links))


def _checked_path(path: Any, seen: set[str]) -> str:
    if not isinstance(path, str):
        raise ManifestError(f"{path!r} is not a path inside a model cache folder")
    segments = path.split("/")
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or "\x00" in path
        or any(s in ("", ".", "..") for s in segments)
        or segments[0] not in TOP_LEVEL
    ):
        raise ManifestError(f"{path} is not a path inside a model cache folder")
    if path in seen:
        raise ManifestError(f"{path} is listed twice")
    seen.add(path)
    return path


def _mtime(item: dict[str, Any], path: str) -> float:
    mtime = item.get("mtime", 0.0)
    if not isinstance(mtime, (int, float)) or isinstance(mtime, bool):
        raise ManifestError(f"{path} has no valid mtime")
    return float(mtime)


def build_manifest(folder_path: Path) -> Manifest:
    """Describe a cache folder on the sending side. Refuses one still downloading.

    A symlink that resolves inside the folder (a snapshot entry pointing at its
    blob) travels as a link. One that resolves outside it -- huggingface_hub 1.x
    lands a xet blob as a link into a shared `hub/blobs/` store -- travels as the
    file it points at, because the receiver refuses a link out of the folder and
    has no such store to point into.
    """
    folder_path = Path(folder_path)
    folder = folder_path.name
    files: list[FileEntry] = []
    links: list[LinkEntry] = []
    for root, dirs, names in os.walk(folder_path, followlinks=False):
        dirs.sort()
        rel_root = Path(root).relative_to(folder_path).as_posix()
        # os.walk lists a symlink to a directory under dirs, not names.
        entries = sorted(names) + [d for d in dirs if os.path.islink(os.path.join(root, d))]
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
        for name in entries:
            if name.endswith(".incomplete"):
                raise ManifestError(f"{folder} is still downloading ({name} is incomplete)")
            if name.endswith(".lock"):
                continue
            full = os.path.join(root, name)
            rel = name if rel_root == "." else f"{rel_root}/{name}"
            st = os.lstat(full)
            if os.path.islink(full):
                target = os.readlink(full)
                inside = posixpath.normpath(posixpath.join(posixpath.dirname(rel), target))
                if not posixpath.isabs(target) and not inside.startswith(".."):
                    links.append(LinkEntry(path=rel, target=target, mtime=st.st_mtime))
                    continue
                if not os.path.isfile(full):
                    raise ManifestError(f"{rel} is a link to {target}, which is not a file")
                st = os.stat(full)
            elif not os.path.isfile(full):
                continue
            files.append(FileEntry(path=rel, size=st.st_size, mode=st.st_mode & 0o777, mtime=st.st_mtime))
    return Manifest(folder=folder, files=tuple(files), links=tuple(links))


def handshake_mac(secret: bytes, nonce: bytes) -> bytes:
    return hmac.new(secret, nonce, hashlib.sha256).digest()


def expected_digest(entry: FileEntry) -> tuple[str, str] | None:
    """What a blob's name says its content hashes to, or None for a non-blob."""
    head, _, name = entry.path.rpartition("/")
    if head != "blobs":
        return None
    if _SHA256_RE.match(name):
        return ("sha256", name)
    if _SHA1_RE.match(name):
        return ("git-sha1", name)
    return None


def new_digest(kind: str, size: int) -> Any:
    """The hash a blob of *size* bytes is named by, with git's header already fed."""
    if kind == "sha256":
        return hashlib.sha256()
    if kind == "git-sha1":
        h = hashlib.sha1(usedforsecurity=False)
        h.update(b"blob %d\0" % size)
        return h
    raise ValueError(f"unknown digest kind {kind}")
