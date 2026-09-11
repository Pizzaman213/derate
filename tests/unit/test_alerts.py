"""The fold from events to standing conditions.

Entirely in memory with an injected clock: no coordinator, no hardware, no
archive. That is the point of the seam -- "is anything wrong right now" must
answer on a dev box and under pytest, where the durable sink is a no-op.

The cases that matter are the ones about NOT firing: a node that stays down,
a model that crash-loops, and a budget that was already over when the process
started. Each was taken from a real state of the development box.
"""

from __future__ import annotations

import pytest

from control_plane import alerts as A


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def book(clock: Clock | None = None) -> tuple[A.AlertBook, Clock]:
    c = clock or Clock()
    return A.AlertBook(clock=c), c


def node_lost(node_id="connor-pi", *, ts, misses=3, age=7200.0, err="ConnectError"):
    return {
        "type": "node_lost", "node_id": node_id, "misses": misses,
        "ts": ts, "last_seen_age_s": age, "last_error": err,
    }


def fit_miss(served="whisper-base.en", *, ts, summary="fit miss: ...", err=None):
    return {
        "type": "fit_miss", "served_name": served, "ts": ts,
        "summary": summary, "actual_error": err,
    }


# -- not firing twice -------------------------------------------------------


def test_a_node_that_stays_down_is_one_alert_not_one_per_health_round():
    """`connor-pi` has been unhealthy for hours on the development box. The
    health loop re-evaluates every few seconds; the panel must not."""
    b, c = book()
    for i in range(50):
        b.on_registry_event(node_lost(ts=c.t, misses=3 + i, age=7200.0 + i))
        c.advance(5.0)

    live = b.current()
    assert len(live) == 1
    assert live[0].count == 50, "each round is evidence it continues, not a new alert"


def test_a_crash_looping_model_is_one_oom_alert_with_a_count():
    """Keyed on the SERVED NAME, because every retry is a new deployment id.
    This box produced a launch_failed every ~5s; on deployment_id that is one
    alert per retry, which is the noise the whole design exists to avoid."""
    b, c = book()
    for i in range(120):
        b.on_deploy_event(
            {"type": "fit_miss", "served_name": "whisper-base.en",
             "deployment_id": "d-%04d" % i, "ts": c.t, "summary": "fit miss: ..."}
        )
        c.advance(5.0)

    live = b.current()
    assert len(live) == 1
    assert live[0].count == 120
    assert live[0].subject == "whisper-base.en"


def test_a_repeat_does_not_move_since():
    """The condition started when it started. `since` moving on every repeat
    would make a three-hour outage permanently read as seconds old."""
    b, c = book()
    b.on_registry_event(node_lost(ts=c.t, age=7200.0))
    first = b.current()[0].since
    c.advance(3600.0)
    b.on_registry_event(node_lost(ts=c.t, age=10800.0))
    assert b.current()[0].since == first
    assert b.current()[0].last_seen == c.t, "but the evidence of it continuing does move"


# -- where `since` comes from -----------------------------------------------


def test_since_comes_from_the_event_not_from_when_the_book_started():
    """A restart rehydrates the roster as healthy and rediscovers the outage
    within a few health rounds, so a still-down node legitimately raises
    again. It still went down when it went down, and `last_seen_age_s`
    reconstructs that."""
    c = Clock(1_000_000.0)
    b, _ = book(c)
    b.on_registry_event(node_lost(ts=c.t, age=7200.0))
    assert b.current()[0].since == pytest.approx(1_000_000.0 - 7200.0)


def test_a_node_lost_with_no_age_falls_back_to_the_event_time():
    """Absence of the field is not a reason to invent a start; the event's own
    timestamp is the closest true thing."""
    b, c = book()
    ev = node_lost(ts=c.t)
    del ev["last_seen_age_s"]
    b.on_registry_event(ev)
    assert b.current()[0].since == c.t


# -- clearing ---------------------------------------------------------------


def test_recovery_clears_and_the_alert_is_gone_rather_than_kept():
    """A current-state surface that accumulates resolved rows is a log with
    extra steps, and `node_recovered` carries `down_s` into the archive."""
    b, c = book()
    b.on_registry_event(node_lost(ts=c.t))
    assert len(b.current()) == 1
    b.on_registry_event({"type": "node_recovered", "node_id": "connor-pi", "down_s": 7200})
    assert b.current() == []


def test_a_model_that_comes_back_ready_clears_its_oom():
    b, c = book()
    b.on_deploy_event(fit_miss(ts=c.t))
    assert len(b.current()) == 1
    b.on_deploy_event(
        {"type": "state_changed", "served_name": "whisper-base.en", "to": "ready"}
    )
    assert b.current() == []


def test_closing_something_that_was_never_open_is_not_an_error():
    b, _ = book()
    b.close("node_down:never-existed")
    assert b.current() == []


# -- the budget day ---------------------------------------------------------


def test_a_budget_alert_from_yesterday_is_not_current():
    """The UTC day is IN THE KEY, so the rollover needs no timer to notice --
    it simply stops matching today at read time."""
    c = Clock(1_000_000.0)
    b, _ = book(c)
    b.on_gateway_event({
        "type": "budget_reached", "provider_id": "openrouter",
        "ts": c.t, "detail": "daily budget of $1.00 reached ($1.20 spent today)",
        "day": A.utc_day(c.t),
    })
    assert len(b.current()) == 1
    c.advance(36 * 3600)  # into the next UTC day
    assert b.current() == [], "the day rolled, so the condition ended"


