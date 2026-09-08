"""A model publisher's own mark, resolved once by the coordinator and cached.

The Models grid draws ~90 cards from ~45 distinct publishers, and until now
every browser resolved every one of them itself, on every page load, against
``huggingface.co/api/{organizations,users}/{name}/overview`` -- unauthenticated
and rate limited hard. Forty-five lookups is over that limit on its own, so the
grid reliably 429ed itself: the client backed off 60s and doubled to half an
hour, and for that whole window every card fell back to two letters. Two tabs,
or one reload, was enough to trigger it. What made it look intermittent rather
than broken is that the limit is a burst window -- it recovers on its own, so
the same page is fine ten minutes later and letters again after a reload.

The coordinator resolves instead, for the reasons ``providers/logos.py`` gives
for provider marks -- it is the machine with egress, and nothing is handed to a
third party -- plus the one that is specific to this path: it resolves an owner
ONCE, ever, and keeps it on disk. Forty-five lookups spread across every
browser, tab and reload in the estate collapse to forty-five lookups total, and
a coordinator restart re-reads them from ``data_dir()/avatars`` rather than
asking again. That is the whole fix; the rate limit is not worked around, it
stops being approached.

Two things are deliberately NOT here.

  * No polling. A cold batch resolves under the caller's own request and the
    ones that do not make the deadline are simply left out of the answer, which
    the UI reads as "ask again", not as "no mark". A three-state answer is what
    lets the client retry without a timer of its own.
  * No blocking image route. ``/api/publishers/{owner}/avatar`` serves what is
    already cached and 404s otherwise; it never fetches. On HTTP/1.1 a browser
    opens about six connections per origin, so an image route that waited on
    the network would sit on all six and starve ``/api/*`` behind it.

Cosmetic, and written so it cannot become load bearing: every failure is a
miss, a miss draws a monogram, and nothing here is awaited by a request that
matters.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Iterable

from .. import paths
from .hf import hf_endpoint, hf_token

log = logging.getLogger(__name__)

#: Where an avatar URL is published. An organization and a personal namespace
#: are different records on the hub and there is no way to tell which a name is
#: without asking, so both are tried in this order -- organizations first
#: because that is what a model publisher almost always is.
_OVERVIEW_PATHS = ("organizations", "users")

#: How many hub lookups may be in flight at once, coordinator-wide.
#:
#: The point of this module is that the hub is asked as little as possible, and
#: a cold grid of forty-five owners arriving as forty-five simultaneous
#: requests is the exact shape that got us rate limited in the first place --
#: moving it from the browser to the server would have changed nothing.
_MAX_CONCURRENT = 6

#: A batch answers within this long or answers partially. Sized so a cold grid
#: usually completes in one round trip while never being the reason a request
#: hangs: at six wide and ~400ms a lookup, forty-five owners land in about
#: three seconds.
DEFAULT_DEADLINE_S = 6.0

_TIMEOUT_S = 6.0
_MAX_REDIRECTS = 3

_semaphore: asyncio.Semaphore | None = None
_cache = None
_locks: dict[str, asyncio.Lock] = {}
#: Warms that outlived the batch that started them. See `resolve_many`.
_running: set[asyncio.Task] = set()


def cache():
    """The shared avatar cache, created on first use.

    ``LogoCache`` is keyed by an arbitrary string and knows nothing about
    providers, so a publisher name goes in it unchanged. Reused rather than
    reimplemented because the hard part is the negative caching -- a publisher
    with no avatar must cost one lookup, not one per repaint -- and that is
    already written and already tested there.

    Imported inside the function so a build without ``httpx`` reachable still
    imports the resolver: this path is the only thing that stops working, and
    it degrades to a monogram.
    """
    global _cache
    if _cache is None:
        from ..providers.logos import LogoCache

        _cache = LogoCache(paths.data_dir() / "avatars")
    return _cache


def _gate() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
    return _semaphore


def _lock_for(owner: str) -> asyncio.Lock:
    """One in-flight fetch per publisher, so a grid of cards is one request."""
    lock = _locks.get(owner)
    if lock is None:
        lock = asyncio.Lock()
        _locks[owner] = lock
    return lock


def normalize(owner: str) -> str:
    """The cache key for a publisher name, or "" for something unusable.

    Case is preserved: the hub's overview endpoint is case sensitive and
    ``Qwen`` is not ``qwen``. Only the characters that cannot appear in a hub
    namespace are refused, which is also what keeps this off the filesystem's
    edge cases -- ``LogoCache`` sanitizes its own paths, but a name that never
    had a separator in it cannot reach that code in the first place.
    """
    name = (owner or "").strip()
    if not name or len(name) > 96:
        return ""
    if any(c in name for c in "/\\?#%") or name.startswith("."):
        return ""
    return name


async def _resolve_url(client, owner: str) -> str | None:
    """The hub's avatar URL for this publisher, or None if it has none.

    The token, when there is one, goes to the hub and to nothing else. That is
    the opposite of the rule in ``logos.py`` -- which sends no credential at
    all, because a provider's API key is for inference and a public favicon has
    no business carrying it -- and the difference is the point: raising this
    endpoint's rate limit is precisely what an ``HF_TOKEN`` is for, and it is
    already how every other hub call in ``resolver/`` behaves.
    """
    headers = {"user-agent": "derate/avatar-fetch"}
    token = hf_token()
    if token:
        headers["authorization"] = f"Bearer {token}"

    endpoint = hf_endpoint()
    for path in _OVERVIEW_PATHS:
        try:
            response = await client.get(
                f"{endpoint}/api/{path}/{owner}/overview", headers=headers
            )
        except Exception as exc:  # noqa: BLE001 - cosmetic path
            log.debug("avatar lookup for %s failed: %s", owner, exc)
            raise
        if response.status_code == 404:
            continue
        if response.status_code != 200:
            # A 429 or a 5xx is not an answer, and remembering it as "this
            # publisher has no mark" would keep the letters for a day. Raised
            # so the caller records a backoff instead.
            raise RuntimeError(f"hub returned {response.status_code} for {owner}")
        try:
            url = (response.json() or {}).get("avatarUrl")
        except ValueError:
            return None
        if not url:
            continue
        return url if str(url).startswith("http") else f"{endpoint}{url}"
    return None


async def fetch(owner: str) -> tuple[bytes, str] | None:
    """Resolve and download one publisher's mark. None means nothing to draw.

    Raises only when the hub gave a non-answer, which the caller turns into a
    backoff rather than into a permanent miss.
    """
    import httpx

    from ..providers.logos import fetch_image

    async with httpx.AsyncClient(
        timeout=_TIMEOUT_S,
        follow_redirects=True,
        max_redirects=_MAX_REDIRECTS,
        headers={"user-agent": "derate/avatar-fetch"},
    ) as client:
        url = await _resolve_url(client, owner)
        if not url:
            return None
        # The image itself lives on a CDN, which is a different host from the
        # API and is not rate limited -- and gets no credential, the way
        # logos.py fetches every mark.
        return await fetch_image(client, url)


async def _warm(owner: str) -> None:
    """Put one publisher's mark in the cache. Never raises."""
    lock = _lock_for(owner)
    async with lock:
        store = cache()
        if store.get(owner) is not None or store.is_fresh_miss(owner):
            return
        async with _gate():
            try:
                found = await fetch(owner)
            except Exception:  # noqa: BLE001 - cosmetic path
                log.debug("avatar warm failed for %s", owner, exc_info=True)
                store.remember_miss(owner, permanent=False)
                return
        if found is None:
            store.remember_miss(owner, permanent=True)
            return
        body, content_type = found
        store.put(owner, body, content_type)


