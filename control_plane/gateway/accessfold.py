"""Fold the UI's routine reads into one access line/row per minute.

A signed-in browser polls a dozen /api reads every few seconds; written one
line and one row each, that traffic was 99.8% of the ``access`` table and
rotated ``node.log``'s history away in a day. Here a session's successful read
of an /api path is folded per (who, address, method, path, query, status):
the first hit in a window is written at once, the rest are counted and written
as ONE record carrying the count when the window closes. The sum of the counts
written always equals the number of hits offered.

Must not import ``authz``: ``authz`` imports this module.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable

WINDOW_S = 60.0
MAX_KEYS = 4096


@dataclass
class AccessHit:
    ctx: Any
    request_id: str
    ts: float
    client_ip: str | None
    client_port: int | None
    peer_ip: str | None
    who: str
    user_agent: str | None
    method: str
    path: str
    query: str
    status: int | None
    duration_ms: float


def foldable(hit: AccessHit) -> bool:
    # A session's successful read of an /api path. Nothing else ever folds:
    # writes, errors, /v1, keys, sockets and open paths each keep their own row.
    return (hit.who.startswith("session:") and hit.method in ("GET", "HEAD")
            and hit.path.startswith("/api/") and hit.status is not None
            and (200 <= hit.status < 300 or hit.status == 304))


@dataclass
class _Open:
    opened: float
    pending: int
    last: AccessHit | None


class AccessFold:
    def __init__(self, write: Callable[[AccessHit, int], None], *,
                 window_s: float = WINDOW_S, max_keys: int = MAX_KEYS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._write = write
        self._window = window_s
        self._max = max_keys
        self._clock = clock
        self._open: OrderedDict[tuple, _Open] = OrderedDict()

    def _drain(self, entry: _Open) -> None:
        if entry.pending > 0 and entry.last is not None:
            self._write(entry.last, entry.pending)

    def offer(self, hit: AccessHit) -> None:
        key = (hit.who, hit.client_ip, hit.method, hit.path, hit.query, hit.status)
        now = self._clock()
        entry = self._open.get(key)
        if entry is None or now - entry.opened >= self._window:
            if entry is not None:
                del self._open[key]
                self._drain(entry)
            self._write(hit, 1)
            self._open[key] = _Open(opened=now, pending=0, last=None)
            while len(self._open) > self._max:
                _, old = self._open.popitem(last=False)
                self._drain(old)
        else:
            entry.pending += 1
            entry.last = hit

    def flush_due(self) -> None:
        now = self._clock()
        for key in [k for k, e in self._open.items() if now - e.opened >= self._window]:
            self._drain(self._open.pop(key))

    def flush_all(self) -> None:
        entries = list(self._open.values())
        self._open.clear()
        for entry in entries:
            self._drain(entry)
