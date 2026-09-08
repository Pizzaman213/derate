"""A provider's own mark, fetched once by the coordinator and cached.

The cluster screen draws providers as a bus rather than a machine, and until
now the only thing distinguishing one from another on it was the provider id in
9px mono. A vendor's own logo says whose hardware it is far more directly, which
is what let the routing-boundary rule come out of that drawing entirely.

The coordinator fetches, not the browser. Three reasons, in order of how often
they bite:

  * The console is frequently the one machine with no egress -- a laptop on the
    lab LAN pointed at a coordinator that does have it. A browser fetch there
    draws nothing on a cluster that is working perfectly.
  * No provider domain is handed to a third party. There are favicon services
    that would answer all seven kinds from one URL shape; using one would mean
    telling that service which vendors this operator has configured.
  * Same-origin means no CORS, so the UI needs none of the machinery
    ``ui/src/tabs/models/owner.ts`` carries for HuggingFace avatars.

This is cosmetic, and the whole module is written so it cannot become load
bearing: every failure is a 404, a 404 draws a monogram, and nothing here is
ever awaited by a request that matters.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .. import paths
from ..contracts.providers import Provider, ProviderKind
from .kinds import spec_for

log = logging.getLogger(__name__)

#: Where each kind's mark actually lives, tried in order.
#:
#: Deliberately NOT a field on ``KindSpec``: that table's docstring is explicit
#: that it holds what a provider needs before anyone configures anything, and a
#: brand asset is not that. Keeping it here also keeps ``kinds_public()`` at the
#: nine fields the UI's ``ProviderKindSpec`` mirrors.
#:
#: These are FULL URLS rather than an origin plus a guessed path, because
#: guessing does not work. Every line below was probed by hand on 2026-09-07
#: and the results are why the obvious construction is not what is here:
#:
#:   api.openai.com/favicon.ico      404  -- no API host serves a mark
#:   openai.com/favicon.ico          403  -- Cloudflare refuses non-browsers
#:   together.ai/favicon.ico         404, and 222 KB of SPA HTML in the body
#:   ollama.com/favicon.ico          404  -- the mark is under /public
#:   anthropic.com/favicon.ico       301  -- to www, so www is pinned
#:
#: A URL that rots is a monogram, never an error, which is what lets this table
#: carry a content-hashed CDN path for the one vendor that offers nothing else.
_BRAND: dict[ProviderKind, tuple[str, ...]] = {
    ProviderKind.OPENROUTER: (
        "https://openrouter.ai/apple-touch-icon.png",  # 200 png 5891
        "https://openrouter.ai/favicon.ico",           # 200 ico 2880
    ),
    ProviderKind.OPENAI: (
        # Content-hashed and so certain to rot eventually. Kept anyway: it is
        # the only URL that answers, and the plain one behind it is here for
        # the day Cloudflare stops refusing us.
        "https://cdn.oaistatic.com/assets/favicon-180x180-od45eci6.webp",  # 200 webp
        "https://openai.com/favicon.ico",              # 403 today
    ),
    ProviderKind.ANTHROPIC: (
        "https://www.anthropic.com/favicon.ico",       # 200 ico 15086
    ),
    ProviderKind.TOGETHER: (
        # The one API host that does serve a mark, which is lucky, because
        # together.ai answers a 404 with a 222 KB HTML document.
        "https://api.together.xyz/favicon.ico",        # 200 ico 15086
    ),
    ProviderKind.GROQ: (
        "https://groq.com/apple-touch-icon.png",       # 200 png 6878
        "https://groq.com/favicon.svg",
    ),
    ProviderKind.OLLAMA: (
        "https://ollama.com/public/apple-touch-icon.png",  # 200 png 5579
    ),
}

#: Probed against an origin the operator configured, where there is no table to
#: consult. SVG first because it is the only one that stays sharp at the 11
#: units the bus draws it at; .ico last because it is the one every server has.
_DERIVED_PATHS = ("/favicon.svg", "/apple-touch-icon.png", "/favicon.ico", "/favicon.png")

#: An image this size is not a favicon, and the point of a cap is that we stop
#: reading rather than discover the size afterwards.
MAX_LOGO_BYTES = 256 * 1024

#: An HTML error page served as a logo would render as a broken image with
#: nothing in the log to explain it, so the type is checked rather than trusted.
_ALLOWED_TYPES = frozenset(
    {
        "image/svg+xml",
        "image/png",
        "image/x-icon",
        "image/vnd.microsoft.icon",
        "image/jpeg",
        "image/webp",
    }
)

#: How much of the body is enough to know what it is.
_SNIFF_BYTES = 1024


def sniff(prefix: bytes) -> str | None:
    """What these BYTES are, or None. Never what the server said they are.

    The header is not enough on its own. `together.ai/favicon.ico` answers a
    404 with 222 KB of SPA HTML, and a server that mislabels that as an image
    would otherwise put a broken picture on the cluster screen with nothing in
    the log to explain it. The type we serve is the type we recognised.
    """
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if prefix.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP":
        return "image/webp"
    # ICONDIR: reserved 0, type 1 (icon), then a non-zero image count.
    if prefix.startswith(b"\x00\x00\x01\x00") and prefix[4:6] != b"\x00\x00":
        return "image/x-icon"
    head = prefix.lstrip(b"\xef\xbb\xbf \t\r\n")[:_SNIFF_BYTES].lower()
    if head.startswith((b"<svg", b"<?xml", b"<!doctype svg")) and b"<svg" in head:
        # An HTML page with an inline <svg> somewhere in it is not an SVG, so
        # the opening token has to be one of these AND <svg> has to be near.
        return "image/svg+xml"
    return None

_TIMEOUT_S = 5.0
_MAX_REDIRECTS = 3

#: A mark does not change often, and a wrong one is visible immediately.
POSITIVE_TTL_S = 30 * 24 * 3600.0
#: A vendor with no favicon still has none tomorrow. Long, because the cost of
#: being wrong is a monogram for a day and the cost of being eager is a request
#: per render.
PERMANENT_MISS_TTL_S = 24 * 3600.0
#: A timeout or a 5xx is not an answer. Back off and double, so a rate limit
#: recovers on its own rather than being hammered -- the same split owner.ts
#: makes between a 404 and a 502.
TRANSIENT_BASE_S = 60.0
TRANSIENT_MAX_S = 30 * 60.0


def brand_origin(provider: Provider) -> str | None:
    """Which origin this provider's mark comes from, or None when there is none.

    A branded kind still pointed at its own default answers from the brand
    table. Anything else -- CUSTOM, or an Ollama the operator aimed at a box on
    their LAN -- answers from the origin they configured, because that box is
    theirs and ollama.com's logo would be a claim about it that nobody made.
    """
    urls = candidate_urls(provider)
    return _origin_of(urls[0]) if urls else None


def _origin_of(base_url: str) -> str | None:
    """scheme://host[:port], or None for anything that is not an http(s) URL.

    Path, query, fragment and any userinfo are dropped. The only thing taken
    from an operator's base_url is which machine it names -- a path they
    configured for inference is never a path we then fetch.
    """
    parts = urlsplit((base_url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname}{port}"


def candidate_urls(provider: Provider) -> list[str]:
    """Every URL worth trying for this provider, in order."""
    kind = ProviderKind(provider.kind)
    default = spec_for(kind).base_url
    branded = _BRAND.get(kind)
    if branded and (provider.base_url or "").strip() in ("", default):
        return list(branded)
    origin = _origin_of(provider.base_url)
    if not origin:
        return []
    return [f"{origin}{path}" for path in _DERIVED_PATHS]


@dataclass
class _Entry:
    """One cache slot. ``body`` is None for a miss."""

    body: bytes | None
    content_type: str
    stored_at: float
    #: How many transient failures in a row. Zero for a hit or a hard miss.
    failures: int = 0
    #: When this entry stops being the answer.
    expires_at: float = 0.0


def _suffix_for(content_type: str) -> str:
    return {
        "image/svg+xml": "svg",
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/webp": "webp",
    }.get(content_type, "ico")


class LogoCache:
    """Bytes and content-type per provider, with negative entries.

    Two tiers, the way ``resolver/cache.py`` does it: an in-process dict in
    front of a directory, so a coordinator restart does not re-fetch seven
    vendors and a cold process still answers from disk.

    A miss is remembered as emphatically as a hit. That is the whole reason
    this class exists rather than a plain dict: without negative caching, a
    vendor with no favicon is a network request every time the Cluster tab
    re-renders.
    """

    def __init__(self, directory: Path | str | None = None) -> None:
        self.directory = Path(directory) if directory else paths.data_dir() / "logos"
        self._memory: dict[str, _Entry] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _path(self, provider_id: str, content_type: str) -> Path:
        return self.directory / f"{_safe(provider_id)}.{_suffix_for(content_type)}"

    def lock_for(self, provider_id: str) -> asyncio.Lock:
        """One in-flight fetch per provider, so a grid of rows is one request."""
        lock = self._locks.get(provider_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[provider_id] = lock
        return lock

    def get(self, provider_id: str, now: float | None = None) -> tuple[bytes, str] | None:
        """The cached mark, or None for "no answer yet, and do not block"."""
        now = time.time() if now is None else now
        entry = self._memory.get(provider_id)
        if entry is None:
            entry = self._from_disk(provider_id, now)
        if entry is None or now >= entry.expires_at:
            return None
        if entry.body is None:
            return None
        return entry.body, entry.content_type

    def is_fresh_miss(self, provider_id: str, now: float | None = None) -> bool:
        """Whether a miss is still current, so the caller skips re-fetching."""
        return self.miss_kind(provider_id, now) is not None

    def miss_kind(self, provider_id: str, now: float | None = None) -> str | None:
        """Which KIND of current miss this is, or None when there is not one.

        ``"permanent"`` -- it was asked and there is nothing there.
        ``"transient"`` -- it could not be asked; a backoff is running.

        `is_fresh_miss` collapses the two because its only question is "should
        I fetch right now", and the answer is no either way. A caller that
        REPORTS the miss needs them apart: telling a client "this publisher has
        no mark" because the hub rate limited us once is how a cosmetic failure
        becomes a sticky one, and it is precisely the bug the avatar path
        exists to end.
        """
        now = time.time() if now is None else now
        entry = self._memory.get(provider_id)
        if entry is None or entry.body is not None or now >= entry.expires_at:
            return None
        return "transient" if entry.failures else "permanent"

    def _from_disk(self, provider_id: str, now: float) -> _Entry | None:
        # Only hits are on disk. A miss is process-local on purpose: it is
        # cheap to rediscover and keeping it would mean a vendor that ADDS a
        # favicon stays blank until somebody clears a directory by hand.
        for suffix in ("svg", "png", "jpg", "webp", "ico"):
            path = self.directory / f"{_safe(provider_id)}.{suffix}"
            try:
                stat = path.stat()
                body = path.read_bytes()
            except OSError:
                continue
            content_type = next(
                (t for t in _ALLOWED_TYPES if _suffix_for(t) == suffix), "image/x-icon"
            )
            entry = _Entry(
                body=body,
                content_type=content_type,
                stored_at=stat.st_mtime,
                expires_at=stat.st_mtime + POSITIVE_TTL_S,
            )
            self._memory[provider_id] = entry
            return entry
        return None

    def put(self, provider_id: str, body: bytes, content_type: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._memory[provider_id] = _Entry(
            body=body,
            content_type=content_type,
            stored_at=now,
            expires_at=now + POSITIVE_TTL_S,
        )
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._path(provider_id, content_type).write_bytes(body)
        except OSError as exc:
            # A read-only estate must not cost us the in-memory copy. Same
            # tradeoff identity.py and roster.py make, and the same reason:
            # a node on a read-only volume should keep working.
            log.debug("could not cache %s logo on disk: %s", provider_id, exc)

    def remember_miss(
        self, provider_id: str, *, permanent: bool, now: float | None = None
    ) -> None:
        now = time.time() if now is None else now
        if permanent:
            self._memory[provider_id] = _Entry(
                body=None, content_type="", stored_at=now,
                expires_at=now + PERMANENT_MISS_TTL_S,
            )
            return
        prev = self._memory.get(provider_id)
        failures = (prev.failures + 1) if prev and prev.body is None else 1
        backoff = min(TRANSIENT_BASE_S * (2 ** (failures - 1)), TRANSIENT_MAX_S)
        self._memory[provider_id] = _Entry(
            body=None, content_type="", stored_at=now,
            failures=failures, expires_at=now + backoff,
        )

    def clear(self) -> None:
        self._memory.clear()


def _safe(provider_id: str) -> str:
    """A provider id is operator-chosen; it never becomes a path segment raw."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in provider_id)[:64]


