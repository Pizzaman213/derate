"""Refusals as evidence: a rejected key, a missing session, a refused join.

Before this, every refusal at the gate returned before any handler and left
nothing but uvicorn's access line -- which rotates out in about a day and
names no key. An intruder probing keys or a rogue node trying the join route
was indistinguishable from silence.

Each event is two things:

* a WARNING on logger ``gateway.security``, every time, so ``node.log``
  carries the full rate;
* an audit row (``AuthService.audit``), at most one per ``(action, actor,
  ip)`` per :data:`WINDOW_S`, whose ``after`` carries ``count`` -- the events
  folded into it since the previous row for that triple. A credential-stuffing
  run is one row a minute per source, not a million rows, and still says how
  many.

For the refusals whose "actor" is a name the caller chose -- a ``derate-<id>``
for any ten characters, a ``node_id`` out of an open join body, a setup
attempt -- the fold key is ``(action, "-", ip)`` and the presented name moves
to ``after["presented"]`` (the one that opened the row; every later one is in
the WARNING line). Folding on the chosen name let every fresh fake id cost a
committed row and an fsync.

The row is written off the request path: :meth:`SecurityEvents.note` puts it
on a bounded queue and one daemon thread drains it. A full queue drops the
row and counts it; the next row written carries ``after["dropped"]``.

A failure to write the row is logged and swallowed: recording a refusal must
never turn it into a 500.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable

from control_plane.contracts.auth import AuditAction
from control_plane.redaction import printable

log = logging.getLogger("gateway.security")

#: One audit row per (action, actor, ip) per this many seconds.
WINDOW_S = 60.0
#: Above this many tracked triples, the ones idle for ten windows are dropped.
_PRUNE_AT = 4096
#: Rows waiting for the writer thread. Past this, rows are dropped and counted.
QUEUE_MAX = 10_000

#: Refusals whose actor is whatever the caller typed: folded per address.
_FOLD_BY_IP = frozenset(
    {AuditAction.KEY_REFUSED, AuditAction.JOIN_REJECTED, AuditAction.SETUP_REFUSED}
)


class SecurityEvents:
    """Rate-limited security-event recorder. Constructed once per gateway."""

    def __init__(
        self,
        audit: Callable[..., None] | None,
        *,
        window_s: float = WINDOW_S,
        clock: Callable[[], float] = time.monotonic,
        queue_max: int = QUEUE_MAX,
    ) -> None:
        #: ``audit(actor, action, target_kind, target_id, *, after, ip,
        #: request_id, user_agent)`` -- ``AuthService.audit``'s signature.
        self._audit = audit
        self._window = window_s
        self._clock = clock
        self._lock = threading.Lock()
        #: (action, actor, ip) -> [monotonic time of the last row, events since]
        self._seen: dict[tuple[str, str, str], list[float]] = {}
        self._queue: queue.Queue[tuple[tuple, dict]] = queue.Queue(maxsize=queue_max)
        self._dropped = 0
        self._thread: threading.Thread | None = None

    @property
    def dropped(self) -> int:
        """Rows dropped on a full queue and not yet reported in a row."""
        return self._dropped

    def flush(self, timeout: float = 5.0) -> bool:
        """Block until every queued row is written, or *timeout*. True when
        drained. For tests and shutdown; the request path never calls it."""
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        return True

    def _ensure_writer(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._drain, name="secevents-writer", daemon=True
            )
            self._thread.start()

    def _drain(self) -> None:
        while True:
            args, kwargs = self._queue.get()
            try:
                self._audit(*args, **kwargs)
            except Exception:
                log.error("could not write the %s audit row", args[1].value, exc_info=True)
            finally:
                self._queue.task_done()

    def note(
        self,
        action: AuditAction,
        *,
        actor: str,
        ip: str | None,
        user_agent: str | None = None,
        request_id: str | None = None,
        target_kind: str = "security",
        target_id: str = "",
        detail: dict | None = None,
    ) -> None:
        detail = dict(detail or {})
        actor = (actor or "-")[:254]
        # printable(): actor and detail are whatever the caller typed, and a
        # newline in them would forge a whole line in node.log. The row
        # below keeps them raw -- it is structured, not a line.
        log.warning(
            "%s actor=%s ip=%s rid=%s %s",
            action.value,
            printable(actor),
            printable(ip or "-"),
            printable(request_id or "-"),
            " ".join(f"{k}={printable(str(v))}" for k, v in sorted(detail.items())),
        )
        if action in _FOLD_BY_IP:
            if actor != "-":
                detail["presented"] = actor
            actor = "-"
        now = self._clock()
        key = (action.value, actor, ip or "")
        flushed: list[tuple[tuple, dict]] = []
        with self._lock:
            entry = self._seen.get(key)
            if entry is not None and now - entry[0] < self._window:
                entry[1] += 1
                return
            count = 1 + (int(entry[1]) if entry is not None else 0)
            self._seen[key] = [now, 0]
            if len(self._seen) > _PRUNE_AT:
                flushed = self._prune(now)
            dropped, self._dropped = self._dropped, 0
        if self._audit is None:
            return
        after = {"count": count, **detail}
        if dropped:
            after["dropped"] = dropped
        row = (
            (actor, action, target_kind, target_id or (detail.get("path") or "")),
            {"after": after, "ip": ip, "request_id": request_id, "user_agent": user_agent},
        )
        for queued in [*flushed, row]:
            try:
                self._queue.put_nowait(queued)
            except queue.Full:
                # The row's own carried "dropped" count is lost with it.
                with self._lock:
                    self._dropped += 1 + (dropped if queued is row else 0)
        self._ensure_writer()

    def _prune(self, now: float) -> list[tuple[tuple, dict]]:
        """Drop triples idle for ten windows. One still holding folded events
        that no row has reported yet is not dropped silently: its count is
        returned as a row of its own first (EV-3), so a quiet source's last
        burst is never lost to the table's size bound. Caller holds the lock."""
        stale = now - 10 * self._window
        rows: list[tuple[tuple, dict]] = []
        for k in [k for k, v in self._seen.items() if v[0] < stale]:
            pending = int(self._seen.pop(k)[1])
            if pending:
                action_value, actor, ip = k
                rows.append((
                    (actor, AuditAction(action_value), "security", ""),
                    {"after": {"count": pending}, "ip": ip or None,
                     "request_id": None, "user_agent": None},
                ))
        return rows
