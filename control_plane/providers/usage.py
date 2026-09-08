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
                return None
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
