"""Upstream proxying.

Three rules, in order of importance:

1. Do not buffer streams. Server-sent events pass through chunk by chunk as
   they arrive. Buffering a stream to inspect it defeats the purpose of it.
2. Do not rewrite backend errors. A client debugging a vLLM error sees the
   vLLM error, with the vLLM status code.
3. Never let a provider API key escape. It is set on the upstream request and
   appears nowhere else -- not in a response, not in a log line, not in an
   exception.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from urllib.parse import urlsplit

import anyio
import httpx
from starlette.responses import Response, StreamingResponse

from control_plane.contracts import TargetKind
from control_plane.providers.errors import (
    AdapterUnsupportedError,
    MissingKeyError,
    ProviderError,
    ProviderNotAdmittingError,
    UpstreamError,
)
from control_plane.providers.kinds import spec_for
from control_plane.providers.service import RESPONSE_HEADER_ALLOWLIST
from control_plane.redaction import Redactor

from control_plane.limits.service import CHARS_PER_TOKEN
from control_plane.providers.config import USAGE_TAIL_BYTES
from control_plane.providers.usage import (
    UsageSniffer,
    audio_duration_of,
    duration_after_key,
    object_after_key,
    usage_after_key,
)

from .errors import error_response
from .settings import GatewaySettings
from .stats import StatsRegistry

log = logging.getLogger("gateway.proxy")

_SSE_TOKEN_MARKER = b"data:"
_DONE_MARKER = b"[DONE]"

# Why an attempt came back without an answer. Transport means we never reached
# the backend; a server error means we reached it and it broke.
RETRY_TRANSPORT = "transport"
RETRY_SERVER_ERROR = "http_5xx"
# The gateway ran out of upstream connections. Neither of the above: we never
# reached the backend, but that says nothing about whether the backend is there.
# Kept apart from "transport" so the breaker can ignore it and so history can
# tell an exhausted pool from a dead node after the fact.
RETRY_POOL = "pool_exhausted"

# ProviderService.open_upstream() has one contract for every failure to reach
# an upstream: raise UpstreamError, always -- never let a raw httpx.HTTPError
# escape. A transport failure is therefore not a distinct exception type on
# this path, it is an UpstreamError synthesized with this exact message and
# status 502 (control_plane/providers/service.py, open_upstream's own
# `except httpx.HTTPError` branch). A genuine answer from a reachable
# backend always carries the backend's own message -- or, absent one,
# f"HTTP {status}" -- which can never collide with this prefix, so matching
# on it is how forward_provider tells "never reached it" from "reached it
# and it answered 502" apart without providers.py needing to say so any
# other way.
class _Discarded(Exception):
    """Thrown into a provider upstream that nobody is going to read.

    `ProviderService.open_upstream` records `note_success` and the request's
    spend in the code *after* its yield. Exiting it cleanly would therefore
    bill a hedge that lost, and vouch for a transfer that never happened.
    Throwing skips both, without the connect-time paths that would mark the
    provider unreachable -- it answered fine; we simply changed our mind.
    """


class _ProviderHandle:
    """What the janitor closes for a provider attempt whose body was never
    started: the provider's own context, exited as a discard -- nothing was
    read, so nothing is billed or vouched for."""

    def __init__(self, manager) -> None:
        self._manager = manager

    async def aclose(self) -> None:
        reason = _Discarded("never read")
        try:
            await self._manager.__aexit__(type(reason), reason, None)
        except _Discarded:
            pass


_SYNTHETIC_TRANSPORT_PREFIX = "could not reach upstream: "

#: The one wrapped class that is not a verdict about the upstream. Matched by
#: name because that is all `_synthetic_transport_error_class` recovers.
_POOL_TIMEOUT_CLASS = httpx.PoolTimeout.__name__


def _synthetic_transport_error_class(exc: UpstreamError) -> str | None:
    """The wrapped exception's class name, if ``exc`` is that stand-in.

    Returns ``None`` for anything else, including a genuine 502 the backend
    answered with itself.
    """
    # A genuine backend 502 that happened to echo the prefix would still carry
    # a body; the synthesized stand-in never does.
    if (
        exc.status_code == 502
        and exc.message.startswith(_SYNTHETIC_TRANSPORT_PREFIX)
        and not exc.body
    ):
        return exc.message[len(_SYNTHETIC_TRANSPORT_PREFIX) :] or "UpstreamError"
    return None


@dataclass
class Attempt:
    """What one target did with a request.

    Either it produced a response to hand the client, or it produced a reason
    to offer the request to the next target. A retryable attempt still carries
    whatever the upstream managed to say, so the last attempt in an exhausted
    chain can be replayed verbatim rather than rewritten into a gateway error
    the client cannot act on.
    """

    response: Response | None = None
    retryable: bool = False
    reason: str = ""
    target_id: str = ""
    # Set for RETRY_SERVER_ERROR: the backend's own answer, held for replay.
    status: int | None = None
    headers: dict[str, str] | None = None
    body: bytes | None = None
    # Set for RETRY_TRANSPORT: the exception class name, and nothing more.
    # A transport failure has no backend words to preserve.
    error: str = ""
    #: Throw this attempt away without ever streaming it. Set only when the
    #: attempt carries a live upstream, and it is what makes a hedge safe:
    #: the loser's body generator is never started, so its `finally` -- the
    #: thing that settles accounting, releases the KV commitment and returns
    #: the connection -- would never run. Discarding does all three by hand.
    #: Without it, every hedged request leaks exactly what the 2026-09-08
    #: outage leaked.
    discard: object = None


@dataclass
class _Usage:
    """What we learned about a response as it went past.

    ``tokens`` is what drives the strength score and has always been an
    estimate for streams. ``prompt_tokens``/``completion_tokens`` come from the
    upstream's own usage block when it sends one, and ``estimated`` says which
    of the two you are looking at -- a record that cannot tell a counted token
    from a guessed one is not worth keeping for a month.
    """

    tokens: int = 0
    ttft_s: float | None = None
    decode_s: float | None = None
    # None until the upstream's own usage block says: the record then falls
    # back to the request's admission estimate rather than recording zero.
    prompt_tokens: int | None = None
    completion_tokens: int = 0
    estimated: bool = True
    # Read off the response as it passed; None when it did not say. A missing
    # count is recorded as missing, never as zero.
    finish_reason: str | None = None
    reasoning_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    # Per-token timing, streams only: first token to last, the mean and p95
    # gap between tokens, and how long the stream ran on after its last token.
    generation_s: float | None = None
    tpot_mean_ms: float | None = None
    tpot_p95_ms: float | None = None
    tail_ms: float | None = None
    # A transcription's own `duration` (seconds), read off an audio
    # response's small JSON body; priced per minute when the provider model
    # carries a rate (K5r). None for every other body.
    audio_duration_s: float | None = None
    # What the upstream said it charged, when its kind meters cost in dollars
    # (OpenRouter's `usage.cost`): the record's cost, METERED, over tokens x
    # list price, which misses what a request's extras (web search) cost.
    cost_usd: float | None = None


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _usage_details(usage: dict) -> dict[str, int | None]:
    """Reasoning and cache counts out of a usage block, whoever wrote it.

    OpenAI and vLLM: ``prompt_tokens_details.cached_tokens`` and
    ``completion_tokens_details.reasoning_tokens``. Anthropic:
    ``cache_read_input_tokens`` / ``cache_creation_input_tokens``.
    OpenRouter: ``native_tokens_cached``.
    """
    prompt_details = usage.get("prompt_tokens_details")
    completion_details = usage.get("completion_tokens_details")
    cache_read = None
    if isinstance(prompt_details, dict):
        cache_read = _int_or_none(prompt_details.get("cached_tokens"))
    if cache_read is None:
        cache_read = _int_or_none(usage.get("cache_read_input_tokens"))
    if cache_read is None:
        cache_read = _int_or_none(usage.get("native_tokens_cached"))
    reasoning = None
    if isinstance(completion_details, dict):
        reasoning = _int_or_none(completion_details.get("reasoning_tokens"))
    return {
        "reasoning_tokens": reasoning,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": _int_or_none(usage.get("cache_creation_input_tokens")),
    }


class _DetailSniffer(UsageSniffer):
    """The usage sniffer, also reading what the token counts leave out: the
    finish reason, and the reasoning and cache counts beside them. Parses the
    same bounded buffer, once, at the end -- never a byte more is held."""

    def _objects(self) -> list[dict]:
        if self._overflowed or not self._buffer:
            return []
        text = bytes(self._buffer).decode("utf-8", "replace")
        if not self._stream:
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                return []
            return [obj] if isinstance(obj, dict) else []
        lines = text.split("\n")
        if self._truncated and len(lines) > 1:
            lines = lines[1:]
        out = []
        for line in lines:
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
        return out

    def details(self) -> dict:
        found: dict = {}
        for obj in self._objects():
            usage = obj.get("usage")
            if isinstance(usage, dict):
                found.update(_usage_details(usage))
            choices = obj.get("choices")
            if isinstance(choices, list):
                for choice in choices:
                    if isinstance(choice, dict) and isinstance(choice.get("finish_reason"), str):
                        found["finish_reason"] = choice["finish_reason"]
            elif isinstance(obj.get("stop_reason"), str):
                found["finish_reason"] = obj["stop_reason"]
        return found


def _apply_sniffed(usage: _Usage, sniffer: "UsageSniffer") -> None:
    """Prefer the upstream's own count over ours, and say that we did."""
    try:
        sniffed = sniffer.result()
    except Exception:  # a malformed tail is not worth a failed request
        sniffed = None
    try:
        details = sniffer.details() if isinstance(sniffer, _DetailSniffer) else {}
    except Exception:
        details = {}
    for name, value in details.items():
        setattr(usage, name, value)
    if sniffed is None:
        usage.completion_tokens = usage.tokens
        return
    usage.prompt_tokens = sniffed.input_tokens
    usage.completion_tokens = sniffed.output_tokens
    usage.estimated = False
    usage.cost_usd = sniffed.cost_usd
    if sniffed.output_tokens:
        usage.tokens = sniffed.output_tokens


