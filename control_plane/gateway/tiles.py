"""OpenStreetMap tiles for the request map, fetched once and served same-origin.

The browser never talks to tile.openstreetmap.org. The CSP's
``img-src 'self' data: blob:`` (``headers.py``) stays as it is, and the
coordinator asks OSM for each tile once, keeps it under ``data_dir()/tiles``
and answers ``GET /api/map/tiles/{z}/{x}/{y}`` from disk after that -- the same
shape as ``resolver/avatars.py``.

OSM's tile usage policy is the reason for every constant below: an identifying
User-Agent, at most two downloads at a time, honour caching, no bulk download.
Zoom stops at :data:`MAX_ZOOM`: street level is enough to see where a point
sits, and OSM's usage policy forbids bulk use.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import httpx

from control_plane import paths

TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
MIN_ZOOM = 1
MAX_ZOOM = 15
#: A cached tile is refetched after this long.
TILE_TTL_S = 30 * 86400
MAX_TILE_BYTES = 1024 * 1024
#: OSM's policy: at most two download threads.
_MAX_CONCURRENT = 2
#: OSM requires a User-Agent that identifies the application.
USER_AGENT = "derate/map-tiles (+https://github.com/pizzaman213/derate)"
_TIMEOUT_S = 10.0
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_semaphore: asyncio.Semaphore | None = None
_locks: dict[tuple[int, int, int], asyncio.Lock] = {}


def cache_dir() -> Path:
    return paths.data_dir() / "tiles"


def tile_path(z: int, x: int, y: int) -> Path:
    return cache_dir() / f"{z}/{x}/{y}.png"


def valid(z: int, x: int, y: int) -> bool:
    """Whether (z, x, y) names a tile this proxy will fetch."""
    return MIN_ZOOM <= z <= MAX_ZOOM and 0 <= x < 2**z and 0 <= y < 2**z


def _gate() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
    return _semaphore


def _lock_for(key: tuple[int, int, int]) -> asyncio.Lock:
    """One in-flight fetch per tile, so a burst of map loads is one request."""
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def _prune_locks() -> None:
    """Forget every per-tile lock nobody holds or waits on."""
    for key in [k for k, lock in _locks.items() if not lock.locked()]:
        del _locks[key]


async def fetch(z: int, x: int, y: int) -> bytes | None:
    """One tile from OSM. None when OSM has no such tile; raises otherwise."""
    async with httpx.AsyncClient(
        timeout=_TIMEOUT_S, follow_redirects=False, headers={"user-agent": USER_AGENT}
    ) as client:
        resp = await client.get(TILE_URL.format(z=z, x=x, y=y))
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise RuntimeError(f"tile.openstreetmap.org answered {resp.status_code}")
    body = resp.content
    if not body.startswith(_PNG_MAGIC) or len(body) > MAX_TILE_BYTES:
        raise RuntimeError("not a PNG tile")
    return body


def _read_fresh(path: Path) -> bytes | None:
    """The cached tile when it exists and is younger than the TTL. Never raises."""
    try:
        if time.time() - path.stat().st_mtime >= TILE_TTL_S:
            return None
        return path.read_bytes()
    except OSError:
        return None


def _write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(".part")
    part.write_bytes(body)
    os.replace(part, path)


async def tile(z: int, x: int, y: int) -> bytes | None:
    """The PNG for (z, x, y): from disk when fresh, else fetched once and kept.
    None when OSM has no such tile. Fetch errors propagate; a cache read or
    write never does."""
    path = tile_path(z, x, y)
    hit = await asyncio.to_thread(_read_fresh, path)
    if hit is not None:
        return hit
    key = (z, x, y)
    try:
        async with _lock_for(key):
            hit = await asyncio.to_thread(_read_fresh, path)
            if hit is not None:
                return hit
            async with _gate():
                body = await fetch(z, x, y)
            if body is not None:
                try:
                    await asyncio.to_thread(_write, path, body)
                except OSError:
                    pass  # served anyway; the next request fetches again
            return body
    finally:
        _prune_locks()