def test_a_budget_alert_carries_its_sentence_verbatim():
    """The text is `admission_block`'s, not a summary of it."""
    b, c = book()
    sentence = "daily budget of $12.00 reached ($12.31 spent today)"
    b.on_gateway_event({
        "type": "budget_reached", "provider_id": "openrouter",
        "ts": c.t, "detail": sentence, "day": A.utc_day(c.t),
    })
    assert b.current()[0].detail == sentence


def test_raising_the_cap_clears_the_alert():
    b, c = book()
    day = A.utc_day(c.t)
    b.on_gateway_event({"type": "budget_reached", "provider_id": "openrouter",
                        "ts": c.t, "detail": "...", "day": day})
    b.on_gateway_event({"type": "budget_cleared", "provider_id": "openrouter",
                        "day": day})
    assert b.current() == []


# -- what a restart re-announces --------------------------------------------


def test_a_fresh_book_announces_nothing_at_all():
    """An OOM is a completed outcome, not a standing condition. A restart must
    not re-announce one -- announcing it on boot is announcing the past, and
    it is in the archive either way. Nothing is replayed into a new book, so
    this is the whole guarantee."""
    b, _ = book()
    assert b.current() == []
    assert b.report()["alerts"] == []


# -- ordering and the report ------------------------------------------------


def test_faults_sort_above_warnings():
    b, c = book()
    b.on_gateway_event({"type": "budget_reached", "provider_id": "openrouter",
                        "ts": c.t, "detail": "cap", "day": A.utc_day(c.t)})
    b.on_registry_event(node_lost(ts=c.t))
    kinds = [a.kind for a in b.current()]
    assert kinds == [A.NODE_DOWN, A.CAP_REACHED]


def test_the_report_says_how_far_back_it_can_see():
    """Without this the set implies it reaches back before the process did."""
    c = Clock(555.0)
    b, _ = book(c)
    assert b.report()["observing_since"] == 555.0


def test_evidence_is_none_rather_than_empty_when_nothing_was_said():
    """An empty string would render as an empty Verbatim block; null renders
    as nothing at all."""
    b, c = book()
    b.on_registry_event(node_lost(ts=c.t, err=""))
    assert b.current()[0].evidence is None


def test_evidence_is_the_probes_own_words_and_is_not_paraphrased():
    b, c = book()
    b.on_registry_event(node_lost(ts=c.t, err="could not reach http://pi:11434: ConnectError"))
    a = b.current()[0]
    assert a.evidence == "could not reach http://pi:11434: ConnectError"
    assert a.evidence not in a.detail, "the sentence and the evidence stay separate"


# -- the taps are safe ------------------------------------------------------


def test_an_event_with_no_subject_is_ignored_rather_than_keyed_on_none():
    b, c = book()
    b.on_registry_event({"type": "node_lost", "ts": c.t})
    b.on_deploy_event({"type": "fit_miss", "ts": c.t})
    b.on_gateway_event({"type": "budget_reached", "ts": c.t})
    assert b.current() == []


def test_a_malformed_event_never_raises_into_the_producer():
    """A tap runs on the producer's thread. Recording that something is wrong
    must never break the thing reporting it."""
    b, _ = book()
    for bad in (None, 42, {"type": "node_lost", "node_id": "n", "last_seen_age_s": "nope"}):
        b.on_registry_event(bad)
        b.on_deploy_event(bad)
        b.on_gateway_event(bad)


def test_attach_skips_a_feed_that_cannot_be_tapped():
    """A day-0 stub, or a gateway wired without a registry, attaches nothing
    and reports an empty set rather than failing to start."""
    seen = []

    class Bus:
        def add_tap(self, fn):
            seen.append(fn)

    class NoTap:
        pass

    b, _ = book()
    b.attach(deploy_bus=Bus(), registry_events=NoTap(), gateway_bus=None)
    assert len(seen) == 1


def test_an_object_event_reads_the_same_as_a_dict_one():
    """The deployment bus hands out objects and the telemetry sink hands out
    dicts; one reader serves both."""
    class Ev:
        type = "node_lost"
        node_id = "spark-26af"
        misses = 3
        ts = 900.0
        last_seen_age_s = 60.0
        last_error = "timeout"

    b, _ = book()
    b.on_registry_event(Ev())
    assert b.current()[0].subject == "spark-26af"
    assert b.current()[0].since == pytest.approx(840.0)


# -- the sentences ----------------------------------------------------------


def test_the_fit_miss_sentence_is_the_gates_own_wording():
    """Pinned character for character: `manager.py` formats its log line from
    this function, so a change here changes both and neither drifts."""
    assert A.fit_miss_sentence("Qwen3-30B-A3B", 84288733184, 115674206698) == (
        "fit miss: Qwen3-30B-A3B passed the gate with 84288733184 bytes "
        "predicted per node against 115674206698 usable, then failed with an "
        "out-of-memory error"
    )
