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
from urllib.parse import urlencode, urlparse, urlunparse

from starlette.websockets import WebSocket, WebSocketDisconnect

from control_plane.contracts import TargetKind

log = logging.getLogger("gateway.realtime")

#: Close codes. 1008 is "policy violation", which is the closest thing the
#: WebSocket spec has to a 4xx and what a client library surfaces as a refusal
#: rather than a network blip.
CLOSE_POLICY = 1008
CLOSE_UPSTREAM = 1011  # internal error: we could not reach the upstream

#: Sub-protocols a client may offer. Passed through untouched when the upstream
#: accepts one; realtime clients use these to carry auth in browsers.
_PASSTHROUGH_HEADERS = ("openai-beta",)


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


async def relay(ctx, websocket: WebSocket) -> None:
    """Join a client's realtime socket to a provider that speaks the protocol.

    Refusals happen before `accept()` wherever possible: a client that is
    rejected during the handshake sees an HTTP status, which is far easier to
    act on than a socket that opens and immediately closes.
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
        import websockets
    except ImportError:  # pragma: no cover - websockets ships with uvicorn[standard]
        await websocket.close(code=CLOSE_UPSTREAM, reason="websocket client library unavailable")
        return

    try:
        connection = await websockets.connect(url, additional_headers=headers)
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
    tasks = [
        asyncio.create_task(_pump(client, upstream, "client->upstream")),
        asyncio.create_task(_pump(upstream, client, "upstream->client")),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await connection.close()
        try:
            await websocket.close()
        except RuntimeError:
            pass  # already closed by the client
