"""An agent's answer is bounded in size and in total time (N-4).

Before this the coordinator's agent client did ``client.get`` then ``.json()``
with a per-read timeout only: an anonymous LAN host whose ``/agent/profile``
wrote one byte every 1.9 s held a join probe-back connection for as long as it
liked, a member answering with a gigabyte grew the coordinator's memory by a
gigabyte, and one slow member held up a whole health or telemetry round.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from control_plane.registry import ProbeFailed
from control_plane.registry import registry as registry_mod
from control_plane.registry.client import HttpAgentClient
from tests.unit.test_registry import FakeClient, make_profile, make_registry, run


class _Drip(httpx.AsyncByteStream):
    """One byte, then another, forever, each inside any per-read timeout."""

    def __init__(self, every: float) -> None:
        self.every = every

    async def __aiter__(self):
        yield b"{"
        while True:
            await asyncio.sleep(self.every)
            yield b" "

    async def aclose(self) -> None:
        return None


def _client(handler) -> HttpAgentClient:
    return HttpAgentClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_a_body_over_the_cap_is_refused_not_buffered():
    big = b'{"x": "' + b"a" * (5 << 20) + b'"}'
    client = _client(lambda request: httpx.Response(200, content=big))
    with pytest.raises(ProbeFailed, match="exceeded 262144 bytes"):
        run(client.get_json("http://10.0.0.50:8081/agent/profile", timeout=2.0))


def test_a_post_answer_over_the_cap_is_refused():
    big = b'{"x": "' + b"a" * 4096 + b'"}'
    client = _client(lambda request: httpx.Response(200, content=big))
    with pytest.raises(ProbeFailed, match="exceeded 1024 bytes"):
        run(client.post_json("http://10.0.0.50:8081/x", {}, timeout=2.0, max_bytes=1024))


def test_a_dripping_answer_is_cut_off_at_the_deadline():
    client = _client(lambda request: httpx.Response(200, stream=_Drip(0.05)))
    started = time.monotonic()
    with pytest.raises(ProbeFailed, match="no complete answer within"):
        run(client.get_json("http://10.0.0.50:8081/agent/profile", timeout=0.2))
    assert time.monotonic() - started < 2.0


def test_an_ordinary_answer_still_parses_and_a_403_still_says_403():
    client = _client(lambda request: httpx.Response(200, json={"node_id": "spark-02"}))
    assert run(client.get_json("http://10.0.0.50:8081/agent/profile", timeout=2.0)) == {
        "node_id": "spark-02"
    }
    refused = _client(lambda request: httpx.Response(403, json={"detail": "no"}))
    with pytest.raises(ProbeFailed, match="403"):
        run(refused.post_json("http://10.0.0.50:8081/api/nodes/join", {}, timeout=2.0))


def test_one_hung_member_does_not_hold_up_the_round(tmp_path, monkeypatch):
    monkeypatch.setattr(registry_mod, "HEARTBEAT_TIMEOUT_S", 0.1)

    class Hung(FakeClient):
        async def get_json(self, url, timeout, headers=None):
            if url.startswith("http://10.0.0.13:8081/agent/") and not url.endswith("/profile"):
                await asyncio.sleep(3600)
            return await super().get_json(url, timeout, headers)

    client = Hung()
    client.serve("http://10.0.0.12:8081", make_profile("spark-02", address="10.0.0.12"))
    client.serve("http://10.0.0.13:8081", make_profile("spark-03", address="10.0.0.13"))
    registry = make_registry(tmp_path, client=client)
    run(registry.add_node("10.0.0.12"))
    run(registry.add_node("10.0.0.13"))

    started = time.monotonic()
    run(registry.health_round())
    run(registry.telemetry_round())
    assert time.monotonic() - started < 2.0
    assert registry._misses["spark-03"] == 1
    assert registry._misses["spark-02"] == 0
