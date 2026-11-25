"""Request bodies read with a ceiling.

Starlette's ``request.json()`` buffers whatever the client sends before anything
can look at its size, and uvicorn applies no body limit of its own: an open
route (login, setup, join) would hold a gigabyte in memory on the word of an
unauthenticated caller. Every body the gateway parses comes through here, so
an oversized one is dropped at the limit instead. The shape refusals every /v1
JSON body gets (a model name past its length, nesting past its depth) live
here too, beside the ceilings they share a reason with.
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

#: The longest ``model`` a /v1 request may name. Longer than any HuggingFace
#: id, ``hf://owner/repo/file.gguf`` or ``<provider>/<model>`` alias; the name
#: lands verbatim in every telemetry row, so an unbounded one was a way to
#: write megabytes per refused request.
MAX_MODEL_CHARS = 512

#: Containers one /v1 JSON body may nest. Parsing survives far deeper, but the
#: estimates re-serialise ``tools`` and ``response_format`` recursively, and
#: one level under the C recursion limit made that a 500.
MAX_JSON_DEPTH = 64


class BodyTooLarge(ValueError):
    """The body went past the caller's limit; nothing past it was read."""


def payload_too_large(limit: int = API_JSON_BODY_MAX) -> Response:
    """The 413 for a body past *limit*."""
    return error_response(
        413, f"Body exceeds {limit // 1024} KiB.", "invalid_request_error", "payload_too_large"
    )


def model_refusal(model: str) -> Response | None:
    """The 400 for a ``model`` past :data:`MAX_MODEL_CHARS`, which does not
    echo it; None otherwise."""
    if len(model) <= MAX_MODEL_CHARS:
        return None
    return error_response(
        400, f"'model' is longer than {MAX_MODEL_CHARS} characters.",
        "invalid_request_error", "invalid_request",
    )


def depth_refusal(value: object, limit: int = MAX_JSON_DEPTH) -> Response | None:
    """The 400 when any object or array in *value* sits more than *limit*
    levels deep (the top-level container is level 1); None otherwise.
    Iterative, so it cannot hit the recursion limit it guards against."""
    stack = [(value, 1)] if isinstance(value, (dict, list)) else []
    while stack:
        item, depth = stack.pop()
        if depth > limit:
            return error_response(
                400, f"Request body is not valid JSON: it is nested more than {limit} levels deep.",
                "invalid_request_error", "invalid_json",
            )
        children = item.values() if isinstance(item, dict) else item
        stack.extend((child, depth + 1) for child in children if isinstance(child, (dict, list)))
    return None


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
    try:
        value = json.loads(raw, parse_constant=_reject_constant)
        _refuse_non_finite(value)
    except RecursionError:
        # The caller's malformed body -- a 400 like any other, never a 500.
        raise ValueError("body is nested too deeply") from None
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
