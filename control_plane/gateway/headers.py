"""Response headers every answer from the gateway carries.

The session cookie is SameSite=Strict, but "site" is scheme plus registrable
domain, not origin: a page on another port of the same host (another service
on this box, say) frames ``http://host:8088/settings`` with the cookie
attached, and the clicks it steals are same-origin POSTs that ``csrf.py``
correctly lets through. ``frame-ancestors 'none'`` and ``X-Frame-Options:
DENY`` are what close that.

The content policy is on HTML only -- it governs what a document may load,
and a JSON body or a static asset is not a document. Each allowance is there
for one thing in ``ui/src``:

- ``style-src 'unsafe-inline'``: React ``style={{...}}`` attributes throughout.
- ``img-src data: blob:``: canvas reads in ``tabs/models/dominant.ts``.
- ``media-src blob:``: generated speech in ``tabs/chat/Clip.tsx``.
- ``connect-src``: ``'self'``, plus ``DERATE_PUBLIC_ORIGIN`` and each
  ``DERATE_ALLOWED_ORIGINS`` entry with its ``ws``/``wss`` twin (the terminal
  and realtime relays are sockets). ``api/origin.ts`` lets an operator point
  the UI at another coordinator; that coordinator has to be named in one of
  the two, which is what ``origin.ts`` already tells them to do. It used to
  be ``http: https: ws: wss:`` -- any host -- which handed an injection a
  channel to send what it read anywhere.

``script-src`` is ``'self'`` and nothing else: no inline script, no eval.
Widening it is the one change here that turns any future injection into code
execution under the admin's session.

An SVG is a document too: opened top-level it runs its own ``<script>`` with
this origin's cookies. Provider logos and publisher avatars are fetched from
hosts an admin named, so every ``image/svg+xml`` answer carries
:data:`CSP_SVG` -- no script, sandboxed -- and is never a download.

``/api`` and ``/v1`` answers are ``cache-control: no-store`` unless the route
set its own: a ``secret_once`` body must not sit in any cache. Over https the
gateway also sends HSTS -- "https" meaning the request's scheme *or* an https
``DERATE_PUBLIC_ORIGIN``, the same test the session cookie's ``Secure`` uses,
so a TLS terminator the gateway cannot see does not switch HSTS off.

``permissions-policy`` denies camera and geolocation and keeps the
microphone for this origin (``tabs/chat`` records speech input);
``cross-origin-opener-policy`` cuts the window reference a page opened from
here would hand back.
"""

from __future__ import annotations

from urllib.parse import urlsplit


def _connect_sources(origins) -> list[str]:
    """Each configured origin, reduced to ``scheme://host[:port]``, plus its
    socket twin. Anything that is not an http(s) origin is dropped, and so is
    anything carrying CSP syntax -- this is interpolated into a header."""
    out: list[str] = []
    for raw in origins:
        parts = urlsplit((raw or "").strip())
        if parts.scheme not in ("http", "https") or not parts.netloc:
            continue
        if any(ch in parts.netloc for ch in " ;,'\"\t\r\n"):
            continue
        twin = "wss" if parts.scheme == "https" else "ws"
        for source in (f"{parts.scheme}://{parts.netloc}", f"{twin}://{parts.netloc}"):
            if source not in out:
                out.append(source)
    return out


def csp_html(origins=()) -> str:
    """The document policy; ``origins`` widen ``connect-src`` beyond 'self'."""
    connect = " ".join(["'self'", *_connect_sources(origins)])
    return (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self' data:; "
        f"connect-src {connect}; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'; object-src 'none'"
    )


#: The policy with nothing configured: connect-src is 'self' alone.
CSP_HTML = csp_html()

CSP_SVG = "default-src 'none'; style-src 'unsafe-inline'; sandbox"

HSTS = b"max-age=31536000"

STATIC_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(self), geolocation=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
)


class SecurityHeaders:
    """Adds STATIC_HEADERS to every HTTP response, and CSP_HTML to HTML ones.

    Plain ASGI for the reason ``authz.AuthGuard`` gives: ``BaseHTTPMiddleware``
    re-wraps every body, which a streamed completion pays for at the tail. This
    touches only the ``http.response.start`` message. A header the app already
    set is left as the app set it.
    """

    def __init__(self, app, settings=None) -> None:
        self.app = app
        public = getattr(settings, "public_origin", "") or ""
        self._public_https = urlsplit(public).scheme == "https"
        self._csp_html = csp_html(
            (public, *(getattr(settings, "allowed_origins", ()) or ()))
        ).encode()

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        gated = scope["path"].startswith(("/api/", "/v1/")) or scope["path"] in ("/api", "/v1")

        async def with_headers(message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {name.lower() for name, _ in headers}
                for name, value in STATIC_HEADERS:
                    if name not in present:
                        headers.append((name, value))
                content_type = next(
                    (v for n, v in headers if n.lower() == b"content-type"), b""
                ).lower()
                if b"content-security-policy" not in present:
                    if content_type.startswith(b"text/html"):
                        headers.append((b"content-security-policy", self._csp_html))
                    elif content_type.startswith(b"image/svg+xml"):
                        headers.append((b"content-security-policy", CSP_SVG.encode()))
                        if b"content-disposition" not in present:
                            headers.append((b"content-disposition", b"inline"))
                if gated and b"cache-control" not in present:
                    headers.append((b"cache-control", b"no-store"))
                # Read at response time, not on entry: AuthGuard (inside this
                # middleware) sets the scheme to https on an edge connection,
                # where the edge terminated TLS.
                https = scope.get("scheme") == "https" or self._public_https
                if https and b"strict-transport-security" not in present:
                    headers.append((b"strict-transport-security", HSTS))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, with_headers)
