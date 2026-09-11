"""Per-provider live state: health, backoff, spend, in-flight count.

None of this is persisted in the frozen :class:`Provider` record beyond the
two fields it already has. It lives here because a rate-limited provider is a
different thing from an unhealthy one, and the difference is what the
gateway routes on.
"""

from __future__ import annotations

import email.utils
import logging
import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import BACKOFF_MAX_S, BACKOFF_MIN_S, RETRY_AFTER_MAX_S, RETRY_JITTER_S

log = logging.getLogger(__name__)


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def parse_retry_after(value: str | None, now: float) -> float | None:
    """Seconds from ``Retry-After``, which is either a delay or an HTTP date."""
    if not value:
        return None
    text = value.strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, parsed.timestamp() - now)


@dataclass
class DaySpend:
    usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0
    # Requests we served but could not price, because the provider publishes
    # no cost for that model. Surfaced so a zero spend is not read as free.
    #: Served, but not charged. Two causes, deliberately one counter: we hold
    #: no rate card for that model, OR the response carried no usage block at
    #: all (audio bytes, a Custom box, Ollama). Both are "this happened and we
    #: cannot say what it cost", which is one fact for a reader to act on.
    unpriced_requests: int = 0
    # Requests the provider itself priced, in its own response. The rest of
    # `requests - unpriced_requests` was priced from our copy of its published
    # rate card, which is a forecast: it cannot see a cached prompt token, a
    # long-context tier or a per-modality surcharge. Counting the two apart is
    # what lets a screen say "charged" for one and "estimated" for the other
    # instead of implying both are the same kind of number.
    metered_requests: int = 0


