"""Deployment event stream.

Agent G consumes this: memory_critical is what tells the gateway to stop
admitting to a deployment, and memory_cleared is what lets it start again.
Agent H renders state_changed and launch_refused.

Producers are background threads; consumers are asyncio tasks. emit() is
therefore thread-safe and never blocks the producer: a subscriber that stops
draining its queue gets its oldest events dropped rather than stalling the
health watch.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)

# Event type names. Agent G codes against these constants, not string literals.
STATE_CHANGED = "state_changed"
LAUNCH_REFUSED = "launch_refused"
LAUNCH_FAILED = "launch_failed"
MEMORY_WARNING = "memory_warning"
MEMORY_CRITICAL = "memory_critical"
MEMORY_CLEARED = "memory_cleared"
NODE_UNHEALTHY = "node_unhealthy"
BACKEND_LOST = "backend_lost"
FIT_MISS = "fit_miss"
STOP_ESCALATED = "stop_escalated"
RECONCILED = "reconciled"

#: Memory fraction at which we warn and mark DEGRADED.
MEMORY_WARN_FRACTION = 0.90
#: Memory fraction at which the gateway must stop admitting. We never kill;
#: shedding load is recoverable, killing a loaded 120B model is not.
MEMORY_CRITICAL_FRACTION = 0.95

_HISTORY = 256
_QUEUE_MAX = 512


class EventBus:
    """Fan-out of deployment events to any number of async subscribers."""

    def __init__(self, history: int = _HISTORY) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._history: deque[dict[str, Any]] = deque(maxlen=history)

    # -- producer side, called from any thread ---------------------------

    def emit(self, type: str, **fields: Any) -> dict[str, Any]:
        event: dict[str, Any] = {"type": type, "ts": time.time()}
        event.update(fields)
        with self._lock:
            self._history.append(event)
            targets = list(self._subscribers)
        for loop, queue in targets:
            try:
                loop.call_soon_threadsafe(self._offer, queue, event)
            except RuntimeError:
                # Subscriber's loop is closed; it will be reaped on unsubscribe.
                logger.debug("dropping event for a closed loop", exc_info=True)
        return event

    @staticmethod
    def _offer(queue: asyncio.Queue, event: dict[str, Any]) -> None:
        if queue.full():
            try:
                queue.get_nowait()  # drop oldest, keep the stream live
            except asyncio.QueueEmpty:
                pass
        queue.put_nowait(event)

    # -- consumer side ---------------------------------------------------

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        """Yield events from now on. Cancel the consuming task to unsubscribe."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
        entry = (loop, queue)
        with self._lock:
            self._subscribers.append(entry)
        try:
            while True:
                yield await queue.get()
        finally:
            with self._lock:
                if entry in self._subscribers:
                    self._subscribers.remove(entry)

    def recent(self, limit: int = _HISTORY) -> list[dict[str, Any]]:
        """The last *limit* events. For a UI that connects after the fact."""
        with self._lock:
            return list(self._history)[-limit:]

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)
