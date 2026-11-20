"""AuditSink: the remote copy of the audit table, against real sockets."""

from __future__ import annotations

import json
import socket
import time

import pytest

from control_plane.auditlog import SYSLOG_PRI, AuditSink


def _udp_listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(5)
    return sock


def test_udp_sink_delivers_an_rfc5424_line():
    listener = _udp_listener()
    port = listener.getsockname()[1]
    sink = AuditSink(f"udp://127.0.0.1:{port}")
    sink.start(heartbeat=lambda: ("abc", 3))
    try:
        sink.send({"kind": "audit", "id": 7, "action": "login", "ts_iso": "2026-11-18T09:00:00.000Z", "host": "spark-01"})
        data, _ = listener.recvfrom(65536)
    finally:
        sink.close()
        listener.close()
    text = data.decode()
    head, body = text.split(" audit - ", 1)
    assert head.startswith(f"<{SYSLOG_PRI}>1 2026-11-18T09:00:00.000Z spark-01 derate ")
    assert json.loads(body)["id"] == 7


def test_heartbeat_carries_the_chain_head():
    listener = _udp_listener()
    port = listener.getsockname()[1]
    sink = AuditSink(f"udp://127.0.0.1:{port}", heartbeat_s=0.05)
    sink.start(heartbeat=lambda: ("head-hash", 42))
    try:
        data, _ = listener.recvfrom(65536)
    finally:
        sink.close()
        listener.close()
    line = json.loads(data.decode().split(" audit - ", 1)[1])
    assert line["kind"] == "heartbeat" and line["head_hash"] == "head-hash" and line["rows"] == 42


def test_overflow_drops_the_oldest_and_says_so_on_the_next_line():
    listener = _udp_listener()
    port = listener.getsockname()[1]
    sink = AuditSink(f"udp://127.0.0.1:{port}", queue_size=2)
    for i in range(5):  # not started yet: the queue only fills
        sink.send({"kind": "audit", "id": i})
    assert sink.dropped == 3
    sink.start()
    try:
        first = json.loads(listener.recvfrom(65536)[0].decode().split(" audit - ", 1)[1])
    finally:
        sink.close()
        listener.close()
    assert first["id"] == 3 and first["dropped"] == 3


def test_an_unreachable_tcp_sink_requeues_and_never_raises():
    sink = AuditSink("tcp://127.0.0.1:9")  # discard port: nothing listens
    assert sink.deliver([{"kind": "audit", "id": 1}]) is False
    # inside the backoff window the next delivery is re-queued without a dial
    started = time.monotonic()
    assert sink.deliver([{"kind": "audit", "id": 2}, {"kind": "heartbeat"}]) is False
    assert time.monotonic() - started < 0.5
    # nothing is dropped: both rows wait at the head of the queue, in order,
    # the newest batch first; the stale heartbeat is not kept
    assert sink.dropped == 0
    assert [sink._take(0.01)["id"] for _ in range(2)] == [2, 1]
    assert sink._take(0.01) is None


def test_requeue_past_the_queue_size_drops_the_oldest_and_counts_it():
    sink = AuditSink("tcp://127.0.0.1:9", queue_size=2)
    sink._retry_at = time.monotonic() + 60
    assert sink.deliver([{"kind": "audit", "id": i} for i in range(3)]) is False
    assert sink.dropped == 1
    assert [sink._take(0.01)["id"] for _ in range(2)] == [1, 2]


@pytest.mark.parametrize("url", ["ftp://x:1", "udp://host-without-port", "nonsense"])
def test_bad_sink_urls_are_refused(url):
    with pytest.raises(ValueError):
        AuditSink(url)
    assert AuditSink.from_env({"DERATE_AUDIT_SINK": url}) is None


def test_no_sink_configured_is_none():
    assert AuditSink.from_env({}) is None
    assert AuditSink.from_env({"DERATE_AUDIT_SINK": "udp://127.0.0.1:5514"}).label == "udp://127.0.0.1:5514"
