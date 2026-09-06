"""Durable telemetry: per-node journals, collected by the coordinator.

Three layers. Every node appends to a local SQLite journal (journal.py); the
coordinator drains each journal over the node agent's HTTP surface
(collector.py) into a typed archive (archive.py); a compaction pass rolls raw
rows into 1-minute and 1-hour buckets and enforces retention (retention.py).

Producers never touch these modules directly. They hold a TelemetrySink whose
default implementation records nothing, so every component still constructs on
a machine with telemetry switched off.
"""

from .records import (
    KIND_EVENT,
    KIND_LOG,
    KIND_REQUEST,
    KIND_SAMPLE,
    KINDS,
    NULL_SINK,
    NullSink,
    RequestRecord,
    RequestTrace,
    TelemetrySink,
)

__all__ = [
    "KIND_EVENT",
    "KIND_LOG",
    "KIND_REQUEST",
    "KIND_SAMPLE",
    "KINDS",
    "NULL_SINK",
    "NullSink",
    "RequestRecord",
    "RequestTrace",
    "TelemetrySink",
]
