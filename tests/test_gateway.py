"""Agent G: gateway, routing, and admission control.

The gateway is the composition root, so every test here builds it with
injected fakes. Nothing in this file requires another agent's component to
exist, which is the point: constructing a gateway with all stubs must work and
must be how the tests run.
"""

from __future__ import annotations

import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from control_plane.contracts import (
    Deployment,
    DeploymentState,
    Provider,
    ProviderKind,
    ProviderModel,
    RoutingPolicy,
    TargetKind,
)
from control_plane.gateway import GatewayDeps, GatewaySettings, create_app
from control_plane.gateway.stubs import (
    StubFit,
    StubLinks,
    StubPlanner,
    StubResolver,
)
from tests.fixtures import (
    MODEL_SHAPES,
    NODE_PROFILES,
    fits,
    node_state,
    pp2_plan,
    single_node_plan,
)

GIB = 1024**3

# The one string that must never appear in a response or a log line.
SECRET_KEY = "sk-do-not-leak-me-0123456789"

# A request carrying this is held open by FakeBackend until released.
HOLD_MARKER = "please-hold-this-request"


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeRegistry:
    def __init__(self, states=None):
        self.states = list(states) if states is not None else [
            node_state(NODE_PROFILES["spark-01"], memory_used_pct=20.0),
            node_state(NODE_PROFILES["spark-02"], memory_used_pct=20.0),
            node_state(NODE_PROFILES["ws-3090"], memory_used_pct=20.0),
        ]

    def list_nodes(self):
        return list(self.states)

    def get_node(self, node_id):
        return next((n for n in self.states if n.profile.node_id == node_id), None)

    def healthy_nodes(self):
        return [n for n in self.states if n.healthy]

    def set_memory_pct(self, node_id, pct):
        for n in self.states:
            if n.profile.node_id == node_id:
                n.memory_used = int(n.profile.addressable_memory * pct / 100.0)


class FakeDeployments:
    def __init__(self, deployments=None):
        self.deployments = list(deployments or [])
        self.launched = []

    def launch(self, shape, plan, fit, runtime, ctx, max_seqs):
        dep = make_deployment(
            f"d-new-{len(self.launched) + 1}",
            shape.model_id,
            backend_url=None,
            state=DeploymentState.LAUNCHING,
            context_length=ctx,
            max_concurrent_seqs=max_seqs,
        )
        self.launched.append(dep)
        self.deployments.append(dep)
        return dep

    def stop(self, deployment_id):
        for d in self.deployments:
            if d.deployment_id == deployment_id:
                d.state = DeploymentState.STOPPING

    def list(self):
        return list(self.deployments)

    def get(self, deployment_id):
        return next(
            (d for d in self.deployments if d.deployment_id == deployment_id), None
        )


class FakeProviders:
    def __init__(self, providers=None):
        self.providers = list(providers or [])
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
        return SECRET_KEY

    def health(self, provider_id):
        p = next((x for x in self.providers if x.provider_id == provider_id), None)
        return (p.healthy, p.last_error) if p else (False, "unknown")


def make_deployment(
    deployment_id,
    served_name,
    *,
    backend_url,
    state=DeploymentState.READY,
    node_ids=("spark-01",),
    predicted_tps=42.0,
    context_length=4096,
    max_concurrent_seqs=8,
    kv_cache_bytes=12 * GIB,
    shape_key="llama-3.3-70b",
) -> Deployment:
    fit = fits()
    fit.predicted_decode_tps = predicted_tps
    fit.breakdown.kv_cache = kv_cache_bytes
    plan = (
        pp2_plan(list(node_ids))
        if len(node_ids) > 1
        else single_node_plan(node_ids[0])
    )
    return Deployment(
        deployment_id=deployment_id,
        served_name=served_name,
        shape=MODEL_SHAPES[shape_key],
        plan=plan,
        fit=fit,
        runtime="vllm",
        state=state,
        backend_url=backend_url,
        context_length=context_length,
        max_concurrent_seqs=max_concurrent_seqs,
        started_at=1757193600.0,
        last_error=None,
    )


def make_provider(
    provider_id="openrouter",
    *,
    base_url="http://127.0.0.1:9/v1",
    served_name="qwen3-30b-a3b",
    upstream_id="qwen/qwen3-30b-a3b",
    healthy=True,
    enabled=True,
    priority=10,
    output_cost=0.30,
) -> Provider:
    return Provider(
        provider_id=provider_id,
        kind=ProviderKind.OPENROUTER,
        display_name="OpenRouter",
        base_url=base_url,
        api_key_ref="OPENROUTER_API_KEY",
        enabled=enabled,
        priority=priority,
        models=[
            ProviderModel(
                served_name=served_name,
                upstream_id=upstream_id,
                context_length=131072,
                supports_streaming=True,
                supports_tools=True,
                input_cost_per_mtok=0.10,
                output_cost_per_mtok=output_cost,
            )
        ],
        healthy=healthy,
        last_error=None,
        last_refreshed=1757193600.0,
    )


def build_deps(*, registry=None, deployments=None, providers=None, settings=None):
    settings = settings or GatewaySettings()
    return GatewayDeps(
        registry=registry or FakeRegistry(),
        links=StubLinks(),
        resolver=StubResolver(),
        fit=StubFit(),
        planner=StubPlanner(),
        deployments=deployments or FakeDeployments(),
        providers=providers or FakeProviders(),
        settings=settings,
    )


# ---------------------------------------------------------------------------
# a real upstream, so streaming is tested against a socket rather than a mock
# ---------------------------------------------------------------------------


class FakeBackend:
    """An OpenAI-compatible runtime. Records what it was asked for."""

    def __init__(self, *, chunk_delay=0.0, chunks=5, status=200, error_body=None):
        self.chunk_delay = chunk_delay
        self.chunks = chunks
        self.status = status
        self.error_body = error_body
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        # Set to a threading.Event to block responses. Only requests whose
        # body carries HOLD_MARKER wait on it, so a test can hold one request
        # open while still probing the gateway with others.
        self.hold = None
        self.app = Starlette(
            routes=[
                Route("/v1/chat/completions", self._chat, methods=["POST"]),
                Route("/v1/completions", self._chat, methods=["POST"]),
                Route("/v1/embeddings", self._embeddings, methods=["POST"]),
            ]
        )

    async def _record(self, request):
        body = await request.json()
        self.requests.append(body)
        self.headers.append(dict(request.headers))
        return body

    async def _chat(self, request):
        body = await self._record(request)
        if self.hold is not None and HOLD_MARKER in json.dumps(body):
            await _await_event(self.hold)
        if self.status != 200:
            return JSONResponse(self.error_body or {"error": "backend said no"},
                                status_code=self.status)
        if body.get("stream"):
            return StreamingResponse(self._sse(), media_type="text/event-stream")
        return JSONResponse(
            {
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 1757193600,
                "model": body.get("model"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hello"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 9,
                    "completion_tokens": 12,
                    "total_tokens": 21,
                },
            }
        )

    async def _embeddings(self, request):
        await self._record(request)
        return JSONResponse(
            {
                "object": "list",
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
                "usage": {"prompt_tokens": 4, "total_tokens": 4},
            }
        )

    async def _sse(self):
        import asyncio

        for i in range(self.chunks):
            if self.chunk_delay:
                await asyncio.sleep(self.chunk_delay)
            payload = {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "created": 1757193600,
                "model": "llama-3.3-70b",
                "choices": [{"index": 0, "delta": {"content": f"tok{i}"}}],
            }
            yield f"data: {json.dumps(payload)}\n\n".encode()
        yield b"data: [DONE]\n\n"


