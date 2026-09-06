"""A minimal TCP throughput sink, and the client that drives it.

This is the bottom rung of the fallback ladder. It exists because the rungs above
it depend on tools we do not ship -- nccl-tests, perftest, iperf3 -- and a
fallback that needs an uninstalled binary is not a fallback. The node agent
starts the sink; the coordinator dials it.

What it measures is wire throughput on the data-plane interface. That is not
NCCL bandwidth and is never labelled as such: the measurement it feeds carries
`method="tcp"` and an estimate flag, so nothing downstream mistakes it for the
real figure.
"""

from __future__ import annotations

import json
import logging
import socket
import socketserver
import threading
import time

log = logging.getLogger(__name__)

DEFAULT_PROBE_PORT = 47100
MAGIC = b"SPLK0001"
_CHUNK = 1024 * 1024
_PAYLOAD = bytes(_CHUNK)  # zeros; we are timing the wire, not compressing


class _SinkHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        sock: socket.socket = self.request
        sock.settimeout(30.0)
        try:
            header = _recv_exactly(sock, len(MAGIC))
            if header != MAGIC:
                return
            received = 0
            started: float | None = None
            while True:
                chunk = sock.recv(_CHUNK)
                if not chunk:
                    break
                if started is None:
                    # Start the clock at the first byte, not at accept(), so
                    # connection setup does not count against throughput.
                    started = time.monotonic()
                received += len(chunk)
            elapsed = (time.monotonic() - started) if started is not None else 0.0
            reply = json.dumps({"bytes": received, "elapsed_s": elapsed}).encode() + b"\n"
            sock.sendall(reply)
        except (OSError, socket.timeout):
            return


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class ProbeServer:
    """Handle for a running sink. The node agent owns one of these."""

    def __init__(self, server: _Server, thread: threading.Thread) -> None:
        self._server = server
        self._thread = thread

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5.0)

    def __enter__(self) -> ProbeServer:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def start_probe_server(host: str = "0.0.0.0", port: int = DEFAULT_PROBE_PORT) -> ProbeServer:
    """Bind the sink and serve it on a daemon thread."""
    server = _Server((host, port), _SinkHandler)
    thread = threading.Thread(target=server.serve_forever, name="link-probe-server", daemon=True)
    thread.start()
    return ProbeServer(server, thread)


def tcp_throughput_gbps(
    host: str,
    port: int = DEFAULT_PROBE_PORT,
    duration_s: float = 5.0,
    connect_timeout: float = 5.0,
) -> float | None:
    """Blast at the peer's sink and report what it says it received.

    The receiver's own count is authoritative: bytes the sender handed to the
    kernel may still be sitting in a socket buffer when the clock stops.
    """
    try:
        with socket.create_connection((host, port), timeout=connect_timeout) as sock:
            sock.settimeout(max(30.0, duration_s * 4))
            sock.sendall(MAGIC)
            deadline = time.monotonic() + duration_s
            while time.monotonic() < deadline:
                sock.sendall(_PAYLOAD)
            sock.shutdown(socket.SHUT_WR)
            reply = _recv_line(sock)
    except (OSError, socket.timeout) as exc:
        log.debug("tcp throughput probe to %s:%s failed: %s", host, port, exc)
        return None

    if not reply:
        return None
    try:
        doc = json.loads(reply)
        received = float(doc["bytes"])
        elapsed = float(doc["elapsed_s"])
    except (ValueError, KeyError, TypeError):
        return None
    if elapsed <= 0 or received <= 0:
        return None
    return received / elapsed / 1e9


def tcp_rtt_us(host: str, port: int, samples: int = 5, timeout: float = 2.0) -> float | None:
    """Round trip time of a TCP connect, in microseconds.

    A crude proxy: this is a network RTT, not collective latency, and whatever
    records it says so.
    """
    times: list[float] = []
    for _ in range(samples):
        started = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=timeout):
                times.append((time.perf_counter() - started) * 1e6)
        except (OSError, socket.timeout):
            continue
    if not times:
        return None
    # Minimum, not mean: scheduling noise only ever adds.
    return min(times)


def _recv_exactly(sock: socket.socket, count: int) -> bytes:
    buf = b""
    while len(buf) < count:
        chunk = sock.recv(count - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def _recv_line(sock: socket.socket, limit: int = 4096) -> bytes:
    buf = b""
    while b"\n" not in buf and len(buf) < limit:
        chunk = sock.recv(limit)
        if not chunk:
            break
        buf += chunk
    return buf.split(b"\n", 1)[0]
