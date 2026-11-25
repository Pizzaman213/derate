"""What a key holder can make the gateway do before any per-key limit runs.

The limits are checked on the parsed body, so reading and parsing it happen
first, for every admitted caller. These pin the bounds on that stretch: a
malformed body is the caller's 400, never a 500 with a traceback; one key
cannot have more than ``MAX_BODIES_IN_FLIGHT_PER_KEY`` bodies being uploaded
at once; and llama.cpp's own output-length spelling cannot outrun a key's cap.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from control_plane.gateway import body as body_mod
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


def test_minus_one_is_no_ceiling_and_never_reaches_the_engine():
    """-1 is "no ceiling" to LM Studio and llama-server, so it is taken out of
    the forwarded body: refused, it broke those clients; forwarded beside a
    cap, llama-server's ``n_predict: -1`` overrode it."""
    backend = FakeBackend()
    with RunningBackend(backend) as running:
        app = create_app(build_deps(deployments=FakeDeployments(
            [make_deployment("d-1", "llama", backend_url=running.base_url)]
        )))
        key = _mint(app, max_output_tokens_per_request=256)
        bearer = {"Authorization": f"Bearer {key}"}
        with TestClient(app) as client:
            beside = client.post("/v1/chat/completions", json={**CHAT, "max_tokens": 16, "n_predict": -1}, headers=bearer)
            forwarded = backend.requests[-1]
            alone = client.post("/v1/chat/completions", json={**CHAT, "max_tokens": -1}, headers=bearer)
            zero = client.post("/v1/chat/completions", json={**CHAT, "n_predict": 0}, headers=bearer)
    assert beside.status_code == 200, beside.text
    assert forwarded["max_tokens"] == 16 and "n_predict" not in forwarded
    assert alone.status_code == 200 and backend.requests[-1]["max_tokens"] == 256
    assert zero.status_code == 400


@pytest.mark.parametrize("stream", ["false", "0", "no", 1, "true", 0])
def test_a_stream_that_is_not_a_boolean_is_refused(stream):
    """The engine reads ``"false"`` as false and answers in JSON, while a
    truthy check here metered that answer as SSE: no frames, no output
    tokens charged, for any length of answer."""
    backend = FakeBackend()
    with RunningBackend(backend) as running:
        app = create_app(build_deps(deployments=FakeDeployments(
            [make_deployment("d-1", "llama", backend_url=running.base_url)]
        )))
        key = _mint(app)
        with TestClient(app) as client:
            r = client.post("/v1/chat/completions", json={**CHAT, "stream": stream},
                            headers={"Authorization": f"Bearer {key}"})
    assert (r.status_code, r.json()["error"]["code"]) == (400, "invalid_request"), r.text
    assert backend.requests == []


@pytest.mark.parametrize(
    "messages",
    [
        [{"role": "user", "content": ""}] * (openai_api.MAX_MESSAGES + 1),
        [{"role": "user", "content": [{"type": "text", "text": ""}] * (openai_api.MAX_CONTENT_PARTS + 1)}],
    ],
    ids=["messages", "parts"],
)
def test_a_body_with_more_messages_or_parts_than_any_prompt_is_refused(messages):
    """Every walk before forwarding is per message and per part, in Python,
    on the event loop."""
    app = create_app(build_deps())
    _, key = credentials(app)
    with TestClient(app) as client:
        r = client.post("/v1/chat/completions", json={"model": "llama", "messages": messages},
                        headers={"Authorization": f"Bearer {key}"})
    assert (r.status_code, r.json()["error"]["code"]) == (413, "too_many_parts")


def test_the_prefix_key_is_linear_in_the_message_count():
    """It summed every part collected so far once per message: one body of
    empty messages blocked the loop for minutes."""
    import time

    started = time.monotonic()
    assert openai_api._prefix_key({"messages": [{"content": ""}] * 50_000}) is None
    assert time.monotonic() - started < 0.5
    assert openai_api._prefix_key({"messages": [{"content": "ab"}, {"content": "cd"}]}) == "abcd"