async def _await_event(event: threading.Event) -> None:
    import asyncio

    while not event.is_set():
        await asyncio.sleep(0.005)


class RunningServer:
    """uvicorn on an ephemeral port, in a thread.

    Starlette's TestClient collects a whole response body before handing back
    the first chunk, so any claim about streaming latency has to be measured
    over a real socket. Both the fake runtime and the gateway itself are run
    this way.
    """

    def __init__(self, app, path_prefix=""):
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}{path_prefix}"
        config = uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="error"
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started and time.time() < deadline:
            time.sleep(0.01)
        if not self.server.started:
            raise RuntimeError("server did not start")
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=10)


class RunningBackend(RunningServer):
    def __init__(self, backend: FakeBackend):
        super().__init__(backend.app, path_prefix="/v1")
        self.backend = backend

    @property
    def base_url(self):
        return self.url


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# composition
# ---------------------------------------------------------------------------


def test_gateway_constructs_and_serves_with_every_dependency_stubbed():
    """The whole surface, from fixtures, with no other agent's code involved."""
    with TestClient(create_app()) as client:
        assert client.get("/healthz").json()["ok"] is True
        assert client.get("/v1/models").status_code == 200
        for path in (
            "/api/cluster",
            "/api/topology",
            "/api/nodes",
            "/api/links",
            "/api/routing",
            "/api/providers",
            "/api/deployments",
        ):
            assert client.get(path).status_code == 200, path


def test_startup_does_not_block_on_a_slow_node():
    """A registry that hangs must degrade startup, not prevent it."""

    class HangingRegistry(FakeRegistry):
        def start(self):
            time.sleep(30)  # never completes within the startup budget

    settings = GatewaySettings(startup_step_timeout_s=0.2)
    deps = build_deps(registry=HangingRegistry(), settings=settings)

    began = time.monotonic()
    with TestClient(create_app(deps, settings=settings)) as client:
        elapsed = time.monotonic() - began
        body = client.get("/healthz").json()
    assert elapsed < 10
    assert any("registry" in step for step in body["degraded_startup"])


def test_startup_does_not_measure_links():
    """Measurement on startup is disruptive. The link store only loads."""
    measured = []

    class WatchfulLinks(StubLinks):
        def measure(self, a, b):
            measured.append((a, b))
            return super().measure(a, b)

    deps = build_deps()
    deps.links = WatchfulLinks()
    with TestClient(create_app(deps)) as client:
        client.get("/api/links")
    assert measured == []


# ---------------------------------------------------------------------------
# the OpenAI surface
# ---------------------------------------------------------------------------


def test_unmodified_openai_client_lists_models_and_completes_a_chat():
    backend = FakeBackend()
    with RunningBackend(backend) as running:
        deps = build_deps(
            deployments=FakeDeployments(
                [make_deployment("d-1", "llama-3.3-70b", backend_url=running.base_url)]
            )
        )
        with TestClient(create_app(deps)) as client:
            listing = client.get("/v1/models").json()
            assert listing["object"] == "list"
            entry = next(m for m in listing["data"] if m["id"] == "llama-3.3-70b")
            # The fields an unmodified client depends on.
            assert entry["object"] == "model"
            assert isinstance(entry["created"], int)
            assert entry["owned_by"]

            reply = client.post(
                "/v1/chat/completions",
                json={
                    "model": "llama-3.3-70b",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )
    assert reply.status_code == 200
    assert reply.json()["choices"][0]["message"]["content"] == "hello"


def test_client_never_learns_which_node_answered():
    backend = FakeBackend()
    with RunningBackend(backend) as running:
        deps = build_deps(
            deployments=FakeDeployments(
                [make_deployment("d-1", "llama-3.3-70b", backend_url=running.base_url)]
            )
        )
        with TestClient(create_app(deps)) as client:
            reply = client.post(
                "/v1/chat/completions",
                json={"model": "llama-3.3-70b", "messages": [{"role": "user", "content": "x"}]},
            )
    blob = json.dumps(dict(reply.headers)) + reply.text
    assert running.base_url not in blob
    assert "d-1" not in blob


def test_unknown_model_returns_404_listing_available_names():
    deps = build_deps(
        deployments=FakeDeployments(
            [make_deployment("d-1", "llama-3.3-70b", backend_url="http://127.0.0.1:9/v1")]
        ),
        providers=FakeProviders([make_provider()]),
    )
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/v1/chat/completions",
            json={"model": "no-such-model", "messages": []},
        )
    assert reply.status_code == 404
    error = reply.json()["error"]
    assert error["code"] == "model_not_found"
    assert set(error["available_models"]) == {"llama-3.3-70b", "qwen3-30b-a3b"}
    assert "llama-3.3-70b" in error["message"]


def test_launching_deployment_returns_503_with_its_state_not_a_hang():
    deps = build_deps(
        deployments=FakeDeployments(
            [
                make_deployment(
                    "d-1",
                    "gpt-oss-120b",
                    backend_url=None,
                    state=DeploymentState.LAUNCHING,
                )
            ]
        )
    )
    with TestClient(create_app(deps)) as client:
        began = time.monotonic()
        reply = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-oss-120b", "messages": []},
        )
        elapsed = time.monotonic() - began
    assert reply.status_code == 503
    assert elapsed < 5
    body = reply.json()["error"]
    assert body["code"] == "model_not_ready"
    assert body["deployment_states"] == ["launching"]
    assert reply.headers["retry-after"]


def test_backend_status_and_error_body_are_preserved_verbatim():
    """A client debugging a vLLM error sees the vLLM error."""
    vllm_error = {
        "object": "error",
        "message": "This model's maximum context length is 4096 tokens.",
        "type": "BadRequestError",
        "code": 400,
    }
    backend = FakeBackend(status=422, error_body=vllm_error)
    with RunningBackend(backend) as running:
        deps = build_deps(
            deployments=FakeDeployments(
                [make_deployment("d-1", "llama-3.3-70b", backend_url=running.base_url)]
            )
        )
        with TestClient(create_app(deps)) as client:
            reply = client.post(
                "/v1/chat/completions",
                json={"model": "llama-3.3-70b", "messages": [{"role": "user", "content": "x"}]},
            )
    assert reply.status_code == 422
    assert reply.json() == vllm_error


