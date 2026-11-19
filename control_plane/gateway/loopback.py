"""The coordinator's own listener on OpenAI's loopback redirect.

OpenAI sends the browser to ``http://127.0.0.1:1455/auth/callback`` and to
nothing else. While a ChatGPT sign-in is pending the coordinator listens there
itself, the way the Codex CLI and omp do, so the redirect finishes the attempt
with nothing to run whenever port 1455 of the browser's machine reaches this
one: a browser on the coordinator, ``ssh -L 1455:127.0.0.1:1455 <coordinator>``,
or an editor's remote port forwarding (VS Code forwards a port it sees a remote
process listen on).

Standard library only, one request per connection, GET only. It answers the
callback path through the same handler as the gateway's ``GET /auth/callback``
and 404s everything else. It holds the port only while it may be needed: from
a sign-in's start until its attempt would expire, or until one callback
finishes. A port somebody else holds is reported, never fought over.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import time
from typing import Awaitable, Callable
from urllib.parse import urlsplit

log = logging.getLogger("gateway.loopback")

#: (status, html) for the query string of one ``/auth/callback`` request.
Handler = Callable[[str], Awaitable[tuple[int, str]]]

_REASONS = {200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed"}
#: A request line and headers larger than this are not a browser's redirect.
_MAX_HEAD = 16 * 1024
_READ_TIMEOUT_S = 10.0


class LoopbackCallback:
    def __init__(self, host: str, port: int, path: str, handler: Handler,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._host, self._port, self._path = host, port, path
        self._handler = handler
        self._clock = clock
        self._server: asyncio.base_events.Server | None = None
        self._deadline = 0.0
        self._closer: asyncio.Task | None = None

    @property
    def listening(self) -> bool:
        return self._server is not None

    async def hold(self, seconds: float) -> str | None:
        """Listen (or keep listening) for at least *seconds* more.

        ``None`` when listening; otherwise the sentence saying why not.
        """
        self._deadline = max(self._deadline, self._clock() + seconds)
        if self._server is None:
            try:
                self._server = await asyncio.start_server(self._serve, self._host, self._port)
            except OSError as exc:
                if exc.errno == errno.EADDRINUSE:
                    reason = (
                        f"port {self._port} on the coordinator is already in use (another "
                        "ChatGPT sign-in tool, e.g. the Codex CLI?)"
                    )
                else:
                    reason = f"could not listen on {self._host}:{self._port}: {exc}"
                log.warning("ChatGPT sign-in loopback not listening: %s", reason)
                return reason
            log.info("listening on %s:%s for the ChatGPT sign-in redirect", self._host, self._port)
        if self._closer is None or self._closer.done():
            self._closer = asyncio.create_task(self._close_at_deadline())
        return None

    async def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()
            log.info("stopped listening on %s:%s", self._host, self._port)
        closer, self._closer = self._closer, None
        if closer is not None and closer is not asyncio.current_task():
            closer.cancel()

    async def _close_at_deadline(self) -> None:
        while self._server is not None:
            remaining = self._deadline - self._clock()
            if remaining <= 0:
                await self.close()
                return
            await asyncio.sleep(min(remaining, 5.0))

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        finished = False
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), _READ_TIMEOUT_S)
            if len(head) > _MAX_HEAD:
                return
            method, _, rest = head.split(b"\r\n", 1)[0].decode("latin-1").partition(" ")
            target = rest.rsplit(" ", 1)[0]
            url = urlsplit(target)
            if method != "GET":
                status, body = 405, "derate answers only GET here."
            elif url.path != self._path:
                status, body = 404, f"derate answers only {self._path} here."
            else:
                status, body = await self._handler(url.query)
                finished = status == 200
            data = body.encode()
            kind = "text/html" if body.lstrip().startswith("<") else "text/plain"
            writer.write(
                (
                    f"HTTP/1.1 {status} {_REASONS.get(status, '')}\r\n"
                    f"Content-Type: {kind}; charset=utf-8\r\n"
                    f"Content-Length: {len(data)}\r\n"
                    "Cache-Control: no-store\r\nConnection: close\r\n\r\n"
                ).encode() + data
            )
            await writer.drain()
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError,
                ConnectionError):
            return
        finally:
            writer.close()
        if finished:
            # One sign-in finished: give the port back (the Codex CLI wants it
            # too). A later sign-in takes it again. A task, not an await:
            # wait_closed() waits for this very connection's handler to return.
            asyncio.get_running_loop().create_task(self.close())
