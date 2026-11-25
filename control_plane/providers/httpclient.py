"""The cookie jar every long-lived upstream ``httpx`` client is built with.

The proxy's per-origin clients and the provider service's client serve every
key holder at once, and httpx keeps one cookie jar per client: an upstream's
``Set-Cookie`` was stored and sent back on every later request through that
client, whoever made it -- linking tenants at the upstream and letting it fix a
session on all of them.
"""

from __future__ import annotations

import http.cookiejar


def no_cookies() -> http.cookiejar.CookieJar:
    """A jar that stores nothing. An empty ``allowed_domains`` accepts cookies
    from no domain (``None`` would mean every domain), so nothing an upstream
    sets is ever replayed. The jar itself, never ``httpx.Cookies``: handed a
    ``Cookies``, the client copies it into a fresh default jar."""
    return http.cookiejar.CookieJar(policy=http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))
