"""Smoke-tests every route the gateway contract declares against a live
coordinator.

``control_plane/contracts/routes.json`` (built by
``control_plane/contracts/routes.py`` by walking the real Starlette app) is
the one enumeration of "every endpoint the gateway answers" that cannot
drift from the code -- see that module's docstring. Everything else that
touches routes exercises them in-process against ``TestClient``; nothing
sends a real HTTP request across the gateway's whole declared surface to a
process that is actually listening. This is that check.

It is a liveness check, not a correctness check: a 404 for a made-up
resource id is the *correct* answer for a resource-scoped route and passes.
Only a 5xx -- an unhandled exception, the class of bug this file exists to
catch -- fails. The body is never fully read for the same reason
``/api/metrics/stream`` exists: a route can legitimately keep the response
open, and this only needs the status line.

Skips entirely, like the e2e tests in test_gateway.py, when no coordinator
is reachable -- set DERATE_E2E_ORIGIN to point at one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

#: Same convention as test_gateway.py's e2e tests: a real coordinator,
#: driven over HTTP the way an operator would.
E2E_ORIGIN = os.environ.get("DERATE_E2E_ORIGIN", "http://localhost:8088")

_ROUTES_PATH = (
    Path(__file__).resolve().parents[2]
    / "control_plane"
    / "contracts"
    / "routes.json"
)

#: Path-param placeholders by name. Anything not listed gets a plain
#: nonexistent-id string -- the route only needs a syntactically acceptable
#: segment, and "does the resource-scoped handler 500 on a miss" is exactly
#: what a made-up id exercises. `pid` is typed as int by the handler, so a
#: non-numeric placeholder would only prove FastAPI's own coercion works.
_PLACEHOLDERS = {"pid": "999999"}


def _fill(path: str) -> str:
    out: list[str] = []
    param = ""
    depth = 0
    for ch in path:
        if ch == "{":
            depth += 1
            param = ""
            continue
        if ch == "}":
            depth -= 1
            out.append(_PLACEHOLDERS.get(param, "does-not-exist"))
            continue
        if depth:
            param += ch
        else:
            out.append(ch)
    return "".join(out)


def _gateway_routes() -> list[tuple[str, str]]:
    routes = json.loads(_ROUTES_PATH.read_text(encoding="utf-8"))["gateway"]
    return [(method, row["path"]) for row in routes for method in row["methods"]]


def _require_coordinator() -> str:
    origin = E2E_ORIGIN.rstrip("/")
    try:
        httpx.get(origin + "/v1/models", timeout=5).raise_for_status()
    except Exception as exc:
        pytest.skip(
            "no coordinator on %s (%s); set DERATE_E2E_ORIGIN"
            % (origin, type(exc).__name__)
        )
    return origin


@pytest.mark.slow
@pytest.mark.parametrize("method,path", _gateway_routes())
def test_route_resolves_without_a_server_error(method, path):
    origin = _require_coordinator()
    url = origin + _fill(path)
    kwargs = {"timeout": 15}
    # A deliberately incomplete body: proving validation runs before a
    # handler crashes on a missing field is the point, not exercising the
    # route's real business logic -- the unit suite already owns that,
    # per-route.
    if method in ("POST", "PUT", "PATCH"):
        kwargs["json"] = {}
    with httpx.stream(method, url, **kwargs) as reply:
        assert reply.status_code < 500, (
            "%s %s -> %s (a route this contract declares must not 500)"
            % (method, path, reply.status_code)
        )
