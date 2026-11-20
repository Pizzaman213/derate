"""The coordinator's own listener on OpenAI's 127.0.0.1:1455 redirect
(``control_plane/gateway/loopback.py``), over a real socket."""

from __future__ import annotations

import asyncio
import socket

from control_plane.gateway.loopback import LoopbackCallback


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_the_coordinators_loopback_finishes_one_callback_and_lets_go():
    """The listener the coordinator itself holds on the redirect address."""
    seen: list[str] = []

    async def handler(query: str) -> tuple[int, str]:
        seen.append(query)
        return (200, "<p>signed in</p>") if "state=good" in query else (400, "<p>no</p>")

    async def get(port: int, target: str) -> tuple[int, str]:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"GET {target} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        await writer.drain()
        raw = await reader.read()
        writer.close()
        head, _, body = raw.decode().partition("\r\n\r\n")
        return int(head.split(" ")[1]), body

    async def main():
        port = _free_port()
        lb = LoopbackCallback("127.0.0.1", port, "/auth/callback", handler)
        assert await lb.hold(60) is None and lb.listening
        # A second sign-in while it is held extends it, never re-binds.
        assert await lb.hold(60) is None
        assert await get(port, "/favicon.ico") == (404, "derate answers only /auth/callback here.")
        # A refused callback (a stale state) keeps listening for the right one.
        assert (await get(port, "/auth/callback?code=x&state=bad"))[0] == 400
        assert lb.listening
        assert await get(port, "/auth/callback?code=x&state=good") == (200, "<p>signed in</p>")
        for _ in range(50):
            if not lb.listening:
                break
            await asyncio.sleep(0.02)
        assert not lb.listening
        # ...and a later sign-in takes the port again.
        assert await lb.hold(60) is None and lb.listening
        await lb.close()
        assert seen == ["code=x&state=bad", "code=x&state=good"]

        # Somebody else's listener is named, not fought over.
        with socket.socket() as held:
            held.bind(("127.0.0.1", 0))
            held.listen()
            taken = LoopbackCallback("127.0.0.1", held.getsockname()[1], "/auth/callback", handler)
            assert "already in use" in (await taken.hold(60) or "")
            assert not taken.listening

        # Unused, it lets go at its deadline.
        clock = [0.0]
        idle = LoopbackCallback("127.0.0.1", _free_port(), "/auth/callback", handler, clock=lambda: clock[0])
        assert await idle.hold(1) is None
        clock[0] = 2.0
        for _ in range(400):
            if not idle.listening:
                break
            await asyncio.sleep(0.02)
        assert not idle.listening

    asyncio.run(main())
