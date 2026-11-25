"""The realtime relay: /v1/realtime, over a WebSocket.

Everything else on the /v1 surface is one request and one response, which is
what lets `openai_api._serve` own failover, parking and the circuit breaker for
all of it. A realtime session is neither: it is a long-lived bidirectional
conversation with its own event protocol, and none of those mechanisms apply to
it. A session cannot be re-offered to another target halfway through -- the
upstream holds conversation state we never saw -- so this file deliberately
shares no machinery with the proxy path beyond the router's own target lookup.

**What this is.** A relay. The client's socket is joined to an upstream that
already speaks the OpenAI realtime protocol, and frames are pumped both ways
without being parsed. The protocol stays the upstream's problem, which is the
only reason this is 200 lines rather than several thousand.

**What this is not.** It does not synthesise a realtime session out of local
speech-to-text, chat and text-to-speech deployments. That means implementing
the protocol ourselves -- roughly thirty event types, server-side voice
activity detection for turn-taking, audio buffering and resampling, and
barge-in -- and none of it exists here. A local model is therefore refused with
a message that says so, rather than accepted into a session that would never
produce audio.
"""

from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import urlencode, urlparse, urlunparse

from starlette.websockets import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect as _connect

from control_plane.contracts import TargetKind
from control_plane.contracts.auth import KeyStatus

log = logging.getLogger("gateway.realtime")

#: Close codes. 1008 is "policy violation", which is the closest thing the
#: WebSocket spec has to a 4xx and what a client library surfaces as a refusal
#: rather than a network blip.
CLOSE_POLICY = 1008
CLOSE_UPSTREAM = 1011  # internal error: we could not reach the upstream

#: How often an open session re-asks whether its key is still active. A key
#: disabled (one key, or every key of an org) ends the session within this
#: long rather than when the client hangs up.
RECHECK_S = 5.0
#: The close reason a disabled key's session ends with.
ENDED_KEY_DISABLED = "key_disabled"

#: Sub-protocols a client may offer. Passed through untouched when the upstream
#: accepts one; realtime clients use these to carry auth in browsers.
_PASSTHROUGH_HEADERS = ("openai-beta",)


class _NoRedirect(_connect):
    """``websockets.connect`` that follows no redirect. The library follows a
    3xx handshake up to ten times and re-sends ``additional_headers`` -- the
    provider's bearer -- on every hop, to whatever host ``Location`` names.
    Returning the exception makes the handshake fail with it instead."""

    def process_redirect(self, exc: Exception) -> Exception:
        return exc


def realtime_url(base_url: str, model: str) -> str:
    """The upstream's realtime address, derived from its normal base URL.

    Providers publish one base URL for everything, so the scheme is swapped for
    its WebSocket equivalent and the path extended. Query parameters already on
    the base URL are preserved; the model is appended rather than replacing
    them.
    """
    parsed = urlparse(base_url)
    scheme = "wss" if parsed.scheme in ("https", "wss") else "ws"
    # Strip the path's own trailing slash rather than the whole URL's: a base
    # URL carrying a query ends in the query, so rstrip on the string leaves
    # "/v1/" intact and the join produces "/v1//realtime".
    path = f"{parsed.path.rstrip('/')}/realtime"
    query = urlencode({"model": model})
    if parsed.query:
        query = f"{parsed.query}&{query}"
    return urlunparse((scheme, parsed.netloc, path, "", query, ""))


async def _pump(source, sink, label: str) -> None:
    """Copy frames one way until either end goes.

    Text and binary are forwarded as themselves: a realtime protocol carries
    JSON events as text and audio as binary, and collapsing the two would
    corrupt one of them.
    """
    try:
        while True:
            message = await source.receive()
            kind = message.get("type", "")
            if kind in ("websocket.disconnect", "websocket.close"):
                break
            if (text := message.get("text")) is not None:
                await sink.send(text)
            elif (data := message.get("bytes")) is not None:
                await sink.send(data)
    except (WebSocketDisconnect, RuntimeError):
        pass  # one side hung up; the other is closed by the caller
    except Exception:
        log.debug("realtime pump ended: %s", label, exc_info=True)


class _ClientSide:
    """Adapts Starlette's WebSocket to the two calls _pump needs."""

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws

    async def receive(self) -> dict:
        return await self._ws.receive()

    async def send(self, payload) -> None:
        if isinstance(payload, bytes):
            await self._ws.send_bytes(payload)
        else:
            await self._ws.send_text(payload)


class _UpstreamSide:
    """Adapts a `websockets` client connection to the same two calls."""

    def __init__(self, conn) -> None:
        self._conn = conn

    async def receive(self) -> dict:
        frame = await self._conn.recv()
        if isinstance(frame, bytes):
            return {"type": "websocket.receive", "bytes": frame}
        return {"type": "websocket.receive", "text": frame}

    async def send(self, payload) -> None:
        await self._conn.send(payload)


