"""Smoke-tests every route the gateway and agent contracts declare against a
live coordinator.

``control_plane/contracts/routes.json`` (built by
``control_plane/contracts/routes.py`` by walking the real Starlette apps) is
the one enumeration of "every endpoint this process answers" that cannot
drift from the code -- see that module's docstring. Everything else that
touches routes exercises them in-process against ``TestClient``; nothing
sends a real HTTP request across the whole declared surface to a process
that is actually listening. This is that check.

It is a liveness check, not a correctness check: a 404 for a made-up
resource id is the *correct* answer for a resource-scoped route and passes.
Only a 5xx -- an unhandled exception, the class of bug this file exists to
catch -- fails. The body is never fully read for the same reason
``/api/metrics/stream`` exists: a route can legitimately keep the response
open, and this only needs the status line.

The gateway answers ``/api`` only to an admin session and ``/v1`` only to a
``derate-`` key (``gateway/authz.py``), so every row is sent through one client
that logged in as ``DERATE_CHECK_ADMIN_USER`` and carries
``DERATE_CHECK_API_KEY`` -- the same three variables ``ui/src/check/auth.mjs``
reads, so one export serves both suites. Without them every gated row would
stop at the credential gate with a 401 and the handler behind it would never
run: 155 passes that checked nothing.

Skips only when nothing names a coordinator. ``DERATE_E2E_ORIGIN`` unset means
"there may not be one", and the suite says so. Set, it is a promise: an
unreachable origin or a missing credential FAILS rather than skips, because a
skip on the machine that was told where to look is the silent-green this file
replaced. ``DERATE_E2E_AGENT_ORIGIN`` names the same process's agent port
for the ``/agent`` rows and has no default.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from tests.unit import e2e_support

#: Same convention as test_gateway.py's e2e tests: a real coordinator,
#: driven over HTTP the way an operator would. No default here, on purpose:
#: a default would make "unset" and "set to the box's own :8088" the same
#: thing, and only one of those is a promise.
E2E_ORIGIN = os.environ.get("DERATE_E2E_ORIGIN")
E2E_AGENT_ORIGIN = os.environ.get("DERATE_E2E_AGENT_ORIGIN")

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


def _routes(app: str) -> list[tuple[str, str]]:
    routes = json.loads(_ROUTES_PATH.read_text(encoding="utf-8"))[app]
    return [(method, row["path"]) for row in routes for method in row["methods"]]


def _require(origin: str | None, variable: str, health: str) -> str:
    return e2e_support.require(origin, variable, health)


def _client(origin: str) -> httpx.Client:
    return e2e_support.client(origin)


def _body_kwargs(method: str) -> dict:
    # A deliberately incomplete body: proving validation runs before a
    # handler crashes on a missing field is the point, not exercising the
    # route's real business logic -- the unit suite already owns that,
    # per-route.
    return {"json": {}} if method in ("POST", "PUT", "PATCH") else {}


@pytest.mark.slow
@pytest.mark.parametrize("method,path", _routes("gateway"))
def test_route_resolves_without_a_server_error(method, path):
    origin = _require(E2E_ORIGIN, "DERATE_E2E_ORIGIN", "/healthz")
    url = origin + _fill(path)
    with _client(origin).stream(method, url, **_body_kwargs(method)) as reply:
        assert reply.status_code < 500, (
            "%s %s -> %s (a route this contract declares must not 500)"
            % (method, path, reply.status_code)
        )


# The agent is gated by X-Derate-Token, a per-member token the registry mints
# at join and which is not reliably on disk for the coordinator's own agent.
# These rows go unauthenticated, so a 401/403 from that gate is an acceptable
# answer: the process is up, the route resolved, nothing raised.
@pytest.mark.slow
@pytest.mark.parametrize("method,path", _routes("agent"))
def test_agent_route_resolves_without_a_server_error(method, path):
    origin = _require(E2E_AGENT_ORIGIN, "DERATE_E2E_AGENT_ORIGIN", "/agent/health")
    url = origin + _fill(path)
    with httpx.stream(method, url, timeout=15, **_body_kwargs(method)) as reply:
        assert reply.status_code < 500, (
            "%s %s -> %s (a route this contract declares must not 500)"
            % (method, path, reply.status_code)
        )
