"""A cap on how much of the cluster's load can be retries.

Retrying every 5xx is the right call when a node has died and the wrong one
when the request itself is what the backend objects to. The two are
indistinguishable from here -- a 500 is a 500 -- so rather than guess, this
bounds the blast radius of guessing wrong.

Without a bound, a bad deploy that answers 500 to everything doubles the
cluster's load at the exact moment it can least afford it, which is the classic
way a partial outage becomes a total one. With it, retries are capped at a
fraction of real traffic and the failure stays the size it started.

The floor matters as much as the ratio. A ratio alone would mean a quiet
cluster gets no retries at all -- one request, ten percent of one is zero --
and a single node dying at 3am is exactly when this feature has to work.
"""

from __future__ import annotations

import logging
import time
from collections import deque

log = logging.getLogger("gateway.budget")


class RetryBudget:
    """Token bucket over a trailing window, gateway-wide."""

    def __init__(
        self,
        *,
        ratio: float = 0.1,
        window_s: float = 10.0,
        floor: int = 3,
        clock=time.monotonic,
        events=None,
    ) -> None:
        self._ratio = ratio
        self._window_s = window_s
        self._floor = floor
        self._clock = clock
        self._requests: deque[float] = deque()
        self._retries: deque[float] = deque()
        self._refused = 0
        self._events = events

    def _trim(self, now: float) -> None:
        cutoff = now - self._window_s
        for stamps in (self._requests, self._retries):
            while stamps and stamps[0] < cutoff:
                stamps.popleft()

    def note_request(self) -> None:
        """One client request arrived. Counts toward what retries may spend."""
        now = self._clock()
        self._trim(now)
        self._requests.append(now)

    def take(self) -> bool:
        """Claim one retry. False means answer with what we already have."""
        now = self._clock()
        self._trim(now)
        allowed = max(self._floor, int(len(self._requests) * self._ratio))
        if len(self._retries) >= allowed:
            self._refused += 1
            # Emitted every time, unlike the log line: a counter that is
            # sampled one-in-fifty cannot answer "how often did failover get
            # refused during that incident".
            if self._events is not None:
                self._events.retry_refused("", self.snapshot())
            if self._refused % 50 == 1:
                log.warning(
                    "retry budget exhausted: %d retries against %d requests in %.0fs; "
                    "answering with the upstream's own error instead",
                    len(self._retries),
                    len(self._requests),
                    self._window_s,
                )
            return False
        self._retries.append(now)
        return True

    def snapshot(self) -> dict:
        now = self._clock()
        self._trim(now)
        return {
            "requests": len(self._requests),
            "retries": len(self._retries),
            "allowed": max(self._floor, int(len(self._requests) * self._ratio)),
            "refused": self._refused,
        }
