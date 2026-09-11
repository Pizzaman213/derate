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
import time
from typing import TYPE_CHECKING, Any

from .records import NULL_SINK, TelemetrySink

if TYPE_CHECKING:  # importing deploy at module scope would put the whole
    from control_plane.deploy.events import EventBus  # deployment manager in
    # every worker process. A worker has no deployments and no gateway; it
    # runs a node agent and a journal. See _new_bus below.

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
#: A provider's daily cap closing and reopening. The crossing was never
#: recorded before: `over_budget()` was only ever ASKED, at admission time, so
#: nothing could say when a cap was reached -- only that it currently was.
BUDGET_REACHED = "budget_reached"
BUDGET_CLEARED = "budget_cleared"
STARTUP_DEGRADED = "startup_degraded"
RESTART_ATTEMPTED = "restart_attempted"
RESTART_EXHAUSTED = "restart_exhausted"

# Registry event types. Everything a node does that an incident review asks
# about a month later, and that today exists only as a log line -- or, in the
# case of a driver upgrade, not even that.
#
# Deliberately NOT named node_unhealthy. deploy/events.py already has one, and
# it is a different fact: it fires once per DEPLOYMENT whose plan contains the
# node (three rows for a node hosting three models, none at all for an idle
# one), while this fires once per node, always, carrying the miss count and the
# probe's own error. Sharing a type name and separating them only by `source`
# would conflate them under the events_type_ts index and make "how many times
# did spark-02 drop" unanswerable without also knowing how many deployments it
# was hosting at each moment.
NODE_JOINED = "node_joined"
NODE_LOST = "node_lost"
NODE_RECOVERED = "node_recovered"
NODE_REMOVED = "node_removed"
PROFILE_CHANGED = "profile_changed"
NODE_REBOOTED = "node_rebooted"
AGENT_RESTARTED = "agent_restarted"
PROBE_DEGRADED = "probe_degraded"
PROBE_RECOVERED = "probe_recovered"
THROTTLE_ENTERED = "throttle_entered"
THROTTLE_CLEARED = "throttle_cleared"
DISK_LOW = "disk_low"
DISK_RECOVERED = "disk_recovered"

# registry/agent.py::note_shell_opened has always written this literal. The
# value is load-bearing -- history is already recorded under it -- so this
# names the existing string rather than changing it.
SOURCE_SHELL = "shell"


def _new_bus():
    """Deferred, so importing this module does not import the deploy package."""
    from control_plane.deploy.events import EventBus

    return EventBus()