async def resolve_many(
    owners: Iterable[str], *, deadline_s: float = DEFAULT_DEADLINE_S
) -> dict[str, bool]:
    """Which of these publishers have a mark. Three answers, not two.

    ``owner -> True``   the mark is cached and the image route will serve it.
    ``owner -> False``  this publisher has no mark, and asking again is waste.
    absent              not determined yet -- ask again in a moment.

    The third state is what makes a cold cache work without the client running
    a timer: a batch that cannot finish inside its deadline says so by leaving
    names out, rather than claiming a publisher has no avatar because the hub
    was slow once.

    Which is why the miss is read by KIND rather than with `is_fresh_miss`.
    Both a publisher with no avatar and a publisher we were rate limited asking
    about are fresh misses, and both mean "do not fetch again yet" -- but only
    the first is False. Reporting the second as False tells the client to stop
    asking, and one rate-limited minute would then keep that publisher on two
    letters, which is the entire failure this path was built to end.
    """
    wanted: list[str] = []
    seen: set[str] = set()
    for raw in owners:
        name = normalize(raw)
        if name and name not in seen:
            seen.add(name)
            wanted.append(name)

    store = cache()
    answer: dict[str, bool] = {}
    cold: list[str] = []
    for name in wanted:
        if store.get(name) is not None:
            answer[name] = True
        else:
            kind = store.miss_kind(name)
            if kind == "permanent":
                answer[name] = False
            elif kind == "transient":
                pass  # A backoff is running. Left out: see the docstring.
            else:
                cold.append(name)

    if cold:
        started = time.monotonic()
        tasks = [asyncio.create_task(_warm(name)) for name in cold]
        # `wait`, never `wait_for(gather(...))`: that one CANCELS what has not
        # finished when the deadline lands, which would throw away a lookup
        # that was already in flight and make the next batch pay for it again.
        # Here the stragglers keep running behind the answer and whoever asks
        # next finds them cached. `_running` holds the reference the event loop
        # needs -- a bare task is collectable, and a collected task is a fetch
        # that silently never happened.
        _, pending = await asyncio.wait(tasks, timeout=deadline_s)
        for task in pending:
            _running.add(task)
            task.add_done_callback(_running.discard)
        if pending:
            log.debug(
                "avatar batch hit its %.1fs deadline after %.1fs: %d of %d owners still resolving",
                deadline_s, time.monotonic() - started, len(pending), len(cold),
            )
        for name in cold:
            if store.get(name) is not None:
                answer[name] = True
            elif store.miss_kind(name) == "permanent":
                answer[name] = False
            # else: still resolving, or backing off. Left out on purpose.

    return answer