def _weighted_p95(gaps: list[tuple[float, int]]) -> float:
    """The 95th percentile per-token gap, each chunk's gap split evenly over
    the tokens it carried."""
    per_token = sorted((gap / n, n) for gap, n in gaps)
    total = sum(n for _, n in per_token)
    threshold = 0.95 * total
    seen = 0
    for value, n in per_token:
        seen += n
        if seen >= threshold:
            return value
    return per_token[-1][0]


class _StreamAccounting:
    """Counts tokens in a passing SSE stream without holding it up.

    Approximate by construction: one ``data:`` frame is one token for a normal
    chat completion stream. Good enough to drive a strength score, and it costs
    a substring scan per chunk rather than a parse.
    """

    def __init__(
        self,
        started: float,
        on_tokens: Callable[[int], None] | None = None,
        *,
        metered: bool = False,
    ) -> None:
        self.started = started
        self.usage = _Usage()
        # Tokens already handed to ``on_tokens`` as they streamed, so the
        # settle step can add only what the final count says on top.
        self.live = 0
        self._on_tokens = on_tokens
        self._tail = b""
        self._first_token_at: float | None = None
        self._last_token_at: float | None = None
        # (seconds since the previous token-bearing chunk, tokens in this one)
        self._gaps: list[tuple[float, int]] = []
        # The frame count keeps driving routing exactly as before. This reads
        # the real usage block out of the stream's tail in parallel, using the
        # implementation the provider path already trusts for spend.
        self._sniffer = _DetailSniffer(stream=True, metered=metered)

    def note_chunk(self, chunk: bytes, now: float) -> None:
        if self.usage.ttft_s is None:
            self.usage.ttft_s = now - self.started
        self._sniffer.feed(chunk)
        # Carry a few bytes across the boundary so a marker split between two
        # chunks is still counted exactly once.
        window = self._tail + chunk
        count = window.count(_SSE_TOKEN_MARKER) - window.count(_DONE_MARKER)
        if count > 0:
            if self._first_token_at is None:
                self._first_token_at = now
            else:
                self._gaps.append((now - self._last_token_at, count))
            self._last_token_at = now
            self.usage.tokens += count
            if self._on_tokens is not None:
                # Live throughput only; never allowed to break the stream.
                try:
                    self._on_tokens(count)
                    self.live += count
                except Exception:
                    log.debug("live token count failed", exc_info=True)
        self._tail = window[-len(_SSE_TOKEN_MARKER) :]

    #: A stream's hang-up stops the generation, so what was counted is what
    #: was served; nothing past it is worth reading.
    wants_rest = False

    def finish(self, now: float) -> _Usage:
        usage = self.usage
        if self._first_token_at is not None:
            usage.decode_s = max(0.0, now - self._first_token_at)
            usage.generation_s = max(0.0, self._last_token_at - self._first_token_at)
            usage.tail_ms = max(0.0, now - self._last_token_at) * 1000.0
        if self._gaps:
            tokens = sum(n for _, n in self._gaps)
            usage.tpot_mean_ms = sum(g for g, _ in self._gaps) / tokens * 1000.0
            usage.tpot_p95_ms = _weighted_p95(self._gaps) * 1000.0
        _apply_sniffed(usage, self._sniffer)
        return usage


class _BodyTail:
    """The last ``USAGE_TAIL_BYTES`` of a body, for when the head we keep is
    not the whole of it: a large body's ``usage`` (after ``choices``) and a
    large transcription's ``duration`` live in the part the head dropped."""

    def __init__(self) -> None:
        self.bytes = bytearray()

    def feed(self, chunk: bytes) -> None:
        self.bytes.extend(chunk[-USAGE_TAIL_BYTES:])
        if len(self.bytes) > USAGE_TAIL_BYTES:
            del self.bytes[: len(self.bytes) - USAGE_TAIL_BYTES]


class _BodyAccounting:
    """Non-streaming responses carry a usage block; read it from a bounded
    copy while the bytes are already on their way to the client."""

    LIMIT = 1024 * 1024
    #: An unstreamed answer exists in full before its first byte, so a client
    #: that hangs up was served all of it, and its usage block is LAST.
    wants_rest = True

    def __init__(self, started: float, *, metered: bool = False) -> None:
        self.started = started
        self.usage = _Usage()
        self._metered = metered
        self._sniffer = _DetailSniffer(stream=False, metered=metered)
        self._seen = 0
        self._total = 0
        self._tail = _BodyTail()

    def note_chunk(self, chunk: bytes, now: float) -> None:
        if self.usage.ttft_s is None:
            self.usage.ttft_s = now - self.started
        if self._seen < self.LIMIT:
            room = self.LIMIT - self._seen
            self._sniffer.feed(chunk[:room])
            self._seen += min(len(chunk), room)
        self._total += len(chunk)
        self._tail.feed(chunk)

    def finish(self, now: float) -> _Usage:
        _apply_sniffed(self.usage, self._sniffer)
        if self.usage.estimated and self._total > self._seen:
            # The head was cut, so its `usage` (if any) was not in it.
            tail = bytes(self._tail.bytes)
            sniffed = usage_after_key(tail, metered=self._metered)
            if sniffed is not None:
                block = object_after_key(tail, "usage")
                for name, value in _usage_details(block).items():
                    setattr(self.usage, name, value)
                self.usage.prompt_tokens = sniffed.input_tokens
                self.usage.completion_tokens = sniffed.output_tokens
                self.usage.estimated = False
                self.usage.cost_usd = sniffed.cost_usd
                if sniffed.output_tokens:
                    self.usage.tokens = sniffed.output_tokens
        if self.usage.estimated and not self.usage.tokens:
            # No usage block anywhere: a body has no frames to count, so
            # without this the answer would be settled as zero output while
            # the upstream bills for it. Its size is the estimate we have;
            # `estimated` stays True so the row says it is one.
            self.usage.tokens = math.ceil(self._total / max(1.0, CHARS_PER_TOKEN))
            self.usage.completion_tokens = self.usage.tokens
        return self.usage