def journal_events(bus: "EventBus", sink: TelemetrySink, source: str) -> None:
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

    def __init__(
        self, bus: "EventBus | None" = None, sink: TelemetrySink = NULL_SINK
    ) -> None:
        self.bus = bus if bus is not None else _new_bus()
        journal_events(self.bus, sink, SOURCE_GATEWAY)

    def emit(self, type: str, **fields: Any) -> dict[str, Any]:
        return self.bus.emit(type, **fields)

    def breaker_opened(self, target_id: str, failures: int, cooldown_s: float) -> None:
        self.emit(
            BREAKER_OPENED, target_id=target_id, failures=failures, cooldown_s=cooldown_s
        )

    def breaker_closed(self, target_id: str) -> None:
        self.emit(BREAKER_CLOSED, target_id=target_id)

    def budget_reached(
        self, provider_id: str, detail: str, daily_budget_usd: float,
        spend_today_usd: float, day: str,
    ) -> None:
        """A provider's cap closed. `detail` is `ProviderRuntime.budget_block`'s
        own sentence, carried rather than recomposed."""
        self.emit(
            BUDGET_REACHED, provider_id=provider_id, detail=detail,
            daily_budget_usd=daily_budget_usd, spend_today_usd=spend_today_usd,
            day=day,
        )

    def budget_cleared(self, provider_id: str, day: str) -> None:
        self.emit(BUDGET_CLEARED, provider_id=provider_id, day=day)

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

    def restart_attempted(
        self,
        *,
        served_name: str,
        previous_deployment_id: str,
        attempt: int,
        max_attempts: int,
        outcome: str,
        new_deployment_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        self.emit(
            RESTART_ATTEMPTED,
            served_name=served_name,
            previous_deployment_id=previous_deployment_id,
            attempt=attempt,
            max_attempts=max_attempts,
            outcome=outcome,
            new_deployment_id=new_deployment_id,
            reason=reason,
        )

    def restart_exhausted(
        self,
        *,
        served_name: str,
        previous_deployment_id: str,
        attempts: int,
        max_attempts: int,
        last_error: str | None,
    ) -> None:
        self.emit(
            RESTART_EXHAUSTED,
            served_name=served_name,
            previous_deployment_id=previous_deployment_id,
            attempts=attempts,
            max_attempts=max_attempts,
            last_error=last_error,
        )


class RegistryEvents:
    """Node lifecycle and node health, written down.

    Two things separate this from :class:`GatewayEvents`.

    It must work with **no bus at all**. A worker runs a node agent and a
    journal and nothing else; importing ``deploy`` to get an ``EventBus`` there
    would pull the whole deployment manager into every worker process, which is
    the thing ``_new_bus``'s deferral exists to prevent. So the bus is optional
    and the sink is written to directly when there is none -- the shape
    ``registry/agent.py::note_shell_opened`` already hand-rolls.

    And ``node_id`` is a constructor argument, because the archive's ``events``
    table takes its ``node_id`` column from the COLLECTOR's cursor -- the
    journal's owner, not the event's subject. An event emitted on the
    coordinator about ``spark-02`` therefore lands under the coordinator's id,
    and ``/api/history/events?node_id=`` filters the observer. Carrying the
    subject in the body is what makes it recoverable, and is what deploy's own
    ``node_unhealthy`` already does.
    """

    def __init__(
        self,
        sink: TelemetrySink = NULL_SINK,
        bus: "EventBus | None" = None,
        node_id: str = "",
    ) -> None:
        self.bus = bus
        self.node_id = node_id
        self._sink = sink
        self._taps: list[Any] = []
        if bus is not None:
            journal_events(bus, sink, SOURCE_REGISTRY)

    def add_tap(self, fn: Any) -> None:
        """Call *fn* synchronously for every event, bus or no bus.

        `EventBus.add_tap` exists for a consumer that cannot be outrun, and
        the same need applies here -- but on a coordinator this class is
        constructed WITHOUT a bus and writes straight to the sink, so there is
        no bus to tap. A live consumer would otherwise have to subscribe to
        the archive, and the archive is a no-op on a dev box and under pytest.

        Same strict contract as the bus's: non-blocking, and a tap that raises
        is logged and ignored, because recording a fact may not break it.
        """
        self._taps.append(fn)

    def _fire_taps(self, event: dict[str, Any]) -> None:
        for fn in self._taps:
            try:
                fn(event)
            except Exception:  # pragma: no cover - a tap of ours misbehaving
                log.debug("registry event tap failed", exc_info=True)

    def emit(self, type: str, **fields: Any) -> dict[str, Any]:
        """Record one event. Never raises: recording a fact may not break it."""
        fields.setdefault("node_id", self.node_id)
        if self.bus is not None:
            try:
                event = self.bus.emit(type, **fields)
            except Exception:  # pragma: no cover - a tap of ours misbehaving
                log.debug("registry event %s could not be emitted", type, exc_info=True)
                return {}
            self._fire_taps(event or {"type": type, "ts": time.time(), **fields})
            return event
        event = {"type": type, "ts": time.time(), **fields}
        try:
            self._sink.event(SOURCE_REGISTRY, event)
        except Exception:  # pragma: no cover
            log.debug("registry event %s could not be recorded", type, exc_info=True)
        self._fire_taps(event)
        return event

    # -- roster ----------------------------------------------------------

    def node_joined(self, node_id: str, **fields: Any) -> None:
        self.emit(NODE_JOINED, node_id=node_id, **fields)

    def node_lost(
        self, node_id: str, misses: int, last_error: str = "", **fields: Any
    ) -> None:
        self.emit(
            NODE_LOST, node_id=node_id, misses=misses, last_error=last_error, **fields
        )

    def node_recovered(self, node_id: str, down_s: float, **fields: Any) -> None:
        self.emit(NODE_RECOVERED, node_id=node_id, down_s=round(down_s, 1), **fields)

    def node_removed(self, node_id: str, reason: str = "", **fields: Any) -> None:
        self.emit(NODE_REMOVED, node_id=node_id, reason=reason, **fields)

    # -- identity --------------------------------------------------------

    def profile_changed(
        self, node_id: str, changed: dict[str, Any], reason: str = ""
    ) -> None:
        """What moved, and what it moved from.

        The whole point is the ``from``: three call sites overwrite a stored
        profile in place on a 60s timer, so "the driver is 580.173.02" was
        always answerable and "the driver changed at 14:02, from what" never
        was. Half of every incident review starts there.
        """
        if not changed:
            return
        self.emit(PROFILE_CHANGED, node_id=node_id, changed=changed, reason=reason)

    def node_rebooted(self, node_id: str, **fields: Any) -> None:
        self.emit(NODE_REBOOTED, node_id=node_id, **fields)

    def agent_restarted(self, node_id: str, **fields: Any) -> None:
        """The agent process restarted while the machine did not.

        Kept apart from :meth:`node_rebooted` because the pair is diagnostic in
        a way neither is alone: the same boot id with a falling agent uptime is
        a container problem, both moving is a real reboot, and a build that
        changed across it is an upgrade rather than a crash.
        """
        self.emit(AGENT_RESTARTED, node_id=node_id, **fields)

    # -- probe and hardware health ---------------------------------------

    def probe_degraded(self, node_id: str, reason: str, **fields: Any) -> None:
        self.emit(PROBE_DEGRADED, node_id=node_id, reason=reason, **fields)

    def probe_recovered(self, node_id: str, **fields: Any) -> None:
        self.emit(PROBE_RECOVERED, node_id=node_id, **fields)

    def throttle_entered(self, node_id: str, reasons: tuple[str, ...], **f: Any) -> None:
        self.emit(THROTTLE_ENTERED, node_id=node_id, reasons=list(reasons), **f)

    def throttle_cleared(self, node_id: str, throttled_s: float, **f: Any) -> None:
        self.emit(
            THROTTLE_CLEARED, node_id=node_id, throttled_s=round(throttled_s, 1), **f
        )

    def disk_low(self, node_id: str, device: str, **fields: Any) -> None:
        self.emit(DISK_LOW, node_id=node_id, device=device, **fields)

    def disk_recovered(self, node_id: str, device: str, **fields: Any) -> None:
        self.emit(DISK_RECOVERED, node_id=node_id, device=device, **fields)
