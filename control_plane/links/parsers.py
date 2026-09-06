"""Turning probe output into numbers.

Every parser here is deliberately tolerant about column layout and strict about
what it will claim. If a parser cannot find the figure it was looking for it
returns None; nothing in this file invents a value, because a wrong bandwidth
number produces a plan that silently underperforms and nobody ever finds out.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

# nccl-tests sweeps from tiny messages upward. Small sizes are latency-bound and
# report near-zero bus bandwidth, so averaging the whole sweep answers a
# different question than "what does a collective get on this link". We average
# only over messages big enough to be bandwidth-bound; 16 MiB is comfortably
# past the knee for a two-rank all-reduce on this fabric.
BW_FLOOR_BYTES = 16 * 1024**2

_AVG_BUSBW_RE = re.compile(r"Avg bus bandwidth\s*:\s*([0-9]*\.?[0-9]+)")
_NUMERIC_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


@dataclass(frozen=True)
class NcclRow:
    size_bytes: int
    busbw_gbps: float
    time_us: float


@dataclass(frozen=True)
class NcclResult:
    rows: tuple[NcclRow, ...]
    reported_avg_busbw: float | None
    """The `# Avg bus bandwidth` line, which averages the entire sweep."""

    @property
    def bandwidth_gbps(self) -> float | None:
        """Bus bandwidth over the bandwidth-bound part of the sweep."""
        big = [r.busbw_gbps for r in self.rows if r.size_bytes >= BW_FLOOR_BYTES]
        if big:
            return sum(big) / len(big)
        # No large messages in this run. The tool's own average is the only
        # figure we have, and it is worth less; the caller notes that.
        return self.reported_avg_busbw

    @property
    def latency_us(self) -> float | None:
        """Time for the smallest message in the sweep."""
        if not self.rows:
            return None
        return min(self.rows, key=lambda r: r.size_bytes).time_us

    @property
    def used_reported_average(self) -> bool:
        return not any(r.size_bytes >= BW_FLOOR_BYTES for r in self.rows)


def parse_nccl_perf(text: str) -> NcclResult:
    """Parse `all_reduce_perf` / `sendrecv_perf` output.

    The data rows are `size count type redop root` followed by two groups of
    `time algbw busbw #wrong`, one out-of-place and one in-place. Op variants
    differ in how many of the leading metadata columns they print, so we anchor
    on the numeric tail rather than on fixed offsets.
    """
    rows: list[NcclRow] = []
    reported: float | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            m = _AVG_BUSBW_RE.search(line)
            if m:
                reported = float(m.group(1))
            continue

        tokens = line.split()
        if len(tokens) < 5:
            continue
        try:
            size = int(tokens[0])
        except ValueError:
            continue

        tail = _numeric_tail(tokens)
        # Two result groups of four, or one when the build prints a single pass.
        if len(tail) >= 8:
            groups = [tail[-8:-4], tail[-4:]]
        elif len(tail) >= 4:
            groups = [tail[-4:]]
        else:
            continue

        times = [t for t in (_as_float(g[0]) for g in groups) if t is not None]
        busbws = [b for b in (_as_float(g[2]) for g in groups) if b is not None]
        if not times or not busbws:
            continue

        # Out-of-place and in-place measure the same operation; their spread is
        # run-to-run noise, so the mean is the estimate and neither is discarded.
        rows.append(
            NcclRow(
                size_bytes=size,
                busbw_gbps=sum(busbws) / len(busbws),
                time_us=sum(times) / len(times),
            )
        )

    return NcclResult(rows=tuple(rows), reported_avg_busbw=reported)


def parse_ib_write_bw(text: str) -> tuple[float | None, str | None]:
    """Peak sustained bandwidth from `ib_write_bw`, in GB/s.

    Returns (gbps, unit_seen). perftest reports either Gb/sec or MB/sec, and
    its MB is 2^20 while the GB/s everything else here speaks is 10^9, so the
    unit has to be read off the header rather than assumed.
    """
    unit: str | None = None
    col: int | None = None
    best: float | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if "BW average" in line:
            headers = _split_bw_header(line)
            for idx, name in enumerate(headers):
                if name.startswith("BW average"):
                    col = idx
                    unit = _unit_of(name)
            continue
        if col is None:
            continue
        tokens = line.split()
        if len(tokens) <= col:
            continue
        value = _as_float(tokens[col])
        if value is None or value <= 0:
            continue
        # `-a` sweeps every size; the small ones are latency-bound. The peak is
        # what the wire can actually carry.
        best = value if best is None else max(best, value)

    if best is None:
        return None, unit
    return _to_gbps(best, unit), unit


def parse_ib_lat(text: str) -> float | None:
    """Typical latency in microseconds from `ib_write_lat` / `ib_send_lat`."""
    col: int | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if "t_typical" in line or "t_avg" in line:
            headers = _split_bw_header(line)
            for idx, name in enumerate(headers):
                if name.startswith("t_typical") or name.startswith("t_avg"):
                    col = idx
            continue
        if col is None:
            continue
        tokens = line.split()
        if len(tokens) > col:
            value = _as_float(tokens[col])
            if value is not None and value > 0:
                return value
    return None


def parse_iperf3(text: str) -> float | None:
    """Receiver-side throughput in GB/s from `iperf3 --json`.

    The receiver's count is the one that matters: the sender can have bytes in
    flight that never landed.
    """
    try:
        doc = json.loads(text)
    except (ValueError, TypeError):
        return _parse_iperf3_text(text)

    if doc.get("error"):
        return None
    end = doc.get("end") or {}
    for key in ("sum_received", "sum_sent", "sum"):
        block = end.get(key) or {}
        bps = block.get("bits_per_second")
        if isinstance(bps, (int, float)) and bps > 0:
            return float(bps) / 8.0 / 1e9
    return None


def _parse_iperf3_text(text: str) -> float | None:
    best: float | None = None
    for raw in text.splitlines():
        if "receiver" not in raw:
            continue
        m = re.search(r"([0-9]*\.?[0-9]+)\s+([KMG]?)bits/sec", raw)
        if not m:
            continue
        scale = {"": 1.0, "K": 1e3, "M": 1e6, "G": 1e9}[m.group(2)]
        gbps = float(m.group(1)) * scale / 8.0 / 1e9
        best = gbps if best is None else max(best, gbps)
    return best


def _numeric_tail(tokens: list[str]) -> list[str]:
    """The run of numeric columns at the end of a row.

    `#wrong` prints as `N/A` when validation is off, so it counts as part of the
    run even though it is not a number.
    """
    out: list[str] = []
    for token in reversed(tokens):
        if _NUMERIC_RE.match(token) or token in ("N/A", "-nan", "nan", "inf"):
            out.append(token)
        else:
            break
    out.reverse()
    return out


def _split_bw_header(line: str) -> list[str]:
    """Split a perftest header on runs of whitespace, keeping bracketed units."""
    return [part for part in re.split(r"\s{2,}|\t", line.strip().lstrip("#").strip()) if part]


def _unit_of(header: str) -> str | None:
    m = re.search(r"\[([^\]]+)\]", header)
    return m.group(1) if m else None


def _to_gbps(value: float, unit: str | None) -> float:
    if unit is None:
        return value
    normalized = unit.replace(" ", "").lower()
    if normalized.startswith("gb/s"):  # Gb/sec, gigabits
        return value / 8.0
    if normalized.startswith("mb/s"):  # perftest MB is 2**20 bytes
        return value * 1048576.0 / 1e9
    if normalized.startswith("mib/s"):
        return value * 1048576.0 / 1e9
    return value


def _as_float(token: str) -> float | None:
    if not _NUMERIC_RE.match(token):
        return None
    try:
        return float(token)
    except ValueError:
        return None
