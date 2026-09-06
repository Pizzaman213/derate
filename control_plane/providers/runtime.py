"""Per-provider live state: health, backoff, spend, in-flight count.

None of this is persisted in the frozen :class:`Provider` record beyond the
two fields it already has. It lives here because a rate-limited provider is a
different thing from an unhealthy one, and the difference is what Agent G
routes on.
"""

from __future__ import annotations

import email.utils
import logging
import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import BACKOFF_MAX_S, BACKOFF_MIN_S, RETRY_JITTER_S

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
    unpriced_requests: int = 0


@dataclass
class ProviderRuntime:
    """Mutable state for one provider. Persisted only in part; see the store."""

    provider_id: str
    healthy: bool = True
    last_error: str | None = None
    last_refreshed: float = 0.0
    backoff_until: float = 0.0
    consecutive_rate_limits: int = 0
    daily_budget_usd: float | None = None
    aliases: dict[str, str] = field(default_factory=dict)
    spend: dict[str, DaySpend] = field(default_factory=dict)
    outstanding: Counter = field(default_factory=Counter)
    # Set when a key reference cannot be resolved. Distinct from an upstream
    # auth rejection, and the reason the provider is disabled rather than sick.
    missing_key_ref: str | None = None

    # -- health transitions ------------------------------------------------

    def note_success(self, now: float) -> None:
        """A request got through. Everything transient clears."""
        self.healthy = True
        self.last_error = None
        self.backoff_until = 0.0
        self.consecutive_rate_limits = 0

    def note_rate_limit(self, now: float, retry_after: str | None, message: str) -> float:
        """429. Temporarily not admitting; still healthy.

        Honours ``Retry-After`` when the provider sends one, otherwise backs
        off exponentially from one second to a sixty second cap.
        """
        seconds = parse_retry_after(retry_after, now)
        if seconds is None:
            seconds = min(
                BACKOFF_MAX_S,
                BACKOFF_MIN_S * (2**self.consecutive_rate_limits),
            )
        seconds = min(seconds, BACKOFF_MAX_S)
        self.consecutive_rate_limits += 1
        self.backoff_until = now + seconds
        self.last_error = f"rate limited, retrying in {seconds:.0f}s: {message}"
        # Health is untouched on purpose. Rate limited is busy, not broken.
        log.info("provider %s rate limited for %.0fs", self.provider_id, seconds)
        return seconds

    def note_server_error(self, now: float, status: int, message: str) -> None:
        self.healthy = False
        self.last_error = f"upstream {status}: {message}"
        log.warning("provider %s unhealthy after %s", self.provider_id, status)

    def note_auth_failure(self, now: float, status: int, api_key_ref: str, message: str) -> None:
        """401 or 403. Unhealthy immediately; retrying a bad key just burns time."""
        self.healthy = False
        self.last_error = (
            f"authentication rejected ({status}); check the key behind "
            f"api_key_ref {api_key_ref!r}: {message}"
        )
        log.warning("provider %s auth rejected (%s)", self.provider_id, status)

    def note_transport_error(self, now: float, message: str) -> None:
        self.healthy = False
        self.last_error = f"could not reach upstream: {message}"

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
        return max(0.0, self.backoff_until - now)

    def over_budget(self, now: float) -> bool:
        if self.daily_budget_usd is None:
            return False
        return self.spend_today(now) >= self.daily_budget_usd

    def admission_block(self, now: float) -> str | None:
        """Why this provider is not admitting, or None when it is."""
        if self.missing_key_ref is not None:
            return f"api_key_ref {self.missing_key_ref!r} is unresolved"
        if self.rate_limited(now):
            return f"rate limited for another {self.retry_in(now):.0f}s"
        if self.over_budget(now):
            return (
                f"daily budget of ${self.daily_budget_usd:.2f} reached "
                f"(${self.spend_today(now):.2f} spent today)"
            )
        if not self.healthy:
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
    ) -> float:
        """Add one request's usage. Returns the dollars charged, zero if unpriced."""
        day = self.day(now)
        day.requests += 1
        day.input_tokens += max(0, input_tokens)
        day.output_tokens += max(0, output_tokens)
        if input_cost_per_mtok is None and output_cost_per_mtok is None:
            day.unpriced_requests += 1
            return 0.0
        cost = (
            max(0, input_tokens) * (input_cost_per_mtok or 0.0)
            + max(0, output_tokens) * (output_cost_per_mtok or 0.0)
        ) / 1_000_000.0
        day.usd += cost
        return cost

    def prune_spend(self, now: float, keep_days: int = 30) -> None:
        if len(self.spend) <= keep_days:
            return
        for key in sorted(self.spend)[:-keep_days]:
            del self.spend[key]


def jittered_delay(base: float = RETRY_JITTER_S) -> float:
    """Small random delay for the single 5xx retry."""
    return base * (0.5 + random.random())
