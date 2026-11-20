"""Local model pricing, and the one function that turns tokens into dollars.

A local model costs nothing per token until an admin says what it should cost
-- electricity, amortised hardware, or a chargeback rate. ``local_pricing``
holds that rate per served name; ``cost()`` is where every request's dollar
figure and its :class:`CostBasis` come from, so "metered", "estimated" and
"unpriced" mean the same thing on a 429, a counter and a telemetry row.

Unpriced is a fact, never zero: a request nobody has a rate for is counted
in ``unpriced_requests`` and never toward a ``*_usd_*`` limit.
"""

from __future__ import annotations

import math
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

from control_plane.contracts.auth import AuthUnavailable, LocalPrice
from control_plane.contracts.routing import TargetKind
from control_plane.contracts.telemetry import CostBasis

from ._db import open_store

SCHEMA_VERSION = 1
VERSION_KEY = "pricing_schema_version"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS local_pricing (
    model               TEXT PRIMARY KEY,
    input_usd_per_mtok  REAL,
    output_usd_per_mtok REAL,
    set_by              TEXT NOT NULL,
    set_at              REAL NOT NULL
);
"""

_MTOK = 1_000_000


def _rate(name: str, value: float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number or null, got {value!r}")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite number >= 0, got {value!r}")
    return value


def _estimate(prompt_tokens: int, completion_tokens: int, rate_in: float, rate_out: float) -> float:
    return max(0, prompt_tokens) * rate_in / _MTOK + max(0, completion_tokens) * rate_out / _MTOK


class PricingStore:
    """``PricingPort`` over the ``local_pricing`` table of ``limits.db``.

    Opened lazily: :meth:`open` never touches the disk, the first call does,
    and a store that cannot be opened raises ``AuthUnavailable`` on that call
    and is tried again on the next.
    """

    def __init__(self, path: Path | str, *, clock: Callable[[], float] = time.time) -> None:
        self._path = Path(path)
        self._clock = clock
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    @classmethod
    def open(cls, path: Path | str, *, clock: Callable[[], float] = time.time) -> PricingStore:
        return cls(path, clock=clock)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = open_store(self._path, _SCHEMA, VERSION_KEY, SCHEMA_VERSION)
        return self._conn

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            try:
                return self._db().execute(sql, params).fetchall()
            except sqlite3.Error as exc:
                raise AuthUnavailable(f"the limits store at {self._path} failed: {exc}") from exc

    @staticmethod
    def _row(row: sqlite3.Row) -> LocalPrice:
        return LocalPrice(
            model=row["model"],
            input_usd_per_mtok=row["input_usd_per_mtok"],
            output_usd_per_mtok=row["output_usd_per_mtok"],
            set_by=row["set_by"],
            set_at=row["set_at"],
        )

    def get(self, model: str) -> LocalPrice | None:
        rows = self._query("SELECT * FROM local_pricing WHERE model=?", (model,))
        return self._row(rows[0]) if rows else None

    def list(self) -> list[LocalPrice]:
        return [self._row(r) for r in self._query("SELECT * FROM local_pricing ORDER BY model")]

    def set(
        self,
        model: str,
        input_usd_per_mtok: float | None,
        output_usd_per_mtok: float | None,
        *,
        actor: str,
    ) -> LocalPrice:
        """Upsert one model's rates. ``None`` is "no rate", never ``0``."""
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty served name")
        price = LocalPrice(
            model=model,
            input_usd_per_mtok=_rate("input_usd_per_mtok", input_usd_per_mtok),
            output_usd_per_mtok=_rate("output_usd_per_mtok", output_usd_per_mtok),
            set_by=actor,
            set_at=self._clock(),
        )
        self._query(
            "INSERT INTO local_pricing(model, input_usd_per_mtok, output_usd_per_mtok, set_by, set_at) "
            "VALUES(?, ?, ?, ?, ?) ON CONFLICT(model) DO UPDATE SET "
            "input_usd_per_mtok=excluded.input_usd_per_mtok, "
            "output_usd_per_mtok=excluded.output_usd_per_mtok, "
            "set_by=excluded.set_by, set_at=excluded.set_at",
            (
                price.model,
                price.input_usd_per_mtok,
                price.output_usd_per_mtok,
                price.set_by,
                price.set_at,
            ),
        )
        return price

    def clear(self, model: str, *, actor: str) -> None:
        self._query("DELETE FROM local_pricing WHERE model=?", (model,))

    def cost(
        self,
        *,
        model: str,
        target_kind: str,
        prompt_tokens: int,
        completion_tokens: int,
        metered_usd: float | None,
        provider_in_per_mtok: float | None,
        provider_out_per_mtok: float | None,
    ) -> tuple[float | None, CostBasis]:
        """``(usd, basis)``, first match wins.

        1. The upstream said what it charged -> ``(metered_usd, METERED)``.
        2. A LOCAL target whose served name has a price row with *both* rates
           -> tokens times those rates, ``ESTIMATED``.
        3. Both provider rates known -> tokens times those, ``ESTIMATED``.
        4. Otherwise ``(None, UNPRICED)`` -- one missing rate is no rate, a
           half-priced request would read as cheap rather than unknown.
        """
        if metered_usd is not None:
            return float(metered_usd), CostBasis.METERED
        if target_kind == TargetKind.LOCAL.value:
            price = self.get(model)
            if (
                price is not None
                and price.input_usd_per_mtok is not None
                and price.output_usd_per_mtok is not None
            ):
                return (
                    _estimate(
                        prompt_tokens,
                        completion_tokens,
                        price.input_usd_per_mtok,
                        price.output_usd_per_mtok,
                    ),
                    CostBasis.ESTIMATED,
                )
        if provider_in_per_mtok is not None and provider_out_per_mtok is not None:
            return (
                _estimate(
                    prompt_tokens, completion_tokens, provider_in_per_mtok, provider_out_per_mtok
                ),
                CostBasis.ESTIMATED,
            )
        return None, CostBasis.UNPRICED
