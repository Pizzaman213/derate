"""Refuses a cross-site write before it reaches any /api or /v1 route.

The gateway has no per-request credential (00-architecture.md: "the /api
surface has no authentication"), so the only thing standing between "a page
you opened" and "a page that rewrote your cluster" is whether a browser sent
the request on your behalf. A GET can't do that -- navigation and
`<img>`/`<script>` tags can't set a method or a JSON body -- but a
same-origin `fetch` can, and so can a cross-origin one that stays inside the
CORS "simple request" shape (no preflight): `internal_api.py`'s handlers read
the body with `request.json()`, which parses whatever arrived regardless of
the `Content-Type` a simple request is limited to.

Comparing `Origin` to the request's own `Host` is the standard defense (OWASP:
"Verifying Origin With Standard Headers"). Neither header is settable by a
page's own script, so a page cannot forge agreement between them the way it
could forge a body or a same-origin script's own custom header.

A missing `Origin` is let through. Every browser sends one on every
state-changing request; its absence means the caller is not a browser --
curl, the openai SDK, a health check -- and none of those are what CSRF is
about.
"""

from __future__ import annotations

from urllib.parse import urlsplit

#: Paths this check applies to. Everything else -- the UI's own static
#: assets, /healthz -- takes no write and needs no Origin.
GUARDED_PREFIXES = ("/api", "/v1")

#: A request with one of these methods cannot carry a JSON body a page's own
#: script chose, so there is nothing here for this check to catch.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def is_cross_site_write(
    *,
    method: str,
    path: str,
    origin: str | None,
    request_scheme: str,
    request_netloc: str,
    allowed_origins: tuple[str, ...],
) -> bool:
    """True when this request must be refused before it reaches a route."""
    if method.upper() in SAFE_METHODS:
        return False
    if not path.startswith(GUARDED_PREFIXES):
        return False
    if origin is None:
        return False
    parsed = urlsplit(origin)
    if parsed.scheme == request_scheme and parsed.netloc == request_netloc:
        return False
    # The same escape hatch CORS already uses for "the UI is served from
    # somewhere else" (npm run dev, a coordinator reached by another name) --
    # an operator who named an origin there has already vouched for it.
    return origin not in allowed_origins
