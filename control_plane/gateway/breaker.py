"""Per-target circuit breaker.

The deployment manager owns the question "is this backend up", and answers it
on a five second poll needing two consecutive failures -- roughly ten seconds
in which a dead node is still marked ``healthy`` and every arriving request is
routed into it and waits out the connect timeout.

This is the gateway's own, faster answer to a narrower question: has the
connection I just tried failed enough times that I should stop trying? It only
ever subtracts from a target's health, never restores it, so the deployment
manager stays authoritative and the two cannot disagree in a way that puts a
dead target back into rotation.

Only transport failures count. A backend that answers 500 is reachable, and
benching it for saying so would let one malformed request take every replica of
a model out of rotation at once.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger("gateway.breaker")

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


class _Circuit:
    __slots__ = ("failures", "opened_at", "probing")

    def __init__(self) -> None:
        self.failures = 0
        self.opened_at: float | None = None
        # A half-open circuit admits exactly one probe. Without this flag a
        # burst arriving the instant the cooldown expires would all be let
        # through at once, which is the stampede the breaker exists to stop.
        self.probing = False


class CircuitBreaker:
    """target_id -> circuit, created on first failure."""

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        cooldown_s: float = 30.0,
        clock=time.monotonic,
        events=None,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._cooldown_s = cooldown_s
        self._clock = clock
        self._circuits: dict[str, _Circuit] = {}
        # A trip and a recovery are events, not just log lines: "which target
        # was benched, when, and how often" is a question about last month.
        self._events = events

    def record_transport_failure(self, target_id: str) -> None:
        circuit = self._circuits.setdefault(target_id, _Circuit())
        circuit.failures += 1
        circuit.probing = False
        if circuit.opened_at is not None:
            # A failed probe buys the target another full cooldown rather than
            # letting it be retried every tick.
            circuit.opened_at = self._clock()
            return
        if circuit.failures >= self._failure_threshold:
            circuit.opened_at = self._clock()
            log.warning(
                "benching %s for %.0fs after %d consecutive transport failures",
                target_id,
                self._cooldown_s,
                circuit.failures,
            )
            if self._events is not None:
                self._events.breaker_opened(
                    target_id, circuit.failures, self._cooldown_s
                )

    def record_success(self, target_id: str) -> None:
        circuit = self._circuits.get(target_id)
        if circuit is None:
            return
        if circuit.opened_at is not None:
            log.info("%s answered again; returning it to the rotation", target_id)
            if self._events is not None:
                self._events.breaker_closed(target_id)
        circuit.failures = 0
        circuit.opened_at = None
        circuit.probing = False

    def state(self, target_id: str) -> str:
        """CLOSED, OPEN or HALF_OPEN. Read-only, and it must stay that way.

        Every read of a circuit is on a path that also serves ``GET
        /api/routing``: ``Router._refresh_live`` runs for the UI's polling as
        well as for a real request. If looking at a circuit consumed its
        half-open probe, a UI open in a browser would eat every probe and no
        benched target would ever come back. Claiming happens in :meth:`begin`,
        which only a dispatch calls.
        """
        circuit = self._circuits.get(target_id)
        if circuit is None or circuit.opened_at is None:
            return CLOSED
        if self._clock() - circuit.opened_at < self._cooldown_s:
            return OPEN
        return HALF_OPEN

    def is_open(self, target_id: str) -> bool:
        """Whether selection should skip this target entirely. Read-only.

        A half-open circuit is deliberately not open: it stays selectable so
        that one request can be spent finding out whether the target is back.
        """
        return self.state(target_id) == OPEN

    def begin(self, target_id: str) -> bool:
        """Claim the right to send to this target. False means "pick another".

        Only ever False for a half-open circuit whose single probe is already
        out. There is no ``await`` between the router's selection and this
        call, so one event loop means the check-and-set needs no lock -- an
        invariant worth preserving if this ever moves.
        """
        circuit = self._circuits.get(target_id)
        if circuit is None or circuit.opened_at is None:
            return True
        if self._clock() - circuit.opened_at < self._cooldown_s:
            return False  # open; the router should not have offered it
        if circuit.probing:
            return False
        circuit.probing = True
        log.info("sending one probe to benched target %s", target_id)
        return True

    def abandon(self, target_id: str) -> None:
        """Give an unused probe back, for a dispatch that claimed and then
        did not send -- refused by admission, or out of budget."""
        circuit = self._circuits.get(target_id)
        if circuit is not None:
            circuit.probing = False

    def forget(self, target_id: str) -> None:
        """Drop a circuit whose target has left the routing table.

        Without this a benched target that is stopped and relaunched under the
        same deployment_id would come back still benched, with nothing in the
        index to ever clear it.
        """
        self._circuits.pop(target_id, None)

    def retain(self, target_ids) -> None:
        """Forget every circuit not in ``target_ids``. Called from a rebuild."""
        for gone in set(self._circuits) - set(target_ids):
            self._circuits.pop(gone, None)

    def opened_targets(self) -> dict[str, str]:
        """Every target that is not closed, and what it is. For the UI."""
        return {
            target_id: state
            for target_id in self._circuits
            if (state := self.state(target_id)) != CLOSED
        }
