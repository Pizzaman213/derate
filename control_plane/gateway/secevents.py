"""Refusals as evidence: a rejected key, a missing session, a refused join.

Before this, every refusal at the gate returned before any handler and left
nothing but uvicorn's access line -- which rotates out in about a day and
names no key. An intruder probing keys or a rogue node trying the join route
was indistinguishable from silence.

Each event is two things:

* a WARNING on logger ``gateway.security`` when a row is opened, carrying the
  count folded into it -- not on every repeat, because a flood of refusals
  would otherwise rotate ``node.log``'s whole history away in minutes;
* an audit row (``AuthService.audit``), at most one per ``(action, actor,
  ip)`` per :data:`WINDOW_S`, whose ``after`` carries ``count`` -- the events
  folded into it since the previous row for that triple. A credential-stuffing
  run is one row a minute per source, not a million rows, and still says how
  many.

For the refusals whose "actor" is a name the caller chose -- a ``derate-<id>``
for any ten characters, a ``node_id`` out of an open join body, a setup
attempt -- the fold key is ``(action, "-", address bucket)`` and the presented
name moves to ``after["presented"]`` (the one that opened the row). Folding on
the chosen name let every fresh fake id cost a committed row and an fsync; the
bucket is :func:`address_bucket`, the same IPv6 /64 the key throttle counts,
for the same reason. Every other anonymous event folds per bucket too; an
event with a real actor folds per full address. A caller-supplied path is cut
to :data:`_DETAIL_MAX`.

The row is written off the request path: :meth:`SecurityEvents.note` puts it
on a bounded queue and one daemon thread drains it. A full queue drops the
row and counts it; the next row written carries ``after["dropped"]``.

A failure to write the row is logged and swallowed: recording a refusal must
never turn it into a 500.
"""

from __future__ import annotations

import ipaddress
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
#: Hard bound on tracked triples. Past it the oldest row's triple is dropped,
#: its pending count written as a row of its own first.
_SEEN_MAX = 65_536
#: A caller-supplied path or target id is cut to this many characters.
_DETAIL_MAX = 256
#: Rows waiting for the writer thread. Past this, rows are dropped and counted.
QUEUE_MAX = 10_000

#: Refusals whose actor is whatever the caller typed: folded per address.
_FOLD_BY_IP = frozenset(
    {AuditAction.KEY_REFUSED, AuditAction.JOIN_REJECTED, AuditAction.SETUP_REFUSED}
)