def key_active(stores, key_id: str, *, now: float | None = None) -> bool:
    """Whether the key that opened a session may still use it: present,
    ``active``, unexpired and in an org that still exists. Synchronous (a
    store read); the relay runs it off the loop."""
    keys = stores.keys
    key = keys.get_key(key_id)
    if key is None or key.status is not KeyStatus.ACTIVE:
        return False
    if getattr(key, "deleted_at", None) is not None:
        return False
    now = time.time() if now is None else now
    if key.expires_at is not None and key.expires_at <= now:
        return False
    org = keys.get_org(key.org_id)
    return org is not None and org.deleted_at is None


async def relay(ctx, websocket: WebSocket, *, still_valid=None) -> str | None:
    """Join a client's realtime socket to a provider that speaks the protocol.

    Refusals happen before `accept()` wherever possible: a client that is
    rejected during the handshake sees an HTTP status, which is far easier to
    act on than a socket that opens and immediately closes.

    Every :data:`RECHECK_S` the awaitable ``still_valid()`` is asked whether
    the key that opened the session is still good; ``False`` closes the socket
    1008 ``key_disabled``. Returns how an accepted session ended (``client``,
    ``upstream`` or ``key_disabled``), ``None`` when it was refused.
    """
    model = websocket.query_params.get("model")
    if not model:
        await websocket.close(code=CLOSE_POLICY, reason="a 'model' query parameter is required")
        return

    index = ctx.router.index()
    if model not in index.targets:
        await websocket.close(code=CLOSE_POLICY, reason=f"unknown model '{model}'")
        return

    selection = ctx.router.select(model)
    if selection is None:
        await websocket.close(
            code=CLOSE_POLICY, reason=f"no target for '{model}' is admitting requests"
        )
        return

    # A local runtime does not speak this protocol, and chaining STT -> chat ->
    # TTS to fake it is a different project (see the module docstring). Say so
    # plainly instead of opening a session that would never produce audio.
    if selection.target.kind is not TargetKind.REMOTE or selection.provider is None:
        await websocket.close(
            code=CLOSE_POLICY,
            reason=(
                f"'{model}' is served locally, and this build relays realtime "
                "sessions to a provider that implements them rather than "
                "composing one from local deployments"
            ),
        )
        return

    # The provider's own gates -- daily budget first among them -- apply to a
    # session exactly as to a completion (finding M4). Asked before
    # connecting: once audio flows nothing here can stop it short.
    admitting = getattr(ctx.deps.providers, "admitting", None)
    provider_id = selection.provider.provider_id
    if admitting is not None:
        try:
            open_door = admitting(provider_id)
        except Exception:
            log.debug("could not ask provider %s whether it admits", provider_id, exc_info=True)
            open_door = False
        if not open_door:
            await websocket.close(
                code=CLOSE_POLICY,
                # A close reason is capped at 123 bytes by the protocol.
                reason=(
                    f"provider '{provider_id}' is not admitting: daily budget "
                    "reached, rate limited or unhealthy"
                ),
            )
            return

    upstream_id = selection.model.upstream_id if selection.model else model
    url = realtime_url(selection.provider.base_url, upstream_id)

    try:
        api_key = ctx.deps.providers.resolve_key(selection.provider.provider_id)
    except Exception:
        # Never echo anything about key material.
        log.warning(
            "could not resolve api key for provider %s",
            selection.provider.provider_id,
        )
        await websocket.close(
            code=CLOSE_UPSTREAM,
            reason=f"provider '{selection.provider.provider_id}' has no usable credential",
        )
        return

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    for name in _PASSTHROUGH_HEADERS:
        value = websocket.headers.get(name)
        if value:
            headers[name] = value

    try:
        connection = await _NoRedirect(url, additional_headers=headers)
    except Exception as exc:
        log.warning("realtime upstream unreachable: %s (%s)", url, type(exc).__name__)
        await websocket.close(
            code=CLOSE_UPSTREAM,
            reason=f"upstream for '{model}' is unreachable: {type(exc).__name__}",
        )
        return

    await websocket.accept()
    client, upstream = _ClientSide(websocket), _UpstreamSide(connection)
    # Both directions run until either ends. The first to finish cancels the
    # other: a half-open relay would leave a client waiting on a socket whose
    # far end is already gone.
    tasks = {
        asyncio.create_task(_pump(client, upstream, "client->upstream")): "client",
        asyncio.create_task(_pump(upstream, client, "upstream->client")): "upstream",
    }
    ended_by: str | None = None
    close_code, close_reason = 1000, ""
    try:
        while ended_by is None:
            done, _ = await asyncio.wait(
                tasks, timeout=RECHECK_S, return_when=asyncio.FIRST_COMPLETED
            )
            if done:
                ended_by = tasks[next(iter(done))]
                break
            if still_valid is None:
                continue
            try:
                valid = await still_valid()
            except Exception:
                # An unreadable key store is not a revocation; ask again next tick.
                log.warning("could not re-check the realtime session's key", exc_info=True)
                continue
            if not valid:
                ended_by = ENDED_KEY_DISABLED
                close_code, close_reason = CLOSE_POLICY, ENDED_KEY_DISABLED
                log.warning("realtime session closed: %s", ENDED_KEY_DISABLED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await connection.close()
        try:
            await websocket.close(code=close_code, reason=close_reason)
        except RuntimeError:
            pass  # already closed by the client
    return ended_by
