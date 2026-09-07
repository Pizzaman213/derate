"""Package P-GW-B: audit rows H-9, H-1 (owed unit test), M-13, M-14, plus the
strict-deps and static-UI enablers for C-1.

This file owns tests for control_plane/gateway/{app,deps,proxy,openai_api,
metrics,stats,settings}.py. tests/test_gateway.py is a sibling package's file
this wave; nothing here duplicates it, and nothing here imports from it, so
this file has no dependency on how that one evolves.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from control_plane.contracts import (
    Deployment,
    DeploymentState,
    Provider,
    ProviderKind,
    ProviderModel,
    RouteTarget,
    TargetKind,
)
from control_plane.gateway import GatewayDeps, GatewaySettings, create_app
from control_plane.gateway import app as app_module
from control_plane.gateway import stubs
from control_plane.gateway.proxy import (
    RETRY_SERVER_ERROR,
    RETRY_TRANSPORT,
    UpstreamProxy,
)
from control_plane.gateway.stats import StatsRegistry
from control_plane.providers import (
    AdapterUnsupportedError,
    MissingKeyError,
    ProviderNotAdmittingError,
    ProviderService,
    SecretStore,
    UpstreamError,
    UpstreamResponse,
)
from control_plane.telemetry import RequestTrace

# ---------------------------------------------------------------------------
# shared fakes
# ---------------------------------------------------------------------------


class _EmptyRegistry:
    """A registry with members none of the time. Enough surface for
    AdmissionController, Router and MetricsHub to poll without crashing."""

    def list_nodes(self):
        return []

    def get_node(self, node_id):
        return None

    def healthy_nodes(self):
        return []


class _NoopDeployments:
    def list(self):
        return []

    def get(self, deployment_id):
        return None


def _route_target(target_id="d-1", *, kind=TargetKind.LOCAL, backend_url="http://upstream") -> RouteTarget:
    return RouteTarget(
        target_id=target_id,
        kind=kind,
        backend_url=backend_url,
        weight=1.0,
        outstanding=0,
        healthy=True,
        admitting=True,
        strength=1.0,
        cost_per_mtok=None,
    )


class _ChunkStream(httpx.AsyncByteStream):
    """A hand-rolled httpx response body, so a MockTransport can stream many
    chunks and tell us afterwards whether it was ever closed."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def _make_provider(
    *,
    served_name="remote-model",
    upstream_id="vendor/remote-model",
    provider_id="openrouter",
) -> Provider:
    return Provider(
        provider_id=provider_id,
        kind=ProviderKind.OPENROUTER,
        display_name="OpenRouter",
        base_url="https://example.invalid/v1",
        api_key_ref="OPENROUTER_API_KEY",
        enabled=True,
        priority=10,
        models=[
            ProviderModel(
                served_name=served_name,
                upstream_id=upstream_id,
                context_length=131072,
                supports_streaming=True,
                supports_tools=True,
                input_cost_per_mtok=0.1,
                output_cost_per_mtok=0.3,
            )
        ],
        healthy=True,
    )


class _FakeOpenUpstreamProviders:
    """A ProviderPort double whose open_upstream is a local async generator.

    No socket needed to prove the gateway calls this instead of resolving a
    raw key: resolve_key records every call it never expects to receive.
    """

    def __init__(self, providers, *, chunks=None, status=200):
        self.providers = list(providers)
        self.key_requests: list[str] = []
        self.calls: list[tuple] = []
        self._chunks = list(chunks) if chunks is not None else [b'data: {"id":"x"}\n\n', b"data: [DONE]\n\n"]
        self._status = status

    def list(self):
        return list(self.providers)

    def add(self, spec):
        raise NotImplementedError

    def refresh(self, provider_id):
        return next(p for p in self.providers if p.provider_id == provider_id)

    def models(self):
        return [(p.provider_id, m) for p in self.providers for m in p.models]

    def resolve_key(self, provider_id):
        self.key_requests.append(provider_id)
        return "SHOULD-NEVER-BE-RESOLVED"

    def health(self, provider_id):
        return (True, None)

    @asynccontextmanager
    async def open_upstream(self, provider_id, upstream_id, body, stream=False, *, endpoint="chat/completions"):
        self.calls.append((provider_id, upstream_id, dict(body), stream, endpoint))

        async def body_iter():
            for chunk in self._chunks:
                yield chunk

        yield UpstreamResponse(
            status_code=self._status,
            headers={"content-type": "text/event-stream"},
            media_type="text/event-stream",
            body=body_iter(),
        )


def _raising_open_upstream(exc: BaseException):
    """A fake open_upstream whose __aenter__ raises before ever yielding --
    exactly how ProviderService._prepare()'s failures surface."""

    @asynccontextmanager
    async def open_upstream(provider_id, upstream_id, body, stream=False, *, endpoint="chat/completions"):
        raise exc
        yield  # pragma: no cover -- unreachable; keeps this an async generator

    return open_upstream


