"""The sending end of a copy: one cache folder to one node's receiver.

The manifest goes to the node's agent over its authenticated HTTP API; the
bytes go over raw TCP, `streams` connections spread across every fabric pair
the two nodes share (one per RoCE rail on a pair of Sparks), each pushing
`CHUNK_BYTES` pieces with `sendfile` so the payload never enters Python. One
ssh stream tops out near 1.3 GB/s on this fabric because of its cipher; the
point of this module is to not have one.

A chunk that fails on a socket error is retried once on a fresh connection;
a second failure, a refused chunk, or `cancel` stops every stream and
discards the session, which removes whatever already arrived.
"""

from __future__ import annotations

import logging
import os
import queue
import socket
import threading
from collections import OrderedDict
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .fabric import LinkAddress, LinkPath, candidate_paths, describe, fastest_tier, link_addresses
from .protocol import (
    ACK_OK,
    CHUNK_HEADER,
    DEFAULT_STREAMS,
    END_OF_STREAM,
    MAGIC,
    Manifest,
    ManifestError,
    build_manifest,
    handshake_mac,
)
from . import protocol

log = logging.getLogger(__name__)

_CONNECT_TIMEOUT_S = 10.0
_IO_TIMEOUT_S = 120.0
#: A candidate link that does not accept a connect within this is not a path.
_PROBE_TIMEOUT_S = 2.0
_WILLNEED = getattr(os, "POSIX_FADV_WILLNEED", None)
#: Source files each stream keeps open at once; older handles are closed.
_OPEN_PER_STREAM = 4
#: Stream cap on a link this slow or slower, or of unknown speed: two streams
#: fill it, and it is usually the link health checks and proxied requests use.
SLOW_LINK_MBPS = 10_000
SLOW_LINK_STREAMS = 2


class TransferError(Exception):
    pass


class TransferCancelled(TransferError):
    pass


class _Refused(Exception):
    pass


@dataclass
class SendResult:
    path: str
    routes: list[str]
    link: str
    files: int
    skipped: int
    bytes_sent: int
    verified: int


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text.strip() or f"HTTP {resp.status_code}"
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str):
        return detail
    if isinstance(detail, dict) and isinstance(detail.get("message"), str):
        return detail["message"]
    return resp.text.strip() or f"HTTP {resp.status_code}"


