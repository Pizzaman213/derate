"""A bounded waiting room for requests whose model has no live target.

The standing rule is 503 rather than queueing *indefinitely*. This is the
bounded case that rule leaves room for: a model that was serving a moment ago
and whose node has just died. Holding such a request for a few seconds turns a
node restart into added latency instead of a wall of 502s, and every way out of
the lot is bounded -- by a grace period, by a queue depth, and by the size of
the body being held.

What is deliberately never parked: a request that admission control refused.
Rate limited, draining, and memory critical are decisions, not outages, and
answering them with a wait would hide the very thing the operator needs to see.
That distinction is made by the router, in :meth:`Router.parkable`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field

from .settings import GatewaySettings

log = logging.getLogger("gateway.parking")


class _Ticket:
    """Identity only. A waiter's place in line."""

    __slots__ = ()


@dataclass
class _Queue:
    tickets: deque = field(default_factory=deque)
    # Set whenever a waiter leaves, so the next in line wakes at once instead
    # of sitting out the rest of its poll interval.
    changed: asyncio.Event = field(default_factory=asyncio.Event)


class ParkingLot:
    """FIFO per served_name, bounded globally and per model."""

    def __init__(
        self, settings: GatewaySettings, clock=time.monotonic, events=None
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._queues: dict[str, _Queue] = {}
        self._parked = 0
        self._closed = False
        self._events = events

    # -- reads -------------------------------------------------------------

    def parked(self, served_name: str | None = None) -> int:
        if served_name is None:
            return self._parked
        queue = self._queues.get(served_name)
        return len(queue.tickets) if queue else 0

    def close(self) -> None:
        """Stop holding anyone. Waiters give up on their next tick and get the
        same 503 they would have got without the lot, which is a better answer
        during shutdown than a connection that never closes."""
        self._closed = True
        for queue in list(self._queues.values()):
            queue.changed.set()

    def accepts(self, served_name: str, body_bytes: int) -> bool:
        """Whether one more request would fit. Overflow is refused, not queued."""
        s = self._settings
        if self._closed or s.park_grace_s <= 0:
            return False
        if body_bytes > s.park_max_body_bytes:
            return False
        if self._parked >= s.park_max_waiters:
            return False
        return self.parked(served_name) < s.park_max_per_model

    # -- waiting -----------------------------------------------------------

    async def park(self, served_name: str, *, select, is_disconnected=None):
        """Wait for ``select()`` to return something, or give up.

        ``select`` is called only when this waiter is at the head of the line,
        so a recovering node is handed to the request that has waited longest.
        Returns whatever ``select`` returned, or None on timeout, on client
        disconnect, or when the lot is full.
        """
        s = self._settings
        queue = self._queues.setdefault(served_name, _Queue())
        ticket = _Ticket()
        queue.tickets.append(ticket)
        self._parked += 1
        deadline = self._clock() + s.park_grace_s
        log.info(
            "holding a request for '%s' for up to %.1fs; %d now waiting",
            served_name,
            s.park_grace_s,
            self._parked,
        )
        # The gauge this queue has never had. Recorded on the way in and, with
        # the outcome, on the way out, so "how long were requests held and did
        # holding them help" is answerable after the fact.
        if self._events is not None:
            self._events.request_parked(
                served_name, len(queue.tickets), self._parked
            )
        entered = self._clock()
        outcome = "timeout"
        try:
            while True:
                if self._closed:
                    outcome = "closed"
                    return None
                if queue.tickets and queue.tickets[0] is ticket:
                    result = select()
                    if result is not None:
                        outcome = "recovered"
                        return result
                if is_disconnected is not None and await is_disconnected():
                    log.info("a parked request for '%s' hung up", served_name)
                    outcome = "disconnected"
                    return None
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return None
                queue.changed.clear()
                try:
                    await asyncio.wait_for(
                        queue.changed.wait(),
                        timeout=min(s.park_poll_interval_s, remaining),
                    )
                except TimeoutError:
                    pass
        finally:
            try:
                queue.tickets.remove(ticket)
            except ValueError:  # pragma: no cover -- defensive
                pass
            self._parked -= 1
            queue.changed.set()
            if not queue.tickets:
                self._queues.pop(served_name, None)
            if self._events is not None:
                self._events.park_resolved(
                    served_name, outcome, self._clock() - entered
                )