class _NoAccounting:
    """For a body that carries no tokens to count.

    TTFT is still real -- it is when the client could first have seen a byte,
    whatever those bytes are -- so it is kept. Everything else stays None,
    which is what settle() records as "not measured" rather than as zero --
    except a transcription's JSON ``duration``, read from a small bounded copy
    of the body's head and tail when it starts as JSON (never an MP3), for
    per-minute pricing.
    """

    LIMIT = 64 * 1024

    def __init__(self, started: float) -> None:
        self.started = started
        self.usage = _Usage()
        self._head = bytearray()
        self._tail = _BodyTail()
        self._json: bool | None = None

    def note_chunk(self, chunk: bytes, now: float) -> None:
        if self.usage.ttft_s is None:
            self.usage.ttft_s = now - self.started
        if self._json is None and chunk.strip():
            self._json = chunk.lstrip()[:1] == b"{"
        if not self._json:
            return
        if len(self._head) <= self.LIMIT:
            self._head.extend(chunk[: self.LIMIT + 1 - len(self._head)])
        self._tail.feed(chunk)

    @property
    def wants_rest(self) -> bool:
        """A JSON transcription carries its duration, possibly last; audio
        bytes carry nothing, and a speech stream may still be generating."""
        return bool(self._json)

    def finish(self, now: float) -> _Usage:
        if not self._json:
            return self.usage
        if len(self._head) <= self.LIMIT:
            self.usage.audio_duration_s = audio_duration_of(bytes(self._head))
        else:
            # verbose_json puts `duration` before `segments`, so the head has
            # it; the tail is the fallback for a server that orders it last.
            self.usage.audio_duration_s = duration_after_key(
                bytes(self._head)
            ) or duration_after_key(bytes(self._tail.bytes))
        return self.usage


#: The only client headers any upstream is sent (finding L11). Everything else
#: -- the client's own ``authorization`` and ``cookie``, ``openai-organization``
#: naming somebody else's account, an ``x-forwarded-for`` the client chose --
#: describes the client's relationship with derate, not derate's with the
#: upstream. A REMOTE target gets the provider's key instead; a LOCAL one its
#: runtime key. ``x-request-id`` is not the client's to choose either: the
#: upstream is sent derate's trace id, so an engine's log joins the
#: ``requests`` row and a client cannot forge the id it records (K10).
_FORWARDED_REQUEST_HEADERS = frozenset(
    {"content-type", "accept", "user-agent", "openai-beta"}
)


