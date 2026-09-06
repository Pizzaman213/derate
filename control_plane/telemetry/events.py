"""Journalling the event buses.

deploy/events.py already produces exactly the right shape -- ``{type, ts,
**fields}``, emitted from whichever thread noticed -- so this adds a tap that
copies each event into the journal and changes nothing about how events are
delivered.

The gateway gets its own EventBus instance rather than importing the
deployment manager's. Same class, no cross-package coupling, and the `source`
column says which one an event came from.
"""

from __future__ import annotations

import logging
from typing import Any

from control_plane.deploy.events import EventBus

from .records import NULL_SINK, TelemetrySink

log = logging.getLogger(__name__)

# Where an event came from. Recorded alongside the type, because "memory
# critical" from the deployment manager and "circuit open" from the gateway
# are both real events and telling them apart later matters.
SOURCE_DEPLOY = "deploy"
SOURCE_GATEWAY = "gateway"
SOURCE_REGISTRY = "registry"
SOURCE_LINKS = "links"

# Gateway event types. The gateway logs all of these today and keeps none.
BREAKER_OPENED = "breaker_opened"
BREAKER_CLOSED = "breaker_closed"
RETRY_REFUSED = "retry_refused"
REQUEST_PARKED = "request_parked"
PARK_RESOLVED = "park_resolved"
ADMISSION_BLOCKED = "admission_blocked"
ADMISSION_CLEARED = "admission_cleared"
ROUTING_SOURCE_FAILED = "routing_source_failed"
STARTUP_DEGRADED = "startup_degraded"


def journal_events(bus: EventBus, sink: TelemetrySink, source: str) -> None:
    """Copy every event this bus emits into *sink*, tagged with *source*."""
    if sink is NULL_SINK or sink is None:
        return
    bus.add_tap(lambda event: sink.event(source, event))


class GatewayEvents:
    """The gateway's own bus, with the emitters it never had.

    Every method here corresponds to something the gateway currently writes to
    a log line and forgets -- a breaker trip, a refused retry, a parked
    request. Emitting them makes them queryable next month instead.
    """

    def __init__(self, bus: EventBus | None = None, sink: TelemetrySink = NULL_SINK) -> None:
        self.bus = bus or EventBus()
        journal_events(self.bus, sink, SOURCE_GATEWAY)

    def emit(self, type: str, **fields: Any) -> dict[str, Any]:
        return self.bus.emit(type, **fields)

    def breaker_opened(self, target_id: str, failures: int, cooldown_s: float) -> None:
        self.emit(
            BREAKER_OPENED, target_id=target_id, failures=failures, cooldown_s=cooldown_s
        )

    def breaker_closed(self, target_id: str) -> None:
        self.emit(BREAKER_CLOSED, target_id=target_id)

    def retry_refused(self, served_name: str, snapshot: dict[str, Any]) -> None:
        self.emit(RETRY_REFUSED, served_name=served_name, **snapshot)

    def request_parked(self, served_name: str, depth: int, parked_total: int) -> None:
        self.emit(
            REQUEST_PARKED, served_name=served_name, depth=depth, parked=parked_total
        )

    def park_resolved(self, served_name: str, outcome: str, waited_s: float) -> None:
        self.emit(
            PARK_RESOLVED,
            served_name=served_name,
            outcome=outcome,
            waited_ms=round(waited_s * 1000.0, 1),
        )

    def admission_blocked(self, target_id: str, reasons: list[str]) -> None:
        self.emit(ADMISSION_BLOCKED, target_id=target_id, reasons=sorted(reasons))

    def admission_cleared(self, target_id: str) -> None:
        self.emit(ADMISSION_CLEARED, target_id=target_id)

    def routing_source_failed(self, source: str, detail: str) -> None:
        self.emit(ROUTING_SOURCE_FAILED, port=source, detail=detail)

    def startup_degraded(self, reasons: list[str]) -> None:
        if reasons:
            self.emit(STARTUP_DEGRADED, reasons=list(reasons))
