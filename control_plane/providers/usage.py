"""Reading token usage off a response without holding on to it.

Spend accounting needs the ``usage`` block. Streams must not be buffered, so
for a stream we keep only a bounded tail and parse it once the stream has
finished. Every chunk is forwarded the instant it arrives either way.
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


def _usage_from_obj(obj: object) -> Usage | None:
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
    )


class UsageSniffer:
    """Observes bytes on their way through. Never withholds one."""

    def __init__(self, *, stream: bool) -> None:
        self._stream = stream
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
                return _usage_from_obj(json.loads(text))
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
                usage = _usage_from_obj(json.loads(payload))
            except json.JSONDecodeError:
                continue
            if usage is not None:
                found = usage
        return found
