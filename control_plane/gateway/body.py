"""Request bodies read with a ceiling.

Starlette's ``request.json()`` buffers whatever the client sends before anything
can look at its size, and uvicorn applies no body limit of its own: an open
route (login, setup, join) would hold a gigabyte in memory on the word of an
unauthenticated caller. Every body the gateway parses comes through here, so
an oversized one is dropped at the limit instead.
"""

from __future__ import annotations

import json
from typing import NoReturn

from starlette.requests import Request
from starlette.responses import Response

from .errors import error_response

#: The ceiling on a JSON body for /api and the open routes. Nothing the UI or
#: a joining node sends is within an order of magnitude of it.
API_JSON_BODY_MAX = 64 * 1024


class BodyTooLarge(ValueError):
    """The body went past the caller's limit; nothing past it was read."""


def payload_too_large(limit: int = API_JSON_BODY_MAX) -> Response:
    """The 413 for a body past *limit*."""
    return error_response(
        413, f"Body exceeds {limit // 1024} KiB.", "invalid_request_error", "payload_too_large"
    )


async def read_bounded_body(request: Request, limit: int) -> bytes | None:
    """Read the whole body, or None if it goes past *limit*.

    Streamed rather than Starlette's ``request.body()`` so an oversized upload is
    dropped at the limit instead of being buffered in full and then rejected.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _reject_constant(name: str) -> NoReturn:
    """``json.loads`` accepts ``NaN``/``Infinity`` (and ``1e999`` -> inf);
    none is JSON, and a stored one makes every later render of it a 500."""
    raise ValueError("NaN and Infinity are not JSON")


def _parse(raw: bytes) -> object:
    value = json.loads(raw, parse_constant=_reject_constant)
    _refuse_non_finite(value)
    return value


def _refuse_non_finite(value: object) -> None:
    # ``1e999`` parses as a float, not through parse_constant.
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("NaN and Infinity are not JSON")
    elif isinstance(value, dict):
        for item in value.values():
            _refuse_non_finite(item)
    elif isinstance(value, list):
        for item in value:
            _refuse_non_finite(item)


async def read_json_object(request: Request, limit: int = API_JSON_BODY_MAX) -> dict:
    """The body as a dict. Raises :class:`BodyTooLarge` past *limit*, and
    ``ValueError`` when the body is not JSON, not an object, or holds a
    non-finite number."""
    raw = await read_bounded_body(request, limit)
    if raw is None:
        raise BodyTooLarge(f"body exceeds {limit} bytes")
    body = _parse(raw)
    if not isinstance(body, dict):
        raise ValueError("body is not a JSON object")
    return body


async def optional_json_object(request: Request, limit: int = API_JSON_BODY_MAX) -> dict:
    """:func:`read_json_object`, except an empty body is ``{}``."""
    raw = await read_bounded_body(request, limit)
    if raw is None:
        raise BodyTooLarge(f"body exceeds {limit} bytes")
    if not raw.strip():
        return {}
    body = _parse(raw)
    if not isinstance(body, dict):
        raise ValueError("body is not a JSON object")
    return body