def test_embeddings_are_proxied():
    backend = FakeBackend()
    with RunningBackend(backend) as running:
        deps = build_deps(
            deployments=FakeDeployments(
                [make_deployment("d-1", "llama-3.3-70b", backend_url=running.base_url)]
            )
        )
        with TestClient(create_app(deps)) as client:
            reply = client.post(
                "/v1/embeddings", json={"model": "llama-3.3-70b", "input": "hello"}
            )
    assert reply.status_code == 200
    assert reply.json()["data"][0]["embedding"] == [0.1, 0.2]


# ---------------------------------------------------------------------------
# streaming
# ---------------------------------------------------------------------------


def _chunk_arrival_times(response) -> list[float]:
    times = []
    for line in response.iter_lines():
        if line and line.startswith("data:") and "[DONE]" not in line:
            times.append(time.monotonic())
    return times


def test_streaming_passes_through_with_no_measurable_added_latency():
    """Measured against calling the backend directly, over real sockets.

    A gateway that buffered the stream would collect every chunk before
    emitting the first, so its first chunk would arrive at roughly the time the
    direct call's *last* chunk did.
    """
    delay = 0.05
    chunks = 6
    backend = FakeBackend(chunk_delay=delay, chunks=chunks)
    payload = {
        "model": "llama-3.3-70b",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }

    with RunningBackend(backend) as running:
        deps = build_deps(
            deployments=FakeDeployments(
                [make_deployment("d-1", "llama-3.3-70b", backend_url=running.base_url)]
            )
        )
        with httpx.Client(timeout=30) as direct:
            began = time.monotonic()
            with direct.stream(
                "POST", f"{running.base_url}/chat/completions", json=payload
            ) as response:
                direct_times = [t - began for t in _chunk_arrival_times(response)]

        with RunningServer(create_app(deps)) as gateway:
            with httpx.Client(timeout=30) as client:
                began = time.monotonic()
                with client.stream(
                    "POST", f"{gateway.url}/v1/chat/completions", json=payload
                ) as response:
                    assert response.status_code == 200
                    gateway_times = [t - began for t in _chunk_arrival_times(response)]

    assert len(gateway_times) == chunks
    assert len(direct_times) == chunks
    # The first chunk arrives at roughly the same time either way, and nowhere
    # near when the whole stream finished.
    assert gateway_times[0] < direct_times[0] + delay
    assert gateway_times[0] < direct_times[-1] / 2
    # Chunks keep arriving spread out rather than in one burst at the end.
    assert gateway_times[-1] - gateway_times[0] > delay * (chunks - 2)
    # Per-chunk overhead against the direct call, averaged over the stream.
    added = sum(g - d for g, d in zip(gateway_times, direct_times)) / chunks
    assert added < delay, f"gateway added {added * 1000:.1f} ms per chunk"


