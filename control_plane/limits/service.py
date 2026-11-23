"""``LimitService``: the per-key and per-org limits, checked before dispatch.

One call answers "may this request run": :meth:`LimitService.check_and_reserve`
walks the checks in a fixed order and either refuses with the limit that
stopped it (``limit_source`` = ``"<scope>:<limit_type>"``) or reserves what the
request is estimated to use. :meth:`LimitService.settle` swaps the estimate for
what it actually used. In between, the reservation *counts*: every check sees
persisted counters plus the estimates of everything still in flight, which is
what stops thirty-two concurrent requests from each seeing the same headroom
and jointly spending it thirty-two times.

Three kinds of state, and where each lives:

* cycle counters -- ``limits.db``, one row per (scope, id, cycle_start,
  metric), written once per settled request in one transaction;
* rpm/tpm windows -- in memory, a sliding 60 s deque per (scope, id[, model]).
  A key with no ``rpm`` is held to ``DEFAULT_KEY_RPM`` and one with no
  ``max_concurrent`` to ``DEFAULT_KEY_MAX_CONCURRENT``;
* in-flight reservations -- in memory, which is also ``max_concurrent``.

The in-memory two do not survive a restart, and do not need to: a window is
60 seconds long and an in-flight request does not outlive the process that
was serving it.
"""

from __future__ import annotations

import json
import logging
import math
import secrets
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path

from control_plane.contracts.auth import (
    DEFAULT_KEY_MAX_CONCURRENT,
    DEFAULT_KEY_RPM,
    NEAR_LIMIT_FRACTION,
    ApiKey,
    AuthErrorCode,
    AuthUnavailable,
    KeyPort,
    LimitDecision,
    LimitScope,
    LimitState,
    Org,
    PricingPort,
    Principal,
    Reservation,
    UsageActuals,
)
from control_plane.contracts.routing import TargetKind
from control_plane.contracts.telemetry import CostBasis

from ._db import open_store
from .cycle import cycle_bounds
from .types import (
    CLOUD_ONLY,
    COUNTING_WINDOWS,
    FALLBACK_BASES,
    KIND,
    LOCAL_ONLY,
    USD_BASES,
    LimitKind,
    cycle_metric,
    parse_limit_type,
)

log = logging.getLogger("control_plane.limits")

SCHEMA_VERSION = 1
VERSION_KEY = "limits_schema_version"

