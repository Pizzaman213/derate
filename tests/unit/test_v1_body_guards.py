"""What a key holder can make the gateway do before any per-key limit runs.

The limits are checked on the parsed body, so reading and parsing it happen
first, for every admitted caller. These pin the bounds on that stretch: a
malformed body is the caller's 400, never a 500 with a traceback; one key
cannot have more than ``MAX_BODIES_IN_FLIGHT_PER_KEY`` bodies being uploaded
at once; and llama.cpp's own output-length spelling cannot outrun a key's cap.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from control_plane.gateway import create_app, openai_api
from tests.unit.conftest import BOOT_PASSWORD, credentials
from tests.unit.test_gateway import (
    FakeBackend,
    FakeDeployments,
    RunningBackend,
    build_deps,
    make_deployment,
)

CHAT = {"model": "llama", "messages": [{"role": "user", "content": "hi"}]}


def _mint(app, **limits) -> str:
    cookie, _ = credentials(app)
    r = TestClient(app).post(
        "/api/keys",
        json={"name": "guarded", "expiry_days": 30, "limits": limits, "current_password": BOOT_PASSWORD},
        headers={"Cookie": f"derate_session={cookie}"},
    )
    assert r.status_code == 200, r.text
    return r.json()["secret_once"]


@pytest.mark.parametrize(
    "raw",
    [
        b'{"model": "llama", "x": "\xff"}',  # not UTF-8: UnicodeDecodeError
        b'{"model": "llama", "x": ' + b"[" * 100_000 + b"}",  # RecursionError
    ],
    ids=["not-utf8", "nested-too-deep"],
)
def test_a_malformed_body_is_the_callers_400_not_a_500(raw):
    with TestClient(create_app(build_deps())) as client:
        r = client.post("/v1/chat/completions", content=raw, headers={"content-type": "application/json"})
    assert (r.status_code, r.json()["error"]["code"]) == (400, "invalid_json"), r.text[:200]


def test_one_key_cannot_hold_more_than_its_body_slots(monkeypatch):
    """Each slot is a body being read: a slow uploader holds one. Past the
    cap the next upload from that key is refused before a byte is read, and
    a finished upload gives its slot back."""
    app = create_app(build_deps())
    _, key = credentials(app)
    limit = openai_api.MAX_BODIES_IN_FLIGHT_PER_KEY
    real = openai_api.read_bounded_body
    reads: list[int] = []
    state: dict = {}

    async def slow_read(request, cap):
        reads.append(1)
        await state["release"].wait()
        return await real(request, cap)

    monkeypatch.setattr(openai_api, "read_bounded_body", slow_read)

    async def scenario():
        state["release"] = asyncio.Event()
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 40000))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            def post():
                return c.post("/v1/chat/completions", json=CHAT, headers={"Authorization": f"Bearer {key}"})

            held = [asyncio.create_task(post()) for _ in range(limit)]
            for _ in range(500):
                if len(reads) >= limit:
                    break
                await asyncio.sleep(0.01)
            refused = await post()
            reads_when_refused = len(reads)
            state["release"].set()
            finished = await asyncio.gather(*held)
            again = await post()
        return refused, reads_when_refused, finished, again

    refused, reads_when_refused, finished, again = asyncio.run(scenario())
    assert refused.status_code == 429, refused.text
    assert refused.json()["error"]["code"] == "too_many_requests"
    assert refused.headers["retry-after"] == "1"
    assert reads_when_refused == limit  # the refused upload was never read
    assert all(r.status_code != 429 for r in finished)
    assert again.status_code != 429  # the slots came back


def test_n_predict_cannot_outrun_the_output_cap():
    """llama-server lets ``n_predict`` override ``max_tokens``; a cap that
    only knew the OpenAI spellings was a cap a llama.cpp caller could skip."""
    backend = FakeBackend()
    with RunningBackend(backend) as running:
        app = create_app(build_deps(deployments=FakeDeployments(
            [make_deployment("d-1", "llama", backend_url=running.base_url)]
        )))
        key = _mint(app, max_output_tokens_per_request=100)
        bearer = {"Authorization": f"Bearer {key}"}
        with TestClient(app) as client:
            over = client.post("/v1/chat/completions", json={**CHAT, "n_predict": 100_000}, headers=bearer)
            capped = client.post("/v1/chat/completions", json=CHAT, headers=bearer)
    assert (over.status_code, over.json()["error"]["code"]) == (400, "max_output_tokens_exceeded"), over.text
    assert capped.status_code == 200, capped.text
    assert backend.requests[-1]["max_tokens"] == 100
    assert "n_predict" not in backend.requests[-1]