def test_streaming_response_is_not_buffered_into_content_length():
    backend = FakeBackend(chunk_delay=0.01, chunks=3)
    with RunningBackend(backend) as running:
        deps = build_deps(
            deployments=FakeDeployments(
                [make_deployment("d-1", "llama-3.3-70b", backend_url=running.base_url)]
            )
        )
        with RunningServer(create_app(deps)) as gateway, httpx.Client(timeout=30) as client:
            with client.stream(
                "POST",
                f"{gateway.url}/v1/chat/completions",
                json={
                    "model": "llama-3.3-70b",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            ) as response:
                headers = {k.lower() for k in response.headers}
                assert "content-length" not in headers
                assert response.headers["content-type"].startswith("text/event-stream")
                response.read()


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------


def make_router(*, deployments=None, providers=None, registry=None, settings=None):
    """A router with no HTTP around it, for policy semantics."""
    from control_plane.gateway.admission import AdmissionController
    from control_plane.gateway.router import Router
    from control_plane.gateway.stats import StatsRegistry

    settings = settings or GatewaySettings()
    deps = build_deps(
        registry=registry,
        deployments=deployments,
        providers=providers,
        settings=settings,
    )
    stats = StatsRegistry()
    admission = AdmissionController(
        registry=deps.registry,
        fit=deps.fit,
        deployments=deps.deployments,
        settings=settings,
    )
    router = Router(
        deployments=deps.deployments,
        providers=deps.providers,
        registry=deps.registry,
        stats=stats,
        admission=admission,
        settings=settings,
    )
    router.rebuild(force_scores=True)
    return router, stats, admission


def two_unequal_replicas(strong_tps=42.0, weak_tps=12.0, seqs=8):
    """A Spark pair and a 3090 serving the same model."""
    return FakeDeployments(
        [
            make_deployment(
                "d-spark",
                "llama-3.3-70b",
                backend_url="http://spark/v1",
                node_ids=("spark-01", "spark-02"),
                predicted_tps=strong_tps,
                max_concurrent_seqs=seqs,
            ),
            make_deployment(
                "d-3090",
                "llama-3.3-70b",
                backend_url="http://ws/v1",
                node_ids=("ws-3090",),
                predicted_tps=weak_tps,
                max_concurrent_seqs=seqs,
            ),
        ]
    )


def counts(router, name, n, policy=None, prefix=None):
    if policy is not None:
        router.set_policy(name, policy)
    tally: dict[str, int] = {}
    for _ in range(n):
        selection = router.select(name, prefix_key=prefix)
        assert selection is not None
        tally[selection.target.target_id] = tally.get(selection.target.target_id, 0) + 1
    return tally


def test_default_policy_is_least_outstanding_not_round_robin():
    """Round robin cannot see a replica mid-prefill, so it is not the default."""
    router, _, _ = make_router(
        deployments=FakeDeployments(
            [
                make_deployment("d-a", "llama-3.3-70b", backend_url="http://a/v1"),
                make_deployment("d-b", "llama-3.3-70b", backend_url="http://b/v1"),
            ]
        )
    )
    assert router.config_for("llama-3.3-70b").policy is RoutingPolicy.LEAST_OUTSTANDING


def test_two_replicas_receive_balanced_load_by_outstanding_requests():
    router, stats, _ = make_router(
        deployments=FakeDeployments(
            [
                make_deployment("d-a", "llama-3.3-70b", backend_url="http://a/v1"),
                make_deployment("d-b", "llama-3.3-70b", backend_url="http://b/v1"),
            ]
        )
    )
    # Simulate real dispatch: each selection takes an outstanding slot.
    tally: dict[str, int] = {}
    for _ in range(20):
        selection = router.select("llama-3.3-70b")
        stats.get(selection.target.target_id).begin()
        tally[selection.target.target_id] = tally.get(selection.target.target_id, 0) + 1
    assert tally == {"d-a": 10, "d-b": 10}


def test_least_outstanding_avoids_a_replica_mid_prefill():
    router, stats, _ = make_router(
        deployments=FakeDeployments(
            [
                make_deployment("d-a", "llama-3.3-70b", backend_url="http://a/v1"),
                make_deployment("d-b", "llama-3.3-70b", backend_url="http://b/v1"),
            ]
        )
    )
    stats.get("d-a").outstanding = 4  # busy on a long prompt
    assert router.select("llama-3.3-70b").target.target_id == "d-b"


def test_weighted_capacity_split_matches_the_strength_ratio_within_10_percent():
    """A Spark and a 3090 serving the same model. The 3090 gets less."""
    router, _, _ = make_router(deployments=two_unequal_replicas(42.0, 12.0))
    tally = counts(router, "llama-3.3-70b", 1000, RoutingPolicy.WEIGHTED_CAPACITY)

    assert tally["d-3090"] < tally["d-spark"]
    expected = 12.0 / (42.0 + 12.0)
    actual = tally["d-3090"] / 1000
    assert abs(actual - expected) / expected < 0.10, (tally, actual, expected)


def test_weighted_capacity_prefers_measured_throughput_over_the_estimate():
    """Never compute strength from a spec sheet once measurement exists."""
    settings = GatewaySettings(measured_strength_min_requests=100)
    router, stats, _ = make_router(
        deployments=two_unequal_replicas(42.0, 12.0), settings=settings
    )
    config = router.config_for("llama-3.3-70b")
    assert {t.target_id: t.strength for t in config.targets}["d-3090"] < 1.0

    # The "weak" node turns out to be the fast one in practice.
    fast = stats.get("d-3090")
    fast.completed = 150
    fast.decode_tps = 90.0
    slow = stats.get("d-spark")
    slow.completed = 150
    slow.decode_tps = 30.0
    router.rebuild(force_scores=True)

    tally = counts(router, "llama-3.3-70b", 600, RoutingPolicy.WEIGHTED_CAPACITY)
    assert tally["d-3090"] > tally["d-spark"]
    expected = 90.0 / (90.0 + 30.0)
    assert abs(tally["d-3090"] / 600 - expected) / expected < 0.10


def test_target_below_15_percent_of_the_strongest_receives_zero_traffic():
    router, _, _ = make_router(deployments=two_unequal_replicas(42.0, 5.0))
    config = router.config_for("llama-3.3-70b")
    weights = {t.target_id: t.weight for t in config.targets}
    assert weights["d-3090"] == 0.0
    assert weights["d-spark"] == 1.0

    tally = counts(router, "llama-3.3-70b", 200, RoutingPolicy.WEIGHTED_CAPACITY)
    assert tally == {"d-spark": 200}


def test_a_zero_weight_target_still_serves_when_nothing_else_admits():
    """Held as failover only, not discarded."""
    router, _, admission = make_router(deployments=two_unequal_replicas(42.0, 5.0))
    router.set_policy("llama-3.3-70b", RoutingPolicy.WEIGHTED_CAPACITY)
    admission.block("d-spark", "memory_critical")
    tally = counts(router, "llama-3.3-70b", 20)
    assert tally == {"d-3090": 20}


def test_the_15_percent_floor_does_not_apply_to_remote_targets():
    router, _, _ = make_router(
        deployments=FakeDeployments(
            [
                make_deployment(
                    "d-spark",
                    "qwen3-30b-a3b",
                    backend_url="http://spark/v1",
                    predicted_tps=42.0,
                )
            ]
        ),
        providers=FakeProviders([make_provider()]),
    )
    config = router.config_for("qwen3-30b-a3b")
    remote = next(t for t in config.targets if t.kind is TargetKind.REMOTE)
    # Remote default strength (1.0) is far below 42, but it is not floored out.
    assert remote.weight > 0.0


@pytest.mark.parametrize(
    "policy",
    [
        RoutingPolicy.LEAST_OUTSTANDING,
        RoutingPolicy.ROUND_ROBIN,
        RoutingPolicy.WEIGHTED_CAPACITY,
        RoutingPolicy.CACHE_AFFINITY,
        RoutingPolicy.FAILOVER,
        RoutingPolicy.LOCAL_FIRST,
        RoutingPolicy.COST_AWARE,
    ],
)
def test_a_non_admitting_target_is_excluded_from_every_policy(policy):
    router, _, admission = make_router(
        deployments=FakeDeployments(
            [
                make_deployment("d-a", "llama-3.3-70b", backend_url="http://a/v1"),
                make_deployment("d-b", "llama-3.3-70b", backend_url="http://b/v1"),
                make_deployment("d-c", "llama-3.3-70b", backend_url="http://c/v1"),
            ]
        )
    )
    admission.block("d-b", "memory_critical")
    tally = counts(router, "llama-3.3-70b", 30, policy, prefix="a shared system prompt")
    assert "d-b" not in tally, (policy, tally)


def test_round_robin_rotates_over_the_admitting_set():
    router, _, _ = make_router(
        deployments=FakeDeployments(
            [
                make_deployment("d-a", "llama-3.3-70b", backend_url="http://a/v1"),
                make_deployment("d-b", "llama-3.3-70b", backend_url="http://b/v1"),
            ]
        )
    )
    router.set_policy("llama-3.3-70b", RoutingPolicy.ROUND_ROBIN)
    picks = [router.select("llama-3.3-70b").target.target_id for _ in range(4)]
    assert picks == ["d-a", "d-b", "d-a", "d-b"]


def test_cache_affinity_sends_a_repeated_prefix_to_the_same_target():
    router, _, _ = make_router(
        deployments=FakeDeployments(
            [
                make_deployment("d-a", "llama-3.3-70b", backend_url="http://a/v1"),
                make_deployment("d-b", "llama-3.3-70b", backend_url="http://b/v1"),
            ]
        )
    )
    router.set_policy("llama-3.3-70b", RoutingPolicy.CACHE_AFFINITY)
    picks = {router.select("llama-3.3-70b", prefix_key="agent system prompt").target.target_id
             for _ in range(20)}
    assert len(picks) == 1

    others = {
        router.select("llama-3.3-70b", prefix_key=f"prompt-{i}").target.target_id
        for i in range(40)
    }
    assert others == {"d-a", "d-b"}  # different prefixes do spread out


def test_cache_affinity_excludes_remote_targets():
    """We cannot reason about a remote runtime's cache."""
    router, _, _ = make_router(
        deployments=FakeDeployments(
            [
                make_deployment("d-local", "qwen3-30b-a3b", backend_url="http://a/v1"),
            ]
        ),
        providers=FakeProviders([make_provider()]),
    )
    router.set_policy("qwen3-30b-a3b", RoutingPolicy.CACHE_AFFINITY)
    picks = {
        router.select("qwen3-30b-a3b", prefix_key=f"p{i}").target.kind for i in range(40)
    }
    assert picks == {TargetKind.LOCAL}


def test_cache_affinity_falls_back_when_the_chosen_target_stops_admitting():
    router, _, admission = make_router(
        deployments=FakeDeployments(
            [
                make_deployment("d-a", "llama-3.3-70b", backend_url="http://a/v1"),
                make_deployment("d-b", "llama-3.3-70b", backend_url="http://b/v1"),
            ]
        )
    )
    router.set_policy("llama-3.3-70b", RoutingPolicy.CACHE_AFFINITY)
    pinned = router.select("llama-3.3-70b", prefix_key="shared").target.target_id
    admission.block(pinned, "memory_critical")
    fallback = router.select("llama-3.3-70b", prefix_key="shared").target.target_id
    assert fallback != pinned


def test_failover_switches_only_when_the_primary_stops_admitting():
    router, _, admission = make_router(
        deployments=FakeDeployments(
            [
                make_deployment("d-a", "llama-3.3-70b", backend_url="http://a/v1"),
                make_deployment("d-b", "llama-3.3-70b", backend_url="http://b/v1"),
            ]
        )
    )
    assert counts(router, "llama-3.3-70b", 10, RoutingPolicy.FAILOVER) == {"d-a": 10}
    admission.block("d-a", "draining")
    assert counts(router, "llama-3.3-70b", 10) == {"d-b": 10}
    admission.unblock("d-a", "draining")
    assert counts(router, "llama-3.3-70b", 10) == {"d-a": 10}


def test_cost_aware_picks_the_cheapest_and_skips_unknown_cost():
    cheap = make_provider("cheap", served_name="qwen3-30b-a3b", output_cost=0.10)
    pricey = make_provider("pricey", served_name="qwen3-30b-a3b", output_cost=9.0)
    unknown = make_provider("unknown", served_name="qwen3-30b-a3b", output_cost=None)
    unknown.models[0].input_cost_per_mtok = None

    router, _, _ = make_router(providers=FakeProviders([pricey, unknown, cheap]))
    tally = counts(router, "qwen3-30b-a3b", 20, RoutingPolicy.COST_AWARE)
    assert list(tally) == ["cheap:qwen/qwen3-30b-a3b"]


def test_cost_aware_prefers_local_when_electricity_is_free():
    router, _, _ = make_router(
        deployments=FakeDeployments(
            [make_deployment("d-local", "qwen3-30b-a3b", backend_url="http://a/v1")]
        ),
        providers=FakeProviders([make_provider()]),
    )
    tally = counts(router, "qwen3-30b-a3b", 10, RoutingPolicy.COST_AWARE)
    assert tally == {"d-local": 10}


def test_local_first_stays_local_spills_to_remote_and_returns():
    """The cluster is the default; the paid API is the overflow valve."""
    router, stats, _ = make_router(
        deployments=FakeDeployments(
            [
                make_deployment(
                    "d-a", "qwen3-30b-a3b", backend_url="http://a/v1",
                    max_concurrent_seqs=2,
                ),
                make_deployment(
                    "d-b", "qwen3-30b-a3b", backend_url="http://b/v1",
                    max_concurrent_seqs=2,
                ),
            ]
        ),
        providers=FakeProviders([make_provider()]),
    )
    assert router.config_for("qwen3-30b-a3b").policy is RoutingPolicy.LOCAL_FIRST

    # Local capacity available: nothing goes remote.
    assert set(counts(router, "qwen3-30b-a3b", 20)) <= {"d-a", "d-b"}

    # Saturate every local target.
    stats.get("d-a").outstanding = 2
    stats.get("d-b").outstanding = 2
    assert counts(router, "qwen3-30b-a3b", 10) == {"openrouter:qwen/qwen3-30b-a3b": 10}

    # Capacity frees: back to local immediately, no cooldown.
    stats.get("d-a").outstanding = 0
    assert counts(router, "qwen3-30b-a3b", 10) == {"d-a": 10}


def test_local_first_spills_when_local_stops_admitting():
    router, _, admission = make_router(
        deployments=FakeDeployments(
            [make_deployment("d-a", "qwen3-30b-a3b", backend_url="http://a/v1")]
        ),
        providers=FakeProviders([make_provider()]),
    )
    admission.block("d-a", "memory_critical")
    assert counts(router, "qwen3-30b-a3b", 5) == {"openrouter:qwen/qwen3-30b-a3b": 5}


def test_no_admitting_target_returns_none_rather_than_queueing():
    router, _, admission = make_router(
        deployments=FakeDeployments(
            [make_deployment("d-a", "llama-3.3-70b", backend_url="http://a/v1")]
        )
    )
    admission.block("d-a", "memory_critical")
    assert router.select("llama-3.3-70b") is None


def test_auto_policy_defaults():
    equal = FakeDeployments(
        [
            make_deployment("d-a", "llama-3.3-70b", backend_url="http://a/v1",
                            predicted_tps=40.0),
            make_deployment("d-b", "llama-3.3-70b", backend_url="http://b/v1",
                            predicted_tps=40.0),
        ]
    )
    router, _, _ = make_router(deployments=equal)
    assert router.config_for("llama-3.3-70b").policy is RoutingPolicy.LEAST_OUTSTANDING

    # More than 25 percent apart in strength: weighted.
    router, _, _ = make_router(deployments=two_unequal_replicas(42.0, 12.0))
    assert router.config_for("llama-3.3-70b").policy is RoutingPolicy.WEIGHTED_CAPACITY

    # Local and remote both present: spill decision comes first.
    router, _, _ = make_router(
        deployments=FakeDeployments(
            [make_deployment("d-a", "qwen3-30b-a3b", backend_url="http://a/v1")]
        ),
        providers=FakeProviders([make_provider()]),
    )
    assert router.config_for("qwen3-30b-a3b").policy is RoutingPolicy.LOCAL_FIRST


def test_changing_policy_takes_effect_on_the_next_request_with_no_restart():
    primary = FakeBackend()
    secondary = FakeBackend()
    with RunningBackend(primary) as a, RunningBackend(secondary) as b:
        deps = build_deps(
            deployments=FakeDeployments(
                [
                    make_deployment("d-a", "llama-3.3-70b", backend_url=a.base_url),
                    make_deployment("d-b", "llama-3.3-70b", backend_url=b.base_url),
                ]
            )
        )
        payload = {"model": "llama-3.3-70b", "messages": [{"role": "user", "content": "x"}]}
        with TestClient(create_app(deps)) as client:
            assert client.put(
                "/api/routing/llama-3.3-70b", json={"policy": "failover"}
            ).json()["policy"] == "failover"
            for _ in range(4):
                assert client.post("/v1/chat/completions", json=payload).status_code == 200
            assert len(primary.requests) == 4 and len(secondary.requests) == 0

            reply = client.put(
                "/api/routing/llama-3.3-70b", json={"policy": "round_robin"}
            )
            assert reply.status_code == 200
            for _ in range(4):
                client.post("/v1/chat/completions", json=payload)
            assert len(secondary.requests) == 2


def test_routing_endpoint_rejects_an_unknown_policy_and_model():
    with TestClient(create_app()) as client:
        bad = client.put("/api/routing/llama-3.3-70b", json={"policy": "vibes"})
        assert bad.status_code == 400
        assert "least_outstanding" in bad.json()["error"]["message"]
        assert client.put(
            "/api/routing/nope", json={"policy": "round_robin"}
        ).status_code == 404


def test_routing_endpoint_exposes_live_weights_and_their_source():
    deps = build_deps(deployments=two_unequal_replicas(42.0, 12.0))
    with TestClient(create_app(deps)) as client:
        config = next(
            c for c in client.get("/api/routing").json()
            if c["served_name"] == "llama-3.3-70b"
        )
    weights = {t["target_id"]: t["weight"] for t in config["targets"]}
    assert abs(weights["d-spark"] - 42 / 54) < 0.01
    assert abs(weights["d-3090"] - 12 / 54) < 0.01
    assert {t["strength_source"] for t in config["targets"]} == {"predicted"}


def test_requests_get_503_when_no_target_is_admitting():
    from control_plane.gateway.admission import BLOCK_RATE_LIMITED

    backend = FakeBackend()
    with RunningBackend(backend) as running:
        deps = build_deps(
            deployments=FakeDeployments(
                [make_deployment("d-a", "llama-3.3-70b", backend_url=running.base_url)]
            )
        )
        app = create_app(deps)
        with TestClient(app) as client:
            app.state.ctx.admission.block("d-a", BLOCK_RATE_LIMITED)
            began = time.monotonic()
            reply = client.post(
                "/v1/chat/completions",
                json={"model": "llama-3.3-70b", "messages": [{"role": "user", "content": "x"}]},
            )
            elapsed = time.monotonic() - began
    assert reply.status_code == 503
    assert reply.json()["error"]["code"] == "no_target_admitting"
    assert elapsed < 5  # refused, not queued
    assert backend.requests == []


# ---------------------------------------------------------------------------
# admission control
# ---------------------------------------------------------------------------


def test_request_exceeding_the_context_returns_400_naming_the_limit():
    deps = build_deps(
        deployments=FakeDeployments(
            [
                make_deployment(
                    "d-a", "llama-3.3-70b", backend_url="http://a/v1",
                    context_length=4096,
                )
            ]
        )
    )
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/v1/chat/completions",
            json={
                "model": "llama-3.3-70b",
                "messages": [{"role": "user", "content": "x" * 40_000}],
                "max_tokens": 200,
            },
        )
    assert reply.status_code == 400
    error = reply.json()["error"]
    assert error["code"] == "context_length_exceeded"
    assert "4096" in error["message"]