class _SpyBreaker:
    """Records what the breaker was told, so a dead target and a reachable
    one that merely answered badly can be told apart in a test (audit H-9)."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def record_transport_failure(self, target_id: str) -> None:
        self.events.append(("transport_failure", target_id))

    def record_success(self, target_id: str) -> None:
        self.events.append(("success", target_id))


class _RecordingSink:
    """A TelemetrySink that keeps every request record, for asserting that
    one was actually written (audit H-9's third refutation: forward_provider
    called no sink at all)."""

    def __init__(self) -> None:
        self.records: list = []

    def sample(self, node_id, sample) -> None:
        pass

    def request(self, record) -> None:
        self.records.append(record)

    def event(self, source, event) -> None:
        pass

    def log(self, entry) -> None:
        pass


# ---------------------------------------------------------------------------
# H-1 (owed unit test): a client disconnect must settle exactly once
# ---------------------------------------------------------------------------


def test_forward_settles_exactly_once_on_a_client_disconnect():
    """Audit H-1, unit-level. GeneratorExit on body_iterator.aclose() derives
    from BaseException, so it never reaches an `except Exception` -- settle()
    has to run from the generator's `finally` or the in-flight count and the
    on_finish release both leak forever."""

    async def run() -> None:
        upstream = _ChunkStream([f"data: chunk-{i}\n\n".encode() for i in range(50)])

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=upstream
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            proxy = UpstreamProxy(GatewaySettings(), client=client)
            stats = StatsRegistry()
            selection = SimpleNamespace(target=_route_target("d-1"))
            finished: list[int] = []

            attempt = await proxy.forward(
                selection=selection,
                path="/chat/completions",
                body={"model": "m", "messages": []},
                client_headers={},
                api_key=None,
                stats=stats,
                streaming=True,
                on_finish=lambda: finished.append(1),
            )

            assert not attempt.retryable
            body_iterator = attempt.response.body_iterator
            for _ in range(3):
                await anext(body_iterator)
            assert stats.outstanding("d-1") == 1, "still mid-stream"

            await body_iterator.aclose()  # what an ASGI server does on disconnect

            assert upstream.closed, "the upstream response was never closed"
            assert stats.outstanding("d-1") == 0, "in-flight count leaked on disconnect"
            assert finished == [1], "on_finish must fire exactly once"
        finally:
            await client.aclose()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# H-9: UpstreamProxy.forward_provider() -- the Attempt-contract adapter
# ---------------------------------------------------------------------------


def test_forward_provider_streams_through_open_upstream_and_settles_once():
    async def run() -> None:
        chunks = [b"data: a\n\n", b"data: b\n\n", b"data: [DONE]\n\n"]
        providers = _FakeOpenUpstreamProviders([_make_provider()], chunks=chunks)
        proxy = UpstreamProxy(GatewaySettings())
        stats = StatsRegistry()
        target = _route_target("openrouter:vendor/remote-model", kind=TargetKind.REMOTE)
        finished: list[int] = []

        attempt = await proxy.forward_provider(
            target=target,
            provider_id="openrouter",
            upstream_id="vendor/remote-model",
            path="/chat/completions",
            body={"model": "remote-model", "messages": []},
            stats=stats,
            streaming=True,
            open_upstream=providers.open_upstream,
            on_finish=lambda: finished.append(1),
        )

        assert not attempt.retryable
        assert attempt.response.status_code == 200
        body = b"".join([chunk async for chunk in attempt.response.body_iterator])
        assert body == b"".join(chunks)
        assert stats.outstanding(target.target_id) == 0
        assert finished == [1]

        # It went through the provider's own open_upstream, unmutated -- the
        # provider service rewrites `model` itself in _prepare().
        provider_id, upstream_id, sent_body, stream, endpoint = providers.calls[0]
        assert provider_id == "openrouter"
        assert upstream_id == "vendor/remote-model"
        assert sent_body["model"] == "remote-model"
        assert stream is True
        assert endpoint == "/chat/completions"

    asyncio.run(run())


def test_forward_provider_not_admitting_settles_and_signals_try_another():
    """ProviderNotAdmittingError happens before anything is sent. It is not
    retryable against this target in the Attempt sense (there is no backend
    answer to preserve) -- the caller reads None as "exclude and continue"."""

    async def run() -> None:
        exc = ProviderNotAdmittingError("openrouter", "rate limited for another 5s", retry_after_s=5.0)
        proxy = UpstreamProxy(GatewaySettings())
        stats = StatsRegistry()
        target = _route_target("openrouter:x", kind=TargetKind.REMOTE)
        finished: list[int] = []

        attempt = await proxy.forward_provider(
            target=target,
            provider_id="openrouter",
            upstream_id="x",
            path="/chat/completions",
            body={"model": "x"},
            stats=stats,
            streaming=False,
            open_upstream=_raising_open_upstream(exc),
            on_finish=lambda: finished.append(1),
        )

        assert attempt is None
        assert stats.outstanding(target.target_id) == 0
        assert finished == [1], "the attempt was claimed and must still settle"

    asyncio.run(run())


def test_forward_provider_5xx_is_retryable_and_preserves_the_backends_words():
    async def run() -> None:
        exc = UpstreamError("openrouter", 503, "backend overloaded", body='{"error":"overloaded"}')
        proxy = UpstreamProxy(GatewaySettings())
        stats = StatsRegistry()
        target = _route_target("openrouter:x", kind=TargetKind.REMOTE)

        attempt = await proxy.forward_provider(
            target=target,
            provider_id="openrouter",
            upstream_id="x",
            path="/chat/completions",
            body={"model": "x"},
            stats=stats,
            streaming=False,
            open_upstream=_raising_open_upstream(exc),
        )

        assert attempt.retryable
        assert attempt.reason == RETRY_SERVER_ERROR
        assert attempt.status == 503
        assert attempt.body == b'{"error":"overloaded"}'
        assert stats.outstanding(target.target_id) == 0

    asyncio.run(run())


def test_forward_provider_4xx_is_not_retryable_and_returns_the_response():
    async def run() -> None:
        exc = UpstreamError(
            "openrouter", 429, "rate limited", body='{"error":"slow down"}', retry_after_s=2.0
        )
        proxy = UpstreamProxy(GatewaySettings())
        stats = StatsRegistry()
        target = _route_target("openrouter:x", kind=TargetKind.REMOTE)

        attempt = await proxy.forward_provider(
            target=target,
            provider_id="openrouter",
            upstream_id="x",
            path="/chat/completions",
            body={"model": "x"},
            stats=stats,
            streaming=False,
            open_upstream=_raising_open_upstream(exc),
        )

        assert not attempt.retryable
        assert attempt.response.status_code == 429
        assert attempt.response.body == b'{"error":"slow down"}'
        assert attempt.response.headers["retry-after"] == "2"

    asyncio.run(run())


def test_forward_provider_transport_failure_is_retryable():
    async def run() -> None:
        proxy = UpstreamProxy(GatewaySettings())
        stats = StatsRegistry()
        target = _route_target("openrouter:x", kind=TargetKind.REMOTE)

        attempt = await proxy.forward_provider(
            target=target,
            provider_id="openrouter",
            upstream_id="x",
            path="/chat/completions",
            body={"model": "x"},
            stats=stats,
            streaming=False,
            open_upstream=_raising_open_upstream(httpx.ConnectError("boom")),
        )

        assert attempt.retryable
        assert attempt.reason == RETRY_TRANSPORT
        assert attempt.error == "ConnectError"
        assert stats.outstanding(target.target_id) == 0

    asyncio.run(run())


def test_forward_provider_transport_failure_through_the_real_provider_service(tmp_path):
    """Audit H-9 refutation: the test above uses a fake whose __aenter__
    raises httpx.ConnectError directly, but the real ProviderService never
    lets that happen -- it wraps every unreachable-upstream failure as a 502
    UpstreamError with no body before the gateway ever sees it
    (service.py's open_upstream, its own `except httpx.HTTPError` branch).
    This is the shape production traffic actually hits; it must still come
    out as RETRY_TRANSPORT, and it must tell the breaker a failure, not a
    success -- a dead remote must be able to trip the breaker, and a
    half-open probe against it must not immediately re-close the circuit."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "vendor/remote-model",
                            "context_length": 4096,
                            "pricing": {"prompt": "0", "completion": "0"},
                        }
                    ]
                },
            )
        raise httpx.ConnectError("boom", request=request)

    async def run() -> None:
        secrets = SecretStore(tmp_path / "secrets.json", env={"OPENROUTER_API_KEY": "sk-test"})
        service = ProviderService(
            data_path=tmp_path,
            secrets=secrets,
            client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        await service.add_async(
            {
                "provider_id": "openrouter",
                "kind": ProviderKind.OPENROUTER,
                "api_key_ref": "OPENROUTER_API_KEY",
                "priority": 10,
            }
        )

        spy = _SpyBreaker()
        proxy = UpstreamProxy(GatewaySettings(), breaker=spy)
        stats = StatsRegistry()
        target = _route_target("openrouter:vendor/remote-model", kind=TargetKind.REMOTE)

        attempt = await proxy.forward_provider(
            target=target,
            provider_id="openrouter",
            upstream_id="vendor/remote-model",
            path="/chat/completions",
            body={"model": "remote-model", "messages": []},
            stats=stats,
            streaming=False,
            open_upstream=service.open_upstream,
        )

        assert attempt.retryable
        assert attempt.reason == RETRY_TRANSPORT
        assert attempt.error == "ConnectError"
        assert attempt.status is None
        assert not attempt.body, "a transport failure has no backend words to replay"
        assert stats.outstanding(target.target_id) == 0
        assert spy.events == [("transport_failure", target.target_id)], (
            "a dead remote must trip the breaker as a failure, not a success"
        )

    asyncio.run(run())


def test_forward_provider_distinguishes_a_genuine_5xx_from_a_disguised_transport_failure():
    """A dead-remote 502 (ProviderService's own stand-in for a transport
    failure) and a genuine 502 answered by a reachable backend must not be
    indistinguishable to the breaker or to the client (audit H-9)."""

    async def run() -> None:
        stats = StatsRegistry()
        target = _route_target("openrouter:x", kind=TargetKind.REMOTE)

        disguised = UpstreamError("openrouter", 502, "could not reach upstream: ConnectError")
        spy1 = _SpyBreaker()
        attempt1 = await UpstreamProxy(GatewaySettings(), breaker=spy1).forward_provider(
            target=target,
            provider_id="openrouter",
            upstream_id="x",
            path="/chat/completions",
            body={"model": "x"},
            stats=stats,
            streaming=False,
            open_upstream=_raising_open_upstream(disguised),
        )
        assert attempt1.retryable
        assert attempt1.reason == RETRY_TRANSPORT
        assert attempt1.error == "ConnectError"
        assert not attempt1.body
        assert spy1.events == [("transport_failure", target.target_id)]

        genuine = UpstreamError("openrouter", 502, "Bad Gateway", body='{"error":"bad gateway"}')
        spy2 = _SpyBreaker()
        attempt2 = await UpstreamProxy(GatewaySettings(), breaker=spy2).forward_provider(
            target=target,
            provider_id="openrouter",
            upstream_id="x",
            path="/chat/completions",
            body={"model": "x"},
            stats=stats,
            streaming=False,
            open_upstream=_raising_open_upstream(genuine),
        )
        assert attempt2.retryable
        assert attempt2.reason == RETRY_SERVER_ERROR
        assert attempt2.status == 502
        assert attempt2.body == b'{"error":"bad gateway"}', "a reachable backend's own words, replayed verbatim"
        assert spy2.events == [("success", target.target_id)], (
            "it answered -- badly, but it answered -- so the transport verdict is success"
        )

    asyncio.run(run())


def test_forward_provider_missing_key_answers_key_unavailable_not_capacity():
    """Audit H-9 refutation: MissingKeyError fell into a bare `except
    ProviderError` that excluded the target and let the loop report
    no_target_admitting -- capacity -- for what is actually the single most
    likely first-run failure: an unset api_key_ref. It must answer exactly
    as the raw-key path does (openai_api._dispatch's resolve_key branch)."""

    async def run() -> None:
        sink = _RecordingSink()
        proxy = UpstreamProxy(GatewaySettings(), sink=sink)
        stats = StatsRegistry()
        target = _route_target("openrouter:x", kind=TargetKind.REMOTE)
        trace = RequestTrace(request_id="r-key", served_name="x")

        attempt = await proxy.forward_provider(
            target=target,
            provider_id="openrouter",
            upstream_id="x",
            path="/chat/completions",
            body={"model": "x"},
            stats=stats,
            streaming=False,
            open_upstream=_raising_open_upstream(
                MissingKeyError("openrouter", "OPENROUTER_API_KEY")
            ),
            trace=trace,
            attempt_no=0,
        )

        assert not attempt.retryable, "not a backend failure to offer to another target"
        assert attempt.response.status_code == 502
        payload = json.loads(attempt.response.body)
        assert payload["error"]["code"] == "provider_key_unavailable"
        assert "OPENROUTER_API_KEY" not in attempt.response.body.decode(), (
            "never echo anything about key material, not even its reference"
        )
        assert stats.outstanding(target.target_id) == 0
        assert len(sink.records) == 1
        assert sink.records[0].error_code == "provider_key_unavailable"

    asyncio.run(run())


def test_forward_provider_adapter_unsupported_answers_a_config_error_not_capacity():
    """Same shape as the missing-key case: a fact about this target's wire
    format, not something excluding-and-continuing should hide as capacity."""

    async def run() -> None:
        proxy = UpstreamProxy(GatewaySettings())
        stats = StatsRegistry()
        target = _route_target("openrouter:x", kind=TargetKind.REMOTE)

        attempt = await proxy.forward_provider(
            target=target,
            provider_id="openrouter",
            upstream_id="x",
            path="/chat/completions",
            body={"model": "x"},
            stats=stats,
            streaming=False,
            open_upstream=_raising_open_upstream(
                AdapterUnsupportedError("provider 'openrouter': no adapter for this kind")
            ),
        )

        assert not attempt.retryable
        assert attempt.response.status_code == 502
        payload = json.loads(attempt.response.body)
        assert payload["error"]["code"] == "provider_adapter_unsupported"
        assert stats.outstanding(target.target_id) == 0

    asyncio.run(run())


def test_forward_provider_records_request_telemetry_when_a_trace_is_given():
    """Audit H-9 refutation: forward_provider took no trace/attempt_no and
    never called sink.request(...), so a request routed through
    open_upstream left no row a client could look up by its X-Request-Id,
    though the header was still returned."""

    async def run() -> None:
        sink = _RecordingSink()
        proxy = UpstreamProxy(GatewaySettings(), sink=sink)
        stats = StatsRegistry()
        target = _route_target("openrouter:vendor/remote-model", kind=TargetKind.REMOTE)
        selection = SimpleNamespace(
            target=target,
            config=None,
            provider=SimpleNamespace(provider_id="openrouter"),
            deployment=None,
        )
        trace = RequestTrace(request_id="r-tel", served_name="remote-model")
        chunks = [b"data: a\n\n", b"data: [DONE]\n\n"]
        providers = _FakeOpenUpstreamProviders([_make_provider()], chunks=chunks)

        attempt = await proxy.forward_provider(
            target=target,
            provider_id="openrouter",
            upstream_id="vendor/remote-model",
            path="/chat/completions",
            body={"model": "remote-model", "messages": []},
            stats=stats,
            streaming=True,
            open_upstream=providers.open_upstream,
            selection=selection,
            trace=trace,
            attempt_no=0,
        )
        body = b"".join([chunk async for chunk in attempt.response.body_iterator])
        assert body == b"".join(chunks)

        assert len(sink.records) == 1, "a request through open_upstream must still leave a telemetry row"
        record = sink.records[0]
        assert record.request_id == "r-tel"
        assert record.target_id == target.target_id
        assert record.provider_id == "openrouter"
        assert record.status == 200

    asyncio.run(run())


def test_forward_provider_settles_exactly_once_on_a_client_disconnect_mid_stream():
    """Audit H-9-test refutation: the remote path's disconnect-mid-stream
    contract had no test, and separately, stream_body's finally always told
    the provider's own context manager a clean exit (None, None, None) even
    when the client hung up -- so ProviderService.open_upstream would call
    an aborted transfer a success and run its post-yield success bookkeeping
    on it. This pins both: settle-exactly-once, no buffering (3 of 50 chunks
    is enough to prove it), and the real exception -- not a synthesized clean
    exit -- reaching the provider's context manager on disconnect."""

    async def run() -> None:
        aexit_calls: list[tuple] = []
        chunks = [f"data: chunk-{i}\n\n".encode() for i in range(50)]

        @asynccontextmanager
        async def open_upstream(provider_id, upstream_id, body, stream=False, *, endpoint="chat/completions"):
            async def body_iter():
                for chunk in chunks:
                    yield chunk

            try:
                yield UpstreamResponse(
                    status_code=200,
                    headers={"content-type": "text/event-stream"},
                    media_type="text/event-stream",
                    body=body_iter(),
                )
            except BaseException as exc:
                aexit_calls.append((type(exc), str(exc)))
                raise
            else:
                aexit_calls.append((None, None))

        proxy = UpstreamProxy(GatewaySettings())
        stats = StatsRegistry()
        target = _route_target("openrouter:x", kind=TargetKind.REMOTE)
        finished: list[int] = []

        attempt = await proxy.forward_provider(
            target=target,
            provider_id="openrouter",
            upstream_id="x",
            path="/chat/completions",
            body={"model": "x"},
            stats=stats,
            streaming=True,
            open_upstream=open_upstream,
            on_finish=lambda: finished.append(1),
        )

        body_iterator = attempt.response.body_iterator
        received = []
        for _ in range(3):
            received.append(await anext(body_iterator))
        assert received == chunks[:3], "unbuffered: the client sees bytes as they arrive, not after 50 chunks"
        assert stats.outstanding(target.target_id) == 1, "still mid-stream"

        await body_iterator.aclose()  # what an ASGI server does on disconnect

        assert stats.outstanding(target.target_id) == 0, "in-flight count leaked on disconnect"
        assert finished == [1], "on_finish must fire exactly once"
        assert aexit_calls, "the provider's context manager must be told the transfer ended"
        assert aexit_calls[0][0] in (GeneratorExit, asyncio.CancelledError), (
            "a disconnect must reach the provider as the real exception, not a clean exit -- "
            "otherwise ProviderService.open_upstream calls an aborted transfer a success and "
            "runs its post-yield success bookkeeping on it"
        )

    asyncio.run(run())


def test_remote_dispatch_prefers_open_upstream_over_a_raw_key():
    """End to end: a providers port that exposes open_upstream is used
    instead of the gateway resolving and forwarding a raw key (audit H-9)."""
    providers = _FakeOpenUpstreamProviders([_make_provider()])
    deps = GatewayDeps(registry=_EmptyRegistry(), deployments=_NoopDeployments(), providers=providers)

    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/v1/chat/completions",
            json={"model": "remote-model", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert reply.status_code == 200
    assert providers.key_requests == [], "the raw-key path must not have run"
    assert len(providers.calls) == 1
    provider_id, upstream_id, sent_body, _stream, _endpoint = providers.calls[0]
    assert provider_id == "openrouter"
    assert upstream_id == "vendor/remote-model"
    assert sent_body["model"] == "remote-model"


def test_remote_dispatch_falls_back_to_the_raw_key_without_open_upstream():
    """A providers port that predates open_upstream (the day-0 stub shape)
    must keep working exactly as before."""
    backend_calls = []

    class _RawKeyProviders:
        def __init__(self, providers):
            self.providers = list(providers)
            self.key_requests = []

        def list(self):
            return list(self.providers)

        def add(self, spec):
            raise NotImplementedError

        def refresh(self, provider_id):
            return next(p for p in self.providers if p.provider_id == provider_id)

        def models(self):
            return [(p.provider_id, m) for p in self.providers for m in p.models]

        def resolve_key(self, provider_id):
            self.key_requests.append(provider_id)
            return "sk-raw-path-key"

        def health(self, provider_id):
            return (True, None)

    import socket

    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def echo(request):
        backend_calls.append(await request.json())
        return JSONResponse({"id": "x", "choices": [{"message": {"content": "hi"}}]})

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    backend_app = Starlette(routes=[Route("/v1/chat/completions", echo, methods=["POST"])])
    config = uvicorn.Config(backend_app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)

    try:
        provider = _make_provider(provider_id="raw", upstream_id="raw/model")
        provider.base_url = f"http://127.0.0.1:{port}/v1"
        providers = _RawKeyProviders([provider])
        deps = GatewayDeps(registry=_EmptyRegistry(), deployments=_NoopDeployments(), providers=providers)

        with TestClient(create_app(deps)) as client:
            reply = client.post(
                "/v1/chat/completions",
                json={"model": "remote-model", "messages": [{"role": "user", "content": "hi"}]},
            )

        assert reply.status_code == 200
        assert providers.key_requests == ["raw"]
        assert backend_calls[0]["model"] == "raw/model"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# M-14: _optional_step awaits async methods; sync methods stay off-thread
# ---------------------------------------------------------------------------


def test_optional_step_awaits_an_async_method_directly():
    calls: list[str] = []

    class Port:
        async def start(self):
            calls.append("started")

    ctx = SimpleNamespace(settings=GatewaySettings(startup_step_timeout_s=1.0), degraded_startup=[])
    asyncio.run(app_module._optional_step(ctx, "port", Port(), "start"))

    assert calls == ["started"], "an async start() must actually run, not just be constructed"
    assert ctx.degraded_startup == []


def test_optional_step_still_runs_a_sync_method_off_the_event_loop():
    calls: list[str] = []

    class Port:
        def start(self):
            calls.append(threading.current_thread().name)

    ctx = SimpleNamespace(settings=GatewaySettings(), degraded_startup=[])
    asyncio.run(app_module._optional_step(ctx, "port", Port(), "start"))

    assert calls and calls[0] != threading.main_thread().name


def test_best_effort_shutdown_swallows_a_failing_teardown():
    class Boom:
        def close(self):
            raise RuntimeError("nope")

    # Must not raise: one component's teardown failing must not stop the rest
    # of shutdown from running.
    asyncio.run(app_module._best_effort_shutdown("x", Boom(), "close"))


def test_best_effort_shutdown_awaits_an_async_method():
    calls: list[str] = []

    class Port:
        async def aclose(self):
            calls.append("closed")

    asyncio.run(app_module._best_effort_shutdown("x", Port(), "aclose"))
    assert calls == ["closed"]


def test_deployments_reconcile_and_start_both_run_with_nothing_to_adopt():
    """M-14: reconcile() and start() are two separate steps, not alternative
    names for one. A fresh install has nothing to adopt, so reconcile()
    returning [] must not be the reason start() never runs."""

    class Port(_NoopDeployments):
        def __init__(self):
            self.reconciled = False
            self.started = False

        def reconcile(self):
            self.reconciled = True
            return []  # nothing to adopt

        def start(self):
            self.started = True

    port = Port()
    with TestClient(create_app(GatewayDeps(registry=_EmptyRegistry(), deployments=port))):
        pass

    assert port.reconciled
    assert port.started, "a fresh install must still start the watch loop"


def test_providers_lifespan_start_step_runs():
    class Port:
        def __init__(self):
            self.started = False

        def list(self):
            return []

        def add(self, spec):
            raise NotImplementedError

        def refresh(self, provider_id):
            raise NotImplementedError

        def models(self):
            return []

        def resolve_key(self, provider_id):
            raise KeyError(provider_id)

        def health(self, provider_id):
            return (False, None)

        async def start(self):
            self.started = True

    port = Port()
    with TestClient(create_app(GatewayDeps(registry=_EmptyRegistry(), providers=port))):
        pass

    assert port.started


def test_shutdown_tears_down_providers_deployments_and_registry():
    class Registry(_EmptyRegistry):
        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

    class Deployments(_NoopDeployments):
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class Providers:
        def __init__(self):
            self.closed = False

        def list(self):
            return []

        def add(self, spec):
            raise NotImplementedError

        def refresh(self, provider_id):
            raise NotImplementedError

        def models(self):
            return []

        def resolve_key(self, provider_id):
            raise KeyError(provider_id)

        def health(self, provider_id):
            return (False, None)

        async def aclose(self):
            self.closed = True

    registry, deployments, providers = Registry(), Deployments(), Providers()
    deps = GatewayDeps(registry=registry, deployments=deployments, providers=providers)
    with TestClient(create_app(deps)):
        pass

    assert registry.stopped
    assert deployments.closed
    assert providers.closed, "aclose must be preferred when a port has one"


def test_shutdown_falls_back_to_close_when_a_port_has_no_aclose():
    class Providers:
        def __init__(self):
            self.closed = False

        def list(self):
            return []

        def add(self, spec):
            raise NotImplementedError

        def refresh(self, provider_id):
            raise NotImplementedError

        def models(self):
            return []

        def resolve_key(self, provider_id):
            raise KeyError(provider_id)

        def health(self, provider_id):
            return (False, None)

        def close(self):
            self.closed = True

    providers = Providers()
    with TestClient(create_app(GatewayDeps(registry=_EmptyRegistry(), providers=providers))):
        pass

    assert providers.closed


# ---------------------------------------------------------------------------
# M-13: deployments.events() drives admission blocking, when the port has one
# ---------------------------------------------------------------------------


class _EventingDeployments(_NoopDeployments):
    def __init__(self, events):
        self._events = list(events)

    async def events(self):
        for event in self._events:
            yield event


def test_memory_critical_event_blocks_admission_for_that_deployment():
    deployments = _EventingDeployments([{"type": "memory_critical", "deployment_id": "d-1"}])
    app = create_app(GatewayDeps(registry=_EmptyRegistry(), deployments=deployments))

    with TestClient(app):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not app.state.ctx.admission.is_blocked("d-1"):
            time.sleep(0.02)
        assert app.state.ctx.admission.is_blocked("d-1")


def test_memory_cleared_event_unblocks_admission_after_a_critical_event():
    deployments = _EventingDeployments(
        [
            {"type": "memory_critical", "deployment_id": "d-1"},
            {"type": "memory_cleared", "deployment_id": "d-1"},
        ]
    )
    app = create_app(GatewayDeps(registry=_EmptyRegistry(), deployments=deployments))

    with TestClient(app):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and app.state.ctx.admission.is_blocked("d-1"):
            time.sleep(0.02)
        assert not app.state.ctx.admission.is_blocked("d-1")


def test_memory_critical_event_survives_the_admission_reconcile_poll():
    """Audit M-13 refutation: AdmissionController.reconcile() (sibling-owned
    this wave, not touched here) unconditionally unblocks
    BLOCK_MEMORY_CRITICAL for every deployment its own registry-derived view
    does not consider pressured. If the event path blocked under that same
    reason, the very next 0.5s poll would erase it whenever the two views
    disagree -- precisely the case M-13 exists for. A registry reporting no
    nodes at all disagrees with an event saying d-1 is critical, and the
    block must still be standing several poll intervals later."""

    dep = Deployment(
        deployment_id="d-1",
        served_name="m",
        shape=None,
        plan=None,
        fit=None,
        runtime="vllm",
        state=DeploymentState.READY,
        backend_url=None,
        context_length=0,
        max_concurrent_seqs=0,
        started_at=None,
        last_error=None,
    )

    class _ReconcilingDeployments(_EventingDeployments):
        def list(self):
            return [dep]

    deployments = _ReconcilingDeployments(
        [{"type": "memory_critical", "deployment_id": "d-1"}]
    )
    settings = GatewaySettings(admission_reconcile_interval_s=0.05)
    app = create_app(
        GatewayDeps(registry=_EmptyRegistry(), deployments=deployments, settings=settings)
    )

    with TestClient(app):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not app.state.ctx.admission.is_blocked("d-1"):
            time.sleep(0.02)
        assert app.state.ctx.admission.is_blocked("d-1")

        # Several reconcile ticks -- each of which considers d-1 unpressured,
        # since _EmptyRegistry reports no nodes -- must not clear the block.
        time.sleep(0.3)
        assert app.state.ctx.admission.is_blocked("d-1"), (
            "the event-driven block must not be erased by the registry-derived "
            "reconcile poll disagreeing with it"
        )


def test_a_port_without_events_is_simply_not_subscribed_to():
    """Duck-typed: StubDeployments has no events(), and that must not be an
    error -- it is how a day-0 stub gateway keeps booting."""
    with TestClient(create_app(GatewayDeps(registry=_EmptyRegistry()))) as client:
        assert client.get("/healthz").status_code == 200


# ---------------------------------------------------------------------------
# strict deps (C-1 enabler)
# ---------------------------------------------------------------------------


def test_strict_deps_raises_listing_every_missing_port():
    with pytest.raises(RuntimeError) as exc_info:
        GatewayDeps(strict=True)
    message = str(exc_info.value)
    for name in ("registry", "links", "resolver", "fit", "planner", "deployments", "providers"):
        assert name in message


def test_strict_deps_succeeds_when_every_port_is_supplied():
    deps = GatewayDeps(
        strict=True,
        registry=stubs.StubRegistry(),
        links=stubs.StubLinks(),
        resolver=stubs.StubResolver(),
        fit=stubs.StubFit(),
        planner=stubs.StubPlanner(),
        deployments=stubs.StubDeployments(),
        providers=stubs.StubProviders(),
    )
    assert isinstance(deps.registry, stubs.StubRegistry)


def test_non_strict_mode_still_backfills_stubs_by_default():
    deps = GatewayDeps()
    assert deps.strict is False
    assert isinstance(deps.registry, stubs.StubRegistry)
    assert isinstance(deps.providers, stubs.StubProviders)


# ---------------------------------------------------------------------------
# static UI (C-1 enabler)
# ---------------------------------------------------------------------------


def test_static_ui_is_served_when_ui_dir_exists_and_api_routes_still_work(tmp_path):
    (tmp_path / "index.html").write_text("<h1>hello derate</h1>")
    settings = GatewaySettings(ui_dir=str(tmp_path))

    with TestClient(create_app(GatewayDeps(registry=_EmptyRegistry(), settings=settings))) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "hello derate" in page.text

        assert client.get("/healthz").status_code == 200
        assert client.get("/v1/models").status_code == 200
        assert client.get("/api/nodes").status_code == 200, (
            "the static UI mount registers after both include_router calls, "
            "but a route ahead of it in registration order must still win"
        )


def _ui_dir(tmp_path):
    """A built UI: the document, and one hashed asset beside it."""
    (tmp_path / "index.html").write_text("<h1>hello derate</h1>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "index-abc123.js").write_text("console.log(1)")
    return tmp_path


def test_a_ui_deep_path_answers_with_the_document(tmp_path):
    """The UI puts its screen in the path, and those paths are the whole point
    of the URL scheme: they get pasted into messages and reloaded. Nothing is
    on disk under any of them, so the mount has to answer with index.html or a
    shared link works exactly once, for the person who never reloads."""
    settings = GatewaySettings(ui_dir=str(_ui_dir(tmp_path)))

    with TestClient(create_app(GatewayDeps(registry=_EmptyRegistry(), settings=settings))) as client:
        for path in ("/cluster", "/models", "/dashboard?dep=qwen3-30b-a3b", "/settings"):
            page = client.get(path)
            assert page.status_code == 200, path
            assert "hello derate" in page.text, path
            # Same rule as the document itself: it is the entry point to a
            # content-hashed bundle, and a cached copy of it outlives the
            # assets it names.
            assert page.headers["cache-control"] == "no-store, must-revalidate"


def test_a_model_deep_path_with_dots_in_it_answers_a_browser_with_the_document(tmp_path):
    """`/models/meta-llama/Llama-3.1-8B` is a screen, not a file, and the dots
    in the last segment are part of a model id. A browser navigating to it says
    it wants a document; that is what separates it from a missing asset."""
    settings = GatewaySettings(ui_dir=str(_ui_dir(tmp_path)))

    with TestClient(create_app(GatewayDeps(registry=_EmptyRegistry(), settings=settings))) as client:
        page = client.get(
            "/models/meta-llama/Llama-3.1-8B",
            headers={"accept": "text/html,application/xhtml+xml"},
        )
        assert page.status_code == 200
        assert "hello derate" in page.text


def test_the_deep_path_fallback_does_not_swallow_a_missing_asset(tmp_path):
    """A stale tab asking for a hash the last build deleted must get a 404. If
    it got index.html instead, the browser would refuse an HTML body in a
    `<script type=module>` and the page would be blank with no failed request
    to point at -- the exact silent failure the cache headers above exist to
    prevent, reintroduced from the other side."""
    settings = GatewaySettings(ui_dir=str(_ui_dir(tmp_path)))

    with TestClient(create_app(GatewayDeps(registry=_EmptyRegistry(), settings=settings))) as client:
        assert client.get("/assets/index-abc123.js").status_code == 200
        gone = client.get(
            "/assets/index-deleted.js", headers={"accept": "text/html,*/*"}
        )
        assert gone.status_code == 404
        # Nor for a file outside /assets that a script went looking for.
        assert client.get("/sw.js").status_code == 404


def test_the_deep_path_fallback_leaves_the_api_surface_answering_404(tmp_path):
    """A mistyped API path must stay a 404. Answering it with the document
    turns every such call into a JSON parse error three layers from the cause
    -- the same hazard the router registration order guards against, and the
    fallback is the second way to walk into it."""
    settings = GatewaySettings(ui_dir=str(_ui_dir(tmp_path)))

    with TestClient(create_app(GatewayDeps(registry=_EmptyRegistry(), settings=settings))) as client:
        for path in ("/api/settngs", "/api/nodes/spark-01/nope", "/v1/chat/completons"):
            miss = client.get(path, headers={"accept": "text/html,*/*"})
            assert miss.status_code == 404, path
            assert "hello derate" not in miss.text, path

        # And the live ones still win, with the mount registered under them.
        assert client.get("/api/nodes").status_code == 200
        assert client.get("/healthz").status_code == 200


def test_missing_ui_dir_warns_and_does_not_crash_the_gateway(tmp_path, caplog):
    missing = tmp_path / "does-not-exist"
    settings = GatewaySettings(ui_dir=str(missing))

    with caplog.at_level(logging.WARNING, logger="gateway"):
        with TestClient(create_app(GatewayDeps(registry=_EmptyRegistry(), settings=settings))) as client:
            assert client.get("/healthz").status_code == 200

    assert "does not exist" in caplog.text


def test_no_ui_dir_by_default_leaves_root_unmounted():
    with TestClient(create_app(GatewayDeps(registry=_EmptyRegistry()))) as client:
        assert client.get("/").status_code == 404


def test_ui_dir_setting_defaults_from_the_environment_variable(monkeypatch, tmp_path):
    monkeypatch.setenv("DERATE_UI_DIR", str(tmp_path))
    assert GatewaySettings().ui_dir == str(tmp_path)


def test_ui_dir_setting_defaults_to_none_without_the_environment_variable(monkeypatch):
    monkeypatch.delenv("DERATE_UI_DIR", raising=False)
    assert GatewaySettings().ui_dir is None


# ---------------------------------------------------------------------------
# small: /redoc disabled, coordinator inference safe on an empty roster
# ---------------------------------------------------------------------------


def test_redoc_is_disabled_but_docs_and_openapi_remain():
    with TestClient(create_app(GatewayDeps(registry=_EmptyRegistry()))) as client:
        assert client.get("/redoc").status_code == 404
        assert client.get("/api/docs").status_code == 200
        assert client.get("/api/openapi.json").status_code == 200


def test_coordinator_node_id_stays_none_when_the_registry_has_no_nodes():
    settings = GatewaySettings()
    with TestClient(create_app(GatewayDeps(registry=_EmptyRegistry(), settings=settings))):
        pass
    assert settings.coordinator_node_id is None


def test_coordinator_node_id_is_left_alone_when_already_set():
    settings = GatewaySettings(coordinator_node_id="spark-01")
    with TestClient(create_app(GatewayDeps(registry=_EmptyRegistry(), settings=settings))):
        pass
    assert settings.coordinator_node_id == "spark-01"


def test_provider_misconfiguration_returns_the_half_open_probe(tmp_path):
    """A terminal misconfiguration Attempt (unresolvable key, unsupported
    adapter) must give the breaker's half-open probe back: without abandon,
    circuit.probing stays True forever and the target is permanently
    unselectable -- misconfiguration reported as capacity, surviving a
    corrected key."""
    import asyncio
    from contextlib import asynccontextmanager

    import httpx

    from control_plane.contracts import RouteTarget, TargetKind
    from control_plane.gateway.breaker import CircuitBreaker, HALF_OPEN
    from control_plane.gateway.proxy import UpstreamProxy
    from control_plane.gateway.settings import GatewaySettings
    from control_plane.gateway.stats import StatsRegistry
    from control_plane.providers.errors import AdapterUnsupportedError, MissingKeyError

    for exc_type in (MissingKeyError, AdapterUnsupportedError):
        now = [1000.0]
        breaker = CircuitBreaker(failure_threshold=1, cooldown_s=5.0, clock=lambda: now[0])
        target_id = "openrouter:m"
        breaker.record_transport_failure(target_id)  # trip it
        now[0] += 6.0  # cooldown elapsed -> HALF_OPEN
        assert breaker.state(target_id) == HALF_OPEN
        assert breaker.begin(target_id) is True  # claim the one probe

        @asynccontextmanager
        async def raising_open_upstream(provider_id, upstream_id, body, stream, *, endpoint):
            raise exc_type("p-1", "ref" if exc_type is MissingKeyError else "no adapter")
            yield  # pragma: no cover

        proxy = UpstreamProxy(
            GatewaySettings(),
            client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
            breaker=breaker,
        )
        target = RouteTarget(
            target_id=target_id, kind=TargetKind.REMOTE, backend_url="http://up/v1",
            weight=1.0, outstanding=0, healthy=True, admitting=True, strength=1.0,
            cost_per_mtok=None,
        )
        attempt = asyncio.run(
            proxy.forward_provider(
                target=target, provider_id="p-1", upstream_id="m", path="/chat/completions",
                body={}, stats=StatsRegistry(), streaming=False,
                open_upstream=raising_open_upstream, on_finish=None, selection=None,
                trace=None, attempt_no=0,
            )
        )
        assert attempt is not None and attempt.response is not None
        assert attempt.response.status_code == 502
        assert breaker.begin(target_id) is True, (
            "%s left the half-open probe claimed: permanent unselectability"
            % exc_type.__name__
        )