def address_bucket(ip: str | None) -> str:
    """The unit one caller is counted as: the address, except an IPv6 address
    counts under its /64 -- one subscriber is routinely handed a whole /64,
    so per-address counting gives an IPv6 caller 2**64 budgets for free. An
    IPv4-mapped address counts as the IPv4 address. Not an address: as is.
    The key throttle (``authz.RefusalWindow``) and the security-event fold
    both count with this, so the two cannot disagree about who one caller is."""
    if not ip:
        return ""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


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
        #: (action, actor, address bucket for actor "-", else the address) ->
        #: [monotonic time of the last row, events folded since, the last full
        #: address seen]
        self._seen: dict[tuple[str, str, str], list] = {}
        self._queue: queue.Queue[tuple[tuple, dict]] = queue.Queue(maxsize=queue_max)
        self._dropped = 0
        self._thread: threading.Thread | None = None
        self._pruned_at = float("-inf")

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

    def close(self, timeout: float = 5.0) -> bool:
        """Write every folded count no row has reported yet, then wait for the
        queue. Once, at shutdown, before the audit store closes: a stopped
        flood's last burst otherwise lives only in ``_seen`` until a prune or
        an eviction that never comes, and the writer is a daemon thread that
        dies with the process. True when drained."""
        if self._audit is None:
            return True
        rows: list[tuple[tuple, dict]] = []
        with self._lock:
            for k, entry in self._seen.items():
                rows.extend(self._pending_row(k, entry))
                entry[1] = 0
        for row in rows:
            try:
                self._queue.put_nowait(row)
            except queue.Full:
                with self._lock:
                    self._dropped += 1
        if rows:
            self._ensure_writer()
        return self.flush(timeout)

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
        # Every string the caller could have chosen is cut -- the path, an
        # Origin header, anything a future caller adds to detail -- or one
        # request writes a permanent row as large as the header it sent.
        detail = {k: (v[:_DETAIL_MAX] if isinstance(v, str) else v) for k, v in (detail or {}).items()}
        target_id = (target_id or "")[:_DETAIL_MAX]
        actor = (actor or "-")[:254]
        if action in _FOLD_BY_IP:
            if actor != "-":
                detail["presented"] = actor
            actor = "-"
        now = self._clock()
        # An anonymous event (actor "-", which includes every caller-chosen
        # name above) folds per address BUCKET -- an IPv6 /64, as the key
        # throttle counts it -- or one /64 would open a fresh row, two fsyncs
        # and a permanent audit entry per request by rotating its low bits
        # (a cross-origin refusal did exactly that after KEY_REFUSED was
        # fixed). The row still records the full address that opened it, and
        # a later count row the last full address seen. An event with a real
        # actor (a session moving, an admin's own key) is not a flood one
        # caller can rotate: it folds per full address, so two machines on
        # one SLAAC /64 stay two rows with their own user agent and detail.
        key = (action.value, actor, address_bucket(ip) if actor == "-" else (ip or ""))
        flushed: list[tuple[tuple, dict]] = []
        with self._lock:
            entry = self._seen.get(key)
            if entry is not None and now - entry[0] < self._window:
                entry[1] += 1
                entry[2] = ip
                return
            count = 1 + (int(entry[1]) if entry is not None else 0)
            # Re-inserted rather than assigned, so dict order is the order
            # rows were opened and the oldest are first to go.
            self._seen.pop(key, None)
            self._seen[key] = [now, 0, ip]
            if len(self._seen) > _PRUNE_AT and now - self._pruned_at >= self._window:
                flushed = self._prune(now)
                self._pruned_at = now
            while len(self._seen) > _SEEN_MAX:
                flushed.extend(self._evict_oldest())
            dropped, self._dropped = self._dropped, 0
        # The WARNING goes with the row, not with every folded repeat: a
        # flood would otherwise rotate node.log's whole history away in
        # minutes. The row's count says how many were folded in.
        # printable(): actor and detail are whatever the caller typed, and a
        # newline in them would forge a whole line in node.log. The row
        # below keeps them raw -- it is structured, not a line.
        log.warning(
            "%s actor=%s ip=%s rid=%s count=%d %s",
            action.value,
            printable(actor),
            printable(ip or "-"),
            printable(request_id or "-"),
            count,
            " ".join(f"{k}={printable(str(v))}" for k, v in sorted(detail.items())),
        )
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

    def _evict_oldest(self) -> list[tuple[tuple, dict]]:
        """Drop the triple whose row was opened longest ago, reporting any
        events folded into it since as a row of their own. Caller holds the
        lock."""
        k = next(iter(self._seen))
        return self._pending_row(k, self._seen.pop(k))

    @staticmethod
    def _pending_row(k: tuple, entry: list) -> list[tuple[tuple, dict]]:
        """The row for events folded into *entry* since its last row, if any.
        ``ip`` is a real address -- the last one seen -- so a search by
        address finds it. An anonymous triple's third element is the /64 it
        stood for, carried in ``after["bucket"]`` when it differs; a named
        actor's is the address itself, so it carries none."""
        pending, last_ip = int(entry[1]), entry[2]
        if not pending:
            return []
        action_value, actor, where = k
        after = {"count": pending}
        if actor == "-" and where and where != last_ip:
            after["bucket"] = where
        return [(
            (actor, AuditAction(action_value), "security", ""),
            {"after": after, "ip": last_ip or None, "request_id": None, "user_agent": None},
        )]

    def _prune(self, now: float) -> list[tuple[tuple, dict]]:
        """Drop triples idle for ten windows. One still holding folded events
        that no row has reported yet is not dropped silently: its count is
        returned as a row of its own first (EV-3), so a quiet source's last
        burst is never lost to the table's size bound. Caller holds the lock."""
        stale = now - 10 * self._window
        rows: list[tuple[tuple, dict]] = []
        for k in [k for k, v in self._seen.items() if v[0] < stale]:
            rows.extend(self._pending_row(k, self._seen.pop(k)))
        return rows
