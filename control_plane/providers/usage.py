"""Reading token usage off a response without holding on to it.

Spend accounting needs the ``usage`` block. Streams must not be buffered, so
for a stream we keep only a bounded tail and parse it once the stream has
finished. Every chunk is forwarded the instant it arrives either way.

Some providers also report what they charged. OpenRouter puts a ``cost`` in
every usage block -- streaming and not -- and that figure is the amount taken
off the account, after prompt caching, long-context price tiers and the
per-modality surcharges its ``pricing`` object carries. Reconstructing it from
the published table means reimplementing thirteen price components and getting
the tiers right; reading it means asking the only party that knows. So it is
read where the kind is known to publish it, and nowhere else -- an unrecognized
upstream's ``cost`` is a number in an unknown unit.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from .config import USAGE_BODY_LIMIT_BYTES, USAGE_TAIL_BYTES

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    #: What the provider says this request cost, in USD, or None when it does
    #: not say. Distinct from a cost of 0.0, which is a free model answering.
    cost_usd: float | None = None


def _cost(usage: dict) -> float | None:
    """The provider's own charge for this request, in USD.

    OpenRouter denominates it in credits, which are dollars. A negative figure
    is not a refund we should bank -- it is a field we do not understand -- and
    is dropped rather than subtracted from the day.
    """
    value = usage.get("cost")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0:
        return None
    return float(value)


def _usage_from_obj(obj: object, *, metered: bool = False) -> Usage | None:
    if not isinstance(obj, dict):
        return None
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
    completion = usage.get("completion_tokens", usage.get("output_tokens"))
    if not isinstance(prompt, (int, float)) and not isinstance(completion, (int, float)):
        return None
    return Usage(
        input_tokens=int(prompt or 0),
        output_tokens=int(completion or 0),
        cost_usd=_cost(usage) if metered else None,
    )


class UsageSniffer:
    """Observes bytes on their way through. Never withholds one."""

    def __init__(self, *, stream: bool, metered: bool = False) -> None:
        self._stream = stream
        # Whether this upstream's `usage.cost` means dollars. Off by default:
        # every other kind's usage block is tokens only, and a stray `cost` in
        # an unknown unit must not reach the ledger.
        self._metered = metered
        self._buffer = bytearray()
        self._overflowed = False
        self._truncated = False

    def feed(self, chunk: bytes) -> None:
        if self._overflowed:
            return
        self._buffer.extend(chunk)
        if self._stream:
            # Keep a tail only. The usage block, when present, is in the last
            # data frame before [DONE].
            if len(self._buffer) > USAGE_TAIL_BYTES:
                del self._buffer[: len(self._buffer) - USAGE_TAIL_BYTES]
                self._truncated = True
        elif len(self._buffer) > USAGE_BODY_LIMIT_BYTES:
            # A non-streamed body this large is not something we need to price
            # badly enough to hold in memory.
            self._buffer.clear()
            self._overflowed = True

    def result(self) -> Usage | None:
        if self._overflowed or not self._buffer:
            return None
        text = bytes(self._buffer).decode("utf-8", "replace")
        if not self._stream:
            try:
                return _usage_from_obj(json.loads(text), metered=self._metered)
            except json.JSONDecodeError:
                # A body cut short still ends its `usage` object somewhere in
                # what we kept; a parse failure is not a count of zero.
                return usage_after_key(self._buffer, metered=self._metered)
        found: Usage | None = None
        lines = text.split("\n")
        if self._truncated and len(lines) > 1:
            # Only when the tail was cut is the first line a fragment.
            lines = lines[1:]
        for line in lines:
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                usage = _usage_from_obj(json.loads(payload), metered=self._metered)
            except json.JSONDecodeError:
                continue
            if usage is not None:
                found = usage
        return found

    def audio_duration_s(self) -> float | None:
        """A Whisper-compatible transcription's top-level ``duration``, in
        seconds, or None. What a per-minute audio price is charged on (K5r)."""
        return audio_duration_of(bytes(self._buffer)) if not self._stream else None


def audio_duration_of(raw: bytes) -> float | None:
    """The top-level ``duration`` of a JSON body, when it is a finite,
    non-negative number; None for anything else (audio bytes, no field)."""
    if not raw or raw.lstrip()[:1] != b"{":
        return None
    try:
        obj = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None
    return _duration(obj.get("duration") if isinstance(obj, dict) else None)


def _duration(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if value != value or value < 0 or value == float("inf"):
        return None
    return value


def object_after_key(raw: bytes, key: str) -> object | None:
    """The JSON value following the LAST ``"key":`` in ``raw``, or None.

    For a fragment of a body -- its head or its tail -- that json.loads cannot
    read whole. An occurrence escaped inside a string (``\\"usage\\"``) is
    skipped, so a completion that quotes the word cannot impersonate the block.
    """
    text = raw.decode("utf-8", "replace")
    needle = f'"{key}"'
    end = len(text)
    decoder = json.JSONDecoder()
    while True:
        at = text.rfind(needle, 0, end)
        if at < 0:
            return None
        end = at
        if at > 0 and text[at - 1] == "\\":
            continue
        rest = text[at + len(needle) :].lstrip()
        if not rest.startswith(":"):
            continue
        try:
            value, _ = decoder.raw_decode(rest[1:].lstrip())
        except ValueError:
            continue
        return value


def usage_after_key(raw: bytes, *, metered: bool = False) -> Usage | None:
    """The last ``usage`` block in a body fragment, for a non-streamed body too
    large to hold whole (``usage`` follows ``choices``, so logprobs or a large
    ``n`` push it to the end)."""
    value = object_after_key(raw, "usage")
    return _usage_from_obj({"usage": value}, metered=metered) if isinstance(value, dict) else None


def duration_after_key(raw: bytes) -> float | None:
    """A transcription's ``duration`` out of a body fragment, as
    audio_duration_of() would read it from the whole."""
    return _duration(object_after_key(raw, "duration"))