def test_admitting_past_the_kv_budget_returns_429_with_retry_guidance():
    deps = build_deps(
        deployments=FakeDeployments(
            [
                make_deployment(
                    "d-a",
                    "llama-3.3-70b",
                    backend_url="http://a/v1",
                    context_length=8192,
                    kv_cache_bytes=8 * 1024 * 1024,  # 8 MiB, far too small
                )
            ]
        )
    )
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/v1/chat/completions",
            json={
                "model": "llama-3.3-70b",
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 512,
            },
        )
    assert reply.status_code == 429
    assert reply.json()["error"]["code"] == "kv_cache_exhausted"
    assert "Retry" in reply.json()["error"]["message"]
    assert int(reply.headers["retry-after"]) >= 1


def test_kv_commitments_are_released_when_a_request_completes():
    backend = FakeBackend()
    with RunningBackend(backend) as running:
        deps = build_deps(
            deployments=FakeDeployments(
                [
                    make_deployment(
                        "d-a", "llama-3.3-70b", backend_url=running.base_url,
                        context_length=8192, kv_cache_bytes=4 * GIB,
                    )
                ]
            )
        )
        app = create_app(deps)
        with TestClient(app) as client:
            payload = {
                "model": "llama-3.3-70b",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 64,
            }
            for _ in range(5):
                assert client.post("/v1/chat/completions", json=payload).status_code == 200
            assert app.state.ctx.admission.committed("d-a") == 0