#: The rpm/tpm window.
WINDOW_S = 60.0
#: What a request with no ``max_tokens`` (and no cap to inject) is assumed to
#: generate, for reservation purposes only; settle replaces it.
DEFAULT_OUTPUT_ESTIMATE = 256
#: ``GatewaySettings.chars_per_token`` / ``tokens_per_message_overhead``
#: defaults (``gateway/settings.py:109-110``), see :func:`estimate_prompt_tokens`.
CHARS_PER_TOKEN = 4.0
TOKENS_PER_MESSAGE_OVERHEAD = 4
#: Every field a client may ask for an output ceiling in: Chat Completions
#: (old and new spelling), Responses, and llama.cpp's ``n_predict``, which
#: llama-server honours over ``max_tokens`` -- unlisted, it was an uncapped
#: output ceiling.
MAX_OUTPUT_FIELDS = ("max_tokens", "max_completion_tokens", "max_output_tokens", "n_predict")
#: What one image part is assumed to cost in prompt tokens. Its bytes are a
#: URL or base64 and say nothing about the vision encoder's tile count, and
#: counting it as zero let an image-heavy prompt past every prompt cap.
IMAGE_TOKENS_ESTIMATE = 1000
#: Content-part types that carry an image: Chat Completions and Responses.
_IMAGE_PART_TYPES = frozenset({"image_url", "input_image", "image"})
#: Audio and video parts are tokens the template renders too; priced at the
#: image estimate, which is the only per-part figure this module has.
_MEDIA_PART_TYPES = _IMAGE_PART_TYPES | {"input_audio", "audio_url", "video_url", "video"}
_MEDIA_PART_KEYS = ("image_url", "input_audio", "audio_url", "video_url")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cycle_counters (
    scope       TEXT NOT NULL,
    scope_id    TEXT NOT NULL,
    cycle_start REAL NOT NULL,
    metric      TEXT NOT NULL,
    value       REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, scope_id, cycle_start, metric)
);
CREATE TABLE IF NOT EXISTS overrides (
    scope       TEXT NOT NULL,
    scope_id    TEXT NOT NULL,
    limit_type  TEXT NOT NULL,
    cycle_start REAL NOT NULL,
    value       REAL NOT NULL,
    actor       TEXT NOT NULL,
    created_at  REAL NOT NULL,
    PRIMARY KEY (scope, scope_id, limit_type, cycle_start)
);
CREATE TABLE IF NOT EXISTS cycle_resets (
    scope       TEXT NOT NULL,
    scope_id    TEXT NOT NULL,
    cycle_start REAL NOT NULL,
    reset_at    REAL NOT NULL,
    actor       TEXT NOT NULL
);
"""

_UPSERT_COUNTER = (
    "INSERT INTO cycle_counters(scope, scope_id, cycle_start, metric, value) "
    "VALUES(?, ?, ?, ?, ?) ON CONFLICT(scope, scope_id, cycle_start, metric) "
    "DO UPDATE SET value = value + excluded.value"
)

#: Check order inside one scope: LIMIT_TYPES order (``KIND`` is keyed in it).
_ORDER = {base: i for i, base in enumerate(KIND)}
_REQUEST_COUNTED = frozenset({"requests_cycle", "rpm", "model:rpm", "max_concurrent"})
_LOCAL = TargetKind.LOCAL.value
_REMOTE = TargetKind.REMOTE.value


def estimate_prompt_tokens(
    body: dict,
    *,
    chars_per_token: float = CHARS_PER_TOKEN,
    per_message: int = TOKENS_PER_MESSAGE_OVERHEAD,
) -> int:
    """The one character-based prompt estimate: the prompt cap, the reservation
    and admission's KV arithmetic all read it (admission passes its own
    ``chars_per_token`` / ``tokens_per_message_overhead`` settings). There is
    no tokenizer here; loading one per model would cost more than it is worth.

    Counts what the chat template renders, not only ``content``: token-id
    prompts, ``tools``/``functions``/``response_format``, an assistant turn's
    ``tool_calls`` arguments, and ``IMAGE_TOKENS_ESTIMATE`` per image, audio or
    video part. Admission used to keep a second formula that counted strings
    only, and settled a 30k-token token-id prompt as zero.
    """
    cpt = max(1.0, chars_per_token)
    chars = 0
    overhead = 0
    images = 0
    tokens = 0

    messages = body.get("messages")
    if isinstance(messages, list):
        for message in messages:
            overhead += per_message
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str):
                chars += len(content)
            elif isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    kind = part.get("type")
                    if isinstance(part.get("text"), str):
                        chars += len(part["text"])
                    elif (isinstance(kind, str) and kind in _MEDIA_PART_TYPES) or any(
                        key in part for key in _MEDIA_PART_KEYS
                    ):
                        images += 1
            elif content is not None:
                chars += len(str(content))
            # An assistant turn's calls are rendered back into the prompt.
            for name in ("tool_calls", "function_call"):
                if message.get(name) is not None:
                    chars += len(json.dumps(message[name], default=str))

    prompt = body.get("prompt")
    if isinstance(prompt, str):
        chars += len(prompt)
    elif isinstance(prompt, list):
        chars, more = _count_list(prompt, chars)
        tokens += more

    inputs = body.get("input")
    if isinstance(inputs, str):
        chars += len(inputs)
    elif isinstance(inputs, list):
        chars, more = _count_list(inputs, chars)
        tokens += more

    # Tool schemas and a JSON-schema response_format are rendered into the
    # prompt by the chat template, and can be the bulk of it.
    for name in ("tools", "functions", "response_format"):
        value = body.get(name)
        if value is not None:
            chars += len(json.dumps(value, default=str))

    return int(chars / cpt) + tokens + overhead + images * IMAGE_TOKENS_ESTIMATE


def _is_token_list(item: object) -> bool:
    return (
        isinstance(item, list)
        and bool(item)
        and all(isinstance(t, int) and not isinstance(t, bool) for t in item)
    )


def _count_list(items: list, chars: int) -> tuple[int, int]:
    """``(chars, tokens)`` of a ``prompt``/``input`` list: strings are
    characters, a list of token ids is already tokens, and a list of such lists
    (a pre-tokenised batch) is the sum of their lengths. Counting ids as zero
    let any prompt past ``max_prompt_tokens_per_request``."""
    if _is_token_list(items):
        return chars, len(items)
    tokens = 0
    for item in items:
        if isinstance(item, str):
            chars += len(item)
        elif _is_token_list(item):
            tokens += len(item)
    return chars, tokens


#: Largest output ceiling a request may name. Anything above is not a budget
#: but an overflow waiting to happen (``int(inf)`` is a 500).
MAX_OUTPUT_CEILING = 2**31


def requested_output(body: dict) -> tuple[str, float] | None:
    """``(field, value)`` of the largest output ceiling the body asks for.

    ``ValueError`` when a ceiling is not an integer (a string such as
    ``"100000"`` is coerced by the engine and would otherwise bypass the cap),
    is not finite, or exceeds :data:`MAX_OUTPUT_CEILING` -- JSON's ``1e999``
    parses to ``inf``.
    """
    best: tuple[str, float] | None = None
    for name in MAX_OUTPUT_FIELDS:
        value = body.get(name)
        if value is None:
            continue
        if isinstance(value, float) and math.isfinite(value) and value.is_integer():
            value = int(value)
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(
                f"{name} must be a finite number no greater than {MAX_OUTPUT_CEILING}, "
                f"not {value!r}"
            )
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer, not {value!r}")
        if value > MAX_OUTPUT_CEILING:
            raise ValueError(
                f"{name} must be a finite number no greater than {MAX_OUTPUT_CEILING}, "
                f"not {value!r}"
            )
        if value < 1:
            # Not "no ceiling": llama-server reads n_predict -1 as unbounded,
            # and skipping it forwarded the body uncapped (vLLM refuses < 1).
            raise ValueError(f"{name} must be a positive integer, not {value!r}")
        if best is None or value > best[1]:
            best = (name, value)
    return best


#: Most completions one request may ask for (``n``/``best_of``). Beyond this
#: the estimate is not a budget but an overflow (K-4).
MAX_COMPLETIONS = 128


def completions_requested(body: dict) -> int:
    """How many completions one request generates: ``n`` returned, or
    ``best_of`` generated server-side and billed even when not returned, times
    the number of prompts in a ``/v1/completions`` batch (a list of strings or
    of token lists generates one set per prompt). Every output ceiling is per
    completion, so a request's output is that ceiling times this.

    ``ValueError`` above :data:`MAX_COMPLETIONS` or for a value that is not an
    integer: the engines coerce ``"128"`` and ``128.0`` to 128, so skipping
    them counted one copy where the engine generated 128."""
    copies = 1
    for name in ("n", "best_of"):
        value = body.get(name)
        if value is None:
            continue
        if isinstance(value, float) and math.isfinite(value) and value.is_integer():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer, not {value!r}")
        if value > MAX_COMPLETIONS:
            raise ValueError(f"{name} must be an integer no greater than {MAX_COMPLETIONS}")
        copies = max(copies, value)
    prompt = body.get("prompt")
    if isinstance(prompt, list) and not _is_token_list(prompt):
        batch = sum(1 for p in prompt if isinstance(p, str) or _is_token_list(p))
        copies *= max(1, batch)
    return copies


def _limit_value(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"a limit must be a number, got {value!r}")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"a limit must be a finite number >= 0, got {value!r}")
    return value


def _kind(target_kind: object) -> str | None:
    value = getattr(target_kind, "value", target_kind)
    return value if value in (_LOCAL, _REMOTE) else None


def _fmt(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _metrics(
    model: str, kind: str | None, prompt: float, completion: float, usd: float | None
) -> dict[str, float]:
    """The counter deltas one request is worth. ``usd=None`` is unpriced."""
    tokens = prompt + completion
    out = {
        "requests": 1.0,
        "tokens_total": tokens,
        "tokens_input": prompt,
        "tokens_output": completion,
        f"model:{model}:tokens": tokens,
    }
    if kind == _LOCAL:
        out["local_tokens"] = tokens
    elif kind == _REMOTE:
        out["cloud_tokens"] = tokens
    if usd is None:
        out["unpriced_requests"] = 1.0
    else:
        out["usd"] = usd
        out[f"model:{model}:usd"] = usd
        if kind == _LOCAL:
            out["local_usd"] = usd
        elif kind == _REMOTE:
            out["cloud_usd"] = usd
    return out


@dataclass(frozen=True)
class _Configured:
    name: str
    base: str
    model: str | None
    value: float

    @property
    def kind(self) -> LimitKind:
        return KIND[self.base]


@dataclass
class _Scope:
    """One scope's limits, resolved against one instant."""

    scope: LimitScope
    scope_id: str
    cycle_start: float
    resets_at: float
    limits: list[_Configured]
    overrides: dict[str, float]

    def effective(self, lim: _Configured) -> float:
        return self.overrides.get(lim.name, lim.value)

    def source(self, lim: _Configured) -> str:
        return f"{self.scope.value}:{lim.name}"