def _recv_exactly(sock: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def send_folder(
    source: Path,
    *,
    agent_url: str,
    token: str,
    streams: int = DEFAULT_STREAMS,
    cancel: threading.Event,
    on_progress: Callable[[int, int], None],
    on_phase: Callable[[str], None] = lambda s: None,
    on_route: Callable[[str, list[str], str], None] = lambda path, routes, link: None,
    http: httpx.Client | None = None,
    local_addresses: Callable[[], Sequence[LinkAddress]] = link_addresses,
) -> SendResult:
    try:
        manifest: Manifest = build_manifest(Path(source))
    except ManifestError as exc:
        raise TransferError(str(exc)) from exc
    own_client = http is None
    client = http or httpx.Client()
    base = agent_url.rstrip("/")
    headers = {"X-Derate-Token": token}
    try:
        try:
            resp = client.post(
                f"{base}/agent/transfers", json=manifest.to_json(), headers=headers, timeout=60
            )
        except httpx.HTTPError as exc:
            raise TransferError(f"could not reach the node's agent: {exc}") from exc
        if resp.status_code == 404:
            raise TransferError(
                "the node's agent has no /agent/transfers route (it runs an older build); "
                "upgrade that node"
            )
        if resp.status_code != 200:
            raise TransferError(_detail(resp))
        session: dict[str, Any] = resp.json()
        session_id: str = session["session_id"]
        try:
            result = _stream(
                manifest, session, source=Path(source), agent_url=base, streams=streams,
                cancel=cancel, on_progress=on_progress, on_route=on_route,
                local_addresses=local_addresses,
            )
            # The last moment a cancel can take effect: past this the node is
            # committing and the copy finishes on its own. `on_phase` may raise
            # TransferCancelled to claim it, atomically with the job's state.
            if cancel.is_set():
                raise TransferCancelled("cancelled")
            on_phase("verifying")
        except TransferError:
            try:
                client.delete(f"{base}/agent/transfers/{session_id}", headers=headers, timeout=60)
            except httpx.HTTPError as exc:
                log.warning("could not discard copy session %s: %s", session_id, exc)
            raise
        try:
            resp = client.post(
                f"{base}/agent/transfers/{session_id}/commit", headers=headers, timeout=3600
            )
        except httpx.HTTPError as exc:
            raise TransferError(f"the node did not answer the commit: {exc}") from exc
        if resp.status_code != 200:
            raise TransferError(_detail(resp))
        res = resp.json()
        return SendResult(
            path=result["path"],
            routes=result["routes"],
            link=result["link"],
            files=int(res.get("files", len(manifest.files))),
            skipped=int(res.get("skipped", 0)),
            bytes_sent=result["sent"],
            verified=int(res.get("verified", 0)),
        )
    finally:
        if own_client:
            client.close()


def _stream(
    manifest: Manifest,
    session: dict[str, Any],
    *,
    source: Path,
    agent_url: str,
    streams: int,
    cancel: threading.Event,
    on_progress: Callable[[int, int], None],
    on_route: Callable[[str, list[str], str], None],
    local_addresses: Callable[[], Sequence[LinkAddress]],
) -> dict[str, Any]:
    port = int(session["port"])
    remote = [
        LinkAddress(
            iface=str(a.get("iface", "")), address=str(a["address"]), prefix=int(a["prefix"]),
            speed_mbps=a.get("speed_mbps") if isinstance(a.get("speed_mbps"), int) else None,
            rdma=bool(a.get("rdma", False)),
        )
        for a in session.get("addresses") or []
        if isinstance(a, dict) and "address" in a and "prefix" in a
    ]
    chosen = fastest_tier(_reachable(candidate_paths(local_addresses(), remote), port))
    host = urlparse(agent_url).hostname or ""
    if chosen:
        path = "fabric" if all(p.rdma for p in chosen) else "network"
        pairs: list[tuple[str | None, str]] = [(p.source, p.dial) for p in chosen]
        speed = chosen[0].speed_mbps
        routes = [p.route for p in chosen]
    else:
        # No link answered: the agent's own address, whatever carries it.
        path, pairs, speed, routes = "management", [(None, host)], None, [f"default->{host}"]
    if path != "fabric" and (speed is None or speed <= SLOW_LINK_MBPS):
        streams = min(streams, SLOW_LINK_STREAMS)
    link = describe(chosen)
    on_route(path, routes, link)
    session_id = session["session_id"].encode("ascii")
    secret = bytes.fromhex(session["secret"])
    have = set(session.get("have") or [])

    order = sorted(
        (i for i in range(len(manifest.files)) if i not in have),
        key=lambda i: manifest.files[i].size,
        reverse=True,
    )
    chunks: queue.Queue[tuple[int, int, int]] = queue.Queue()
    total = 0
    count = 0
    step = protocol.CHUNK_BYTES
    # File after file, largest first, so files finish one by one and each is
    # hashed on the receiver while the rest still stream. Round-robin across
    # files was measured too: no faster, and every file then ends at once,
    # leaving the whole hash to run after the last byte.
    for i in order:
        size = manifest.files[i].size
        total += size
        for offset in range(0, size, step):
            chunks.put((i, offset, min(step, size - offset)))
            count += 1
    on_progress(0, total)

    lock = threading.Lock()
    failed = threading.Event()
    errors: list[str] = []
    retries: dict[tuple[int, int, int], int] = {}
    sent = [0]

    def fail(message: str) -> None:
        with lock:
            errors.append(message)
        failed.set()

    def connect(local: str | None, remote_addr: str) -> socket.socket:
        sock = socket.create_connection(
            (remote_addr, port), timeout=_CONNECT_TIMEOUT_S,
            source_address=(local, 0) if local else None,
        )
        try:
            sock.settimeout(_IO_TIMEOUT_S)
            sock.sendall(MAGIC + session_id)
            nonce = _recv_exactly(sock, 32)
            if nonce is None:
                raise _Refused("the node refused the copy session")
            sock.sendall(handshake_mac(secret, nonce))
            if _recv_exactly(sock, 1) != ACK_OK:
                raise _Refused("the node refused this copy's credential")
        except BaseException:
            sock.close()
            raise
        return sock

    def worker(k: int) -> None:
        local, remote_addr = pairs[k % len(pairs)]
        sock: socket.socket | None = None
        # Bounded: a folder of thousands of shards must not hold thousands of
        # descriptors per stream. Chunks are queued largest file first, so a
        # worker rarely returns to an evicted file.
        handles: OrderedDict[int, Any] = OrderedDict()
        try:
            while not cancel.is_set() and not failed.is_set():
                try:
                    chunk = chunks.get_nowait()
                except queue.Empty:
                    if sock is not None:
                        try:
                            sock.sendall(CHUNK_HEADER.pack(END_OF_STREAM, 0, 0))
                        except OSError:
                            pass
                    return
                index, offset, length = chunk
                entry = manifest.files[index]
                try:
                    if sock is None:
                        sock = connect(local, remote_addr)
                    fh = handles.get(index)
                    if fh is None:
                        while len(handles) >= _OPEN_PER_STREAM:
                            handles.popitem(last=False)[1].close()
                        fh = handles[index] = open(source / entry.path, "rb")
                    else:
                        handles.move_to_end(index)
                    if _WILLNEED is not None:
                        # Read-ahead is 128 KiB on these boxes: left to it, each
                        # stream keeps one small read in flight and the disk,
                        # not the fabric, sets the rate. Ask for the whole chunk.
                        os.posix_fadvise(fh.fileno(), offset, length, _WILLNEED)
                    sock.sendall(CHUNK_HEADER.pack(index, offset, length))
                    n = sock.sendfile(fh, offset, length)
                    if n != length:
                        raise _Refused(f"{entry.path} changed size while it was being copied")
                    ack = _recv_exactly(sock, 1)
                    if ack is None:
                        raise ConnectionError("the node closed the stream before acknowledging")
                    if ack != ACK_OK:
                        raise _Refused(f"the node refused a chunk of {entry.path}")
                except _Refused as exc:
                    fail(str(exc))
                    return
                except OSError as exc:
                    if sock is not None:
                        sock.close()
                        sock = None
                    with lock:
                        retries[chunk] = retries.get(chunk, 0) + 1
                        again = retries[chunk] == 1
                    if not again:
                        fail(f"{remote_addr}: {exc}")
                        return
                    chunks.put(chunk)
                    continue
                with lock:
                    sent[0] += length
                    done = sent[0]
                on_progress(done, total)
        finally:
            if sock is not None:
                sock.close()
            for fh in handles.values():
                fh.close()

    threads = [
        threading.Thread(target=worker, args=(k,), name=f"derate-send-{k}", daemon=True)
        for k in range(min(max(1, streams), count))
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if cancel.is_set():
        raise TransferCancelled("cancelled")
    if failed.is_set():
        raise TransferError(errors[0] if errors else "the copy failed")
    if sent[0] != total:
        raise TransferError(f"only {sent[0]} of {total} bytes were sent")
    return {"path": path, "routes": routes, "link": link, "sent": sent[0]}


def _reachable(candidates: Sequence[LinkPath], port: int) -> list[LinkPath]:
    """The candidates that accept a TCP connect on the receiver's port, in order.

    A link-local pairing cannot be judged from its addresses, and a routed one
    can still be firewalled; one connect each, all at once, settles both.
    """
    if not candidates:
        return []

    def probe(p: LinkPath) -> bool:
        try:
            sock = socket.create_connection(
                (p.dial, port), timeout=_PROBE_TIMEOUT_S,
                source_address=(p.source, 0) if p.source else None,
            )
        except OSError:
            return False
        sock.close()
        return True

    with ThreadPoolExecutor(max_workers=min(16, len(candidates))) as pool:
        answers = list(pool.map(probe, candidates))
    return [p for p, ok in zip(candidates, answers) if ok]