def test_a_critical_memory_event_stops_new_admissions_within_one_second():
    backend = FakeBackend()
    registry = FakeRegistry()
    with RunningBackend(backend) as running:
        deps = build_deps(
            registry=registry,
            deployments=FakeDeployments(
                [
                    make_deployment(
                        "d-a", "llama-3.3-70b", backend_url=running.base_url,
                        node_ids=("spark-01",),
                    )
                ]
            ),
        )
        payload = {"model": "llama-3.3-70b", "messages": [{"role": "user", "content": "x"}]}
        with TestClient(create_app(deps)) as client:
            assert client.post("/v1/chat/completions", json=payload).status_code == 200

            registry.set_memory_pct("spark-01", 97.0)
            began = time.monotonic()
            while time.monotonic() - began < 3.0:
                if client.post("/v1/chat/completions", json=payload).status_code == 503:
                    break
                time.sleep(0.02)
            elapsed = time.monotonic() - began
            assert elapsed < 1.0, f"still admitting after {elapsed:.2f}s"

            # And it resumes on its own when the pressure clears.
            registry.set_memory_pct("spark-01", 20.0)
            began = time.monotonic()
            while time.monotonic() - began < 3.0:
                if client.post("/v1/chat/completions", json=payload).status_code == 200:
                    break
                time.sleep(0.02)
            else:
                raise AssertionError("never resumed admitting")


def test_memory_pressure_never_kills_in_flight_work():
    backend = FakeBackend()
    backend.hold = threading.Event()
    registry = FakeRegistry()
    results: list[int] = []

    with RunningBackend(backend) as running:
        deps = build_deps(
            registry=registry,
            deployments=FakeDeployments(
                [
                    make_deployment(
                        "d-a", "llama-3.3-70b", backend_url=running.base_url,
                        node_ids=("spark-01",),
                    )
                ]
            ),
        )
        payload = {"model": "llama-3.3-70b", "messages": [{"role": "user", "content": "x"}]}
        with TestClient(create_app(deps)) as client:
            held = {
                "model": "llama-3.3-70b",
                "messages": [{"role": "user", "content": HOLD_MARKER}],
            }

            def in_flight():
                results.append(client.post("/v1/chat/completions", json=held).status_code)

            worker = threading.Thread(target=in_flight)
            worker.start()
            time.sleep(0.3)  # let it reach the backend and block there

            registry.set_memory_pct("spark-01", 97.0)
            deadline = time.time() + 3
            while time.time() < deadline:
                if client.post("/v1/chat/completions", json=payload).status_code == 503:
                    break
                time.sleep(0.02)
            else:
                raise AssertionError("new admissions were not stopped")

            backend.hold.set()  # release the in-flight request
            worker.join(timeout=15)

    assert results == [200], "an in-flight request was killed by memory pressure"


