"""What is wrong right now, folded from events that already exist.

**This adds almost no detection.** Two of the three conditions the TODO names
were already computed, already edge-triggered and already durable before this
module existed:

    node down    registry.py::record_health  -> node_lost / node_recovered
    OOM          manager.py::_emit_fit_miss  -> fit_miss
    cap reached  nowhere: over_budget() was only ever ASKED, at admission

So what is built here is the part that was missing: the **fold** from a stream
of moments into a set of standing conditions, and one place to read it.

**An event is a point in time; an alert is a condition with a lifetime.** That
distinction is the whole module. `/api/history/events` answers "what
happened" and keeps answering it -- it is unchanged, and it remains the audit
trail. This answers "what is wrong now", which that stream cannot: a node that
went down three hours ago is absent from a one-hour query, and a `limit` that
truncates can drop the opening event while keeping the close, which would fold
to "nothing is wrong". Absence is not zero here either.

**Nothing fires twice.** Opening a key that is already open moves `last_seen`
and increments `count`; it never moves `since` and never re-announces. A node
that is down stays down, and a stream that re-announced it every health round
is precisely the noise that teaches people to stop reading the panel. The
development box makes the point at both ends: `connor-pi` has been unhealthy
for hours (one condition, one alert), and `whisper-base.en` was crash-looping
every five seconds (one condition, one alert, `count` in the hundreds -- which
is why OOM is keyed on the SERVED NAME and not the deployment id, since every
retry is a new deployment).

**`since` comes from the event, never from when this book started.** A
coordinator restart rehydrates the roster as healthy and rediscovers an
outage within a few health rounds, so a still-down node legitimately raises
again -- but it went down when it went down. `node_lost` already carries
`last_seen_age_s`, and `ts - last_seen_age_s` reconstructs the true start of
an outage this process never saw. Same class of rule as the metrics hub's
baseline: a restart must not turn a continuing condition into a fresh one.

**It is in memory, and that is not a gap.** Every alert's source event is
already in the archive, so the audit answer survives a restart even when the
fold does not. What a restart re-announces is deliberate and differs by kind:
a node still down raises again (it is still true), a budget still over raises
again at the next request that notices, and an OOM does **not** -- a
`fit_miss` is a completed outcome, not a standing condition, and announcing
one on boot is announcing the past.

Top level rather than under `deploy/` or `registry/`, for the reason
`redaction.py` gives for the same choice: three different packages feed it and
it must import none of them.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)

#: The three kinds. Not an enum: these travel as strings on a JSON payload and
#: `contracts/` is frozen, so a string here is one fact rather than two.
NODE_DOWN = "node_down"
CAP_REACHED = "cap_reached"
OOM = "oom"

#: Deliberately the UI's own `Lamp` signal vocabulary rather than a new one --
#: a severity that did not map onto something drawable would be a third
#: spelling of the same idea.
SEVERITY_WARN = "warn"
SEVERITY_FAULT = "fault"

#: `fault` first, then `warn`. An ordering, not a ranking of importance.
_SEVERITY_ORDER = {SEVERITY_FAULT: 0, SEVERITY_WARN: 1}


def utc_day(now: float) -> str:
    """The UTC day a timestamp falls in, as `YYYY-MM-DD`.

    Spelled here as well as in `providers/runtime.py` only because this module
    may not import that package; the format is the same and the budget key
    below depends on it agreeing.
    """
    return time.strftime("%Y-%m-%d", time.gmtime(now))


# -- the sentences ----------------------------------------------------------
#
# Each alert's text has exactly one author. Where a sentence already existed in
# the product it stays where it was and is passed in; where none existed, it is
# composed here and nowhere else.


def fit_miss_sentence(
    served_name: str, predicted_total: int, usable_per_node: int
) -> str:
    """The fit gate's own words when a launch it approved died out of memory.

    This wording lived only as lazy `%`-args inside a `logger.error` call, so
    there was no assembled string an alert could carry. It is here, and
    `manager.py::_emit_fit_miss` now formats the log line from it, so the
    product's most valuable sentence has one author rather than two copies
    drifting apart.
    """
    return (
        "fit miss: %s passed the gate with %d bytes predicted per node "
        "against %d usable, then failed with an out-of-memory error"
        % (served_name, predicted_total, usable_per_node)
    )


def node_down_sentence(node_id: str, misses: int, last_seen_age_s: float) -> str:
    """Why a node counts as down, in the terms the registry decided it.

    Composed here because no product sentence existed for this one -- the
    registry's log line is written for a log. The probe's OWN words travel
    separately as `evidence`, and are never paraphrased into this.
    """
    return (
        "%s stopped answering after %d consecutive health misses; "
        "its last telemetry is %.0fs old"
        % (node_id, misses, last_seen_age_s)
    )


@dataclass
class Alert:
    """One standing condition.

    `since` is `None` when the start genuinely is not recorded -- a budget that
    was already over when this process first looked. A `None` there is stated
    on screen, never rendered as a date and never back-filled with "now".
    """

    key: str
    kind: str
    severity: str
    subject: str
    subject_kind: str
    detail: str
    since: float | None = None
    evidence: str | None = None
    count: int = 1
    last_seen: float = 0.0
    #: Only on a budget alert: the UTC day it belongs to, which is what makes
    #: "clears at the day rollover" fall out of the key instead of a timer.
    day: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "severity": self.severity,
            "subject": self.subject,
            "subject_kind": self.subject_kind,
            "detail": self.detail,
            "since": self.since,
            "evidence": self.evidence,
            "count": self.count,
            "last_seen": self.last_seen,
            "day": self.day,
        }


class AlertBook:
    """The fold. Open and close are idempotent; `current()` is the answer.

    Mutations run on the producer's thread -- the manager's watch thread, the
    registry's health loop, the gateway's loop -- because `EventBus.add_tap`'s
    contract is that a tap is non-blocking and cannot be outrun. Every
    operation here is therefore an O(1) dict write under one lock, and every
    entry point swallows its own exceptions: recording that something is wrong
    must never break the thing that was reporting it.
    """

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._open: dict[str, Alert] = {}
        #: When this book started watching. The UI says so rather than implying
        #: the set reaches back before the process did.
        self.observing_since = clock()

    # -- the two primitives ------------------------------------------------

    def open(self, alert: Alert) -> None:
        """Raise a condition, or note that an already-raised one persists."""
        now = self._clock()
        with self._lock:
            existing = self._open.get(alert.key)
            if existing is None:
                alert.last_seen = now
                self._open[alert.key] = alert
                return
            # Already standing. The condition did not start again, so `since`
            # is untouched; only the evidence of it continuing moves.
            existing.count += 1
            existing.last_seen = now
            existing.detail = alert.detail
            if alert.evidence is not None:
                existing.evidence = alert.evidence

    def close(self, key: str) -> None:
        """Clear a condition. Closing what is not open is a no-op, not an error.

        A cleared alert is REMOVED rather than kept with an end time. A
        current-state surface that accumulates resolved rows becomes a log with
        extra steps, and there already is one: `node_recovered` carries
        `down_s` and is durable, so "when did it come back" is answered by
        `/api/history/events`, not here.
        """
        with self._lock:
            self._open.pop(key, None)

    def current(self) -> list[Alert]:
        """What is wrong right now, worst first.

        Budget alerts from a previous UTC day are dropped at READ time rather
        than by a timer -- the day is in the key, so the rollover needs no
        background task to notice it.
        """
        today = utc_day(self._clock())
        with self._lock:
            live = [
                a for a in self._open.values()
                if a.kind != CAP_REACHED or a.day == today
            ]
        live.sort(
            key=lambda a: (
                _SEVERITY_ORDER.get(a.severity, 9),
                -(a.since if a.since is not None else 0.0),
            )
        )
        return live

    def report(self) -> dict[str, Any]:
        return {
            "alerts": [a.payload() for a in self.current()],
            "observing_since": self.observing_since,
            "measured_at": self._clock(),
        }

    # -- the taps ----------------------------------------------------------

    def on_deploy_event(self, event: Any) -> None:
        """The deployment bus: `fit_miss` opens, a return to READY closes.

        Keyed on the SERVED NAME. A crash loop is a new deployment id every
        few seconds -- the box was producing one every five -- so keying on the
        id would make a single broken model into hundreds of alerts. One alert
        carrying `count` is the true reading.
        """
        try:
            kind = _get(event, "type") or _get(event, "kind")
            served = _get(event, "served_name")
            if not served:
                return
            key = "%s:%s" % (OOM, served)
            if kind == "fit_miss":
                self.open(
                    Alert(
                        key=key,
                        kind=OOM,
                        severity=SEVERITY_FAULT,
                        subject=served,
                        subject_kind="deployment",
                        detail=_get(event, "summary")
                        or "a launch this gate approved died out of memory",
                        since=_get(event, "ts") or self._clock(),
                        evidence=_get(event, "actual_error") or None,
                    )
                )
            elif kind == "state_changed" and _get(event, "to") in ("ready", "stopped"):
                self.close(key)
        except Exception:  # pragma: no cover - a tap may never raise
            log.debug("alert tap failed on a deployment event", exc_info=True)

    def on_registry_event(self, event: Any) -> None:
        """The registry: `node_lost` opens, `node_recovered` closes.

        `node_lost`, never deploy's `NODE_UNHEALTHY`. telemetry/events.py
        spells out why they are different facts: deploy's fires once per
        DEPLOYMENT whose plan contains the node -- three rows for a node
        hosting three models, and none at all for an idle one. Consuming it
        would draw three alerts for one dead machine, and none for a machine
        that was dead and empty.
        """
        try:
            kind = _get(event, "type") or _get(event, "kind")
            node_id = _get(event, "node_id")
            if not node_id:
                return
            key = "%s:%s" % (NODE_DOWN, node_id)
            if kind == "node_lost":
                ts = _get(event, "ts") or self._clock()
                age = _get(event, "last_seen_age_s")
                misses = _get(event, "misses") or 0
                self.open(
                    Alert(
                        key=key,
                        kind=NODE_DOWN,
                        severity=SEVERITY_FAULT,
                        subject=node_id,
                        subject_kind="node",
                        detail=node_down_sentence(node_id, int(misses), float(age or 0.0)),
                        # The outage started when it started, not when this
                        # process first noticed. See the module docstring.
                        since=(ts - float(age)) if age is not None else ts,
                        evidence=_get(event, "last_error") or None,
                    )
                )
            elif kind in ("node_recovered", "node_removed"):
                self.close(key)
        except Exception:  # pragma: no cover
            log.debug("alert tap failed on a registry event", exc_info=True)

    def on_gateway_event(self, event: Any) -> None:
        """The gateway bus: `budget_reached` opens, `budget_cleared` closes."""
        try:
            kind = _get(event, "type") or _get(event, "kind")
            provider_id = _get(event, "provider_id")
            if not provider_id:
                return
            day = _get(event, "day") or utc_day(self._clock())
            key = "%s:%s:%s" % (CAP_REACHED, provider_id, day)
            if kind == "budget_reached":
                self.open(
                    Alert(
                        key=key,
                        kind=CAP_REACHED,
                        severity=SEVERITY_WARN,
                        subject=provider_id,
                        subject_kind="provider",
                        detail=_get(event, "detail") or "daily budget reached",
                        # Deliberately null unless the event carries one: spend
                        # is persisted but the MINUTE of the crossing is not,
                        # so a restart cannot know when it happened. The screen
                        # says the day and says the minute is unrecorded.
                        since=_get(event, "ts"),
                        day=day,
                    )
                )
            elif kind == "budget_cleared":
                self.close(key)
        except Exception:  # pragma: no cover
            log.debug("alert tap failed on a gateway event", exc_info=True)

    def attach(
        self, *, deploy_bus: Any = None, registry_events: Any = None,
        gateway_bus: Any = None,
    ) -> None:
        """Install the three taps, skipping any feed this build does not have.

        `getattr`-guarded throughout so a day-0 stub, or a gateway wired
        without a registry, attaches nothing and reports an empty set rather
        than failing to start.
        """
        for source, handler in (
            (deploy_bus, self.on_deploy_event),
            (registry_events, self.on_registry_event),
            (gateway_bus, self.on_gateway_event),
        ):
            add_tap = getattr(source, "add_tap", None)
            if add_tap is None:
                continue
            try:
                add_tap(handler)
            except Exception:
                log.warning("could not attach an alert tap", exc_info=True)


def _get(event: Any, name: str) -> Any:
    """One reader for both shapes an event arrives in.

    The deployment bus hands out objects; the telemetry sink hands out dicts.
    Rather than make every tap know which, ask for a field the same way.
    """
    if isinstance(event, dict):
        return event.get(name)
    return getattr(event, name, None)