async def fetch_logo(provider: Provider) -> tuple[bytes, str] | None:
    """Try each candidate URL once. None means "nothing to draw".

    Every limit here exists because the response is a file we are about to
    hand to a browser:

      * the size cap is enforced WHILE reading, so a hostile or broken server
        cannot make us hold 2 GB before we notice;
      * redirects are followed but bounded, and the content type is re-checked
        after them rather than before;
      * the type allowlist is what stops an HTML error page rendering as a
        broken image with nothing in the log to explain it.

    No credential is ever sent. The provider's API key is for inference; a
    public favicon does not need one and spending one here would put key
    material on a request that had no business carrying it.
    """
    import httpx

    urls = candidate_urls(provider)
    if not urls:
        return None

    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT_S,
            follow_redirects=True,
            max_redirects=_MAX_REDIRECTS,
            # No cookie jar, and no headers carried over from anywhere.
            headers={"user-agent": "derate/logo-fetch"},
        ) as client:
            for url in urls:
                found = await fetch_image(client, url)
                if found:
                    return found
    except Exception as exc:  # noqa: BLE001 - cosmetic path, never propagates
        log.debug("logo fetch for %s failed: %s", provider.provider_id, exc)
        return None
    return None


async def fetch_image(client, url: str) -> tuple[bytes, str] | None:
    """One candidate URL as (bytes, content type). None means "not this one".

    Never raises. Public because the publisher-avatar path in
    ``resolver/avatars.py`` is fetching the same kind of thing from the same
    kind of place -- somebody else's server, over a link we do not control,
    into a response we are about to hand a browser -- and the three rules that
    make that safe (cap while reading, sniff the bytes rather than trust the
    header, refuse anything not on the allowlist) should have exactly one
    implementation. Duplicating them is how one copy quietly loses the cap.
    """
    try:
        async with client.stream("GET", url) as response:
            if response.status_code != 200:
                return None
            # A declared non-image is refused before the body is read at all,
            # which is what keeps a 222 KB HTML "404" to a single chunk.
            declared = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if declared and not declared.startswith("image/"):
                return None
            declared_length = response.headers.get("content-length")
            if declared_length and declared_length.isdigit() and int(declared_length) > MAX_LOGO_BYTES:
                return None

            chunks: list[bytes] = []
            total = 0
            kind: str | None = None
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > MAX_LOGO_BYTES:
                    log.debug("logo at %s exceeded %d bytes; refused", url, MAX_LOGO_BYTES)
                    return None
                chunks.append(chunk)
                # As soon as there is enough to identify, identify: a body that
                # is not an image stops downloading here rather than in full.
                if kind is None and total >= 16:
                    kind = sniff(b"".join(chunks)[:_SNIFF_BYTES])
                    if kind is None and total >= _SNIFF_BYTES:
                        return None
            body = b"".join(chunks)
            if not body:
                return None
            kind = kind or sniff(body[:_SNIFF_BYTES])
            if kind is None or kind not in _ALLOWED_TYPES:
                return None
            return body, kind
    except Exception as exc:  # noqa: BLE001
        log.debug("logo candidate %s failed: %s", url, exc)
        return None