# ---------------------------------------------------------------------------
# remote providers
# ---------------------------------------------------------------------------


def test_a_model_served_locally_and_remotely_appears_once_with_both_targets():
    deps = build_deps(
        deployments=FakeDeployments(
            [make_deployment("d-local", "qwen3-30b-a3b", backend_url="http://a/v1")]
        ),
        providers=FakeProviders([make_provider()]),
    )
    with TestClient(create_app(deps)) as client:
        models = client.get("/v1/models").json()["data"]
        routing = client.get("/api/routing").json()

    entries = [m for m in models if m["id"] == "qwen3-30b-a3b"]
    assert len(entries) == 1
    assert entries[0]["target_count"] == 2
    assert sorted(entries[0]["target_kinds"]) == ["local", "remote"]

    config = next(c for c in routing if c["served_name"] == "qwen3-30b-a3b")
    assert {t["kind"] for t in config["targets"]} == {"local", "remote"}


def test_a_remote_request_is_rewritten_to_the_upstream_id_and_carries_the_key():
    backend = FakeBackend()
    with RunningBackend(backend) as running:
        providers = FakeProviders(
            [make_provider(base_url=running.base_url, served_name="remote-only-model")]
        )
        deps = build_deps(providers=providers)
        with TestClient(create_app(deps)) as client:
            reply = client.post(
                "/v1/chat/completions",
                json={
                    "model": "remote-only-model",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )

    assert reply.status_code == 200
    # The client's served_name is translated to what the provider calls it.
    assert backend.requests[0]["model"] == "qwen/qwen3-30b-a3b"
    # The key is resolved at request time and sent upstream, and only upstream.
    assert backend.headers[0]["authorization"] == f"Bearer {SECRET_KEY}"
    assert providers.key_requests == ["openrouter"]
    assert SECRET_KEY not in reply.text
    assert SECRET_KEY not in json.dumps(dict(reply.headers))


def test_no_provider_api_key_appears_in_any_response_or_log_line(caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    providers = FakeProviders([make_provider()])
    deps = build_deps(
        deployments=FakeDeployments(
            [make_deployment("d-local", "qwen3-30b-a3b", backend_url="http://a/v1")]
        ),
        providers=providers,
    )
    with TestClient(create_app(deps)) as client:
        bodies = []
        for path in (
            "/v1/models",
            "/api/cluster",
            "/api/topology",
            "/api/nodes",
            "/api/links",
            "/api/routing",
            "/api/providers",
            "/api/providers/openrouter/models",
            "/api/deployments",
            "/healthz",
        ):
            reply = client.get(path)
            assert reply.status_code == 200, path
            bodies.append(reply.text)
        bodies.append(client.post("/api/providers/openrouter/refresh").text)

    blob = "".join(bodies)
    assert SECRET_KEY not in blob
    assert SECRET_KEY not in caplog.text
    # The reference is shown -- it is what tells a user which variable to set --
    # and any key material is rendered as *** and nothing else.
    provider = json.loads(bodies[6])[0]
    assert provider["api_key_ref"] == "OPENROUTER_API_KEY"
    assert provider["api_key"] == "***"
    assert providers.key_requests == []  # never resolved for a read


def test_a_disabled_provider_is_not_served():
    deps = build_deps(providers=FakeProviders([make_provider(enabled=False)]))
    with TestClient(create_app(deps)) as client:
        assert client.get("/v1/models").json()["data"] == []


def test_an_unhealthy_provider_is_not_admitting():
    router, _, _ = make_router(
        providers=FakeProviders([make_provider(healthy=False)])
    )
    assert router.select("qwen3-30b-a3b") is None


def test_an_unreachable_upstream_returns_502_without_inventing_a_status():
    deps = build_deps(
        deployments=FakeDeployments(
            [
                make_deployment(
                    "d-a", "llama-3.3-70b",
                    backend_url=f"http://127.0.0.1:{_free_port()}/v1",
                )
            ]
        )
    )
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/v1/chat/completions",
            json={"model": "llama-3.3-70b", "messages": [{"role": "user", "content": "x"}]},
        )
    assert reply.status_code == 502
    assert reply.json()["error"]["code"] == "upstream_unreachable"


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def test_plan_returns_a_plan_and_a_fit_and_launches_nothing():
    deployments = FakeDeployments()
    deps = build_deps(deployments=deployments)
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/api/plan",
            json={
                "model_id": "meta-llama/Llama-3.3-70B-Instruct",
                "context": 32768,
                "concurrency": 8,
                "target": "throughput",
            },
        )
    assert reply.status_code == 200
    body = reply.json()
    assert body["plan"]["kind"] == "pipeline"
    assert body["plan"]["reason"]
    assert body["plan"]["measured_link_gbps"] == 10.2  # from the measurement, not a default
    assert body["fit"]["verdict"] in ("fits", "fits_degraded", "wont_fit")
    assert body["shape"]["model_id"] == "meta-llama/Llama-3.3-70B-Instruct"
    assert deployments.launched == []
    assert deployments.list() == []


def test_plan_requires_a_model_id():
    with TestClient(create_app()) as client:
        assert client.post("/api/plan", json={}).status_code == 400


# ---------------------------------------------------------------------------
# topology and metrics
# ---------------------------------------------------------------------------


def test_topology_never_emits_an_unmeasured_bandwidth_figure():
    with TestClient(create_app()) as client:
        edges = client.get("/api/topology").json()["edges"]

    by_pair = {(e["src"], e["dst"]): e for e in edges}
    measured = by_pair[("spark-01", "spark-02")]
    assert measured["measured"] is True
    assert measured["all_reduce_gbps"] == 10.2
    assert measured["medium"] == "connectx-7"

    unmeasured = by_pair[("spark-02", "ws-3090")]
    assert unmeasured["measured"] is False
    assert "all_reduce_gbps" not in unmeasured