class _Entry:
    """One request in the rpm/tpm windows. Shared by all four windows it sits
    in, so settle corrects the token count once for all of them."""

    __slots__ = ("ts", "tokens", "void")

    def __init__(self, ts: float, tokens: float) -> None:
        self.ts = ts
        self.tokens = tokens
        self.void = False


@dataclass
class _Held:
    reservation: Reservation
    key_anchor: float
    org_anchor: float
    estimate: dict[str, float]
    entry: _Entry
    scopes: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    #: The target kind the request was checked against (``None``: unknown).
    kind: str | None = None
    #: Set by :meth:`LimitService.mark_started` once bytes reached the client.
    #: A started request is never voided: a ``None`` settle counts its
    #: estimate instead, because an upstream was paid to produce what it sent.
    started: bool = False


class LimitService:
    """``LimitPort`` over ``limits.db`` and in-memory windows.

    Opened lazily: :meth:`open` never touches the disk; the first call that
    needs the store opens it, and one that cannot be opened raises
    ``AuthUnavailable`` (fail closed) and is tried again on the next call.
    Everything runs under one ``RLock``.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        keys: KeyPort,
        pricing: PricingPort,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = Path(path)
        self._keys = keys
        self._pricing = pricing
        self._clock = clock
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._windows: dict[tuple[str, str, str | None], deque[_Entry]] = {}
        self._live: dict[tuple[str, str], dict[str, float]] = {}
        self._held: dict[str, _Held] = {}
        self._swept_at = 0.0
        self._warned: set[str] = set()

    @classmethod
    def open(
        cls,
        path: Path | str,
        *,
        keys: KeyPort,
        pricing: PricingPort,
        clock: Callable[[], float] = time.time,
    ) -> LimitService:
        return cls(path, keys=keys, pricing=pricing, clock=clock)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def cycle_bounds(self, anchor: float, now: float) -> tuple[float, float]:
        return cycle_bounds(anchor, now)

    # -- store ---------------------------------------------------------------

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = open_store(self._path, _SCHEMA, VERSION_KEY, SCHEMA_VERSION)
        return self._conn

    def _query(self, sql: str, params: tuple) -> list[sqlite3.Row]:
        try:
            return self._db().execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            raise AuthUnavailable(f"the limits store at {self._path} failed: {exc}") from exc

    def _transaction(self, statements: Iterable[tuple[str, Iterable[tuple]]]) -> None:
        conn = self._db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for sql, rows in statements:
                    conn.executemany(sql, rows)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        except sqlite3.Error as exc:
            raise AuthUnavailable(f"the limits store at {self._path} failed: {exc}") from exc

    def _counters(self, sc: _Scope) -> dict[str, float]:
        rows = self._query(
            "SELECT metric, value FROM cycle_counters WHERE scope=? AND scope_id=? AND cycle_start=?",
            (sc.scope.value, sc.scope_id, sc.cycle_start),
        )
        return {r["metric"]: r["value"] for r in rows}

    # -- resolving what applies ---------------------------------------------

    def _now(self, now: float | None) -> float:
        return float(self._clock() if now is None else now)

    def _configured(self, limits: dict | None, model: str | None) -> list[_Configured]:
        """The limits in ``limits`` that apply to ``model`` (``None``: all)."""
        out: list[_Configured] = []
        for name, value in (limits or {}).items():
            try:
                base, served = parse_limit_type(name)
                amount = _limit_value(value)
            except (ValueError, TypeError):
                if name not in self._warned:
                    self._warned.add(name)
                    log.warning("ignoring a limit that cannot be enforced: %r = %r", name, value)
                continue
            if model is not None and served is not None and served != model:
                continue
            out.append(_Configured(name, base, served, amount))
        out.sort(key=lambda lim: (_ORDER[lim.base], lim.model or ""))
        return out

    def _scope(
        self,
        scope: LimitScope,
        scope_id: str,
        anchor: float,
        limits: dict | None,
        model: str | None,
        now: float,
    ) -> _Scope:
        # One place, so check_and_reserve, settle, states() and raise all see
        # the same limit: a key that names no rpm is held to DEFAULT_KEY_RPM
        # rather than being unlimited. An operator who wants more sets more.
        if scope is LimitScope.KEY and "rpm" not in (limits or {}):
            limits = {**(limits or {}), "rpm": DEFAULT_KEY_RPM}
        # Same for concurrency: one key could otherwise hold every connection
        # a backend's pool has.
        if scope is LimitScope.KEY and "max_concurrent" not in (limits or {}):
            limits = {**(limits or {}), "max_concurrent": DEFAULT_KEY_MAX_CONCURRENT}
        configured = self._configured(limits, model)
        start, resets_at = cycle_bounds(anchor, now)
        overrides: dict[str, float] = {}
        if configured:
            rows = self._query(
                "SELECT limit_type, value FROM overrides "
                "WHERE scope=? AND scope_id=? AND cycle_start=?",
                (scope.value, scope_id, start),
            )
            overrides = {r["limit_type"]: r["value"] for r in rows}
        return _Scope(scope, scope_id, start, resets_at, configured, overrides)

    def _subject(self, scope: LimitScope, scope_id: str) -> ApiKey | Org | None:
        if scope is LimitScope.KEY:
            return self._keys.get_key(scope_id)
        return self._keys.get_org(scope_id)

    def _window_usage(
        self, wkey: tuple[str, str, str | None], counting: bool, now: float
    ) -> tuple[float, float | None]:
        """``(requests or tokens in the last WINDOW_S, oldest counted ts)``."""
        dq = self._windows.get(wkey)
        if not dq:
            return 0.0, None
        horizon = now - WINDOW_S
        while dq and dq[0].ts <= horizon:
            dq.popleft()
        used = 0.0
        oldest: float | None = None
        for entry in dq:
            if entry.void or entry.ts <= horizon:
                continue
            used += 1.0 if counting else entry.tokens
            if oldest is None or entry.ts < oldest:
                oldest = entry.ts
        return used, oldest

    def _usage(
        self, sc: _Scope, lim: _Configured, counters: dict[str, float], now: float
    ) -> tuple[float, float | None]:
        """``(used, resets_at)`` of one limit right now."""
        kind = lim.kind
        if kind is LimitKind.CYCLE:
            metric = cycle_metric(lim.base, lim.model)
            live = self._live.get((sc.scope.value, sc.scope_id), {})
            return counters.get(metric, 0.0) + live.get(metric, 0.0), sc.resets_at
        if kind is LimitKind.WINDOW:
            used, oldest = self._window_usage(
                (sc.scope.value, sc.scope_id, lim.model), lim.base in COUNTING_WINDOWS, now
            )
            return used, (oldest + WINDOW_S if oldest is not None else now)
        if kind is LimitKind.CONCURRENCY:
            return self._live.get((sc.scope.value, sc.scope_id), {}).get("requests", 0.0), now
        return 0.0, None

    def _state(
        self, sc: _Scope, lim: _Configured, counters: dict[str, float], now: float
    ) -> LimitState:
        effective = sc.effective(lim)
        used, resets_at = self._usage(sc, lim, counters, now)
        if lim.kind is LimitKind.REQUEST_CAP:
            fraction = 0.0
        else:
            fraction = used / effective if effective > 0 else 1.0
        return LimitState(
            limit_type=lim.name,
            scope=sc.scope,
            scope_id=sc.scope_id,
            limit=lim.value,
            effective_limit=effective,
            used=used,
            remaining=max(0.0, effective - used),
            resets_at=resets_at,
            fraction=fraction,
            near=fraction >= NEAR_LIMIT_FRACTION,
            at=fraction >= 1.0,
        )

    # -- refusals ------------------------------------------------------------

    @staticmethod
    def _exceeded(
        sc: _Scope, lim: _Configured, used: float, limit: float, resets_at: float
    ) -> LimitDecision:
        source = sc.source(lim)
        if lim.base in USD_BASES:
            quantity = f"${_fmt(used)} of ${_fmt(limit)}"
        else:
            unit = "requests" if lim.base in _REQUEST_COUNTED else "tokens"
            quantity = f"{_fmt(used)} of {_fmt(limit)} {unit}"
        if lim.kind is LimitKind.CYCLE:
            detail = f"{quantity} used this cycle; resets at {_iso(resets_at)}"
        elif lim.kind is LimitKind.WINDOW:
            detail = f"{quantity} in the last {_fmt(WINDOW_S)} s; resets at {_iso(resets_at)}"
        else:
            detail = f"{quantity} in flight; a slot frees when one of them finishes"
        return LimitDecision(
            allowed=False,
            status=429,
            code=AuthErrorCode.LIMIT_EXCEEDED.value,
            message=f"{source} limit reached: {detail}",
            limit_source=source,
            used=used,
            limit=limit,
            resets_at=resets_at,
        )

    @staticmethod
    def _tightest(scopes: list[_Scope], base: str) -> tuple[_Scope, _Configured, float] | None:
        """The lowest effective value of a per-request cap; the key wins a tie."""
        best: tuple[_Scope, _Configured, float] | None = None
        for sc in scopes:
            for lim in sc.limits:
                if lim.base == base:
                    value = sc.effective(lim)
                    if best is None or value < best[2]:
                        best = (sc, lim, value)
        return best

    def _check_caps(
        self,
        scopes: list[_Scope],
        asked: tuple[str, float] | None,
        est_prompt: int,
        copies: int = 1,
    ) -> tuple[LimitDecision | None, int | None]:
        """``copies`` is :func:`completions_requested`: the output cap bounds
        the whole request, so a per-completion ceiling is checked times it and
        an injected one is the cap divided by it."""
        inject: int | None = None
        cap = self._tightest(scopes, "max_output_tokens_per_request")
        if cap is not None:
            sc, lim, value = cap
            per_copy = int(value // copies)
            if asked is None:
                if per_copy < 1:
                    return (
                        LimitDecision(
                            allowed=False,
                            status=400,
                            code=AuthErrorCode.MAX_OUTPUT_TOKENS_EXCEEDED.value,
                            message=(
                                f"{copies} completions (n/best_of) cannot fit "
                                f"{sc.source(lim)} of {_fmt(value)}; ask for "
                                f"{int(value)} or fewer completions"
                            ),
                            limit_source=sc.source(lim),
                            used=float(copies),
                            limit=value,
                        ),
                        None,
                    )
                inject = per_copy
            elif asked[1] * copies > value:
                if copies > 1:
                    message = (
                        f"{asked[0]} {_fmt(asked[1])} x {copies} completions "
                        f"(n/best_of) is {_fmt(asked[1] * copies)}, over "
                        f"{sc.source(lim)} of {_fmt(value)}; ask for "
                        f"{_fmt(max(per_copy, 0))} or fewer per completion, or "
                        f"fewer completions"
                    )
                else:
                    message = (
                        f"{asked[0]} {_fmt(asked[1])} is over {sc.source(lim)} of "
                        f"{_fmt(value)}; ask for {_fmt(value)} or fewer"
                    )
                return (
                    LimitDecision(
                        allowed=False,
                        status=400,
                        code=AuthErrorCode.MAX_OUTPUT_TOKENS_EXCEEDED.value,
                        message=message,
                        limit_source=sc.source(lim),
                        used=float(asked[1] * copies),
                        limit=value,
                    ),
                    None,
                )
        cap = self._tightest(scopes, "max_prompt_tokens_per_request")
        if cap is not None:
            sc, lim, value = cap
            if est_prompt > value:
                return (
                    LimitDecision(
                        allowed=False,
                        status=413,
                        code=AuthErrorCode.MAX_PROMPT_TOKENS_EXCEEDED.value,
                        message=(
                            f"the prompt is an estimated {est_prompt} tokens, over "
                            f"{sc.source(lim)} of {_fmt(value)}"
                        ),
                        limit_source=sc.source(lim),
                        used=float(est_prompt),
                        limit=value,
                    ),
                    None,
                )
        return None, inject

    def _check_rates(self, sc: _Scope, now: float) -> tuple[_Configured, LimitDecision] | None:
        for lim in sc.limits:
            if lim.kind not in (LimitKind.CONCURRENCY, LimitKind.WINDOW):
                continue
            effective = sc.effective(lim)
            used, resets_at = self._usage(sc, lim, {}, now)
            if used >= effective:
                return lim, self._exceeded(sc, lim, used, effective, resets_at)
        return None

    def _check_cycle(
        self, sc: _Scope, kind: str | None, spends_usd: bool, now: float
    ) -> tuple[_Configured, LimitDecision] | None:
        applicable = [
            lim
            for lim in sc.limits
            if lim.kind is LimitKind.CYCLE
            and (lim.base not in LOCAL_ONLY or kind == _LOCAL)
            and (lim.base not in CLOUD_ONLY or kind == _REMOTE)
            and (lim.base not in USD_BASES or spends_usd)
        ]
        if not applicable:
            return None
        counters = self._counters(sc)
        for lim in applicable:
            effective = sc.effective(lim)
            used, resets_at = self._usage(sc, lim, counters, now)
            if used >= effective:
                return lim, self._exceeded(sc, lim, used, effective, resets_at)
        return None

    # -- the port ------------------------------------------------------------

    def check_and_reserve(
        self,
        principal: Principal,
        *,
        model: str,
        target_kind: str | None,
        body: dict,
        now: float | None = None,
        route_limits: dict | None = None,
    ) -> tuple[LimitDecision, Reservation | None]:
        """Refuse with the first limit that stops this request, or reserve it.

        Order: allowlist (403) -> per-request caps (400/413) -> max_concurrent,
        rpm, tpm and their per-model forms, key then org -> cycle limits, key
        then org (429). ``local_*`` limits apply only to a LOCAL target and
        ``cloud_*`` only to a REMOTE one; ``target_kind=None`` (not known yet)
        applies neither. A dollar limit does not stop a LOCAL request whose
        model has no complete price row: it settles unpriced and cannot spend
        a dollar, which is what makes falling back from an exhausted cloud
        budget to a local model possible at all. A count is blocked at
        ``used >= limit``, where ``used`` includes every live reservation --
        so a limit of 5 admits exactly 5, and a token or dollar limit admits
        while under and lets the one request that crosses it finish.

        ``route_limits`` is the served name's own ``rpm``/``tpm``
        (``RoutingConfig.limits``), checked after the org as a third scope,
        ``route:<model>``. It has no cycle and no overrides: its windows are
        the only state it keeps.
        """
        now = self._now(now)
        key, org = principal.key, principal.org
        body = body if isinstance(body, dict) else {}
        if model not in self._keys.effective_allowlist(key, org, [model]):
            return (
                LimitDecision(
                    allowed=False,
                    status=403,
                    code=AuthErrorCode.MODEL_NOT_ALLOWED.value,
                    message=f"model {model!r} is not in this key's allowed models",
                ),
                None,
            )
        kind = _kind(target_kind)
        est_prompt = estimate_prompt_tokens(body)
        try:
            asked = requested_output(body)
            copies = completions_requested(body)
        except ValueError as exc:
            return (
                LimitDecision(
                    allowed=False, status=400, code="invalid_request", message=str(exc)
                ),
                None,
            )
        with self._lock:
            scopes = [
                self._scope(LimitScope.KEY, key.key_id, key.created_at, key.limits, model, now),
                self._scope(LimitScope.ORG, org.org_id, org.created_at, org.limits, model, now),
            ]
            if route_limits:
                # Built directly: a route has no override rows and no cycle
                # anchor, so _scope's SQL query has nothing to find.
                scopes.append(
                    _Scope(
                        LimitScope.ROUTE, model, 0.0, now,
                        self._configured(route_limits, None), {},
                    )
                )
            refusal, inject = self._check_caps(scopes, asked, est_prompt, copies)
            if refusal is not None:
                return refusal, None
            est_output = copies * (
                int(asked[1])
                if asked is not None
                else (inject if inject is not None else DEFAULT_OUTPUT_ESTIMATE)
            )
            est_usd, spends_usd = 0.0, True
            if any(lim.base in USD_BASES for sc in scopes for lim in sc.limits):
                usd, basis = self._pricing.cost(
                    model=model,
                    target_kind=kind or "",
                    prompt_tokens=est_prompt,
                    completion_tokens=est_output,
                    metered_usd=None,
                    provider_in_per_mtok=None,
                    provider_out_per_mtok=None,
                )
                priced = usd is not None and basis != CostBasis.UNPRICED
                est_usd = usd if priced else 0.0
                spends_usd = priced or kind != _LOCAL
            blocked = None
            for sc in scopes:
                blocked = self._check_rates(sc, now)
                if blocked:
                    break
            if blocked is None:
                for sc in scopes:
                    blocked = self._check_cycle(sc, kind, spends_usd, now)
                    if blocked:
                        break
            if blocked is not None:
                lim, decision = blocked
                fallback = key.cloud_fallback_local_model
                if (
                    lim.base in FALLBACK_BASES
                    and kind == _REMOTE
                    and fallback
                    and fallback != model
                ):
                    decision = replace(decision, fallback_model=fallback)
                return decision, None
            reservation = self._reserve(
                principal, model, kind, est_prompt, est_output, est_usd, now,
                route_limits=route_limits,
            )
            return LimitDecision(allowed=True, inject_max_tokens=inject), reservation

    def _reserve(
        self,
        principal: Principal,
        model: str,
        kind: str | None,
        est_prompt: int,
        est_output: int,
        est_usd: float,
        now: float,
        route_limits: dict | None = None,
    ) -> Reservation:
        key, org = principal.key, principal.org
        scopes = ((LimitScope.KEY.value, key.key_id), (LimitScope.ORG.value, org.org_id))
        estimate = _metrics(model, kind, est_prompt, est_output, est_usd)
        for sk in scopes:
            live = self._live.setdefault(sk, {})
            for metric, value in estimate.items():
                live[metric] = live.get(metric, 0.0) + value
        entry = _Entry(now, float(est_prompt + est_output))
        slots: list[str] = []
        for scope, scope_id in scopes:
            slots.append(f"concurrent:{scope}:{scope_id}")
            for wmodel in (None, model):
                self._windows.setdefault((scope, scope_id, wmodel), deque()).append(entry)
                slots.append(f"window:{scope}:{scope_id}" + (f":{wmodel}" if wmodel else ""))
        if route_limits:
            # The same entry object, so settle's token correction and _sweep
            # cover this window too. Not in `scopes`: settle pairs those with
            # the key and org cycle anchors, and a route has no cycle.
            route = LimitScope.ROUTE.value
            self._windows.setdefault((route, model, None), deque()).append(entry)
            slots.append(f"window:{route}:{model}")
        reservation = Reservation(
            reservation_id=secrets.token_hex(12),
            key_id=key.key_id,
            org_id=org.org_id,
            model=model,
            started_at=now,
            estimated_prompt=est_prompt,
            estimated_output=est_output,
            slots=tuple(slots),
        )
        self._held[reservation.reservation_id] = _Held(
            reservation=reservation,
            key_anchor=key.created_at,
            org_anchor=org.created_at,
            estimate=estimate,
            entry=entry,
            scopes=scopes,
            kind=kind,
        )
        self._sweep(now)
        return reservation

    def _sweep(self, now: float) -> None:
        """Drop windows nobody has touched for a whole window. Bounded work
        once per WINDOW_S, so a key that stops calling stops costing memory."""
        if now - self._swept_at < WINDOW_S:
            return
        self._swept_at = now
        horizon = now - WINDOW_S
        for wkey in list(self._windows):
            dq = self._windows[wkey]
            while dq and dq[0].ts <= horizon:
                dq.popleft()
            if not dq:
                del self._windows[wkey]

    def settle(
        self, reservation: Reservation, actuals: UsageActuals | None, *, now: float | None = None
    ) -> None:
        """Free the reservation and count what the request actually used.

        ``actuals=None`` means the request never ran: everything it reserved
        is given back and nothing is counted -- unless :meth:`mark_started`
        said bytes reached the client, in which case the request did run and
        its reserved estimate is what gets counted (and its window entry
        stays). Otherwise one transaction adds every metric to the key's and
        the org's counters for the cycle each is in *now*. A second settle of
        the same reservation is a no-op.

        Never raises: by the time this runs the response has been served, and
        a store failure here is logged. The next check that needs the store
        fails closed on its own.
        """
        now = self._now(now)
        with self._lock:
            held = self._held.pop(reservation.reservation_id, None)
            if held is None:
                return
            for sk in held.scopes:
                live = self._live.get(sk)
                if live is None:
                    continue
                for metric, value in held.estimate.items():
                    live[metric] = live.get(metric, 0.0) - value
                if live.get("requests", 0.0) <= 0:
                    del self._live[sk]
            if actuals is None and not held.started:
                held.entry.void = True
                return
            if actuals is None:
                # Served, but nothing said how much: the estimate it was
                # admitted on is the honest figure, and zero is not.
                deltas = dict(held.estimate)
            else:
                prompt = max(0, int(actuals.prompt_tokens or 0))
                completion = max(0, int(actuals.completion_tokens or 0))
                held.entry.tokens = float(prompt + completion)
                priced = (
                    actuals.cost_basis != CostBasis.UNPRICED and actuals.cost_usd is not None
                )
                deltas = _metrics(
                    actuals.model_served or reservation.model,
                    _kind(actuals.target_kind),
                    prompt,
                    completion,
                    float(actuals.cost_usd) if priced else None,
                )
            rows = []
            for (scope, scope_id), anchor in zip(
                held.scopes, (held.key_anchor, held.org_anchor), strict=True
            ):
                start, _ = cycle_bounds(anchor, now)
                rows.extend((scope, scope_id, start, m, v) for m, v in deltas.items())
            try:
                self._transaction([(_UPSERT_COUNTER, rows)])
            except AuthUnavailable:
                log.exception(
                    "usage for reservation %s (key %s) was not counted",
                    reservation.reservation_id,
                    reservation.key_id,
                )

    def mark_started(self, reservation: Reservation) -> None:
        """Bytes of this request's answer reached the client. From here a
        ``None`` settle counts the reservation's estimate rather than voiding
        it: an upstream produced them, and a provider billed for it."""
        with self._lock:
            held = self._held.get(reservation.reservation_id)
            if held is not None:
                held.started = True

    def check_target_kind(
        self, reservation: Reservation, target_kind: str, *, now: float | None = None
    ) -> LimitDecision:
        """Re-check the kind-specific cycle limits once the target is chosen.

        A served name with both LOCAL and REMOTE targets is admitted as REMOTE
        (the stricter assumption: ``cloud_*`` limits apply). When the router
        then picks a LOCAL target, the ``local_*`` limits are checked here,
        so neither family is skipped for a name that has both (finding L9).
        This reservation's own estimate was booked under the other kind, so
        it counts nothing against the limits checked here.
        """
        now = self._now(now)
        kind = _kind(target_kind)
        with self._lock:
            held = self._held.get(reservation.reservation_id)
            if held is None or kind is None or kind == held.kind:
                return LimitDecision(allowed=True)
            only = LOCAL_ONLY if kind == _LOCAL else CLOUD_ONLY
            key = self._keys.get_key(reservation.key_id)
            org = self._keys.get_org(reservation.org_id)
            subjects = (
                (LimitScope.KEY, reservation.key_id, held.key_anchor, key),
                (LimitScope.ORG, reservation.org_id, held.org_anchor, org),
            )
            spends_usd = True
            for scope, scope_id, anchor, subject in subjects:
                if subject is None:
                    continue
                sc = self._scope(scope, scope_id, anchor, subject.limits, reservation.model, now)
                sc.limits = [lim for lim in sc.limits if lim.base in only]
                if not sc.limits:
                    continue
                if kind == _LOCAL and any(lim.base in USD_BASES for lim in sc.limits):
                    usd, basis = self._pricing.cost(
                        model=reservation.model,
                        target_kind=kind,
                        prompt_tokens=reservation.estimated_prompt,
                        completion_tokens=reservation.estimated_output,
                        metered_usd=None,
                        provider_in_per_mtok=None,
                        provider_out_per_mtok=None,
                    )
                    spends_usd = usd is not None and basis != CostBasis.UNPRICED
                blocked = self._check_cycle(sc, kind, spends_usd, now)
                if blocked is not None:
                    return blocked[1]
            return LimitDecision(allowed=True)

    def states(
        self, scope: LimitScope, scope_id: str, *, now: float | None = None
    ) -> list[LimitState]:
        """One :class:`LimitState` per limit configured on the key or org."""
        now = self._now(now)
        scope = LimitScope(scope)
        subject = self._subject(scope, scope_id)
        if subject is None:
            return []
        with self._lock:
            sc = self._scope(scope, scope_id, subject.created_at, subject.limits, None, now)
            counters = (
                self._counters(sc) if any(lim.kind is LimitKind.CYCLE for lim in sc.limits) else {}
            )
            return [self._state(sc, lim, counters, now) for lim in sc.limits]

    def raise_for_cycle(
        self,
        scope: LimitScope,
        scope_id: str,
        limit_type: str,
        value: float,
        *,
        actor: str,
        now: float | None = None,
    ) -> LimitState:
        """Set ``limit_type``'s effective value until this cycle ends.

        The counters are untouched; the next cycle reads the configured value
        again, because the override row is keyed by this cycle's start.
        """
        now = self._now(now)
        scope = LimitScope(scope)
        parse_limit_type(limit_type)
        amount = _limit_value(value)
        subject = self._subject(scope, scope_id)
        if subject is None:
            raise ValueError(f"there is no {scope.value} {scope_id!r}")
        with self._lock:
            sc = self._scope(scope, scope_id, subject.created_at, subject.limits, None, now)
            lim = next((lim for lim in sc.limits if lim.name == limit_type), None)
            if lim is None:
                raise ValueError(
                    f"{scope.value} {scope_id} has no {limit_type} limit to raise; set one first"
                )
            self._transaction(
                [
                    (
                        "INSERT INTO overrides(scope, scope_id, limit_type, cycle_start, value, "
                        "actor, created_at) VALUES(?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(scope, scope_id, limit_type, cycle_start) DO UPDATE SET "
                        "value=excluded.value, actor=excluded.actor, created_at=excluded.created_at",
                        [(scope.value, scope_id, limit_type, sc.cycle_start, amount, actor, now)],
                    )
                ]
            )
            sc.overrides[limit_type] = amount
            counters = self._counters(sc) if lim.kind is LimitKind.CYCLE else {}
            return self._state(sc, lim, counters, now)

    def reset_cycle(
        self, scope: LimitScope, scope_id: str, *, actor: str, now: float | None = None
    ) -> None:
        """Zero this cycle's counters; the schedule and older cycles stand."""
        now = self._now(now)
        scope = LimitScope(scope)
        subject = self._subject(scope, scope_id)
        if subject is None:
            raise ValueError(f"there is no {scope.value} {scope_id!r}")
        with self._lock:
            start, _ = cycle_bounds(subject.created_at, now)
            self._transaction(
                [
                    (
                        "DELETE FROM cycle_counters WHERE scope=? AND scope_id=? AND cycle_start=?",
                        [(scope.value, scope_id, start)],
                    ),
                    (
                        "INSERT INTO cycle_resets(scope, scope_id, cycle_start, reset_at, actor) "
                        "VALUES(?, ?, ?, ?, ?)",
                        [(scope.value, scope_id, start, now, actor)],
                    ),
                ]
            )
