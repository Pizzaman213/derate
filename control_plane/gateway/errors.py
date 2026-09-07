"""OpenAI-shaped error bodies.

These are the gateway's own errors -- unknown model, nothing admitting,
admission refused. Errors that came from a backend are passed through
untouched and never reach this module.
"""

from __future__ import annotations

import logging

from starlette.responses import JSONResponse

log = logging.getLogger("gateway.errors")

#: An exception message is a diagnostic, not a document. Long enough to
#: carry a URL and a cause, short enough not to become the response.
MAX_DETAIL_CHARS = 500
MAX_CAUSE_DEPTH = 4


def error_body(message: str, type_: str, code: str, **extra) -> dict:
    body = {
        "error": {
            "message": message,
            "type": type_,
            "param": None,
            "code": code,
        }
    }
    body["error"].update(extra)
    return body


def error_response(
    status: int, message: str, type_: str, code: str, headers: dict | None = None, **extra
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=error_body(message, type_, code, **extra),
        headers=headers or {},
    )


def unknown_model(model: str, available: list[str]) -> JSONResponse:
    """404 naming what does exist, so a client can correct itself."""
    listing = ", ".join(f"'{name}'" for name in available) if available else "none"
    return error_response(
        404,
        f"The model '{model}' does not exist. Available models: {listing}.",
        "invalid_request_error",
        "model_not_found",
        available_models=available,
    )


def model_not_ready(model: str, states: list[str]) -> JSONResponse:
    """503 carrying the current state, so a client can retry sensibly rather
    than hanging on a deployment that is still loading."""
    detail = ", ".join(states) if states else "unknown"
    return error_response(
        503,
        f"The model '{model}' is not ready to serve. Current state: {detail}.",
        "server_error",
        "model_not_ready",
        headers={"Retry-After": "5"},
        deployment_states=states,
    )


def wrong_modality(model: str, has, wants, endpoint: str) -> JSONResponse:
    """400 when a real model is named on an endpoint it does not answer.

    "No such model" would be a lie -- the model exists and is serving -- and a
    bare refusal leaves the caller thinking they mistyped the name. So this
    names the endpoint that *would* work, which is the one thing that turns the
    error into an action. Same reasoning as the dtype_not_launchable refusal:
    say what the mechanism is, not just that it was refused.
    """
    from control_plane.contracts import ENDPOINT_FOR_MODALITY

    correct = ENDPOINT_FOR_MODALITY.get(has)
    remedy = f" Send it to {correct} instead." if correct else ""
    return error_response(
        400,
        f"The model '{model}' is a {has.value} model and cannot serve "
        f"{endpoint}, which requires a {wants.value} model.{remedy}",
        "invalid_request_error",
        "wrong_modality",
        model_modality=has.value,
        endpoint_modality=wants.value,
        correct_endpoint=correct,
    )


def no_target_admitting(model: str) -> JSONResponse:
    """503 rather than queueing indefinitely. Every target for this model is
    unhealthy, draining, or under critical memory pressure."""
    return error_response(
        503,
        f"No target for '{model}' is currently admitting requests.",
        "server_error",
        "no_target_admitting",
        headers={"Retry-After": "5"},
    )


def upstream_unreachable(model: str, detail: str, tried: int) -> JSONResponse:
    """502 when no target for this model could be reached at all.

    There is no backend error to preserve here: we never got one. Say that
    plainly, and say how many targets were tried, rather than inventing an
    upstream status.
    """
    attempts = "1 target" if tried == 1 else f"{tried} targets"
    return error_response(
        502,
        f"No upstream for '{model}' is reachable: {detail} after trying {attempts}.",
        "server_error",
        "upstream_unreachable",
    )


def _scrub(text: str, redactor) -> str:
    """Run a diagnostic through the provider redactor before it leaves.

    Exception messages carry resolver URLs and values the provider service was
    told to remember. `4.4`'s rule -- a key that leaves the process once is
    leaked -- applies to an error body exactly as it does to a log line, so
    this is not optional and the redactor must be the *shared* one: a fresh
    instance only scrubs values it has been told about.
    """
    if redactor is None:
        return text
    try:
        scrubbed = redactor.scrub(text)
    except Exception:
        log.exception("redaction failed; withholding the detail")
        # Better a class name than an unredacted message.
        return ""
    return scrubbed if isinstance(scrubbed, str) else text


def detail(exc: BaseException, redactor=None) -> str:
    """'MetadataUnavailable: hub request failed: <url>: RemoteDisconnected'.

    `f"{type(exc).__name__}."` discards the only part an operator can act on.
    A live plan returned 'Could not plan: MetadataUnavailable.' whose actual
    cause -- the hub closing the connection -- existed solely in the log.

    Falls back to the bare class name when the exception carries no message,
    so nothing regresses for an exception that never had one.
    """
    name = type(exc).__name__
    try:
        body = str(exc).strip()
    except Exception:
        body = ""
    body = _scrub(body, redactor)
    if not body or body == name:
        return name
    text = f"{name}: {body}"
    if len(text) > MAX_DETAIL_CHARS:
        text = text[: MAX_DETAIL_CHARS - 1].rstrip() + "\u2026"
    return text


def cause_chain(exc: BaseException, redactor=None) -> list[str]:
    """The __cause__/__context__ chain, deduped and capped.

    The RemoteDisconnected underneath a MetadataUnavailable is the line
    somebody actually acts on, and it is the one the top-level message hides.
    """
    out: list[str] = []
    seen: set[int] = {id(exc)}
    cur = exc.__cause__ or exc.__context__
    while cur is not None and len(out) < MAX_CAUSE_DEPTH:
        if id(cur) in seen:
            break
        seen.add(id(cur))
        line = detail(cur, redactor)
        if line and line not in out:
            out.append(line)
        cur = cur.__cause__ or cur.__context__
    return out
