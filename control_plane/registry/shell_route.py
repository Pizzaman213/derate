"""``GET /agent/shell`` -- the WebSocket half of the node shell.

Its own module, and that is not organisational tidiness. ``agent.py`` imports
FastAPI *inside* ``create_agent_app`` on purpose, so that importing the module
and unit-testing ``NodeAgent`` work on a machine with no web framework. It also
carries ``from __future__ import annotations``, so every annotation is a
string that FastAPI resolves against the **module's** globals -- and a
``WebSocket`` imported into a function's local scope is not there. The route
then looks to FastAPI like an ordinary handler with a required query parameter
called ``websocket``, and every handshake is refused with a validation error
about a missing field. It fails as a 403, which reads exactly like the
credential check working.

So: FastAPI is imported at the top here, and this module is imported only when
the shell is switched on. Both properties survive.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import WebSocket, WebSocketDisconnect

from . import shell, shell_config

log = logging.getLogger(__name__)

#: The subprotocol a client offers, carrying the key as its second entry.
#: A header would be the natural home for a credential, but a browser cannot
#: set one on a WebSocket handshake; a query parameter can, and lands in access
#: logs. This is what is left, and it is what the platform intends.
SUBPROTOCOL = "derate-shell"


def install(app, node_agent) -> None:
    """Register the route on the agent app."""

    @app.websocket("/agent/shell")
    async def agent_shell(websocket: WebSocket) -> None:
        key = subprotocol_key(websocket)
        try:
            shell.check_origin(websocket.headers.get("origin"))
            shell.check_key(key)
            if not node_agent.claim_shell():
                raise shell.ShellRefused(
                    1008, "A shell session is already open on this node."
                )
        except shell.ShellRefused as exc:
            # Refused during the handshake, before accept: Starlette turns a
            # close-before-accept into an HTTP status, which a caller can act
            # on. A socket that opens and immediately shuts cannot say why.
            await websocket.close(code=exc.code, reason=exc.reason)
            return

        # Echo the subprotocol back. A browser aborts the connection when the
        # server does not select one of the protocols it offered.
        await websocket.accept(subprotocol=SUBPROTOCOL)
        session = None
        try:
            session = shell.open_session()
            node_agent.note_shell_opened(host=shell.host_reachable())
            await _run(websocket, session)
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("shell session failed")
        finally:
            if session is not None:
                session.close()
            node_agent.release_shell()


async def _run(websocket: WebSocket, session: shell.Session) -> None:
    """Pump both directions until either end stops.

    Output goes out as binary and input arrives as JSON, which is not symmetry
    for its own sake. Terminal output is bytes that are frequently not valid
    UTF-8 on their own -- an escape sequence can split across two reads -- so
    decoding it would corrupt the stream. Input has to carry a resize alongside
    keystrokes, so it needs a tag.
    """
    idle = shell_config.idle_timeout_s()

    async def send(data: bytes) -> None:
        await websocket.send_bytes(data)

    stopped = asyncio.Event()
    reader = asyncio.create_task(shell.pump_out(session, send, stopped.set))
    try:
        while not stopped.is_set():
            receive = asyncio.create_task(websocket.receive())
            waiter = asyncio.create_task(stopped.wait())
            done, pending = await asyncio.wait(
                [receive, waiter],
                timeout=idle or None,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if not done:
                # A root prompt in a forgotten tab is the failure this guards.
                await websocket.close(code=1000, reason="Idle.")
                return
            if receive not in done:
                return
            message = receive.result()
            if message.get("type") in ("websocket.disconnect", "websocket.close"):
                return
            text = message.get("text")
            if text is None:
                continue
            try:
                frame = json.loads(text)
            except ValueError:
                continue
            if (data := frame.get("i")) is not None:
                session.write(str(data).encode("utf-8", "ignore"))
            elif (size := frame.get("r")) is not None and len(size) == 2:
                session.resize(size[0], size[1])
    finally:
        reader.cancel()


def subprotocol_key(websocket: WebSocket) -> str | None:
    """The key, from ``Sec-WebSocket-Protocol: derate-shell, <key>``."""
    raw = websocket.headers.get("sec-websocket-protocol", "")
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) < 2 or parts[0] != SUBPROTOCOL:
        return None
    return parts[1]
