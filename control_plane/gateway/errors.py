"""OpenAI-shaped error bodies.

These are the gateway's own errors -- unknown model, nothing admitting,
admission refused. Errors that came from a backend are passed through
untouched and never reach this module.
"""

from __future__ import annotations

from starlette.responses import JSONResponse


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
