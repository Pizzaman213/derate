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

from .config import (
    BACKOFF_MAX_S,
    BACKOFF_MIN_S,
    KEY_EXPIRY_WARN_DAYS_DEFAULT,
    RETRY_AFTER_MAX_S,
    RETRY_JITTER_S,
)

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


@dataclass(frozen=True)
class KeyCheck:
    """What the last key check found. Kept in memory only.

    ``credential`` is a fingerprint of the key that was checked, so a check
    made with a different key stops counting without any reset code. It is
    never serialized.
    """

    state: str  # "ok" | "rejected" | "inconclusive"
    checked_at: float
    credential: str | None
    detail: str | None = None
    expires_at: float | None = None


@dataclass
class ProviderRuntime:
    """Mutable state for one provider. Persisted only in part; see the store."""

    provider_id: str
    healthy: bool = True
    last_error: str | None = None
    last_refreshed: float = 0.0
    #: 429 state belongs to the upstream model, never its served-name alias.
    rate_limit_until: dict[str, float] = field(default_factory=dict)
    rate_limit_errors: dict[str, str] = field(default_factory=dict)
    consecutive_rate_limits: Counter[str] = field(default_factory=Counter)
    #: Consecutive provider-wide transport/5xx failures, independent of 429s.
    consecutive_failures: int = 0
    #: Provider-wide transport/5xx backoff, separate from model throttling.
    failure_backoff_until: float = 0.0
    #: Set when the upstream rejected our credentials, and the one kind of
    #: unhealthy that does NOT expire. A bad key is not transient: retrying it
    #: on a timer burns quota and can get an account locked, so this latches
    #: until a successful request or key check, a sign-in, or a credential
    #: that differs from the one refused (`rejected_credential`).
    auth_rejected: bool = False
    #: Fingerprint of the key the upstream refused. The same key never
    #: releases the latch; a different one does. In memory only.
    rejected_credential: str | None = None
    #: The last key check, in memory only. See `KeyCheck`.
    key_check: KeyCheck | None = None
    #: Days ahead of a reported key expiry to warn. Persisted by the store.
    key_expiry_warn_days: int = KEY_EXPIRY_WARN_DAYS_DEFAULT
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
    #: Upstream id -> whether OpenRouter may fall back to other hosts when the
    #: pinned one is down: ``provider: {"order": [pin], "allow_fallbacks":
    #: true}`` instead of the strict ``{"only": [pin]}``. Only meaningful
    #: beside a pin; clearing the pin clears it. Merged a key at a time, like
    #: ``backend_pins`` (`ProviderService._screen_backend_fallbacks`).
    backend_fallbacks: dict[str, bool] = field(default_factory=dict)
    #: Upstream id -> ``(input_cost_per_mtok, output_cost_per_mtok)`` of the
    #: pinned host, captured from the live endpoints call when the pin was set.
    #: It replaces the catalogue price on the model
    #: (`ProviderService._apply_pin_prices`), so routing cost and un-metered
    #: spend follow the host that actually serves the request.
    backend_pin_prices: dict[str, tuple[float | None, float | None]] = field(
        default_factory=dict
    )
    #: Upstream id -> admin-set USD per minute of audio (``audio_prices`` on
    #: the provider PATCH). Applied onto ``ProviderModel.price_per_minute_usd``
    #: by `ProviderService._apply_pin_prices`, on load, every refresh and
    #: every patch, because discovery rebuilds the model list without it.
    audio_prices: dict[str, float] = field(default_factory=dict)

    # -- the allowlist -----------------------------------------------------

    def serves(self, upstream_id: str) -> bool:
        """Whether this provider offers *upstream_id*.

        The only reader of the tri-state above. Every filter in the package
        calls this rather than testing ``enabled_models`` itself, so there is
        one place that decides what ``None`` means.
        """
        return self.enabled_models is None or upstream_id in self.enabled_models

    # -- health transitions ------------------------------------------------

    def note_success(self, now: float, upstream_id: str | None = None) -> None:
        """Clear provider failures and only the successful model's 429 state.

        Credential updates have no model and must not erase active cooldowns.
        """
        self.healthy = True
        self.last_error = None
        if upstream_id is not None:
            self.rate_limit_until.pop(upstream_id, None)
            self.rate_limit_errors.pop(upstream_id, None)
            self.consecutive_rate_limits.pop(upstream_id, None)
        self.failure_backoff_until = 0.0
        self.consecutive_failures = 0
        self.auth_rejected = False
        self.rejected_credential = None

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

    def note_rate_limit(
        self, now: float, upstream_id: str, retry_after: str | None, message: str
    ) -> float:
        """429. Temporarily stop admitting this model; still healthy.

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
                BACKOFF_MIN_S * (2**self.consecutive_rate_limits[upstream_id]),
            )
        else:
            seconds = min(seconds, RETRY_AFTER_MAX_S)
        self.consecutive_rate_limits[upstream_id] += 1
        self.rate_limit_until[upstream_id] = now + seconds
        self.rate_limit_errors[upstream_id] = message
        # Health is untouched on purpose. Rate limited is busy, not broken.
        log.info(
            "provider %s model %s rate limited for %.0fs: %s",
            self.provider_id, upstream_id, seconds, message,
        )
        return seconds

    def note_server_error(self, now: float, status: int, message: str) -> None:
        self.healthy = False
        self.last_error = f"upstream {status}: {message}"
        seconds = self._back_off(now)
        log.warning(
            "provider %s unhealthy after %s; retrying in %.0fs",
            self.provider_id, status, seconds,
        )

    def note_auth_failure(
        self,
        now: float,
        status: int,
        api_key_ref: str,
        message: str,
        *,
        credential: str | None,
        sign_in: bool = False,
        from_environment: bool = False,
    ) -> None:
        """401 or 403. Unhealthy immediately; retrying a bad key just burns time.

        ``credential`` is the fingerprint of the refused key: the latch is
        released by a different one and never by the same one again.
        ``sign_in`` names a credential that came from signing in, where
        ``api_key_ref`` is a label rather than a reference and "check the key
        behind it" would send the operator looking for a key that never existed.
        ``from_environment`` says the key is read from the coordinator's
        environment, where a new value needs a restart.
        """
        self.healthy = False
        self.auth_rejected = True
        self.rejected_credential = credential
        if sign_in:
            self.last_error = f"authentication rejected ({status}) for {api_key_ref}: {message}"
        else:
            self.last_error = (
                f"authentication rejected ({status}); check the key behind "
                f"api_key_ref {api_key_ref!r}: {message}"
            )
            if from_environment:
                self.last_error += (
                    "; it is read from the coordinator's environment, so a new value "
                    "there needs a restart -- or paste a key in Settings → Providers, "
                    "which takes effect at once"
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

    def note_missing_key(self, api_key_ref: str, *, sign_in: bool = False) -> None:
        self.missing_key_ref = api_key_ref
        self.healthy = False
        if sign_in:
            self.last_error = (
                f"{api_key_ref} is missing or expired; sign in again from "
                "Settings → Providers"
            )
            return
        self.last_error = (
            f"api_key_ref {api_key_ref!r} resolves to nothing; set that environment "
            f"variable or add it to secrets.json, then re-enable the provider"
        )

    def clear_missing_key(self) -> None:
        self.missing_key_ref = None

    # -- admission ---------------------------------------------------------

    def rate_limited(self, now: float, upstream_id: str | None = None) -> bool:
        return now < self.rate_limit_until.get(upstream_id, 0.0)

    def retry_in(self, now: float, upstream_id: str | None = None) -> float:
        """Wait for this model plus provider failures; no model means global only."""
        return max(
            0.0, self.rate_limit_until.get(upstream_id, 0.0) - now,
            self.failure_backoff_until - now,
        )

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

    def admission_block(self, now: float, upstream_id: str | None = None) -> str | None:
        """Provider-wide block, plus this model's cooldown when specified."""
        if self.missing_key_ref is not None:
            return f"api_key_ref {self.missing_key_ref!r} is unresolved"
        if self.rate_limited(now, upstream_id):
            return f"rate limited for another {self.retry_in(now, upstream_id):.0f}s"
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

    def admitting(self, now: float, upstream_id: str | None = None) -> bool:
        return self.admission_block(now, upstream_id) is None

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
