"""Frozen constants from 00-architecture.md section 4.1.

Day-0 file. Transcribed from the architecture doc, not designed here.
"""

from __future__ import annotations

GB10_TOTAL_MEMORY = 128 * 1024**3
GB10_ADDRESSABLE = int(119.7 * 1024**3)  # GPU-reachable slice, not nameplate
GB10_MEM_BANDWIDTH = 273.0  # GB/s, intra-node

TP_VIABLE_THRESHOLD = 40.0  # GB/s all-reduce below which TP loses to PP when batched
EP_VIABLE_THRESHOLD = 40.0  # GB/s below which cross-node expert parallel is refused
DEGRADED_TPS_THRESHOLD = 10.0  # decode tok/s below which we call a fit degraded

DEFAULT_GUARDRAIL = 0.90  # fraction of addressable memory we will spend
COMM_BUFFER_BYTES = int(1.5 * 1024**3)
EP_EXTRA_BUFFER_BYTES = int(2.0 * 1024**3)
FRAMEWORK_OVERHEAD = int(1.0 * 1024**3)
