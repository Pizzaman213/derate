"""The coordinator's half of the node shell.

Two routes, and the same rule the rest of this package follows: registered
above the ``/`` StaticFiles mount, or the SPA swallows them.

``GET /api/nodes/{node_id}/shell`` is a **relay**, not a composition -- the
same shape and the same reasoning as ``/v1/realtime``. Frames are pumped
between the browser and the node agent without being parsed, because the thing
on the wire is a terminal session and the coordinator has no business
understanding it. What the coordinator does own is finding the node.

Three checks guard it, and two audit rows record it. The coordinator requires
an admin session and refuses a cross-site ``Origin`` before it relays anything
(``authz.require_session``, closing 1008 before accept), and it forwards the
browser's ``Origin`` so the agent's own check sees it too. The shell key itself
is checked by the agent, at the far end, against a secret only that machine
holds -- so the coordinator cannot grant a terminal, cannot be tricked into
granting one, and does not need to be trusted with the key. It forwards the
subprotocol and lets the node refuse. Every attempt past the session check
writes ``shell_opened`` and, when it ends, ``shell_closed`` with duration, byte
counts and reason; the commands typed are not recorded.

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
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..registry import shell_config
from control_plane.contracts.auth import AuditAction

from . import authz

log = logging.getLogger("gateway.shell")

#: Matches the agent. A browser must offer it, and gets it echoed back.
SUBPROTOCOL = "derate-shell"

CLOSE_POLICY = 1008
CLOSE_UPSTREAM = 1011

#: How often an open terminal re-asks whether its session still exists. A
#: revoked session (sign-out everywhere, a removed admin, a changed password)
#: ends the pty within this long rather than whenever the browser lets go.
RECHECK_S = 5.0
#: The close reason a revoked session's terminal ends with, and its audit row's
#: ``ended_by``.
ENDED_REVOKED = "session_revoked"


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
            # bit of information about a secret, offered to any signed-in
            # session (this route is gated like every /api one), for no
            # benefit to a legitimate caller.
            "idle_timeout_s": shell_config.idle_timeout_s(),
        }

    if not shell_config.enabled():
        # No route at all. An absent path cannot be probed for, and cannot be
        # switched on by anything arriving over the network.
        return router

    @router.websocket("/api/nodes/{node_id}/shell")
    async def node_shell(websocket: WebSocket, node_id: str) -> None:
        # An admin session first, before anything about the node is looked up
        # or said: the agent's own key check is the second lock, not the only one.
        session = await authz.require_session(websocket, ctx.auth_stores)
        if session is None:
            return
        _audit(
            websocket,
            AuditAction.SHELL_OPENED,
            node_id,
            {
                "request_id": websocket.scope["state"].get("request_id"),
                "user_agent": websocket.scope["state"].get("user_agent"),
            },
        )
        began = time.monotonic()
        tally = {"bytes_in": 0, "bytes_out": 0, "reason": "refused", "ended_by": None}
        try:
            await _open(websocket, node_id, tally, session.username)
        finally:
            _audit(
                websocket,
                AuditAction.SHELL_CLOSED,
                node_id,
                {
                    "duration_s": round(time.monotonic() - began, 3),
                    "bytes_in": tally["bytes_in"],
                    "bytes_out": tally["bytes_out"],
                    "reason": tally["reason"],
                    "ended_by": tally["ended_by"],
                    # The relay is an opaque byte stream; recording keystrokes
                    # would capture every secret typed at the prompt.
                    "note": "commands are not recorded",
                },
            )

    async def _open(websocket: WebSocket, node_id: str, tally: dict, username: str) -> None:
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
                # The agent's check_origin sees the browser's origin, not none.
                origin=websocket.headers.get("origin") or None,
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
        tally["reason"] = "closed"

        async def still_signed_in() -> bool:
            live = await asyncio.to_thread(authz.current_session, ctx.auth_stores, websocket)
            return live is not None and live.username == username

        try:
            await _relay(websocket, upstream, tally, still_valid=still_signed_in)
        except BaseException as exc:
            tally["reason"] = f"error: {exc.__class__.__name__}"
            raise
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


def _audit(websocket: WebSocket, action, node_id: str, after: dict) -> None:
    try:
        authz.audit_request(websocket, action, "node", node_id, after=after)
    except Exception:
        log.error("could not write the %s audit row", action.value, exc_info=True)


def _reason(exc: Exception) -> str:
    """A close reason short enough for the frame and specific enough to act on.

    WebSocket close reasons are capped at 123 bytes, and a truncated one is
    worse than a short one.
    """
    text = str(exc) or exc.__class__.__name__
    return text[:110]


async def _relay(client: WebSocket, upstream, tally: dict, *, still_valid=None) -> None:
    """Pump both ways until either end stops, or the session behind it goes.

    Opaque in both directions: text stays text, bytes stay bytes, and nothing
    in between is decoded. Terminal output is frequently not valid UTF-8 on its
    own -- an escape sequence can split across reads -- so a relay that tried
    to be helpful about encoding would corrupt the stream.

    Every :data:`RECHECK_S` the awaitable ``still_valid()`` is asked whether
    the credential that opened the socket is still good; ``False`` closes it
    1008 ``session_revoked``. ``tally["ended_by"]`` records which side ended
    it: ``client``, ``node`` or ``session_revoked``.
    """

    async def to_upstream() -> None:
        while True:
            message = await client.receive()
            if message.get("type") in ("websocket.disconnect", "websocket.close"):
                return
            if (text := message.get("text")) is not None:
                tally["bytes_in"] += len(text.encode("utf-8", "surrogatepass"))
                await upstream.send(text)
            elif (data := message.get("bytes")) is not None:
                tally["bytes_in"] += len(data)
                await upstream.send(data)

    async def to_client() -> None:
        async for message in upstream:
            if isinstance(message, bytes):
                tally["bytes_out"] += len(message)
                await client.send_bytes(message)
            else:
                tally["bytes_out"] += len(message.encode("utf-8", "surrogatepass"))
                await client.send_text(message)

    tasks = {
        asyncio.create_task(to_upstream()): "client",
        asyncio.create_task(to_client()): "node",
    }
    first: set = set()
    try:
        while not first:
            first, _ = await asyncio.wait(
                tasks, timeout=RECHECK_S, return_when=asyncio.FIRST_COMPLETED
            )
            if first or still_valid is None:
                continue
            try:
                valid = await still_valid()
            except Exception:
                # An unreadable auth store is not a revocation; the next tick asks again.
                log.warning("could not re-check the shell's session", exc_info=True)
                continue
            if not valid:
                tally["ended_by"] = tally["reason"] = ENDED_REVOKED
                log.warning("shell closed: %s", ENDED_REVOKED)
                try:
                    await client.close(code=CLOSE_POLICY, reason=ENDED_REVOKED)
                except RuntimeError:
                    pass  # the browser went in the same instant
                return
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
    for task in first:
        tally["ended_by"] = tally["ended_by"] or tasks[task]
        exc = task.exception()
        if exc is not None and not isinstance(exc, WebSocketDisconnect):
            log.debug("shell relay ended: %s", exc)
