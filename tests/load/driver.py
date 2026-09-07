"""One load-driver process.

Open loop, constant arrival rate, corrected for coordinated omission: every
request has a send time fixed before the run starts, and its latency is
measured from that intended time rather than from when a busy driver actually
got around to it. A closed-loop driver self-throttles when the service slows
and so can never show queue collapse, which is precisely the thing being
looked for.

HTTP is hand-rolled over ``asyncio`` streams rather than httpx. The request
bytes are built once, connections are kept alive, and chunked framing is parsed
properly -- which also means chunk boundaries are exact, so inter-token timing
is not at the mercy of TCP coalescing.

    echo '<job json>' | python -m tests.load.driver -
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import deque
from urllib.parse import urlsplit

# Hist lives in control_plane.telemetry.hist: the archive rolls minutes into
# hours with the same bucket-wise merge this harness uses to combine driver
# processes, and two copies of that arithmetic would eventually disagree.
from control_plane.telemetry.hist import Hist


# --- minimal HTTP/1.1 -----------------------------------------------------

_SSE_MARKER = b"data:"
_DONE_MARKER = b"[DONE]"


class Abandon(Exception):
    """Raised to hang up mid-stream on purpose."""


class Conn:
    __slots__ = ("reader", "writer", "buf", "usable")

    def __init__(self, reader, writer) -> None:
        self.reader = reader
        self.writer = writer
        self.buf = bytearray()
        self.usable = True

    @classmethod
    async def open(cls, host: str, port: int) -> "Conn":
        reader, writer = await asyncio.open_connection(host, port)
        try:
            writer.transport.get_extra_info("socket").setsockopt(
                __import__("socket").IPPROTO_TCP, __import__("socket").TCP_NODELAY, 1
            )
        except Exception:
            pass
        return cls(reader, writer)

    def close(self) -> None:
        self.usable = False
        try:
            self.writer.close()
        except Exception:
            pass

    async def _fill(self) -> None:
        data = await self.reader.read(65536)
        if not data:
            raise ConnectionResetError("upstream closed the connection")
        self.buf.extend(data)

    async def _line(self) -> bytes:
        while True:
            idx = self.buf.find(b"\r\n")
            if idx >= 0:
                line = bytes(self.buf[:idx])
                del self.buf[: idx + 2]
                return line
            await self._fill()

    async def _exactly(self, n: int) -> bytes:
        while len(self.buf) < n:
            await self._fill()
        data = bytes(self.buf[:n])
        del self.buf[:n]
        return data

    async def _headers(self) -> tuple[int, dict[str, str]]:
        while True:
            idx = self.buf.find(b"\r\n\r\n")
            if idx >= 0:
                break
            await self._fill()
        head = bytes(self.buf[:idx])
        del self.buf[: idx + 4]
        lines = head.split(b"\r\n")
        status = int(lines[0].split(b" ")[1])
        headers = {}
        for raw in lines[1:]:
            if b":" in raw:
                key, _, value = raw.partition(b":")
                headers[key.decode("latin-1").strip().lower()] = value.decode(
                    "latin-1"
                ).strip()
        return status, headers

    async def roundtrip(
        self,
        request: bytes,
        *,
        timed_chunks: bool = False,
        abandon_after: int = 0,
    ) -> dict:
        """Send one request and consume one response.

        With ``timed_chunks`` the arrival time of every HTTP chunk is recorded,
        which for an SSE response is the arrival time of a token.
        """
        self.writer.write(request)
        await self.writer.drain()
        sent = time.perf_counter()

        status, headers = await self._headers()
        first: float | None = None
        markers = 0
        chunks = 0
        widest = 0
        times: list[float] = []

        encoding = headers.get("transfer-encoding", "")
        length = headers.get("content-length")

        if "chunked" in encoding:
            while True:
                size_line = await self._line()
                size = int(size_line.split(b";")[0] or b"0", 16)
                if size == 0:
                    # Consume trailers up to the blank line.
                    while await self._line():
                        pass
                    break
                payload = await self._exactly(size + 2)
                now = time.perf_counter()
                if first is None:
                    first = now
                chunks += 1
                if timed_chunks:
                    times.append(now)
                found = payload.count(_SSE_MARKER) - payload.count(_DONE_MARKER)
                if found > 0:
                    markers += found
                    if found > widest:
                        widest = found
                if abandon_after and chunks >= abandon_after:
                    # Hang up mid-stream, exactly as a client that gave up
                    # would. The connection is not reusable afterwards.
                    self.close()
                    raise Abandon()
        elif length is not None:
            body = await self._exactly(int(length))
            first = time.perf_counter()
            chunks = 1
            markers = max(0, body.count(_SSE_MARKER) - body.count(_DONE_MARKER))
        else:
            # No framing: the response ends at EOF, so the connection dies with
            # it. Rare, and never on the happy path.
            self.usable = False
            try:
                while True:
                    await self._fill()
            except ConnectionResetError:
                pass
            first = time.perf_counter()

        if headers.get("connection", "").lower() == "close":
            self.usable = False

        return {
            "status": status,
            "sent": sent,
            "first": first if first is not None else sent,
            "done": time.perf_counter(),
            "markers": markers,
            "chunks": chunks,
            "widest_chunk": widest,
            "times": times,
        }


class Pool:
    """Keep-alive connections. Mandatory: without reuse, TIME_WAIT exhausts the
    28k ephemeral ports on this box above a few hundred requests a second."""

    def __init__(self, host: str, port: int, cap: int) -> None:
        self.host = host
        self.port = port
        self.cap = cap
        self.free: deque[Conn] = deque()
        self.opened = 0

    async def acquire(self) -> Conn | None:
        while self.free:
            conn = self.free.popleft()
            if conn.usable:
                return conn
            self.opened -= 1
        if self.opened >= self.cap:
            return None
        self.opened += 1
        try:
            return await Conn.open(self.host, self.port)
        except Exception:
            self.opened -= 1
            raise

    def release(self, conn: Conn) -> None:
        if conn.usable:
            self.free.append(conn)
        else:
            self.opened -= 1
            conn.close()

    def close(self) -> None:
        for conn in self.free:
            conn.close()
        self.free.clear()


# --- the run --------------------------------------------------------------


class Run:
    def __init__(self, job: dict) -> None:
        self.job = job
        parts = urlsplit(job["url"])
        self.host = parts.hostname or "127.0.0.1"
        self.port = parts.port or 80
        path = parts.path or "/"

        body = json.dumps(job["body"]).encode()
        self.request = (
            f"POST {path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Content-Type: application/json\r\n"
            "Accept: */*\r\n"
            "Connection: keep-alive\r\n"
            f"Content-Length: {len(body)}\r\n"
            "\r\n"
        ).encode() + body

        self.timeout = float(job.get("timeout_s", 30.0))
        self.timed = bool(job.get("timed_chunks", job.get("mode") == "stream"))
        self.abandon_every = int(job.get("abandon_every", 0))
        self.abandon_after = int(job.get("abandon_after", 3))

        self.pool = Pool(self.host, self.port, int(job.get("max_conns", 8192)))

        self.latency = Hist()
        self.service = Hist()
        self.ttft = Hist()
        self.itl = Hist()
        self.send_delay = Hist()

        self.sent = 0
        self.ok = 0
        self.dropped = 0
        self.abandoned = 0
        self.errors: dict[str, int] = {}
        self.tokens = 0
        self.chunks = 0
        self.widest_chunk = 0
        self.inflight = 0
        self.peak_inflight = 0
        self.seq = 0

    def _err(self, key: str) -> None:
        self.errors[key] = self.errors.get(key, 0) + 1

    async def _one(self, due: float) -> None:
        self.inflight += 1
        if self.inflight > self.peak_inflight:
            self.peak_inflight = self.inflight
        self.seq += 1
        abandon = (
            self.abandon_after
            if self.abandon_every and self.seq % self.abandon_every == 0
            else 0
        )
        conn = None
        try:
            conn = await self.pool.acquire()
            if conn is None:
                self.dropped += 1
                return
            began = time.perf_counter()
            self.send_delay.add(max(0.0, began - due) * 1000.0)
            result = await asyncio.wait_for(
                conn.roundtrip(
                    self.request, timed_chunks=self.timed, abandon_after=abandon
                ),
                timeout=self.timeout,
            )
            done = result["done"]
            self.latency.add(max(0.0, done - due) * 1000.0)
            self.service.add(max(0.0, done - result["sent"]) * 1000.0)
            status = result["status"]
            if status == 200:
                self.ok += 1
                self.tokens += result["markers"]
                self.chunks += result["chunks"]
                self.widest_chunk = max(self.widest_chunk, result["widest_chunk"])
                self.ttft.add(max(0.0, result["first"] - result["sent"]) * 1000.0)
                times = result["times"]
                for i in range(1, len(times)):
                    self.itl.add((times[i] - times[i - 1]) * 1000.0)
            else:
                self._err(str(status))
            self.pool.release(conn)
            conn = None
        except Abandon:
            self.abandoned += 1
        except TimeoutError:
            self._err("timeout")
        except (ConnectionError, OSError) as exc:
            self._err(type(exc).__name__)
        except Exception as exc:  # pragma: no cover - driver bug, surface it
            self._err(f"driver:{type(exc).__name__}")
        finally:
            if conn is not None:
                conn.close()
                self.pool.opened -= 1
            self.inflight -= 1

    async def rps(self) -> None:
        rate = float(self.job["rate"])
        duration = float(self.job["duration_s"])
        cap = int(self.job.get("inflight_cap", 20000))
        interval = 1.0 / rate if rate > 0 else 0.0
        tasks: set[asyncio.Task] = set()

        start = time.perf_counter()
        i = 0
        while True:
            elapsed = time.perf_counter() - start
            if elapsed >= duration:
                break
            # Everything whose scheduled moment has passed goes now, capped so
            # one long stall cannot turn into an unbounded burst.
            target = int(elapsed / interval) + 1 if interval else i + 1
            burst = 0
            while i < target and burst < 512:
                due = start + i * interval
                i += 1
                burst += 1
                self.sent += 1
                if self.inflight >= cap:
                    self.dropped += 1
                    continue
                task = asyncio.create_task(self._one(due))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            if burst:
                await asyncio.sleep(0)
            else:
                await asyncio.sleep(max(0.0, (start + i * interval) - time.perf_counter()))

        self.wall = time.perf_counter() - start
        if tasks:
            await asyncio.wait(tasks, timeout=self.timeout + 5.0)

    async def stream(self) -> None:
        concurrency = int(self.job["concurrency"])
        duration = float(self.job["duration_s"])
        start = time.perf_counter()

        async def worker() -> None:
            while time.perf_counter() - start < duration:
                self.sent += 1
                await self._one(time.perf_counter())

        await asyncio.gather(*(worker() for _ in range(concurrency)))
        self.wall = time.perf_counter() - start

    async def go(self) -> dict:
        self.wall = 0.0
        try:
            if self.job.get("mode") == "stream":
                await self.stream()
            else:
                await self.rps()
        finally:
            self.pool.close()
        return self.result()

    def result(self) -> dict:
        wall = self.wall or 1e-9
        return {
            "mode": self.job.get("mode", "rps"),
            "offered": float(self.job.get("rate", 0.0)),
            "concurrency": int(self.job.get("concurrency", 0)),
            "wall_s": round(wall, 4),
            "sent": self.sent,
            "ok": self.ok,
            "dropped": self.dropped,
            "abandoned": self.abandoned,
            "errors": self.errors,
            "achieved_rps": round(self.ok / wall, 2),
            "attempted_rps": round(self.sent / wall, 2),
            "tokens": self.tokens,
            "chunks": self.chunks,
            "widest_chunk": self.widest_chunk,
            "peak_inflight": self.peak_inflight,
            "conns": self.pool.opened,
            "latency": self.latency.to_json(),
            "service": self.service.to_json(),
            "ttft": self.ttft.to_json(),
            "itl": self.itl.to_json(),
            "send_delay": self.send_delay.to_json(),
        }


def main() -> None:
    source = sys.argv[1] if len(sys.argv) > 1 else "-"
    raw = sys.stdin.read() if source == "-" else open(source).read()
    job = json.loads(raw)
    try:
        import uvloop

        uvloop.install()
    except Exception:
        pass
    result = asyncio.run(Run(job).go())
    blob = json.dumps(result)
    out = job.get("out")
    if out:
        # A file rather than a pipe: eight drivers all finishing at once is
        # exactly when a full pipe buffer would deadlock the harness.
        with open(out, "w") as handle:
            handle.write(blob)
    else:
        sys.stdout.write(blob)
        sys.stdout.flush()


if __name__ == "__main__":
    main()
