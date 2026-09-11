"""Frozen constants from 00-architecture.md section 4.1.

Day-0 file. Transcribed from the architecture doc, not designed here.
"""

from __future__ import annotations

GB10_TOTAL_MEMORY = 128 * 1024**3
GB10_ADDRESSABLE = int(119.7 * 1024**3)  # GPU-reachable slice, not nameplate
GB10_MEM_BANDWIDTH = 273.0  # GB/s, intra-node

#: What one cross-node collective is charged at when nothing measured one.
#: NOT a measurement, and named so no reader mistakes it for one. Taken from
#: this project's own nccl-tests fixtures for this hardware class
#: (LINK_SPARK_10G, GDR off); LINK_SPARK_FAST records 12.0 with GDR on, which
#: GB10 cannot do. Deliberately pessimistic for tensor parallel: charging too
#: little here is what let TP win plans it then lost, and a plan built on this
#: figure has to say so rather than quote it as fact.
UNMEASURED_COLLECTIVE_LATENCY_US = 40.0

#: GB/s all-reduce below which TP loses to PP when batched.
#:
#: UNREACHABLE ON GB10, and knowing that saves rediscovering it. A DGX Spark
#: reaches 23.15 GB/s aggregate across both ConnectX-7s -- the limit is the
#: PCIe Gen5 x4 slot per card (`max == current`, so there is no negotiation to
#: fix), not the 200 Gb/s port rating. No two-Spark cluster can clear 40, so
#: the bandwidth branch of `_Facts.tp_preferred` is dead on this class of
#: machine and `latency_override` is the only route to tensor parallel that
#: has ever fired here. Kept because it is the right answer for the rung with
#: no measurement at all, and because it is frozen.
TP_VIABLE_THRESHOLD = 40.0
EP_VIABLE_THRESHOLD = 40.0  # GB/s below which cross-node expert parallel is refused
DEGRADED_TPS_THRESHOLD = 10.0  # decode tok/s below which we call a fit degraded

DEFAULT_GUARDRAIL = 0.90  # fraction of addressable memory we will spend
COMM_BUFFER_BYTES = int(1.5 * 1024**3)
EP_EXTRA_BUFFER_BYTES = int(2.0 * 1024**3)
FRAMEWORK_OVERHEAD = int(1.0 * 1024**3)