def test_metrics_stream_sustains_its_cadence_to_multiple_subscribers():
    interval = 0.25
    settings = GatewaySettings(metrics_interval_s=interval)
    deps = build_deps(
        deployments=FakeDeployments(
            [make_deployment("d-a", "llama-3.3-70b", backend_url="http://a/v1")]
        ),
        settings=settings,
    )
    collected: dict[int, list] = {}

    def subscribe(index: int, wanted: int = 4):
        events = []
        with httpx.Client(timeout=30) as client:
            with client.stream("GET", f"{gateway.url}/api/metrics/stream") as response:
                assert response.headers["content-type"].startswith("text/event-stream")
                for line in response.iter_lines():
                    if line.startswith("data:"):
                        events.append((time.monotonic(), json.loads(line[5:])))
                        if len(events) >= wanted:
                            break
        collected[index] = events

    with RunningServer(create_app(deps, settings=settings)) as gateway:
        threads = [threading.Thread(target=subscribe, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

    assert len(collected) == 3, "not every subscriber received its events"
    for index, events in collected.items():
        assert len(events) >= 4
        gaps = [b[0] - a[0] for a, b in zip(events, events[1:])]
        assert all(g < interval * 3 for g in gaps), (index, gaps)
        payload = events[-1][1]
        assert set(payload) == {"ts", "cluster", "nodes", "deployments"}
        assert payload["deployments"][0]["deployment_id"] == "d-a"
        assert payload["nodes"][0]["node_id"] == "spark-01"


def test_the_default_metrics_cadence_is_one_hertz():
    assert GatewaySettings().metrics_interval_s == 1.0


def test_an_unavailable_source_nulls_its_field_rather_than_dropping_the_event():
    from control_plane.gateway.metrics import MetricsHub
    from control_plane.gateway.stats import StatsRegistry

    class BrokenRegistry:
        def list_nodes(self):
            raise RuntimeError("node agent unreachable")

    hub = MetricsHub(
        registry=BrokenRegistry(),
        deployments=FakeDeployments(
            [make_deployment("d-a", "llama-3.3-70b", backend_url="http://a/v1")]
        ),
        stats=StatsRegistry(),
        settings=GatewaySettings(),
    )
    event = hub.snapshot()
    assert event["nodes"] is None
    assert event["cluster"]["total_power_w"] is None
    # The event still goes out, and the panels that do have data still fill.
    assert event["ts"] > 0
    assert event["deployments"][0]["deployment_id"] == "d-a"


# ---------------------------------------------------------------------------
# the headline acceptance: a real, unmodified OpenAI client
# ---------------------------------------------------------------------------


def test_an_unmodified_openai_client_lists_models_and_completes_a_chat():
    """Given only the base URL, using the real openai package."""
    openai = pytest.importorskip("openai")

    backend = FakeBackend(chunks=4)
    with RunningBackend(backend) as running:
        deps = build_deps(
            deployments=FakeDeployments(
                [make_deployment("d-1", "llama-3.3-70b", backend_url=running.base_url)]
            )
        )
        with RunningServer(create_app(deps)) as gateway:
            client = openai.OpenAI(
                base_url=f"{gateway.url}/v1", api_key="anything", max_retries=0
            )

            assert "llama-3.3-70b" in [m.id for m in client.models.list()]

            completion = client.chat.completions.create(
                model="llama-3.3-70b",
                messages=[{"role": "user", "content": "hello"}],
            )
            assert completion.choices[0].message.content == "hello"

            stream = client.chat.completions.create(
                model="llama-3.3-70b",
                messages=[{"role": "user", "content": "hello"}],
                stream=True,
            )
            deltas = [
                chunk.choices[0].delta.content
                for chunk in stream
                if chunk.choices and chunk.choices[0].delta.content
            ]
            assert deltas == ["tok0", "tok1", "tok2", "tok3"]

            with pytest.raises(openai.NotFoundError):
                client.chat.completions.create(
                    model="not-a-model", messages=[{"role": "user", "content": "x"}]
                )


def test_weights_are_recomputed_on_their_own_so_a_throttling_node_sheds_share():
    """No request has to arrive for the split to change."""
    settings = GatewaySettings(
        weight_refresh_interval_s=0.15, measured_strength_min_requests=10
    )
    deps = build_deps(deployments=two_unequal_replicas(42.0, 40.0), settings=settings)
    app = create_app(deps, settings=settings)
    with TestClient(app) as client:
        stats = app.state.ctx.stats
        # The Spark starts to throttle: measured throughput collapses.
        slow = stats.get("d-spark")
        slow.completed = 50
        slow.decode_tps = 8.0
        fast = stats.get("d-3090")
        fast.completed = 50
        fast.decode_tps = 40.0

        deadline = time.time() + 5
        while time.time() < deadline:
            config = next(
                c for c in client.get("/api/routing").json()
                if c["served_name"] == "llama-3.3-70b"
            )
            weights = {t["target_id"]: t["weight"] for t in config["targets"]}
            if weights["d-spark"] < weights["d-3090"]:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("weights never adjusted to measured throughput")

    assert {t["strength_source"] for t in config["targets"]} == {"measured"}


def test_an_unmeasured_remote_is_weighted_neutrally_not_starved():
    """A remote's placeholder score is not in tok/s, so it must not be
    compared against local throughput as if it were."""
    router, _, _ = make_router(
        deployments=FakeDeployments(
            [
                make_deployment(
                    "d-a", "qwen3-30b-a3b", backend_url="http://a/v1",
                    predicted_tps=42.0,
                )
            ]
        ),
        providers=FakeProviders([make_provider()]),
    )
    config = router.config_for("qwen3-30b-a3b")
    weights = {t.target_id: t.weight for t in config.targets}
    assert weights["openrouter:qwen/qwen3-30b-a3b"] == pytest.approx(0.5)
    assert weights["d-a"] == pytest.approx(0.5)


def test_a_wont_fit_verdict_refuses_the_launch_and_starts_nothing():
    from tests.fixtures import wont_fit

    class RefusingFit(StubFit):
        def check(self, req, nodes):
            return wont_fit()

    deployments = FakeDeployments()
    deps = build_deps(deployments=deployments)
    deps.fit = RefusingFit()
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/api/deployments",
            json={"model_id": "meta-llama/Llama-3.3-70B-Instruct", "context": 131072},
        )
    assert reply.status_code == 400
    assert reply.json()["error"]["code"] == "wont_fit"
    assert "Drop context" in reply.json()["error"]["message"]
    assert deployments.launched == []


def test_memory_percentages_match_the_architecture_docs_topology_example():
    """The doc's worked example puts spark-01 at 78 percent."""
    registry = FakeRegistry(
        [node_state(NODE_PROFILES["spark-01"], memory_used_pct=78.0)]
    )
    deps = build_deps(registry=registry)
    with TestClient(create_app(deps)) as client:
        assert client.get("/api/topology").json()["nodes"][0]["memory_used_pct"] == 78.0
        assert client.get("/api/nodes").json()[0]["memory_used_pct"] == 78.0
