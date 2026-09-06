"""Log-spaced latency histogram.

Moved here from tests/load/driver.py, which still imports it. Two properties
earn it a place in the archive:

Fixed cost. A bucket array is 920 ints however many samples went into it, so
a busy minute costs the same as a quiet one.

Exact merge. Rolling an hour from sixty 1-minute rows is a bucket-wise add,
so an hourly p99 is a real p99 of the hour's samples -- not an average of
sixty percentiles, which is the usual and quietly wrong way to do this.

2% relative bucket width, 0.01 ms to about ten minutes.
"""

from __future__ import annotations

import array
import math
import zlib

_HIST_BASE = 1.02
_HIST_MIN_MS = 0.01
_HIST_N = 920
_LOG_BASE = math.log(_HIST_BASE)


class Hist:
    __slots__ = ("buckets", "count", "total", "max")

    def __init__(self, buckets: list[int] | None = None) -> None:
        self.buckets = buckets if buckets is not None else [0] * _HIST_N
        self.count = sum(self.buckets) if buckets else 0
        self.total = 0.0
        self.max = 0.0

    def add(self, ms: float) -> None:
        if ms <= _HIST_MIN_MS:
            idx = 0
        else:
            idx = int(math.log(ms / _HIST_MIN_MS) / _LOG_BASE) + 1
            if idx >= _HIST_N:
                idx = _HIST_N - 1
        self.buckets[idx] += 1
        self.count += 1
        self.total += ms
        if ms > self.max:
            self.max = ms

    def merge(self, other: "Hist") -> None:
        for i, n in enumerate(other.buckets):
            if n:
                self.buckets[i] += n
        self.count += other.count
        self.total += other.total
        self.max = max(self.max, other.max)

    @staticmethod
    def edge(idx: int) -> float:
        if idx == 0:
            return _HIST_MIN_MS
        return _HIST_MIN_MS * (_HIST_BASE ** idx)

    def pct(self, q: float) -> float:
        if not self.count:
            return 0.0
        want = q * self.count
        seen = 0
        for i, n in enumerate(self.buckets):
            seen += n
            if seen >= want:
                return round(self.edge(i), 3)
        return round(self.max, 3)

    def mean(self) -> float:
        return round(self.total / self.count, 3) if self.count else 0.0

    def to_json(self) -> dict:
        return {
            "buckets": self.buckets,
            "count": self.count,
            "total": self.total,
            "max": self.max,
        }

    @classmethod
    def from_json(cls, blob: dict) -> "Hist":
        hist = cls(list(blob["buckets"]))
        hist.count = blob["count"]
        hist.total = blob["total"]
        hist.max = blob["max"]
        return hist

    def summary(self) -> dict:
        return {
            "n": self.count,
            "mean_ms": self.mean(),
            "p50_ms": self.pct(0.50),
            "p90_ms": self.pct(0.90),
            "p99_ms": self.pct(0.99),
            "p999_ms": self.pct(0.999),
            "max_ms": round(self.max, 3),
        }

    # -- storage form ----------------------------------------------------

    def to_blob(self) -> bytes:
        """Compact bytes for a rollup column.

        Most buckets are zero in any real minute, so a deflated int array is a
        few dozen bytes rather than the ~7 KB the JSON form would cost per
        row -- and there is one of these per minute per series.
        """
        counts = array.array("i", self.buckets)
        head = array.array("d", [self.count, self.total, self.max])
        return zlib.compress(head.tobytes() + counts.tobytes(), 6)

    @classmethod
    def from_blob(cls, blob: bytes | None) -> "Hist":
        hist = cls()
        if not blob:
            return hist
        raw = zlib.decompress(blob)
        head = array.array("d")
        head.frombytes(raw[:24])
        counts = array.array("i")
        counts.frombytes(raw[24:])
        hist.buckets = list(counts)
        hist.count = int(head[0])
        hist.total = float(head[1])
        hist.max = float(head[2])
        return hist


def merged(hists) -> Hist:
    """Exact merge of an iterable of Hists."""
    out = Hist()
    for h in hists:
        if h is not None:
            out.merge(h)
    return out
