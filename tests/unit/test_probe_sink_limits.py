"""The link probe sink is bounded (plan step 5.16, N6).

It listens on 0.0.0.0:47100 on every node with no credential, because the
measurer on the far side holds none for it. Before this it took any number of
connections from any address and read each for as long as the sender kept
sending.
"""

from __future__ import annotations

import json
import socket
import time

from control_plane.links import probe_server
from control_plane.links.probe_server import (
    MAGIC,
    SINK_MAX_CONCURRENT,
    _peer_allowed,
    start_probe_server,
    tcp_throughput_gbps,
)
from control_plane.registry.agent import NodeAgent
from tests.unit.test_agent_tokens import profile


def test_only_cluster_shaped_addresses_may_blast():
    assert _peer_allowed("10.0.0.5", ())
    assert _peer_allowed("192.168.11.14", ())
    assert _peer_allowed("169.254.10.1", ())
    assert _peer_allowed("127.0.0.1", ())
    assert _peer_allowed("::ffff:10.0.0.5", ())
    assert not _peer_allowed("8.8.8.8", ())
    assert not _peer_allowed("not-an-ip", ())
    # A public coordinator is admitted once it has presented the agent token.
    assert _peer_allowed("8.8.8.8", ["8.8.8.8"])


def test_the_agent_offers_the_peers_that_presented_its_token():
    agent = NodeAgent(profile(), token="t")
    assert agent.trusted_peers() == ()
    agent.note_trusted("203.0.113.7")
    assert agent.trusted_peers() == ("203.0.113.7",)


def test_a_full_house_is_refused_rather_than_queued():
    with start_probe_server("127.0.0.1", 0) as sink:
        slots = sink._server.slots
        for _ in range(SINK_MAX_CONCURRENT):
            assert slots.acquire(blocking=False)
        try:
            assert tcp_throughput_gbps("127.0.0.1", sink.port, duration_s=0.2) is None
        finally:
            for _ in range(SINK_MAX_CONCURRENT):
                slots.release()
        assert tcp_throughput_gbps("127.0.0.1", sink.port, duration_s=0.2) is not None


def test_a_sender_that_never_stops_is_cut_off_at_the_deadline():
    with start_probe_server("127.0.0.1", 0, duration_s=0.05) as sink:  # 0.2 s deadline
        with socket.create_connection(("127.0.0.1", sink.port), timeout=5) as sock:
            sock.sendall(MAGIC + b"x" * 4096)
            began = time.monotonic()
            reply = b""
            while not reply.endswith(b"\n"):
                chunk = sock.recv(4096)
                if not chunk:
                    break
                reply += chunk
            waited = time.monotonic() - began
    assert waited < 3.0, "the sink kept reading past its deadline"
    assert json.loads(reply)["bytes"] == 4096


def test_the_byte_cap_ends_a_blast(monkeypatch):
    monkeypatch.setattr(probe_server, "SINK_MAX_BYTES", 1000)
    with start_probe_server("127.0.0.1", 0) as sink:
        with socket.create_connection(("127.0.0.1", sink.port), timeout=5) as sock:
            sock.sendall(MAGIC + b"x" * 5000)
            reply = b""
            while not reply.endswith(b"\n"):
                chunk = sock.recv(4096)
                if not chunk:
                    break
                reply += chunk
    assert json.loads(reply)["bytes"] == 1000


def test_tcp_rtt_times_a_real_connect_and_says_none_for_a_closed_port():
    from control_plane.links.probe_server import tcp_rtt_us

    with start_probe_server("127.0.0.1", 0) as sink:
        rtt = tcp_rtt_us("127.0.0.1", sink.port, samples=3)
        port = sink.port
    assert isinstance(rtt, float) and rtt > 0
    # The sink is closed now, so nothing is listening on its port.
    assert tcp_rtt_us("127.0.0.1", port, samples=2, timeout=0.5) is None
