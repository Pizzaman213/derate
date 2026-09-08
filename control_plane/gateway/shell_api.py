"""The coordinator's half of the node shell.

Two routes, and the same rule the rest of this package follows: registered
above the ``/`` StaticFiles mount, or the SPA swallows them.

``GET /api/nodes/{node_id}/shell`` is a **relay**, not a composition -- the
same shape and the same reasoning as ``/v1/realtime``. Frames are pumped
between the browser and the node agent without being parsed, because the thing
on the wire is a terminal session and the coordinator has no business
understanding it. What the coordinator does own is finding the node.

The credential is NOT checked here. It is checked by the agent, at the far end,
against a secret only that machine holds -- so the coordinator cannot grant a
session, cannot be tricked into granting one, and does not need to be trusted
with the key. It forwards the subprotocol and lets the node refuse.

There is one deliberate asymmetry with the kill route next door. That one has
the coordinator fetch the cluster token and present it on the caller's behalf,
which is a confused deputy: an anonymous request gets a credentialed action.
This route does the opposite on purpose. The shell key never passes through the
coordinator's own credential store, so relaying it is carrying an envelope, not
signing one.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..registry import shell_config

log = logging.getLogger("gateway.shell")

#: Matches the agent. A browser must offer it, and gets it echoed back.
SUBPROTOCOL = "derate-shell"

CLOSE_POLICY = 1008
CLOSE_UPSTREAM = 1011


def create_router(ctx) -> APIRouter:
    router = APIRouter()

    @router.get("/api/shell/status")
    async def shell_status() -> dict:
        """Whether a terminal can be opened, and if not, why not.

        Always present, even when the shell is off, so the UI can render a
        sentence naming the variable instead of a button that fails. It reports
        the coordinator's own setting: a node is free to differ, and the
        handshake is what settles it.
        """
        return {
            "enabled": shell_config.enabled(),
            "reason": (
                ""
                if shell_config.enabled()
                else "DERATE_SHELL is not set on the coordinator."
            ),
            # Never the key, and never whether one is configured -- that is a
            # bit of information about a secret, offered to an unauthenticated
            # caller, for no benefit to a legitimate one.
            "idle_timeout_s": shell_config.idle_timeout_s(),
        }

    if not shell_config.enabled():
        # No route at all. An absent path cannot be probed for, and cannot be
        # switched on by anything arriving over the network.
        return router

    @router.websocket("/api/nodes/{node_id}/shell")
    async def node_shell(websocket: WebSocket, node_id: str) -> None:
        agent_url = _agent_url(ctx, node_id)
        if not agent_url:
            # Refused before accept, so the caller sees an HTTP status rather
            # than a socket that opens and shuts with nothing to act on.
            await websocket.close(
                code=CLOSE_POLICY, reason=f"No agent address is known for {node_id}."
            )
            return

        offered = websocket.headers.get("sec-websocket-protocol", "")
        target = agent_url.rstrip("/").replace("http://", "ws://", 1).replace(
            "https://", "wss://", 1
        ) + "/agent/shell"

        try:
            import websockets
        except Exception:  # pragma: no cover - pinned in requirements.txt
            await websocket.close(code=CLOSE_UPSTREAM, reason="websockets is missing.")
            return

        subprotocols = [p.strip() for p in offered.split(",") if p.strip()]
        try:
            upstream = await websockets.connect(
                target,
                subprotocols=subprotocols or None,
                open_timeout=10,
                ping_interval=20,
                max_size=None,
            )
        except Exception as exc:
            # The node refused, or is unreachable. Its refusal is the useful
            # sentence, and it arrives as a handshake failure rather than a
            # body -- so the reason is the exception's own text, not a rewrite.
            await websocket.close(code=CLOSE_POLICY, reason=_reason(exc))
            return

        await websocket.accept(subprotocol=subprotocols[0] if subprotocols else None)
        try:
            await _relay(websocket, upstream)
        finally:
            await upstream.close()

    return router


def _agent_url(ctx, node_id: str) -> str | None:
    lookup = getattr(ctx.deps.registry, "agent_url", None)
    if not callable(lookup):
        return None
    try:
        return lookup(node_id)
    except Exception:
        return None


def _reason(exc: Exception) -> str:
    """A close reason short enough for the frame and specific enough to act on.

    WebSocket close reasons are capped at 123 bytes, and a truncated one is
    worse than a short one.
    """
    text = str(exc) or exc.__class__.__name__
    return text[:110]


async def _relay(client: WebSocket, upstream) -> None:
    """Pump both ways until either end stops.

    Opaque in both directions: text stays text, bytes stay bytes, and nothing
    in between is decoded. Terminal output is frequently not valid UTF-8 on its
    own -- an escape sequence can split across reads -- so a relay that tried
    to be helpful about encoding would corrupt the stream.
    """

    async def to_upstream() -> None:
        while True:
            message = await client.receive()
            if message.get("type") in ("websocket.disconnect", "websocket.close"):
                return
            if (text := message.get("text")) is not None:
                await upstream.send(text)
            elif (data := message.get("bytes")) is not None:
                await upstream.send(data)

    async def to_client() -> None:
        async for message in upstream:
            if isinstance(message, bytes):
                await client.send_bytes(message)
            else:
                await client.send_text(message)

    first, pending = await asyncio.wait(
        [asyncio.create_task(to_upstream()), asyncio.create_task(to_client())],
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    for task in first:
        exc = task.exception()
        if exc is not None and not isinstance(exc, WebSocketDisconnect):
            log.debug("shell relay ended: %s", exc)