def _forward_request_headers(incoming: dict[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in incoming.items()
        if key.lower() in _FORWARDED_REQUEST_HEADERS
    }


def _trace_record(
    trace,
    usage: _Usage | None,
    *,
    selection,
    attempt_no: int,
    retry_reason: str,
    status: int | None,
    error_class: str,
    duration_s: float,
    error_message: str | None = None,
    retry_after_s: float | None = None,
):
    """One attempt's row. Shared by both forward paths so neither drifts."""
    rate = getattr(getattr(selection, "model", None), "price_per_minute_usd", None)
    seconds = None if usage is None else usage.audio_duration_s
    record = trace.record(
        selection=selection,
        attempt_no=attempt_no,
        retry_reason=retry_reason,
        status=status,
        error_class=error_class,
        tokens=0 if usage is None else usage.tokens,
        completion_tokens=None if usage is None else usage.completion_tokens,
        prompt_tokens=None if usage is None else usage.prompt_tokens,
        tokens_estimated=True if usage is None else usage.estimated,
        ttft_s=None if usage is None else usage.ttft_s,
        decode_s=None if usage is None else usage.decode_s,
        duration_s=duration_s,
        finish_reason=None if usage is None else usage.finish_reason,
        cancelled=error_class == "ClientDisconnected",
        generation_s=None if usage is None else usage.generation_s,
        tpot_mean_ms=None if usage is None else usage.tpot_mean_ms,
        tpot_p95_ms=None if usage is None else usage.tpot_p95_ms,
        tail_ms=None if usage is None else usage.tail_ms,
        reasoning_tokens=None if usage is None else usage.reasoning_tokens,
        cache_read_tokens=None if usage is None else usage.cache_read_tokens,
        cache_write_tokens=None if usage is None else usage.cache_write_tokens,
        # Metered for audio when a per-minute rate exists; else what the
        # upstream said it charged, when its kind meters cost; else None, and
        # the request is priced from tokens or settles unpriced (K5r).
        cost_usd=(
            rate * seconds / 60.0 if rate is not None and seconds is not None
            else None if usage is None else usage.cost_usd
        ),
    )
    if error_message is not None:
        record = replace(record, attempt_chain=[{
            "target_id": record.target_id,
            "status": record.status,
            "error_class": record.error_class or None,
            "retry_reason": record.retry_reason or None,
            "ms": record.duration_ms,
            "error_message": error_message,
            "retry_after_s": retry_after_s,
        }])
    return record


def _meters_cost(selection) -> bool:
    """Whether this provider's `usage.cost` is dollars (OpenRouter's is).
    Off for every other kind: a stray `cost` in an unknown unit must not
    become what a key is charged."""
    kind = getattr(getattr(selection, "provider", None), "kind", None)
    if kind is None:
        return False
    try:
        return spec_for(kind).meters_cost
    except (KeyError, ValueError):
        return False


#: What a client is shown of an upstream's response headers: the provider
#: path's allowlist (finding K6), plus ``content-encoding`` because this path
#: relays ``aiter_raw()`` bytes and the header has to travel with them.
#: ``x-request-id`` is derate's own trace id, set below, never the upstream's.
_RESPONSE_PASS = RESPONSE_HEADER_ALLOWLIST | {"content-encoding"}


def _forward_response_headers(
    headers: httpx.Headers, request_id: str | None = None
) -> dict[str, str]:
    out = {k: v for k, v in headers.items() if k.lower() in _RESPONSE_PASS}
    if request_id:
        out["x-request-id"] = request_id
    return out


def _scrub_upstream_body(body: bytes, api_key: str | None) -> bytes:
    """Never let *api_key* -- or anything shaped like one -- reach a client.

    ``forward()`` attaches a raw resolved key to the outgoing request (rule 3
    up top: never let a provider API key escape). ``ProviderService
    .open_upstream`` covers the same risk with a ``Redactor`` that has
    remembered every key it has ever resolved; this path only ever knows the
    one key it just used, so a ``Redactor`` scoped to this single call is
    exactly as much as there is to scrub.
    """
    redactor = Redactor()
    redactor.remember(api_key)
    return redactor.scrub_bytes(body)


#: How long an upstream response may sit with its body generator never started
#: before the janitor closes it. Starlette begins iterating a StreamingResponse
#: immediately after it sends the response line, so a generator that has not
#: started within this long is never going to start. Deliberately not a read
#: timeout: a *running* stream is never touched however long it takes, which is
#: the promise `upstream_read_timeout_s = None` makes.
_UNSTARTED_GRACE_S = 30.0


@dataclass
class _OpenUpstream:
    """One upstream response this proxy opened and has not yet closed.

    ``started`` flips the first time the body generator yields a chunk. It is
    the whole point of the record: a generator that started owns its own
    cleanup, and one that never started has no ``finally`` to run.
    """

    response: object
    opened_at: float
    started: bool = False
    #: The first-chunk peek, when it outlived the header hold and was handed to
    #: the generator to finish awaiting. If the generator never runs, nobody
    #: ever awaits it: it keeps the response alive and, left alone, retires with
    #: an exception nothing retrieves. Reaping cancels it first.
    pending: object = None
    #: Settles the attempt as abandoned: the janitor calls it, because a
    #: generator that never started never reaches the `finally` that would
    #: release its KV commitment and the request's limit reservation.
    abandon: object = None


def _origin_of(url: str) -> str:
    """`http://host:8101/v1/chat/completions` -> `http://host:8101`.

    A connection pool is per origin, so that is the key. Falls back to the whole
    URL rather than guessing when it does not parse, which costs an extra client
    and never mixes two origins into one pool.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.scheme or not parts.netloc:
        return url
    return f"{parts.scheme}://{parts.netloc}"


async def close_quietly(response) -> None:
    """Close an upstream response even when the surrounding scope is cancelled.

    Starlette cancels the scope a ``StreamingResponse`` body runs in the moment
    the client hangs up, and an anyio cancel scope is *level*-triggered: every
    subsequent ``await`` inside it raises ``CancelledError`` at once. So an
    unshielded ``await response.aclose()`` in a ``finally`` never actually runs.
    httpx never gets the connection back, and the socket stays in CLOSE-WAIT for
    the life of the process -- 256 of those is one exhausted pool and a gateway
    that cannot reach *any* backend, including the ones that never leaked.

    ``asyncio.shield`` does not help here: the cancellation comes from the anyio
    scope, not from ``Task.cancel``, so the shielded await is re-cancelled at its
    first checkpoint just the same.

    Never raises. This runs on cleanup paths that are already carrying an
    exception worth more than this one.
    """
    with anyio.CancelScope(shield=True):
        try:
            await response.aclose()
        except Exception:
            log.debug("could not close an upstream response", exc_info=True)


#: How long, and how far, a hang-up on an unstreamed body keeps reading it for
#: the usage block at its end. The answer is already generated -- this is the
#: network, not the engine -- and without it a key that reads one chunk of a
#: 150k-token answer is charged that chunk's size / 4.
USAGE_DRAIN_S = 10.0
USAGE_DRAIN_MAX_BYTES = 32 * 1024 * 1024
#: How long one shielded read of an unstreamed body may take (`_next_chunk`).
#: Generous on purpose: some providers send keep-alive whitespace inside a
#: non-streamed body while still generating, and a client that is still there
#: must not be cut off. It only bounds a read nobody can cancel any more.
SHIELDED_READ_S = 300.0


async def _drain_for_usage(chunks, accounting) -> None:
    """Feed the rest of an abandoned unstreamed body to *accounting*.

    Shielded, because the client's hang-up has usually cancelled the scope
    this runs in (see `close_quietly`), and bounded by `USAGE_DRAIN_S` and
    `USAGE_DRAIN_MAX_BYTES`. Never raises: an upstream that cannot be read
    any further leaves the estimate from what was."""
    read = 0
    with anyio.CancelScope(shield=True, deadline=anyio.current_time() + USAGE_DRAIN_S):
        try:
            async for chunk in chunks:
                accounting.note_chunk(chunk, time.monotonic())
                read += len(chunk)
                if read >= USAGE_DRAIN_MAX_BYTES:
                    break
        except Exception:
            log.debug("could not read the rest of an abandoned body", exc_info=True)


async def _next_chunk(chunks, accounting) -> bytes | None:
    """The next upstream chunk, or None at the end.

    For a body whose rest is worth reading (`wants_rest`) the read is
    shielded: a client's hang-up otherwise cancels it mid-read, which finishes
    the upstream iterator, and `_drain_for_usage` then has nothing left to
    read. The checkpoint first is what delivers the hang-up: anyio retries a
    cancellation once on leaving a shielded scope, and entering the next
    shield hid it for good -- the send after a disconnect does not suspend,
    so without it the loop read an endless upstream for ever. A read stalled
    past `SHIELDED_READ_S` is a broken upstream."""
    if not accounting.wants_rest:
        return await anext(chunks, None)
    await anyio.lowlevel.checkpoint_if_cancelled()
    with anyio.CancelScope(shield=True, deadline=anyio.current_time() + SHIELDED_READ_S) as scope:
        chunk = await anext(chunks, None)
    if scope.cancelled_caught:
        raise httpx.ReadTimeout("upstream stalled mid-body")
    return chunk


async def _read_bounded(response: httpx.Response, limit: int = _BodyAccounting.LIMIT) -> bytes:
    """Drain a server error body so it can be replayed later, capped.

    An error body is small. Reading one costs a target its place in the chain
    but keeps its words, which is what lets an exhausted chain answer with the
    backend's own error instead of a gateway error the client cannot act on.
    """
    buf = bytearray()
    try:
        async for chunk in response.aiter_raw():
            if len(buf) >= limit:
                break
            buf.extend(chunk[: limit - len(buf)])
    except httpx.HTTPError:
        pass  # a truncated error body still says more than none
    finally:
        await close_quietly(response)
    return bytes(buf)


class UpstreamProxy:
    def __init__(
        self,
        settings: GatewaySettings,
        client: httpx.AsyncClient | None = None,
        breaker=None,
        sink=None,
    ):
        from control_plane.telemetry import NULL_SINK

        self._settings = settings
        self._client = client
        self._owns_client = client is None
        self._breaker = breaker
        self._sink = sink or NULL_SINK
        # origin -> (client, last used). One pool per origin, so a backend that
        # exhausts its slots cannot take the others with it.
        self._clients: dict[str, tuple[httpx.AsyncClient, float]] = {}
        # Every upstream response this proxy has opened and not yet closed.
        # `close_quietly` covers the response whose body generator ran and was
        # cancelled; this covers the one whose generator was never started at
        # all, which has no `finally` to run. See `_reap`.
        self._open: dict[int, _OpenUpstream] = {}
        self._janitor: asyncio.Task | None = None

    async def start(self) -> None:
        if self._janitor is None:
            self._janitor = asyncio.create_task(self._janitor_loop())

    async def stop(self) -> None:
        if self._janitor is not None:
            self._janitor.cancel()
            try:
                await self._janitor
            except asyncio.CancelledError:
                pass
            self._janitor = None
        for entry in list(self._open.values()):
            await close_quietly(entry.response)
        self._open.clear()
        for client, _ in self._clients.values():
            await client.aclose()
        self._clients.clear()
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        if self._owns_client:
            self._client = None

    # -- clients -----------------------------------------------------------

    def client_for(self, url: str) -> httpx.AsyncClient:
        """The client for this URL's origin, created on first use.

        An injected client (tests, and anything that wants one pool) is always
        used as-is: the caller has said which pool it wants.
        """
        if self._client is not None:
            return self._client
        origin = _origin_of(url)
        existing = self._clients.get(origin)
        now = time.monotonic()
        if existing is not None:
            self._clients[origin] = (existing[0], now)
            return existing[0]
        timeout = httpx.Timeout(
            connect=self._settings.upstream_connect_timeout_s,
            read=self._settings.upstream_read_timeout_s,
            write=self._settings.upstream_connect_timeout_s,
            pool=self._settings.upstream_connect_timeout_s,
        )
        limits = httpx.Limits(
            max_connections=self._settings.upstream_per_origin_limit,
            max_keepalive_connections=self._settings.upstream_per_origin_limit,
        )
        # identity: an upstream error body is scrubbed for key material before
        # a client sees it, and a scrubber cannot read gzip (finding K4).
        client = httpx.AsyncClient(
            timeout=timeout, limits=limits, headers={"accept-encoding": "identity"}
        )
        self._clients[origin] = (client, now)
        return client

    @property
    def client(self) -> httpx.AsyncClient:
        """The injected client, for callers that still expect exactly one.

        Kept because ``forward_provider``'s upstream is opened by
        ``ProviderService`` with its own client; only the direct path routes
        through ``client_for``.
        """
        if self._client is None:
            raise RuntimeError("UpstreamProxy has no single client; use client_for()")
        return self._client

    # -- open upstream responses -------------------------------------------

    def _track(self, response) -> _OpenUpstream:
        entry = _OpenUpstream(response=response, opened_at=time.monotonic())
        self._open[id(response)] = entry
        return entry

    def _release(self, response) -> None:
        self._open.pop(id(response), None)

    async def _janitor_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._settings.upstream_janitor_interval_s)
                await self._reap()
            except asyncio.CancelledError:
                raise
            except Exception:  # a janitor that dies stops reclaiming anything
                log.exception("upstream janitor failed")

    async def _reap(self) -> None:
        """Close what nothing else will, and drop clients nobody is using.

        Only responses whose body generator was *never started* are closed. A
        running stream is left alone however long it takes -- reaping on age
        would be a read timeout by the back door, and `upstream_read_timeout_s`
        is None on purpose. Starlette starts iterating a StreamingResponse right
        after it sends the response line, so a generator still unstarted after
        `_UNSTARTED_GRACE_S` never will be: its client went away between the
        headers and the first chunk.
        """
        now = time.monotonic()
        for key, entry in list(self._open.items()):
            if entry.started or now - entry.opened_at < _UNSTARTED_GRACE_S:
                continue
            self._open.pop(key, None)
            log.warning(
                "closing an upstream response whose body was never read (%.0fs)",
                now - entry.opened_at,
            )
            if entry.pending is not None:
                entry.pending.cancel()
            if entry.abandon is not None:
                try:
                    entry.abandon()
                except Exception:
                    log.exception("could not settle a reaped upstream response")
            await close_quietly(entry.response)

        idle = self._settings.upstream_client_idle_s
        for origin, (client, last_used) in list(self._clients.items()):
            if now - last_used < idle:
                continue
            self._clients.pop(origin, None)
            try:
                await client.aclose()
            except Exception:
                log.debug("could not close the client for %s", origin, exc_info=True)

    # -- circuit breaker ---------------------------------------------------
    #
    # Only the transport verdict is reported. A backend that answers 500 is
    # reachable, and benching it for saying so would let one malformed request
    # take every replica of a model out of rotation at once.

    def _note_transport_failure(self, target_id: str) -> None:
        if self._breaker is not None:
            self._breaker.record_transport_failure(target_id)

    def _abandon_probe(self, target_id: str) -> None:
        """Give a claimed half-open probe back with NO transport verdict.

        For exits that never sent anything (misconfiguration: unresolvable
        key, unsupported adapter). Recording success would close a circuit
        the network never vouched for; recording failure would bench a
        target for a config fact. Without this, ``circuit.probing`` stays
        True forever and the target becomes permanently unselectable.
        """
        if self._breaker is not None:
            self._breaker.abandon(target_id)

    def _note_transport_success(self, target_id: str) -> None:
        if self._breaker is not None:
            self._breaker.record_success(target_id)

    def _emit(self, trace, usage, on_record, *, failed: bool, selection, **fields) -> None:
        """Write one attempt's row, after ``on_record`` has had its say.

        ``on_record(record, failed=, selection=)`` is the caller's hook: it
        returns the row to write (with the attempt chain, cost and basis
        filled in) and is where the request's limits are settled. A hook that
        fails still leaves the row written as it stood.
        """
        try:
            record = _trace_record(trace, usage, selection=selection, **fields)
        except Exception:
            log.debug("could not record request telemetry", exc_info=True)
            return
        if on_record is not None:
            try:
                record = on_record(record, failed=failed or usage is None, selection=selection)
            except Exception:
                log.exception("request accounting failed for %s", trace.request_id)
        try:
            self._sink.request(record)
        except Exception:
            log.debug("could not record request telemetry", exc_info=True)

    async def forward(
        self,
        *,
        selection,
        path: str,
        body: dict | None,
        client_headers: dict[str, str],
        api_key: str | None,
        stats: StatsRegistry,
        streaming: bool,
        on_finish=None,
        trace=None,
        attempt_no: int = 0,
        content: bytes | None = None,
        content_type: str | None = None,
        count_tokens: bool = True,
        reopen=None,
        on_record=None,
    ) -> Attempt:
        """One target's turn. Returns an :class:`Attempt`, not a response.

        The caller decides whether a retryable attempt is worth handing to
        another target; this method only reports what happened.

        ``reopen`` is what keeps a *committed* response recoverable. Headers go
        out once the hold budget expires (see below), and before this existed
        that also ended the request's protection -- the comment there said the
        attempt "stops being retryable from here". But at that moment the client
        has a status line and **zero body bytes**, so nothing it has seen would
        be contradicted by streaming a different target's body. When the awaited
        callable is supplied and the upstream dies with nothing yielded yet, the
        generator asks for a replacement and carries on; the client sees one
        uninterrupted response and never learns.

        Protection therefore tracks bytes, not a clock. That matters because
        the clock got it backwards: `upstream_header_hold_s` is 10s, so on this
        cluster 130,815 requests crossed it -- every one of them the 30B, whose
        prefill is 76-130s, and none of them the 0.5B, which answers in 5s. The
        fast model was always protected and the slow one never was.
        """
        target = selection.target
        url = target.backend_url.rstrip("/") + path
        headers = _forward_request_headers(client_headers)
        # A multipart upload is forwarded byte for byte under the client's own
        # content-type, boundary included -- re-encoding it would mean parsing
        # and rebuilding an audio file to change nothing about it.
        headers["content-type"] = content_type or "application/json"
        # Uncompressed, on an injected client too: an error body is scrubbed
        # before it is relayed, and gzip would hide a key from the scrubber.
        headers["accept-encoding"] = "identity"
        rid = trace.request_id if trace is not None else None
        if rid:
            headers["x-request-id"] = rid

        # Only a REMOTE target is sent a credential, and it is the provider's
        # own. The client's key is derate's, and a LOCAL backend is never
        # handed it: _forward_request_headers has already dropped it.
        if target.kind is TargetKind.REMOTE and api_key:
            headers["authorization"] = f"Bearer {api_key}"
        elif target.kind is TargetKind.LOCAL and target.runtime_api_key:
            # H3: the engine requires its per-deployment key; the gateway is
            # its only holder.
            headers["authorization"] = f"Bearer {target.runtime_api_key}"

        target_stats = stats.get(target.target_id)
        target_stats.begin()
        started = time.monotonic()
        # Token accounting reads SSE frames or a JSON usage block; an audio
        # body is neither. Sniffing one would buffer a megabyte of MP3 to
        # learn nothing, and the frame count it produced would be fiction.
        if not count_tokens:
            accounting = _NoAccounting(started)
        elif streaming:
            accounting = _StreamAccounting(started, on_tokens=target_stats.note_tokens)
        else:
            accounting = _BodyAccounting(started)
        finished = False

        def settle(
            usage: _Usage | None,
            failed: bool,
            *,
            status: int | None = None,
            retry_reason: str = "",
            error_class: str = "",
            truncated: bool = False,
        ) -> None:
            nonlocal finished
            if finished:
                return
            finished = True
            duration = time.monotonic() - started
            # A stream cut short is served (and counted against the key) but
            # says nothing about how fast this target is, so it never feeds
            # the strength EWMA.
            if failed or usage is None or truncated:
                target_stats.fail()
            else:
                target_stats.complete(
                    tokens=usage.tokens,
                    duration_s=duration,
                    decode_s=usage.decode_s,
                    ttft_s=usage.ttft_s,
                    already_counted=getattr(accounting, "live", 0),
                )
            # The numbers above are folded into an EWMA and are unrecoverable
            # from here on. This is the one moment they exist as themselves.
            if trace is not None:
                self._emit(
                    trace, usage, on_record, failed=failed, selection=selection,
                    attempt_no=attempt_no, retry_reason=retry_reason, status=status,
                    error_class=error_class, duration_s=duration,
                )
            if on_finish is not None:
                on_finish()

        client = self.client_for(url)
        if content is not None:
            request = client.build_request(
                "POST", url, content=content, headers=headers
            )
        else:
            request = client.build_request("POST", url, json=body, headers=headers)
        try:
            response = await client.send(request, stream=True)
        except httpx.PoolTimeout as exc:
            # Our own pool would not hand out a slot. That is a fact about this
            # gateway, not about the backend -- which may well be answering in
            # under a millisecond -- so the breaker must not hear about it. It
            # benched two healthy targets on 2026-09-08 and then re-benched them
            # forever, because the half-open probe died on the same empty pool.
            settle(
                None,
                failed=True,
                retry_reason=RETRY_POOL,
                error_class=type(exc).__name__,
            )
            self._abandon_probe(target.target_id)
            log.warning("no upstream connection available for %s", url)
            return Attempt(
                retryable=True,
                reason=RETRY_POOL,
                target_id=target.target_id,
                error=type(exc).__name__,
            )
        except httpx.HTTPError as exc:
            settle(
                None,
                failed=True,
                retry_reason=RETRY_TRANSPORT,
                error_class=type(exc).__name__,
            )
            self._note_transport_failure(target.target_id)
            # There is no backend error to preserve here: we never reached it.
            log.warning("upstream unreachable: %s (%s)", url, type(exc).__name__)
            return Attempt(
                retryable=True,
                reason=RETRY_TRANSPORT,
                target_id=target.target_id,
                error=type(exc).__name__,
            )
        except BaseException:
            # Cancellation, in the window between admission committing this
            # request's KV and there being a response to track. Nothing else
            # would ever settle it. httpx failures are handled above, so this
            # only ever sees a caller changing its mind -- a losing hedge.
            settle(None, failed=True, error_class="Discarded")
            raise

        tracked = self._track(response)
        tracked.abandon = lambda: settle(None, failed=True, error_class="Reaped")

        # It answered, so the transport is fine whatever it went on to say.
        self._note_transport_success(target.target_id)

        if response.status_code >= 500:
            captured = _scrub_upstream_body(await _read_bounded(response), api_key)
            self._release(response)
            settle(
                None,
                failed=True,
                status=response.status_code,
                retry_reason=RETRY_SERVER_ERROR,
            )
            log.warning(
                "upstream %s answered %s; offering the request to another target",
                target.target_id,
                response.status_code,
            )
            return Attempt(
                retryable=True,
                reason=RETRY_SERVER_ERROR,
                target_id=target.target_id,
                status=response.status_code,
                headers=_forward_response_headers(response.headers, rid),
                body=captured,
            )

        if response.status_code >= 400:
            # Not retryable -- the client's own request was wrong, not this
            # target -- and small enough to buffer without trading away rule
            # 1's "do not buffer streams", which exists for a long-lived
            # completion or audio body, not a JSON error object. This is the
            # shape rule 3 actually has to catch: a 401 that echoes "Invalid
            # API key sk-..." would otherwise stream straight through
            # unredacted, since only >=500 was ever captured before now.
            captured = _scrub_upstream_body(await _read_bounded(response), api_key)
            self._release(response)
            settle(
                accounting.finish(time.monotonic()),
                failed=False,
                status=response.status_code,
            )
            return Attempt(
                response=Response(
                    content=captured,
                    status_code=response.status_code,
                    headers=_forward_response_headers(response.headers, rid),
                ),
                target_id=target.target_id,
            )

        # Hold the response line until the first body chunk lands, so a node
        # that dies during prefill is still someone else's to answer. One chunk
        # is held, never the stream, so rule 1 is intact: the client's first
        # byte arrives when it always would have, and only the status line
        # moves to that moment.
        #
        # aiter_raw() may be called exactly once -- a second call raises
        # StreamConsumed -- so the iterator, not the response, is what gets
        # handed to the generator below.
        stream = response.aiter_raw()
        pending = asyncio.ensure_future(anext(stream))
        # asyncio.wait rather than wait_for: a timeout must leave the read
        # running. wait_for would cancel it mid-read and corrupt the stream,
        # and a slow first token is a long prefill, not a failure.
        try:
            await asyncio.wait(
                {pending}, timeout=self._settings.upstream_header_hold_s
            )
        except BaseException:
            # This is where a losing hedge is cancelled: waiting on a first
            # chunk that is never going to be wanted. The upstream is already
            # open and tracked, and nothing downstream will ever start the body
            # generator that would settle it, so the cleanup has to happen here
            # or the attempt strands its KV commitment and its connection.
            pending.cancel()
            settle(None, failed=True, error_class="Discarded")
            self._release(response)
            await close_quietly(response)
            raise

        first: bytes | None = None
        held = pending
        if pending.done():
            held = None
            try:
                first = pending.result()
            except StopAsyncIteration:
                first = None  # an empty body is an answer, not a failure
            except httpx.HTTPError as exc:
                self._release(response)
                await close_quietly(response)
                settle(
                    None,
                    failed=True,
                    retry_reason=RETRY_TRANSPORT,
                    error_class=type(exc).__name__,
                )
                self._note_transport_failure(target.target_id)
                log.warning(
                    "upstream died before its first chunk: %s (%s)",
                    url,
                    type(exc).__name__,
                )
                return Attempt(
                    retryable=True,
                    reason=RETRY_TRANSPORT,
                    target_id=target.target_id,
                    error=type(exc).__name__,
                )
        else:
            # Past the hold budget. Commit the headers now and let the
            # generator wait out the rest of the prefill; the attempt simply
            # stops being retryable from here.
            log.info(
                "%s has not produced a first chunk in %.0fs; releasing headers",
                target.target_id,
                self._settings.upstream_header_hold_s,
            )

        tracked.pending = held

        sent_content_type = _forward_response_headers(response.headers).get(
            "content-type"
        )

        async def stream_body():
            # The generator is running, so its `finally` below owns the close
            # and the janitor must keep its hands off. Set before the first
            # await: a cancellation between here and the first chunk still
            # unwinds through that `finally`.
            tracked.started = True
            # Bytes the CLIENT has seen. Not bytes read from the upstream:
            # the difference is the whole recovery window, because a response
            # whose headers are committed but whose body is still empty can
            # still be finished by somebody else without the client noticing.
            committed = 0
            # True while the upstream is mid-body and could still be read: a
            # hang-up then leaves the rest of an unstreamed answer (and its
            # usage block) on the wire, which the `finally` reads.
            readable = False
            try:
                opening = first
                if held is not None:
                    try:
                        opening = await held
                    except StopAsyncIteration:
                        opening = None
                readable = opening is not None
                if opening is not None:
                    # Counted here rather than at the peek, so TTFT is the
                    # moment the client could first have seen a byte and a
                    # marker split across the peek boundary is still counted
                    # exactly once.
                    accounting.note_chunk(opening, time.monotonic())
                    committed += len(opening)
                    yield opening
                while (chunk := await _next_chunk(stream, accounting)) is not None:
                    accounting.note_chunk(chunk, time.monotonic())
                    committed += len(chunk)
                    yield chunk
                readable = False
                settle(
                    accounting.finish(time.monotonic()),
                    failed=False,
                    status=response.status_code,
                )
            except httpx.HTTPError as exc:
                readable = False
                self._note_transport_failure(target.target_id)
                replacement = None
                if not committed and reopen is not None:
                    replacement = await reopen(sent_content_type)
                if replacement is None:
                    # Rule 1: a body the client has already begun reading is
                    # never rewritten. Past the first byte this is somebody
                    # else's stream only in the sense that it is broken. The
                    # row keeps what did get through; the request's limits
                    # count it from the reservation (the transfer started).
                    settle(
                        accounting.finish(time.monotonic()),
                        failed=True,
                        status=response.status_code,
                        retry_reason=RETRY_TRANSPORT,
                        error_class=type(exc).__name__,
                        truncated=True,
                    )
                    raise
                log.info(
                    "%s died before the client saw a byte; finishing from "
                    "another target",
                    target.target_id,
                )
                # Settle and let go of the dead one here rather than in the
                # `finally`: the replacement owns the rest of this response,
                # and its own generator carries its own accounting, breaker
                # verdict, KV commitment and cleanup. settle/_release/
                # close_quietly are all idempotent, so the finally below is a
                # no-op after this.
                settle(
                    None,
                    failed=True,
                    retry_reason=RETRY_TRANSPORT,
                    error_class=type(exc).__name__,
                )
                self._release(response)
                await close_quietly(response)
                async for chunk in replacement:
                    committed += len(chunk)
                    yield chunk
            finally:
                # A client that hangs up raises GeneratorExit or CancelledError,
                # both of which derive from BaseException and so never reached
                # an `except Exception`. settle() is idempotent, so settling
                # here is a no-op after a clean finish and is the only thing
                # that releases the KV commitment of an abandoned stream.
                #
                # settle() is synchronous and the close is not, which is exactly
                # how this leaked for months while the books stayed straight:
                # under Starlette's disconnect the surrounding anyio scope is
                # already cancelled, so a bare `await response.aclose()` raises
                # at its first checkpoint and the connection is never returned.
                # `close_quietly` shields it. Do not unwrap it.
                #
                # A client that hangs up was still *served*: the upstream ran
                # the prompt and every token up to the hang-up, and a provider
                # bills for all of it. So this is settled with what was seen,
                # not voided -- voiding is how a key streamed without being
                # counted (finding H5). An unstreamed answer was served in
                # full, so the rest of it is read for its usage block first.
                if readable and accounting.wants_rest:
                    await _drain_for_usage(stream, accounting)
                settle(
                    accounting.finish(time.monotonic()),
                    failed=False,
                    status=response.status_code,
                    error_class="ClientDisconnected",
                    truncated=True,
                )
                self._release(response)
                await close_quietly(response)

        async def discard() -> None:
            """Undo an attempt nobody will read. See `Attempt.discard`."""
            settle(None, failed=True, error_class="Discarded")
            self._release(response)
            await close_quietly(response)

        return Attempt(
            response=StreamingResponse(
                stream_body(),
                status_code=response.status_code,
                headers=_forward_response_headers(response.headers, rid),
            ),
            target_id=target.target_id,
            discard=discard,
        )

    async def forward_provider(
        self,
        *,
        target,
        provider_id: str,
        upstream_id: str,
        path: str,
        body: dict | None,
        stats: StatsRegistry,
        streaming: bool,
        open_upstream,
        on_finish=None,
        selection=None,
        trace=None,
        attempt_no: int = 0,
        reopen=None,
        on_record=None,
        content: bytes | None = None,
        content_type: str | None = None,
        open_upstream_raw=None,
        count_tokens: bool = True,
    ) -> Attempt | None:
        """One target's turn, routed through a provider's own ``open_upstream``.

        ``reopen`` behaves exactly as it does in :meth:`forward` -- see that
        docstring. A provider stream is if anything more worth recovering: it
        crosses a network we do not own.

        This is what makes a provider's backoff, spend accounting, auth
        handling and budget enforcement actually run on live traffic (audit
        H-9) -- forwarding with a raw resolved key, as :meth:`forward` does,
        bypasses all of it. Settle-exactly-once and the outstanding count
        follow the same shape as :meth:`forward`; only where the bytes come
        from differs.

        Returns ``None`` when the provider refused to admit the request
        (rate limited, over budget, disabled, or otherwise unusable) before
        anything was sent. That is not a backend failure to replay -- there
        is nothing of the backend's to replay -- it is a reason for the
        caller to try a different target right away.

        ``content`` is an opaque upload (a transcription) forwarded under the
        client's ``content_type`` instead of ``body``: it goes through the same
        provider gate and spend accounting as a JSON request (finding K5) and,
        like :meth:`forward`'s audio path, counts no tokens.
        """
        target_stats = stats.get(target.target_id)
        target_stats.begin()
        started = time.monotonic()
        if not count_tokens or content is not None:
            # An opaque upload, or audio bytes: no usage block to look for, and
            # buffering the body to look for one costs a megabyte to learn nothing.
            accounting = _NoAccounting(started)
        elif streaming:
            accounting = _StreamAccounting(
                started, on_tokens=target_stats.note_tokens, metered=_meters_cost(selection)
            )
        else:
            accounting = _BodyAccounting(started, metered=_meters_cost(selection))
        finished = False

        def settle(
            usage: _Usage | None,
            failed: bool,
            *,
            status: int | None = None,
            retry_reason: str = "",
            error_class: str = "",
            truncated: bool = False,
            error_message: str | None = None,
            retry_after_s: float | None = None,
        ) -> None:
            nonlocal finished
            if finished:
                return
            finished = True
            duration = time.monotonic() - started
            # See forward(): a truncated stream is counted, never timed.
            if failed or usage is None or truncated:
                target_stats.fail()
            else:
                target_stats.complete(
                    tokens=usage.tokens,
                    duration_s=duration,
                    decode_s=usage.decode_s,
                    ttft_s=usage.ttft_s,
                    already_counted=getattr(accounting, "live", 0),
                )
            # Mirrors forward(): the one moment these numbers exist as
            # themselves, before they are folded into an EWMA. Without this a
            # request that a client can name by its X-Request-Id has no row
            # once the providers port exposes open_upstream -- exactly the
            # failure RequestTrace exists to prevent (audit H-9).
            if trace is not None:
                self._emit(
                    trace, usage, on_record, failed=failed, selection=selection,
                    attempt_no=attempt_no, retry_reason=retry_reason, status=status,
                    error_class=error_class, duration_s=duration,
                    error_message=error_message, retry_after_s=retry_after_s,
                )
            if on_finish is not None:
                on_finish()

        # An @asynccontextmanager instance: __aenter__ runs the provider's
        # _prepare() and the request loop up to its first yield. If it raises
        # there -- every branch below except the final one -- the generator
        # is already fully unwound and __aexit__ must not be called: the
        # context manager was never successfully entered.
        extra: dict = {}
        if trace is not None:
            extra["request_id"] = trace.request_id
        if content is not None and open_upstream_raw is not None:
            # An opaque upload: multipart, which has no dict to build a payload
            # from. Same class, same gates, same accounting -- only the body
            # differs. See ProviderService.open_upstream_raw.
            manager = open_upstream_raw(
                provider_id, upstream_id, content, content_type, endpoint=path, **extra
            )
        else:
            if content is not None:
                extra["content"] = content
                extra["content_type"] = content_type
            manager = open_upstream(
                provider_id, upstream_id, body, streaming, endpoint=path, **extra
            )
        try:
            upstream = await manager.__aenter__()
        except ProviderNotAdmittingError:
            # Rate limited, over budget, or disabled -- nothing was sent, and
            # there is no transport verdict to report either way.
            settle(None, failed=True)
            return None
        except MissingKeyError as exc:
            # The referenced env var or secret does not resolve: a
            # misconfiguration, not a capacity problem, and the single most
            # likely first-run failure. Answer exactly as the raw-key path
            # does (openai_api._dispatch's resolve_key branch) rather than
            # letting a broader `except ProviderError` fold it into "try
            # another target", which would report it to the client as
            # no_target_admitting -- capacity, not the honest "this
            # provider's key is unusable" (audit H-9).
            if trace is not None:
                trace.error_code = "provider_key_unavailable"
            settle(None, failed=True, status=502, error_class=type(exc).__name__)
            self._abandon_probe(target.target_id)
            log.warning(
                "could not resolve api key for provider %s", provider_id
            )
            return Attempt(
                response=error_response(
                    502,
                    f"Provider '{provider_id}' has no usable credential configured.",
                    "server_error",
                    "provider_key_unavailable",
                ),
                target_id=target.target_id,
            )
        except AdapterUnsupportedError as exc:
            # This provider's wire format has no adapter in this build: also
            # a configuration fact, not capacity, and also swallowed by a
            # bare `except ProviderError` before this fix.
            if trace is not None:
                trace.error_code = "provider_adapter_unsupported"
            settle(None, failed=True, status=502, error_class=type(exc).__name__)
            self._abandon_probe(target.target_id)
            log.warning("provider %s: %s", provider_id, exc)
            return Attempt(
                response=error_response(
                    502, str(exc), "server_error", "provider_adapter_unsupported"
                ),
                target_id=target.target_id,
            )
        except UpstreamError as exc:
            transport_class = _synthetic_transport_error_class(exc)
            if transport_class == _POOL_TIMEOUT_CLASS:
                # The provider's own pool, not the provider. Same reasoning as
                # the direct path: no breaker verdict, because nothing here is
                # evidence about whether the upstream is up.
                settle(
                    None,
                    failed=True,
                    retry_reason=RETRY_POOL,
                    error_class=transport_class,
                )
                self._abandon_probe(target.target_id)
                log.warning(
                    "no upstream connection available for provider %s (%s)",
                    provider_id, upstream_id,
                )
                return Attempt(
                    retryable=True,
                    reason=RETRY_POOL,
                    target_id=target.target_id,
                    error=transport_class,
                )
            if transport_class is not None:
                # ProviderService could not reach the upstream at all and has
                # no way to say so except by raising UpstreamError -- its
                # contract has no other shape for a transport failure. There
                # is no backend answer here to replay, so this is a
                # transport failure like any other: retryable, and the
                # breaker must hear about it as a failure, not a success
                # (audit H-9 -- a dead remote must be able to trip the
                # breaker, and a half-open probe against it must not
                # immediately re-close the circuit).
                settle(
                    None,
                    failed=True,
                    retry_reason=RETRY_TRANSPORT,
                    error_class=transport_class,
                )
                self._note_transport_failure(target.target_id)
                log.warning(
                    "provider %s unreachable for %s (%s)",
                    provider_id, upstream_id, transport_class,
                )
                return Attempt(
                    retryable=True,
                    reason=RETRY_TRANSPORT,
                    target_id=target.target_id,
                    error=transport_class,
                )
            # It answered -- with an error, but it answered -- so the
            # transport verdict is success regardless of the status code.
            self._note_transport_success(target.target_id)
            headers: dict[str, str] = {"content-type": "application/json"}
            if exc.retry_after_s:
                headers["retry-after"] = str(int(exc.retry_after_s))
            if exc.status_code >= 500:
                settle(
                    None,
                    failed=True,
                    status=exc.status_code,
                    retry_reason=RETRY_SERVER_ERROR,
                )
                return Attempt(
                    retryable=True,
                    reason=RETRY_SERVER_ERROR,
                    target_id=target.target_id,
                    status=exc.status_code,
                    headers=headers,
                    body=exc.body.encode("utf-8", "replace"),
                )
            settle(
                None, failed=True, status=exc.status_code,
                error_class="RateLimitError" if exc.status_code == 429 else "",
                error_message=exc.message if exc.status_code == 429 else None,
                retry_after_s=exc.retry_after_s,
            )
            return Attempt(
                response=Response(
                    content=exc.body.encode("utf-8", "replace"),
                    status_code=exc.status_code,
                    headers=headers,
                ),
                target_id=target.target_id,
            )
        except ProviderError:
            # Anything else this package raises (UnknownProviderError and any
            # future ProviderError this method does not yet know to name):
            # a fact about this target's configuration, not this request.
            # Try someone else rather than fail the whole request on it.
            settle(None, failed=True)
            return None
        except httpx.PoolTimeout as exc:
            # Same split as the direct path, for the same reason.
            settle(
                None,
                failed=True,
                retry_reason=RETRY_POOL,
                error_class=type(exc).__name__,
            )
            self._abandon_probe(target.target_id)
            log.warning(
                "no upstream connection available for provider %s (%s)",
                provider_id, upstream_id,
            )
            return Attempt(
                retryable=True,
                reason=RETRY_POOL,
                target_id=target.target_id,
                error=type(exc).__name__,
            )
        except httpx.HTTPError as exc:
            # Defensive: ProviderService itself never lets a raw httpx error
            # escape __aenter__ (it wraps every one as UpstreamError above),
            # but a providers port is a Protocol, not this one class, and a
            # future or third-party implementation may not make that promise.
            settle(
                None,
                failed=True,
                retry_reason=RETRY_TRANSPORT,
                error_class=type(exc).__name__,
            )
            self._note_transport_failure(target.target_id)
            log.warning(
                "provider %s unreachable for %s (%s)",
                provider_id, upstream_id, type(exc).__name__,
            )
            return Attempt(
                retryable=True,
                reason=RETRY_TRANSPORT,
                target_id=target.target_id,
                error=type(exc).__name__,
            )

        self._note_transport_success(target.target_id)

        # Tracked like a direct response: Starlette sends the response line
        # before it first iterates the body, and a client gone in between
        # leaves this generator unstarted -- no `finally`, so the provider's
        # context never exited and the attempt never settled. The janitor
        # does both (`Reaped` also gives the request's reservation back).
        handle = _ProviderHandle(manager)
        tracked = self._track(handle)
        tracked.abandon = lambda: settle(None, failed=True, error_class="Reaped")

        sent_content_type = upstream.headers.get("content-type") if upstream.headers else None

        def settle_body(**fields) -> None:
            # ProviderService owns adapter classification. The HTTP response
            # stays 200 once streaming starts, but history must name the
            # logical error, even if the client closes on its error frame.
            error = upstream.error
            if error is not None:
                fields.update(
                    failed=True, status=error.status_code,
                    error_class="RateLimitError" if error.status_code == 429 else "UpstreamError",
                    error_message=error.message if error.status_code == 429 else None,
                    retry_after_s=error.retry_after_s,
                )
            settle(accounting.finish(time.monotonic()), **fields)

        async def stream_body():
            exc_info: tuple = (None, None, None)
            # See `forward`: bytes the CLIENT has seen, which is what decides
            # whether this response can still be finished by somebody else.
            committed = 0
            # Set once the handover has already run this upstream's teardown.
            # `__aexit__` is not re-entrant: calling it twice throws into a
            # generator that has already finished.
            torn_down = False
            # See `forward`: an unstreamed body the client hung up on is read
            # to its usage block before it is settled.
            readable = True
            tracked.started = True
            try:
                while (chunk := await _next_chunk(upstream.body, accounting)) is not None:
                    accounting.note_chunk(chunk, time.monotonic())
                    committed += len(chunk)
                    yield chunk
                readable = False
                settle_body(
                    failed=False,
                    status=upstream.status_code,
                )
            except httpx.HTTPError as exc:
                if committed or reopen is None:
                    exc_info = (type(exc), exc, exc.__traceback__)
                    self._note_transport_failure(target.target_id)
                    raise
                replacement = await reopen(sent_content_type)
                if replacement is None:
                    exc_info = (type(exc), exc, exc.__traceback__)
                    self._note_transport_failure(target.target_id)
                    raise
                self._note_transport_failure(target.target_id)
                log.info(
                    "provider %s died before the client saw a byte; finishing "
                    "from another target",
                    provider_id,
                )
                # The provider's context manager still has to hear the real
                # exception -- passing None would tell open_upstream this
                # transfer finished cleanly and it would record spend and call
                # note_success for one that did not.
                settle(
                    None,
                    failed=True,
                    retry_reason=RETRY_TRANSPORT,
                    error_class=type(exc).__name__,
                )
                with anyio.CancelScope(shield=True):
                    try:
                        await manager.__aexit__(type(exc), exc, exc.__traceback__)
                    except Exception:
                        log.debug("provider teardown failed", exc_info=True)
                torn_down = True
                async for chunk in replacement:
                    committed += len(chunk)
                    yield chunk
            except BaseException as exc:
                # A client disconnect raises GeneratorExit or CancelledError
                # here, both of which derive from BaseException and so never
                # reach an `except Exception` -- and an httpx.HTTPError mid
                # stream is a genuine transport failure the breaker must
                # hear about, same as one before the first byte. Either way
                # the real exception, not a synthesized clean exit, is what
                # goes to the provider's own context manager below: passing
                # None there would tell ProviderService.open_upstream this
                # transfer finished cleanly, and it would call
                # runtime.note_success() for one that did not. (It records
                # the spend either way once the body was read: a provider
                # bills a stream the client walked away from.)
                if isinstance(exc, httpx.HTTPError):
                    self._note_transport_failure(target.target_id)
                exc_info = (type(exc), exc, exc.__traceback__)
                raise
            finally:
                # settle() is idempotent, so this is a no-op after a clean
                # finish and is the only thing that releases the commitment
                # of an abandoned stream otherwise.
                #
                # Shielded for the same reason as the direct path's close: a
                # client disconnect leaves this generator running inside an
                # already-cancelled anyio scope, where an unshielded await
                # raises at once and the provider's context manager never gets
                # to release its connection.
                #
                # What it settles with is what was served: a hang-up is
                # counted against the key, not voided (finding H5), and a
                # transfer that broke keeps its partial counts on the row.
                kind = exc_info[0]
                if kind is None or issubclass(kind, (GeneratorExit, asyncio.CancelledError)):
                    if kind is not None and readable and accounting.wants_rest:
                        # Through the provider's own iterator, so its ledger's
                        # sniffer reads the usage block too.
                        await _drain_for_usage(upstream.body, accounting)
                    settle_body(
                        failed=False,
                        status=upstream.status_code,
                        error_class="ClientDisconnected",
                        truncated=True,
                    )
                else:
                    settle_body(
                        failed=True,
                        status=upstream.status_code,
                        retry_reason=(
                            RETRY_TRANSPORT if issubclass(kind, httpx.HTTPError) else ""
                        ),
                        error_class=kind.__name__,
                        truncated=True,
                    )
                if not torn_down:
                    with anyio.CancelScope(shield=True):
                        try:
                            await manager.__aexit__(*exc_info)
                        except Exception:
                            log.debug(
                                "provider upstream teardown failed", exc_info=True
                            )
                self._release(handle)

        async def discard() -> None:
            """Undo an attempt nobody will read. See `Attempt.discard`."""
            settle(None, failed=True, error_class="Discarded")
            self._release(handle)
            reason = _Discarded("hedge lost")
            with anyio.CancelScope(shield=True):
                try:
                    await manager.__aexit__(type(reason), reason, None)
                except _Discarded:
                    pass  # ours, and it has done its job
                except Exception:
                    log.debug("provider teardown failed", exc_info=True)

        return Attempt(
            response=StreamingResponse(
                stream_body(),
                status_code=upstream.status_code,
                media_type=upstream.media_type,
                headers=upstream.headers,
            ),
            target_id=target.target_id,
            discard=discard,
        )
