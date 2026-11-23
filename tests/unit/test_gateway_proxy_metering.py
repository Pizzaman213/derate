"""Metering integrity: what was served is counted (findings H5, M4, L9, L11).

A key holder used to be able to spend without being counted -- hang up before
``[DONE]``, or ask the upstream for no usage frame -- and a realtime session
answered to no limit but the allowlist. Every test here drives the real
gateway (or the real ProviderService) and asserts on what was counted: the
request row, the key's limit state, the provider's day.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from control_plane.contracts.auth import LimitScope
from control_plane.gateway import create_app
from control_plane.limits.service import estimate_prompt_tokens
from tests.unit.conftest import BOOT_PASSWORD, credentials
from tests.unit.test_gateway import (
    SECRET_KEY,
    FakeBackend,
    FakeDeployments,
    FakeProviders,
    RunningBackend,
    RunningServer,
    _free_port,
    build_deps,
    make_deployment,
    make_provider,
)
from tests.unit.test_providers import Upstream as ProviderUpstream
from tests.unit.test_providers import add_openrouter, make_service

LOCAL = "local-m"
CLOUD = "cloud-m"
MIXED = "mixed-m"
PROMPT = {"model": CLOUD, "messages": [{"role": "user", "content": "x" * 400}]}


def _sse_without_usage(frames: int = 4) -> bytes:
    out = b""
    for i in range(frames):
        chunk = {"id": "c", "object": "chat.completion.chunk",
                 "choices": [{"index": 0, "delta": {"content": f"t{i}"}}]}
        out += f"data: {json.dumps(chunk)}\n\n".encode()
    return out + b"data: [DONE]\n\n"


class Upstream:
    """Every backend behind one MockTransport. Streams carry no usage frame,
    and every answer tries to set a cookie on the client."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def body(self, i: int = -1) -> dict:
        return json.loads(self.requests[i].content or b"{}")

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        body = json.loads(request.content or b"{}")
        headers = {
            "set-cookie": "upstream_session=abc; Path=/",
            "x-upstream": "1",
            "openai-organization": "org-derate-account",
            "x-ratelimit-remaining-requests": "4999",
            "x-request-id": "upstream-own-id",
            "cache-control": "no-cache",
        }
        if body.get("stream"):
            headers["content-type"] = "text/event-stream"
            return httpx.Response(200, headers=headers, stream=httpx.ByteStream(_sse_without_usage()))
        headers["content-type"] = "application/json"
        payload = {
            "id": "c", "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        return httpx.Response(200, headers=headers, stream=httpx.ByteStream(json.dumps(payload).encode()))


class Rows:
    def __init__(self) -> None:
        self.rows: list = []

    def request(self, record) -> None:
        self.rows.append(record)

    def __getattr__(self, name):
        return lambda *a, **k: None


def _wire(app) -> Rows:
    rows = Rows()
    app.state.ctx.sink = rows
    app.state.ctx.proxy._sink = rows
    return rows


def _mint(app, **limits) -> tuple[str, str]:
    """A fresh key with ``limits``: ``(key_id, secret)``."""
    cookie, _ = credentials(app)
    r = TestClient(app).post(
        "/api/keys",
        json={"name": "metered", "expiry_days": 30, "limits": limits,
              "current_password": BOOT_PASSWORD},
        headers={"Cookie": f"derate_session={cookie}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return body["key"]["key_id"], body["secret_once"]


def _state(app, key_id: str, name: str):
    states = app.state.ctx.auth_stores.limits.states(LimitScope.KEY, key_id)
    return {s.limit_type: s for s in states}[name]


@pytest.fixture
def mocked():
    upstream = Upstream()
    deps = build_deps(
        deployments=FakeDeployments(
            [
                make_deployment("d-local", LOCAL, backend_url="http://local-backend/v1"),
                make_deployment("d-mixed", MIXED, backend_url="http://local-backend/v1"),
            ]
        ),
        providers=FakeProviders(
            [
                make_provider("openrouter", base_url="http://cloud/v1", served_name=CLOUD,
                              upstream_id="up/cloud"),
                make_provider("other", base_url="http://cloud/v1", served_name=MIXED,
                              upstream_id="up/mixed"),
            ]
        ),
    )
    app = create_app(deps, http_client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    rows = _wire(app)
    return app, upstream, rows


# -- (a) a stream with no usage frame is metered on the admission estimate ------


def test_a_stream_without_a_usage_frame_records_the_prompt_estimate_not_zero(mocked):
    app, upstream, rows = mocked
    _, key = _mint(app)
    body = {**PROMPT, "stream": True}
    with TestClient(app) as client:
        reply = client.post("/v1/chat/completions", json=body,
                            headers={"Authorization": f"Bearer {key}"})
    assert reply.status_code == 200
    row = [r for r in rows.rows if r.status == 200][-1]
    assert row.prompt_tokens == estimate_prompt_tokens(body) > 0
    assert row.tokens_estimated is True
    assert row.completion_tokens == 4  # one per frame, with no usage block


# -- (b) a body past the 1 MiB head is still counted (finding H1) ---------------


def _big_body_app(payload: bytes):
    """The gateway over one cloud target that answers every request with
    ``payload``."""

    def answer(request: httpx.Request) -> httpx.Response:
        request.read()
        return httpx.Response(200, headers={"content-type": "application/json"},
                              stream=httpx.ByteStream(payload))

    deps = build_deps(
        deployments=FakeDeployments([]),
        providers=FakeProviders([make_provider("openrouter", base_url="http://cloud/v1",
                                               served_name=CLOUD, upstream_id="up/cloud")]),
    )
    app = create_app(deps, http_client=httpx.AsyncClient(transport=httpx.MockTransport(answer)))
    return app, _wire(app)


def _two_mib_choices() -> str:
    logprobs = [{"token": "x", "logprob": -0.1, "top_logprobs": []}] * 40000
    return json.dumps([{"index": 0, "message": {"role": "assistant", "content": "x"},
                        "logprobs": {"content": logprobs}, "finish_reason": "length"}])


def test_a_body_past_the_head_limit_records_its_usage_block_not_zero():
    payload = ('{"id":"c","object":"chat.completion","choices":' + _two_mib_choices()
               + ',"usage":{"prompt_tokens":10,"completion_tokens":5000}}').encode()
    assert len(payload) > 2 * 1024 * 1024
    app, rows = _big_body_app(payload)
    key_id, key = _mint(app, cloud_tokens_cycle=10_000_000)
    with TestClient(app) as client:
        reply = client.post("/v1/chat/completions", json=PROMPT,
                            headers={"Authorization": f"Bearer {key}"})
    assert reply.status_code == 200
    assert len(reply.content) == len(payload)
    row = [r for r in rows.rows if r.status == 200][-1]
    assert row.completion_tokens == 5000
    assert row.prompt_tokens == 10
    assert row.tokens_estimated is False
    assert _state(app, key_id, "cloud_tokens_cycle").used == 5010


def test_a_large_body_with_no_usage_block_settles_an_estimate_not_zero():
    payload = ('{"id":"c","object":"chat.completion","choices":'
               + _two_mib_choices() + "}").encode()
    assert len(payload) > 1024 * 1024
    app, rows = _big_body_app(payload)
    key_id, key = _mint(app, cloud_tokens_cycle=10_000_000)
    with TestClient(app) as client:
        reply = client.post("/v1/chat/completions", json=PROMPT,
                            headers={"Authorization": f"Bearer {key}"})
    assert reply.status_code == 200
    row = [r for r in rows.rows if r.status == 200][-1]
    assert row.tokens_estimated is True
    assert row.completion_tokens > 0
    assert _state(app, key_id, "cloud_tokens_cycle").used >= row.completion_tokens


def test_a_size_estimate_never_exceeds_what_the_request_could_produce():
    """With no usage block the output is estimated from the body's size, which
    counts JSON and logprobs too -- ~500k tokens for 2 MiB. When the request
    named a ceiling the engine stopped there, so the estimate is settled at it
    rather than charging a key's whole cycle for one call."""
    payload = ('{"id":"c","object":"chat.completion","choices":'
               + _two_mib_choices() + "}").encode()
    app, rows = _big_body_app(payload)
    key_id, key = _mint(app, cloud_tokens_cycle=10_000_000)
    with TestClient(app) as client:
        reply = client.post("/v1/chat/completions", json={**PROMPT, "max_tokens": 300},
                            headers={"Authorization": f"Bearer {key}"})
    assert reply.status_code == 200
    row = [r for r in rows.rows if r.status == 200][-1]
    assert (row.tokens_estimated, row.completion_tokens) == (True, 300)
    assert _state(app, key_id, "cloud_tokens_cycle").used == (row.prompt_tokens or 0) + 300


def test_a_large_transcription_keeps_its_duration_for_per_minute_pricing():
    # Driven on the accounting directly: the FakeBackend's transcription route
    # answers one fixed small body and no fixture carries a per-minute price,
    # so the read H1 broke is pinned where it happens.
    from control_plane.gateway.proxy import _NoAccounting

    segments = json.dumps([{"id": i, "start": i, "end": i + 1, "text": "word " * 20}
                           for i in range(3000)])
    for body in (
        '{"task":"transcribe","duration":3000.5,"segments":' + segments + "}",
        '{"task":"transcribe","segments":' + segments + ',"duration":3000.5}',
    ):
        raw = body.encode()
        assert len(raw) > _NoAccounting.LIMIT
        accounting = _NoAccounting(0.0)
        for i in range(0, len(raw), 4096):
            accounting.note_chunk(raw[i : i + 4096], 0.0)
        assert accounting.finish(0.0).audio_duration_s == 3000.5


# -- (c) local streams always ask for usage ------------------------------------


def test_a_local_stream_asks_for_usage_whatever_the_client_sent(mocked):
    app, upstream, _ = mocked
    _, key = _mint(app)
    body = {"model": LOCAL, "messages": [{"role": "user", "content": "hi"}], "stream": True,
            "stream_options": {"include_usage": False, "continuous_usage_stats": False}}
    with TestClient(app) as client:
        reply = client.post("/v1/chat/completions", json=body,
                            headers={"Authorization": f"Bearer {key}"})
    assert reply.status_code == 200
    sent = upstream.body()
    assert sent["stream_options"] == {"include_usage": True, "continuous_usage_stats": False}


def test_a_remote_provider_that_supports_stream_usage_is_asked_over_the_client(tmp_path):
    provider_upstream = ProviderUpstream()
    service = make_service(tmp_path, provider_upstream)
    add_openrouter(service)
    body = {"model": "x", "messages": [{"role": "user", "content": "hi"}],
            "stream_options": {"include_usage": False}}

    async def run():
        async with service.open_upstream("openrouter", "openai/gpt-4o-mini", body, True) as up:
            async for _ in up.body:
                pass

    asyncio.run(run())
    sent = json.loads(provider_upstream.requests[-1].content)
    assert sent["stream_options"] == {"include_usage": True}


# -- (b) a client that hangs up is still counted -------------------------------


def test_a_client_that_hangs_up_mid_stream_is_counted_against_its_key():
    backend = FakeBackend(chunk_delay=0.2, chunks=20)
    with RunningBackend(backend) as running:
        deps = build_deps(
            deployments=FakeDeployments(
                [make_deployment("d-1", "llama-3.3-70b", backend_url=running.base_url)]
            )
        )
        app = create_app(deps)
        rows = _wire(app)
        key_id, key = _mint(app, requests_cycle=10, rpm=10)
        with RunningServer(app) as gateway, httpx.Client(timeout=30) as client:
            body = {"model": "llama-3.3-70b", "stream": True,
                    "messages": [{"role": "user", "content": "y" * 200}]}
            with client.stream("POST", f"{gateway.url}/v1/chat/completions", json=body,
                               headers={"Authorization": f"Bearer {key}"}) as response:
                assert response.status_code == 200
                next(response.iter_raw())  # one chunk, then walk away
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not any(
                r.error_class == "ClientDisconnected" for r in rows.rows
            ):
                time.sleep(0.05)
            row = next(r for r in rows.rows if r.error_class == "ClientDisconnected")
            assert row.status == 200 and row.cancelled
            assert row.prompt_tokens > 0
            assert _state(app, key_id, "requests_cycle").used == 1
            assert _state(app, key_id, "rpm").used == 1  # the window entry was kept


def test_a_provider_stream_the_client_walked_away_from_is_billed_to_its_day(tmp_path):
    provider_upstream = ProviderUpstream()
    provider_upstream.chat_responses.append(
        httpx.Response(200, headers={"content-type": "text/event-stream"},
                       stream=httpx.ByteStream(_sse_without_usage(6)))
    )
    service = make_service(tmp_path, provider_upstream)
    add_openrouter(service)
    body = {"model": "x", "messages": [{"role": "user", "content": "z" * 4000}]}

    async def run():
        manager = service.open_upstream("openrouter", "openai/gpt-4o-mini", body, True)
        up = await manager.__aenter__()
        await anext(up.body)
        # What the gateway's proxy hands the provider when its client hangs up.
        exit_ = GeneratorExit()
        await manager.__aexit__(GeneratorExit, exit_, None)

    asyncio.run(run())
    day = service._entries["openrouter"].runtime.day(time.time())
    assert day.requests == 1
    assert day.input_tokens == estimate_prompt_tokens(body)
    assert service.spend_today("openrouter") > 0


def test_a_provider_body_nobody_read_is_not_billed(tmp_path):
    """A hedge that lost: the body was never started, so it is not ours."""
    service = make_service(tmp_path)
    add_openrouter(service)
    body = {"model": "x", "messages": [{"role": "user", "content": "z" * 4000}]}

    class Discarded(Exception):
        pass

    async def run():
        manager = service.open_upstream("openrouter", "openai/gpt-4o-mini", body, True)
        await manager.__aenter__()
        reason = Discarded()
        with pytest.raises(Discarded):
            if not await manager.__aexit__(Discarded, reason, None):
                raise reason

    asyncio.run(run())
    assert service._entries["openrouter"].runtime.day(time.time()).requests == 0


# -- (f) a name with both kinds answers to the cloud limits ----------------------


def test_a_name_served_both_locally_and_remotely_enforces_the_cloud_cycle_limit(mocked):
    app, _, rows = mocked
    _, key = _mint(app, cloud_tokens_cycle=1)
    with TestClient(app) as client:
        # A remote-only name spends the cloud pool (15 tokens against 1)...
        spent = client.post(
            "/v1/chat/completions",
            json={"model": CLOUD, "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert spent.status_code == 200
        # ...and a name with a local target too still answers to it.
        reply = client.post(
            "/v1/chat/completions",
            json={"model": MIXED, "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {key}"},
        )
    assert reply.status_code == 429
    assert reply.json()["error"]["limit_source"] == "key:cloud_tokens_cycle"


# -- (g) only the request's own headers go upstream, and no cookie comes back ----


def test_client_routing_headers_are_not_forwarded_and_upstream_cookies_not_returned(mocked):
    app, upstream, _ = mocked
    _, key = _mint(app)
    with TestClient(app) as client:
        reply = client.post(
            "/v1/chat/completions",
            json={"model": CLOUD, "messages": [{"role": "user", "content": "hi"}]},
            headers={
                "Authorization": f"Bearer {key}",
                "OpenAI-Organization": "org-somebody-else",
                "X-Forwarded-For": "203.0.113.9",
                "Cookie": "derate_session=nope",
                "OpenAI-Beta": "assistants=v2",
                "X-Request-Id": "forged",
            },
        )
    assert reply.status_code == 200
    sent = upstream.requests[-1].headers
    assert "openai-organization" not in sent
    assert "x-forwarded-for" not in sent
    assert "cookie" not in sent
    assert sent["authorization"] == f"Bearer {SECRET_KEY}"
    assert sent["openai-beta"] == "assistants=v2"
    # K10: the upstream is sent derate's trace id, never the client's.
    assert sent["x-request-id"] == reply.headers["x-request-id"]
    assert sent["x-request-id"].startswith("r-")
    # K4: asked uncompressed, so an error body can be scrubbed before relay.
    assert sent["accept-encoding"] == "identity"
    # K6: an allowlist -- derate's account with the provider stays here.
    assert "set-cookie" not in reply.headers
    assert "x-upstream" not in reply.headers
    assert "openai-organization" not in reply.headers
    assert "x-ratelimit-remaining-requests" not in reply.headers
    assert reply.headers["x-request-id"] != "upstream-own-id"


# -- (d) a realtime session holds a concurrency slot -------------------------------


class _RealtimeUpstream:
    """A websockets server on its own loop, in a thread."""

    def __init__(self) -> None:
        self.port = _free_port()
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        import websockets

        async def handler(conn):
            await conn.send(json.dumps({"type": "session.created"}))
            async for _ in conn:
                pass

        async def main():
            self._server = await websockets.serve(handler, "127.0.0.1", self.port)
            self._ready.set()
            await self._server.wait_closed()

        self._loop.run_until_complete(main())

    def __enter__(self):
        self._thread.start()
        assert self._ready.wait(10)
        return self

    def __exit__(self, *exc):
        self._loop.call_soon_threadsafe(self._server.close)
        self._thread.join(timeout=10)


def test_a_second_realtime_session_over_max_concurrent_is_refused():
    """The first session is held over a real socket (a relay that runs until
    its client leaves); the second is refused before accept, where the close
    code and reason are what the client sees."""
    import websockets

    with _RealtimeUpstream() as upstream:
        provider = make_provider(
            "openai", base_url=f"http://127.0.0.1:{upstream.port}/v1",
            served_name="voice", upstream_id="gpt-4o-realtime",
        )
        app = create_app(build_deps(providers=FakeProviders([provider])))
        key_id, key = _mint(app, max_concurrent=1)
        auth = {"Authorization": f"Bearer {key}"}

        def second_session() -> WebSocketDisconnect:
            # A copy: TestClient adds its handshake headers to the dict it is given.
            with pytest.raises(WebSocketDisconnect) as caught:
                with TestClient(app).websocket_connect(
                    "/v1/realtime?model=voice", headers=dict(auth)
                ) as second:
                    second.receive()
            return caught.value

        async def scenario(url: str):
            async with websockets.connect(url, additional_headers=auth) as first:
                hello = json.loads(await asyncio.wait_for(first.recv(), 10))
                held = _state(app, key_id, "max_concurrent").used
                refused = await asyncio.to_thread(second_session)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and _state(app, key_id, "max_concurrent").used:
                await asyncio.sleep(0.05)
            # The slot came back when the first session closed.
            async with websockets.connect(url, additional_headers=auth) as third:
                again = json.loads(await asyncio.wait_for(third.recv(), 10))
            return hello, held, refused, again

        with RunningServer(app) as gateway:
            hello, held, refused, again = asyncio.run(
                scenario(f"ws://127.0.0.1:{gateway.port}/v1/realtime?model=voice")
            )

    assert hello["type"] == "session.created"
    assert held == 1
    assert (refused.code, refused.reason) == (1008, "limit_exceeded")
    assert again["type"] == "session.created"