@dataclass
class ProviderRuntime:
    """Mutable state for one provider. Persisted only in part; see the store."""

    provider_id: str
    healthy: bool = True
    last_error: str | None = None
    last_refreshed: float = 0.0
    backoff_until: float = 0.0
    consecutive_rate_limits: int = 0
    #: Consecutive failures to reach this provider at all, or 5xx from it. Sizes
    #: the same exponential backoff `consecutive_rate_limits` does, kept apart
    #: because "busy" and "broken" should not deepen each other's wait.
    consecutive_failures: int = 0
    #: When a provider we could not reach (or that answered 5xx) may be tried
    #: again. Deliberately NOT `backoff_until`: that one means "429, come back
    #: later" and `rate_limited()` reads it, so folding a transport failure into
    #: it would report an unreachable box as rate limited.
    failure_backoff_until: float = 0.0
    #: Set when the upstream rejected our credentials, and the one kind of
    #: unhealthy that does NOT expire. A bad key is not transient: retrying it
    #: on a timer burns quota and can get an account locked, so this latches
    #: until a successful request or an operator edit clears it.
    auth_rejected: bool = False
    daily_budget_usd: float | None = None
    aliases: dict[str, str] = field(default_factory=dict)
    spend: dict[str, DaySpend] = field(default_factory=dict)
    outstanding: Counter = field(default_factory=Counter)
    # Set when a key reference cannot be resolved. Distinct from an upstream
    # auth rejection, and the reason the provider is disabled rather than sick.
    missing_key_ref: str | None = None
    #: Upstream ids the operator has switched on. A provider serves nothing but
    #: these. ``None`` means the record predates the allowlist and keeps serving
    #: its whole catalogue; the empty set means it serves nothing at all.
    #:
    #: The two are deliberately distinguishable. Collapsing ``None`` into "every
    #: id we can see right now" at load time would read the persisted model list
    #: to do it, and that list is empty on any record written before the first
    #: successful refresh -- which would pin such a provider to serving nothing,
    #: permanently, for having been saved at the wrong moment.
    enabled_models: frozenset[str] | None = None

    #: Upstream id -> the backend tag it should be forced to, for a kind that
    #: aggregates several backend hosts per model (OpenRouter). Absent means
    #: no preference: the upstream's own default routing applies. Written a
    #: key at a time (`ProviderService._screen_backend_pins`), not as a
    #: complete replacement like `aliases` -- the control that sets it edits
    #: one model at a time and must not have to know every other model's pin
    #: on the same provider to avoid erasing it.
    backend_pins: dict[str, str] = field(default_factory=dict)

    # -- the allowlist -----------------------------------------------------

    def serves(self, upstream_id: str) -> bool:
        """Whether this provider offers *upstream_id*.

        The only reader of the tri-state above. Every filter in the package
        calls this rather than testing ``enabled_models`` itself, so there is
        one place that decides what ``None`` means.
        """
        return self.enabled_models is None or upstream_id in self.enabled_models

    # -- health transitions ------------------------------------------------

    def note_success(self, now: float) -> None:
        """A request got through. Everything transient clears."""
        self.healthy = True
        self.last_error = None
        self.backoff_until = 0.0
        self.failure_backoff_until = 0.0
        self.consecutive_rate_limits = 0
        self.consecutive_failures = 0
        self.auth_rejected = False

    def _back_off(self, now: float) -> float:
        """Exponential wait for a provider we could not reach or that broke.

        The reason this exists at all: `healthy = False` used to be absorbing.
        `admission_block` keeps an unhealthy provider out of rotation, and
        `note_success` -- which needs a request to get through -- was the only
        thing that could clear it, so the block prevented its own cure. The only
        other way out was the model-list refresh, six hours away. One transient
        ReadTimeout took an Ollama box off the air for a working day while it
        answered pings in 11 ms.
        """
        seconds = min(BACKOFF_MAX_S, BACKOFF_MIN_S * (2**self.consecutive_failures))
        self.consecutive_failures += 1
        self.failure_backoff_until = now + seconds
        return seconds

    def half_open(self, now: float) -> None:
        """Stop asserting a failed provider is down once its backoff expires.

        Routing selects on `healthy`, so this is what actually puts the provider
        back in front of a request. It is not a claim of recovery -- `last_error`
        stays -- it is the admission that we no longer know, which is the only
        honest state before something has been tried. The next request settles
        it: `note_success` clears everything, another failure backs off further.

        Auth rejection is exempt. Retrying a key the upstream refused is how an
        account gets locked, and no amount of waiting fixes a wrong key.
        """
        if self.healthy or self.auth_rejected:
            return
        if self.failure_backoff_until <= 0.0 or now < self.failure_backoff_until:
            return
        self.healthy = True

    def note_rate_limit(self, now: float, retry_after: str | None, message: str) -> float:
        """429. Temporarily not admitting; still healthy.

        Honours ``Retry-After`` when the provider sends one, up to
        ``RETRY_AFTER_MAX_S`` (a DoS guard, not a real-world expectation —
        clamping to our own short exponential cap instead would re-admit
        earlier than the upstream asked). Absent a header, backs off
        exponentially from one second to ``BACKOFF_MAX_S``.
        """
        seconds = parse_retry_after(retry_after, now)
        if seconds is None:
            seconds = min(
                BACKOFF_MAX_S,
                BACKOFF_MIN_S * (2**self.consecutive_rate_limits),
            )
        else:
            seconds = min(seconds, RETRY_AFTER_MAX_S)
        self.consecutive_rate_limits += 1
        self.backoff_until = now + seconds
        self.last_error = f"rate limited, retrying in {seconds:.0f}s: {message}"
        # Health is untouched on purpose. Rate limited is busy, not broken.
        log.info("provider %s rate limited for %.0fs", self.provider_id, seconds)
        return seconds

    def note_server_error(self, now: float, status: int, message: str) -> None:
        self.healthy = False
        self.last_error = f"upstream {status}: {message}"
        seconds = self._back_off(now)
        log.warning(
            "provider %s unhealthy after %s; retrying in %.0fs",
            self.provider_id, status, seconds,
        )

    def note_auth_failure(self, now: float, status: int, api_key_ref: str, message: str) -> None:
        """401 or 403. Unhealthy immediately; retrying a bad key just burns time."""
        self.healthy = False
        self.auth_rejected = True
        self.last_error = (
            f"authentication rejected ({status}); check the key behind "
            f"api_key_ref {api_key_ref!r}: {message}"
        )
        log.warning("provider %s auth rejected (%s)", self.provider_id, status)

    def note_transport_error(self, now: float, message: str) -> None:
        self.healthy = False
        self.last_error = f"could not reach upstream: {message}"
        seconds = self._back_off(now)
        log.warning(
            "provider %s unreachable (%s); retrying in %.0fs",
            self.provider_id, message, seconds,
        )

    def note_missing_key(self, api_key_ref: str) -> None:
        self.missing_key_ref = api_key_ref
        self.healthy = False
        self.last_error = (
            f"api_key_ref {api_key_ref!r} resolves to nothing; set that environment "
            f"variable or add it to secrets.json, then re-enable the provider"
        )

    def clear_missing_key(self) -> None:
        self.missing_key_ref = None

    # -- admission ---------------------------------------------------------

    def rate_limited(self, now: float) -> bool:
        return now < self.backoff_until

    def retry_in(self, now: float) -> float:
        """When this provider may be tried again, whatever is holding it back.

        The max of both waits: a screen asking "how long" wants one number, and
        the honest one is the later of the 429 backoff and the unreachable one.
        """
        return max(0.0, self.backoff_until - now, self.failure_backoff_until - now)

    def over_budget(self, now: float) -> bool:
        if self.daily_budget_usd is None:
            return False
        return self.spend_today(now) >= self.daily_budget_usd

    def budget_block(self, now: float) -> str | None:
        """Why the cap is closed, in one sentence, or None when it is not.

        The ONE author of this text. It used to be composed here and composed
        again -- differently, dropping the "spent today" half -- in
        `service.py::_admit`'s refusal, so an operator saw one sentence on the
        providers screen and a shorter one in the error that refused their
        request. Both call it now, and so does the alert that reports the
        crossing.
        """
        if not self.over_budget(now):
            return None
        return (
            f"daily budget of ${self.daily_budget_usd:.2f} reached "
            f"(${self.spend_today(now):.2f} spent today)"
        )

    def admission_block(self, now: float) -> str | None:
        """Why this provider is not admitting, or None when it is."""
        if self.missing_key_ref is not None:
            return f"api_key_ref {self.missing_key_ref!r} is unresolved"
        if self.rate_limited(now):
            return f"rate limited for another {self.retry_in(now):.0f}s"
        budget = self.budget_block(now)
        if budget is not None:
            return budget
        if self.auth_rejected:
            return self.last_error or "authentication rejected"
        if not self.healthy and self.failure_backoff_until <= 0.0:
            # Unhealthy with no backoff running is not a request failure: it is
            # a provider whose model list could not be fetched and which has
            # nothing cached to serve (`ProviderService._note_refresh_failure`).
            # Nothing to probe with, so it stays blocked until a refresh.
            return self.last_error or "unhealthy"
        if not self.healthy and now < self.failure_backoff_until:
            # Blocked while the backoff runs, and offered again once it expires
            # so a single request can find out whether the provider is back.
            # `healthy` stays False until one actually gets through: this is a
            # half-open door, not a claim of recovery.
            return self.last_error or "unhealthy"
        return None

    def admitting(self, now: float) -> bool:
        return self.admission_block(now) is None

    # -- spend -------------------------------------------------------------

    def day(self, now: float) -> DaySpend:
        return self.spend.setdefault(utc_day(now), DaySpend())

    def spend_today(self, now: float) -> float:
        return self.spend.get(utc_day(now), DaySpend()).usd

    def record_usage(
        self,
        now: float,
        input_tokens: int,
        output_tokens: int,
        input_cost_per_mtok: float | None,
        output_cost_per_mtok: float | None,
        metered_cost_usd: float | None = None,
    ) -> float:
        """Add one request's usage. Returns the dollars charged, zero if unpriced.

        ``metered_cost_usd`` is the provider's own figure for this request and
        wins outright when there is one: it is what the account was charged,
        where the rate-card arithmetic below is only what we predict from two
        numbers. A metered request is never unpriced, even at 0.0 -- a free
        model answering costs nothing, and that is knowledge, not a gap.
        """
        day = self.day(now)
        day.requests += 1
        day.input_tokens += max(0, input_tokens)
        day.output_tokens += max(0, output_tokens)
        if metered_cost_usd is not None:
            charged = max(0.0, metered_cost_usd)
            day.metered_requests += 1
            day.usd += charged
            return charged
        if input_cost_per_mtok is None and output_cost_per_mtok is None:
            day.unpriced_requests += 1
            return 0.0
        cost = (
            max(0, input_tokens) * (input_cost_per_mtok or 0.0)
            + max(0, output_tokens) * (output_cost_per_mtok or 0.0)
        ) / 1_000_000.0
        day.usd += cost
        return cost

    def record_unpriced(self, now: float) -> None:
        """One request served whose response said nothing about its usage.

        Separate from :meth:`record_usage` because there are no token counts to
        add -- not zero of them, none. Reporting zeros would put a measured
        looking "0 in, 0 out" beside requests that really did carry tokens
        nobody told us about.

        It still counts, and that is the point. Before this existed the caller
        returned early and incremented nothing, so a day of provider audio
        traffic left `requests` at 0 and the Spend screen read that as "a real
        zero. Nothing was served today."
        """
        day = self.day(now)
        day.requests += 1
        day.unpriced_requests += 1

    def prune_spend(self, now: float, keep_days: int = 30) -> None:
        if len(self.spend) <= keep_days:
            return
        for key in sorted(self.spend)[:-keep_days]:
            del self.spend[key]


def jittered_delay(base: float = RETRY_JITTER_S) -> float:
    """Small random delay for the single 5xx retry."""
    return base * (0.5 + random.random())