def test_an_upload_of_too_many_parts_is_refused():
    app = create_app(build_deps())
    _, key = credentials(app)
    parts = "".join(f'--b\r\nContent-Disposition: form-data; name="f{i}"\r\n\r\nx\r\n' for i in range(100))
    raw = (parts + '--b\r\nContent-Disposition: form-data; name="model"\r\n\r\nwhisper\r\n--b--\r\n').encode()
    with TestClient(app) as client:
        r = client.post("/v1/audio/transcriptions", content=raw,
                        headers={"content-type": "multipart/form-data; boundary=b",
                                 "Authorization": f"Bearer {key}"})
    assert (r.status_code, r.json()["error"]["code"]) == (413, "too_many_parts")


def test_every_body_counts_against_the_key_rpm_and_a_raise_applies_at_once(monkeypatch):
    """Only requests the limit service admitted counted, so a key could have
    the gateway parse body after body refused for some other reason (here an
    unknown model) at whatever rate it could upload."""
    app = create_app(build_deps())
    cookie, _ = credentials(app)
    r = TestClient(app).post(
        "/api/keys",
        json={"name": "windowed", "expiry_days": 30, "limits": {"rpm": 2}, "current_password": BOOT_PASSWORD},
        headers={"Cookie": f"derate_session={cookie}"},
    )
    key, key_id = r.json()["secret_once"], r.json()["key"]["key_id"]
    bearer = {"Authorization": f"Bearer {key}"}
    bad = {**CHAT, "model": "no-such-model"}  # refused after parsing: 404
    with TestClient(app) as client:
        codes = [client.post("/v1/chat/completions", json=bad, headers=bearer).status_code for _ in range(2)]
        reads: list[int] = []
        real = openai_api.read_bounded_body

        async def counting(request, cap):
            reads.append(1)
            return await real(request, cap)

        monkeypatch.setattr(openai_api, "read_bounded_body", counting)
        third = client.post("/v1/chat/completions", json=bad, headers=bearer)
        assert (codes, third.status_code, reads) == ([404, 404], 429, [])
        assert third.json()["error"]["limit_source"] == "key:rpm"
        raised = client.patch(f"/api/keys/{key_id}", json={"limits": {"rpm": 10}},
                              headers={"Cookie": f"derate_session={cookie}"})
        assert raised.status_code == 200, raised.text
        assert client.post("/v1/chat/completions", json=bad, headers=bearer).status_code == 404


def _counting_loads(monkeypatch) -> list[int]:
    calls: list[int] = []
    real = openai_api.json.loads

    def loads(raw, *a, **k):
        calls.append(len(raw))
        return real(raw, *a, **k)

    monkeypatch.setattr(openai_api.json, "loads", loads)
    return calls


def test_a_body_of_too_many_values_is_refused_before_it_is_parsed(monkeypatch):
    """Parsing and estimating run on the event loop: a 23 MiB batch of
    one-token lists held every stream and /healthz for ~3 s."""
    app = create_app(build_deps())
    _, key = credentials(app)
    raw = b'{"model": "llama", "prompt": [' + b"1," * openai_api.MAX_JSON_VALUES + b"1]}"
    calls = _counting_loads(monkeypatch)
    with TestClient(app) as client:
        r = client.post("/v1/completions", content=raw,
                        headers={"content-type": "application/json", "Authorization": f"Bearer {key}"})
    assert (r.status_code, r.json()["error"]["code"]) == (413, "too_many_json_values")
    assert len(raw) not in calls


def test_a_key_over_its_rpm_is_refused_before_its_body_is_parsed(monkeypatch):
    """The rpm refused a request only after parsing it, so a key past its
    limit could still make the gateway parse as fast as it could upload."""
    backend = FakeBackend()
    with RunningBackend(backend) as running:
        app = create_app(build_deps(deployments=FakeDeployments(
            [make_deployment("d-1", "llama", backend_url=running.base_url)]
        )))
        key = _mint(app, rpm=2)
        bearer = {"Authorization": f"Bearer {key}"}
        with TestClient(app) as client:
            codes = [client.post("/v1/chat/completions", json=CHAT, headers=bearer).status_code for _ in range(3)]
            calls = _counting_loads(monkeypatch)
            raw = json.dumps({**CHAT, "user": "cooled"}).encode()
            cooled = client.post("/v1/chat/completions", content=raw,
                                 headers={**bearer, "content-type": "application/json"})
    assert codes == [200, 200, 429]
    assert cooled.status_code == 429 and cooled.json()["error"]["code"] == "limit_exceeded"
    assert cooled.json()["error"]["limit_source"] == "key:rpm"
    assert len(raw) not in calls


