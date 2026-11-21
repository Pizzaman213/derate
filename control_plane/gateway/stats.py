"""Live per-target counters.

The gateway is the only component that sees every request end to end, which
makes it the authoritative source for measured decode throughput and TTFT.
The routing strength score prefers these numbers over any estimate.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

_EWMA_ALPHA = 0.2


def _ewma(prev: float | None, sample: float, alpha: float = _EWMA_ALPHA) -> float:
    if prev is None:
        return sample
    return (1.0 - alpha) * prev + alpha * sample


#: Live tokens landing within this many seconds of the last entry share it.
_LIVE_BUCKET_S = 0.25


@dataclass
class TargetStats:
    """Counters for one routing target."""

    target_id: str
    outstanding: int = 0
    completed: int = 0
    failed: int = 0
    total_tokens: int = 0
    decode_tps: float | None = None
    ttft_ms: float | None = None
    mean_duration_s: float | None = None
    last_completed_at: float | None = None
    # When the latest request was admitted; the idle-unload clock reads it.
    last_begin_at: float | None = None
    # (timestamp, tokens) for a short rolling throughput window
    _recent: deque[tuple[float, int]] = field(default_factory=lambda: deque(maxlen=512))

    def begin(self) -> None:
        self.outstanding += 1
        self.last_begin_at = time.time()

    def note_tokens(self, tokens: int, now: float | None = None) -> None:
        """Tokens seen leaving the target while it is still streaming.

        Counted into the throughput window the moment they pass through the
        proxy, so ``tokens_per_sec`` rises when decoding starts and falls when
        it stops -- not ten seconds after the response has already finished,
        which is what counting only at ``complete()`` drew. Merged into
        quarter-second buckets: a chunk per entry would overrun ``_recent``'s
        512 slots in half a second at 1000 tok/s and under-report the rate.
        """
        if tokens <= 0:
            return
        now = time.time() if now is None else now
        if self._recent and now - self._recent[-1][0] < _LIVE_BUCKET_S:
            ts, prev = self._recent[-1]
            self._recent[-1] = (ts, prev + tokens)
        else:
            self._recent.append((now, tokens))

    def complete(
        self,
        *,
        tokens: int,
        duration_s: float,
        decode_s: float | None = None,
        ttft_s: float | None = None,
        now: float | None = None,
        already_counted: int = 0,
    ) -> None:
        """Settle one request. ``already_counted`` is what ``note_tokens``
        put in the window while it streamed; only the remainder (the
        upstream's own usage count can differ from the frame count) is added
        here, so a streamed token is never counted twice."""
        now = time.time() if now is None else now
        self.outstanding = max(0, self.outstanding - 1)
        self.completed += 1
        self.total_tokens += tokens
        self.last_completed_at = now
        self.mean_duration_s = _ewma(self.mean_duration_s, duration_s)
        if ttft_s is not None:
            self.ttft_ms = _ewma(self.ttft_ms, ttft_s * 1000.0)
        # Decode rate is tokens after the first one, over the decode window.
        # Falls back to whole-request duration when we never saw a first-token
        # boundary (non-streaming responses).
        window = decode_s if decode_s and decode_s > 0 else duration_s
        if tokens > 0 and window > 0:
            self.decode_tps = _ewma(self.decode_tps, tokens / window)
        remainder = tokens - max(0, already_counted)
        if remainder > 0:
            self._recent.append((now, remainder))

    def fail(self) -> None:
        self.outstanding = max(0, self.outstanding - 1)
        self.failed += 1

    def tokens_per_sec(self, window_s: float, now: float | None = None) -> float:
        """Observed throughput over the trailing window. Zero when idle."""
        now = time.time() if now is None else now
        cutoff = now - window_s
        total = sum(tok for ts, tok in self._recent if ts >= cutoff)
        return total / window_s if window_s > 0 else 0.0

    def retry_after_s(self, default: int) -> int:
        """How long a rejected client should wait, from observed request time."""
        if self.mean_duration_s is None or self.mean_duration_s <= 0:
            return default
        return max(1, int(round(self.mean_duration_s)))


class StatsRegistry:
    """target_id -> TargetStats, created on first use."""

    def __init__(self) -> None:
        self._by_target: dict[str, TargetStats] = {}

    def get(self, target_id: str) -> TargetStats:
        st = self._by_target.get(target_id)
        if st is None:
            st = TargetStats(target_id=target_id)
            self._by_target[target_id] = st
        return st

    def peek(self, target_id: str) -> TargetStats | None:
        return self._by_target.get(target_id)

    def all(self) -> dict[str, TargetStats]:
        return dict(self._by_target)

    def outstanding(self, target_id: str) -> int:
        st = self._by_target.get(target_id)
        return st.outstanding if st else 0

    def total_tokens_per_sec(self, window_s: float, now: float | None = None) -> float:
        return sum(st.tokens_per_sec(window_s, now) for st in self._by_target.values())
