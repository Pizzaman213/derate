"""What the tests that drive a LIVE coordinator over HTTP share.

Not collected (no ``test_`` prefix). Two rules live here so no test has to
re-learn them:

* **A set origin is a promise.** The variable unset means "there may be no
  coordinator" and the test skips, saying which variable to set. Set and
  unreachable, or set and missing a credential, FAILS: a skip on the machine
  that was told where to look is a green that checked nothing.
* **The gateway is gated.** ``/api`` answers only to an admin session and
  ``/v1`` only to a ``derate-`` key (``gateway/authz.py``), so the client logs in
  once and carries both. The variable names are the ones
  ``ui/src/check/auth.mjs`` reads, so one export serves both suites.
"""

from __future__ import annotations

import functools
import os

import httpx
import pytest

CREDENTIALS = ("DERATE_CHECK_ADMIN_USER", "DERATE_CHECK_ADMIN_PASSWORD", "DERATE_CHECK_API_KEY")


def require(origin: str | None, variable: str, health: str) -> str:
    """The origin, proven reachable on its open health route."""
    if not origin:
        pytest.skip("%s is not set; nothing names a coordinator to drive" % variable)
    origin = origin.rstrip("/")
    try:
        httpx.get(origin + health, timeout=5).raise_for_status()
    except Exception as exc:
        pytest.fail(
            "%s=%s is set but %s answered/raised %s: %s"
            % (variable, origin, health, type(exc).__name__, exc)
        )
    return origin


@functools.lru_cache(maxsize=None)
def client(origin: str) -> httpx.Client:
    """One logged-in client: the session cookie in the jar for /api, the
    bearer key on every request for /v1. Built once -- a login per request
    would be an audit row each and a lockout risk."""
    missing = [name for name in CREDENTIALS if not os.environ.get(name)]
    if missing:
        pytest.fail(
            "%s %s empty; the gateway answers /api only to a session and /v1 "
            "only to a derate- key, so without them every gated call stops at the "
            "credential gate and checks nothing"
            % (", ".join(missing), "is" if len(missing) == 1 else "are")
        )
    http = httpx.Client(timeout=15)
    http.post(
        origin + "/api/auth/login",
        json={
            "username": os.environ["DERATE_CHECK_ADMIN_USER"],
            "password": os.environ["DERATE_CHECK_ADMIN_PASSWORD"],
        },
    ).raise_for_status()
    http.headers["Authorization"] = "Bearer " + os.environ["DERATE_CHECK_API_KEY"]
    return http