LONG_MODEL = "a" * (body_mod.MAX_MODEL_CHARS + 88)


def _requests_rows(app) -> list:
    rows: list = []
    app.state.ctx.sink.request = rows.append
    return rows


def test_a_model_name_past_the_cap_is_refused_before_any_row_is_written():
    """`model` landed verbatim in the trace's three name columns: a 25 MiB
    name wrote a ~75 MiB journal row per refused request."""
    from starlette.websockets import WebSocketDisconnect

    app = create_app(build_deps())
    with TestClient(app) as client:
        rows = _requests_rows(app)
        chat = client.post("/v1/chat/completions", json={**CHAT, "model": LONG_MODEL})
        upload = client.post(
            "/v1/audio/transcriptions",
            data={"model": LONG_MODEL}, files={"file": ("a.mp3", b"\x00\x01")},
        )
        voices = client.get("/v1/audio/voices", params={"model": LONG_MODEL})
        with pytest.raises(WebSocketDisconnect) as caught:
            with client.websocket_connect(f"/v1/realtime?model={LONG_MODEL}") as ws:
                ws.receive()
        assert rows == []
        at_cap = client.post("/v1/chat/completions", json={**CHAT, "model": "b" * body_mod.MAX_MODEL_CHARS})
    for reply in (chat, upload, voices):
        assert (reply.status_code, reply.json()["error"]["code"]) == (400, "invalid_request"), reply.text[:200]
        assert LONG_MODEL not in reply.text
    assert (caught.value.code, caught.value.reason) == (1008, "model_too_long")
    # A name at the cap is an ordinary unknown model, recorded like any other.
    assert at_cap.status_code == 404, at_cap.text[:200]
    assert len(rows) == 1


def test_a_prompt_batch_past_the_cap_is_a_400_and_leaks_no_slot():
    backend = FakeBackend()
    with RunningBackend(backend) as running:
        app = create_app(build_deps(deployments=FakeDeployments(
            [make_deployment("d-1", "llama", backend_url=running.base_url)]
        )))
        key = _mint(app, max_concurrent=1)
        bearer = {"Authorization": f"Bearer {key}"}
        with TestClient(app) as client:
            over = client.post("/v1/completions", json={"model": "llama", "prompt": ["a"] * 129}, headers=bearer)
            after = client.post("/v1/chat/completions", json=CHAT, headers=bearer)
    assert over.status_code == 400, over.text[:200]
    assert "at most 128" in over.json()["error"]["message"]
    assert after.status_code == 200, after.text[:200]


def _nested(levels: int) -> list:
    value: list = []
    for _ in range(levels - 1):
        value = [value]
    return value


@pytest.mark.parametrize(
    "levels, code",
    [(body_mod.MAX_JSON_DEPTH - 1, 404), (body_mod.MAX_JSON_DEPTH, 400), (900, 400)],
    ids=["at-the-cap", "one-past", "near-the-recursion-limit"],
)
def test_a_body_nested_past_the_depth_cap_is_a_400(levels, code):
    """The body is level 1 and ``tools`` level 2, so *levels* of lists put the
    deepest at 1 + levels. The estimates re-serialise ``tools`` recursively,
    and a body one level under the C recursion limit used to be a 500."""
    with TestClient(create_app(build_deps())) as client:
        r = client.post("/v1/chat/completions", json={**CHAT, "model": "nope", "tools": _nested(levels)})
    assert r.status_code == code, r.text[:200]
    if code == 400:
        assert r.json()["error"]["code"] == "invalid_json"


def test_an_api_body_nested_near_the_recursion_limit_is_a_400():
    """`read_json_object` caught ValueError only: a session's `POST
    /api/downloads` nested just under the recursion limit parsed, then
    `_refuse_non_finite` recursed past it, and the answer was a 500."""
    import sys

    raw = '{"a": ' + "[" * (sys.getrecursionlimit() - 30) + "]" * (sys.getrecursionlimit() - 30) + "}"
    with TestClient(create_app(build_deps())) as client:
        r = client.post("/api/downloads", content=raw, headers={"content-type": "application/json"})
    assert r.status_code == 400, r.text[:200]
