"""Agent G: gateway, routing, and admission control.

The gateway is the composition root, so every test here builds it with
injected fakes. Nothing in this file requires another agent's component to
exist, which is the point: constructing a gateway with all stubs must work and
must be how the tests run.
"""

from __future__ import annotations

import dataclasses

import asyncio
import json
import os
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.websockets import WebSocketDisconnect
from starlette.routing import Route

from control_plane.contracts import (
    Deployment,
    DeploymentState,
    DeviceClass,
    Modality,
    NodeProfile,
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
from control_plane.planner import Planner
from control_plane.providers import UnknownProviderError
from control_plane.registry import JoinRejected, NodeNotFound
from tests.fixtures import (
    LINKS,
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

#: A minimal MP3: an ID3v2 header and a frame's worth of noise. Not valid
#: audio, deliberately -- it only has to be bytes that are not UTF-8 JSON,
#: so that anything on the path which tried to decode it would fail loudly.
SPEECH_BYTES = b"ID3\x03\x00\x00\x00\x00\x00\x00" + bytes(range(256)) * 4


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


class FakeJoinableRegistry(FakeRegistry):
    """A FakeRegistry that also implements the admission surface (H-5), with
    the same method names and exception types as the real Registry for
    handle_join and admit.

    remove_node is the one place this fake is aspirational rather than
    mirroring production: today's control_plane.registry.Registry.remove_node
    (line ~396) pops every dict with a default and never raises NodeNotFound
    for an unknown node_id -- it silently no-ops (confirmed against the real
    Registry: DELETE on an unknown node currently returns 200, not 404). This
    fake raises NodeNotFound anyway so the *route's* contract (call
    remove_node, map NodeNotFound to 404, per this package's H-5 task) is
    exercised end to end. Closing the real gap means Registry.remove_node
    raising NodeNotFound for an absent node -- that is registry.py, outside
    this package's ownership, and is called out as a cross-team follow-up
    rather than silently masked here.
    """

    def __init__(self, *, states=None, reject_join=False):
        super().__init__(states)
        self.reject_join = reject_join
        self.joins = []
        self.admitted = []
        self.removed = []

    async def handle_join(self, token, profile, agent_url):
        self.joins.append((token, profile, agent_url))
        if self.reject_join:
            raise JoinRejected("invalid cluster token")
        if token:
            return {
                "node_id": profile.node_id,
                "cluster_id": "c-test",
                "status": "member",
            }
        return {"node_id": profile.node_id, "status": "candidate"}

    def admit(self, node_id):
        node = self.get_node(node_id)
        if node is None:
            raise NodeNotFound(node_id)
        self.admitted.append(node_id)
        return node

    def remove_node(self, node_id):
        node = self.get_node(node_id)
        if node is None:
            raise NodeNotFound(node_id)
        self.removed.append(node_id)
        self.states = [n for n in self.states if n.profile.node_id != node_id]

    def candidates(self):
        return []


def make_node_profile(node_id="spark-99", device_class=DeviceClass.GB10):
    return NodeProfile(
        node_id=node_id,
        hostname=node_id,
        address="192.168.11.99",
        device_class=device_class,
        gpu_name="NVIDIA GB10",
        gpu_count=1,
        total_memory=GIB * 128,
        addressable_memory=GIB * 119,
        memory_bandwidth_gbps=273.0,
        compute_capability="12.1",
        driver_version="580.95.05",
    )


class FakeDeployments:
    def __init__(self, deployments=None):
        self.deployments = list(deployments or [])
        self.launched = []

    def launch(
        self, shape, plan, fit, runtime, ctx, max_seqs, *,
        modality=Modality.TEXT, extra_args=(), custom_command=(),
    ):
        if extra_args and custom_command:
            raise ValueError(
                "extra_args and custom_command are mutually exclusive: extra_args "
                "appends to the generated serve command, custom_command replaces "
                "it, and a request cannot mean both at once"
            )
        dep = make_deployment(
            f"d-new-{len(self.launched) + 1}",
            shape.model_id,
            backend_url=None,
            state=DeploymentState.LAUNCHING,
            context_length=ctx,
            max_concurrent_seqs=max_seqs,
            extra_args=tuple(extra_args),
            custom_command=tuple(custom_command),
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

    def _find(self, provider_id):
        provider = next(
            (p for p in self.providers if p.provider_id == provider_id), None
        )
        if provider is None:
            raise UnknownProviderError(provider_id)
        return provider

    def get(self, provider_id):
        return self._find(provider_id)

    def refresh(self, provider_id):
        return self._find(provider_id)

    def update(self, provider_id, patch):
        provider = self._find(provider_id)
        if "priority" in patch:
            provider.priority = int(patch["priority"])
        if "enabled" in patch:
            provider.enabled = bool(patch["enabled"])
        return provider

    def remove(self, provider_id):
        provider = self._find(provider_id)
        self.providers.remove(provider)

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
    modality=Modality.TEXT,
    extra_args=(),
    custom_command=(),
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
        modality=modality,
        extra_args=tuple(extra_args),
        custom_command=tuple(custom_command),
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
    modality=Modality.TEXT,
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
                modality=modality,
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

    def __init__(
        self,
        *,
        chunk_delay=0.0,
        chunks=5,
        status=200,
        error_body=None,
        fail_first=0,
        die_before_first_chunk=False,
        first_chunk_delay=None,
    ):
        self.chunk_delay = chunk_delay
        self.chunks = chunks
        self.status = status
        self.error_body = error_body
        # Answer 500 to this many requests, then behave. A node that is coming
        # back, rather than one that is simply broken.
        self.fail_first = fail_first
        # Send 200 and the headers, then drop the connection before the first
        # SSE frame -- a node dying during prefill.
        self.die_before_first_chunk = die_before_first_chunk
        # Stall before the first frame only, to outlast the header hold budget
        # without slowing the rest of the stream.
        self.first_chunk_delay = first_chunk_delay
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        # Raw multipart bodies, kept unparsed. See _transcriptions.
        self.uploads: list[bytes] = []
        # Raw JSON bodies, kept unparsed alongside the parsed `requests` list,
        # so a test can tell a re-serialization from a verbatim forward.
        self.raw_bodies: list[bytes] = []
        # Full URLs of GETs to /v1/audio/voices, so a test can prove the
        # gateway asked the backend rather than answering from a guess.
        self.voice_requests: list[str] = []
        # Set to a threading.Event to block responses. Only requests whose
        # body carries HOLD_MARKER wait on it, so a test can hold one request
        # open while still probing the gateway with others.
        self.hold = None
        self.app = Starlette(
            routes=[
                Route("/v1/chat/completions", self._chat, methods=["POST"]),
                Route("/v1/completions", self._chat, methods=["POST"]),
                Route("/v1/embeddings", self._embeddings, methods=["POST"]),
                Route("/v1/audio/speech", self._speech, methods=["POST"]),
                Route("/v1/audio/voices", self._voices, methods=["GET"]),
                Route(
                    "/v1/audio/transcriptions", self._transcriptions, methods=["POST"]
                ),
            ]
        )

    async def _record(self, request):
        raw = await request.body()
        self.raw_bodies.append(raw)
        body = json.loads(raw)
        self.requests.append(body)
        self.headers.append(dict(request.headers))
        return body

    async def _chat(self, request):
        body = await self._record(request)
        if self.hold is not None and HOLD_MARKER in json.dumps(body):
            await _await_event(self.hold)
        if self.fail_first > 0:
            self.fail_first -= 1
            return JSONResponse(
                self.error_body or {"error": "still starting up"}, status_code=500
            )
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

    async def _transcriptions(self, request):
        """Record the upload verbatim.

        Deliberately reads request.body() rather than request.form(): the point
        of the test is that the bytes the gateway sent are the bytes the client
        sent, and parsing them would hide a re-encode instead of catching it.
        """
        self.uploads.append(await request.body())
        self.headers.append(dict(request.headers))
        return JSONResponse({"text": "hello from the cluster"})

    async def _speech(self, request):
        """Answer the way a TTS server does: binary audio, not JSON.

        The ID3 header is what makes `file` call it an MP3, and asserting on it
        is how a test proves the gateway did not decode, re-encode or otherwise
        touch the bytes on their way through.
        """
        await self._record(request)
        return Response(
            SPEECH_BYTES,
            media_type="audio/mpeg",
            headers={"content-disposition": 'attachment; filename="speech.mp3"'},
        )

    async def _voices(self, request):
        """The tts runtime's own envelope, `skipped` included.

        `skipped` is the server saying which clips it declined and why -- a
        clip with no transcript beside it, or one over the reference limit --
        and the gateway forwarding it unaltered is what this exists to pin.
        """
        self.voice_requests.append(str(request.url))
        return JSONResponse(
            {
                "object": "list",
                "data": [{"id": "af"}, {"id": "narrator"}],
                "skipped": ["ambient.wav: no ambient.txt beside it"],
            }
        )

    async def _sse(self):
        import asyncio

        if self.first_chunk_delay:
            await asyncio.sleep(self.first_chunk_delay)
        if self.die_before_first_chunk:
            # Headers are already on the wire; dropping here is what a node
            # that dies during prefill looks like from the gateway's side.
            #
            # The delay runs FIRST so the two compose: stalling past
            # `upstream_header_hold_s` and then dying is the case where the
            # gateway has committed a status line and the client still has
            # zero body bytes. Ordering these the other way round only ever
            # produced the easy case, where the hold has not yet expired.
            raise RuntimeError("backend died during prefill")
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


class PullableProviders(FakeProviders):
    """A provider store that can be told to fetch weights.

    Reports a download size through on_size exactly as the streaming pull does,
    which is the only hook the memory gate has.
    """

    def __init__(self, providers=None, *, size=0, raises=None, frames=None):
        super().__init__(providers)
        self.size = size
        self.raises = raises
        self.pulled = []
        #: Frames to replay through on_progress, as the real stream does.
        self.frames = frames or []
        #: Held open, a transfer can be inspected while it is still running --
        #: which is the only state /api/activity exists to report, and one that
        #: a fake completing instantly can never produce.
        self.gate = asyncio.Event()
        self.gate.set()
        self.started = asyncio.Event()

    async def pull(self, provider_id, model, *, on_size=None, on_progress=None):
        if self.raises is not None:
            raise self.raises
        if on_size is not None and self.size:
            on_size(self.size)
        self.started.set()
        for frame in self.frames:
            if on_progress is not None:
                on_progress(frame)
        await self.gate.wait()
        self.pulled.append((provider_id, model))
        return {"provider_id": provider_id, "model": model, "digest": "sha256:x"}


def _pull_setup(*, size, free_bytes, used=0, address="192.168.11.99"):
    """A cluster with one node and one provider pointing at that node."""
    from dataclasses import replace as _replace

    from tests.fixtures import node_state

    profile = _replace(make_node_profile(), address=address)
    state = node_state(profile)
    state.memory_total = free_bytes + used
    state.memory_used = used
    provider = make_provider(base_url=f"http://{address}:11434/v1")
    providers = PullableProviders([provider], size=size)
    deps = build_deps(registry=FakeRegistry([state]), providers=providers)
    return deps, providers, profile.node_id


def test_a_pull_that_fits_is_accepted_and_runs_in_the_background():
    deps, providers, node_id = _pull_setup(size=300 * 1024**2, free_bytes=2 * GIB)
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/api/providers/openrouter/pull", json={"model": "qwen2.5:0.5b"}
        )
    assert reply.status_code == 202, reply.text
    body = reply.json()
    assert body["download_bytes"] == 300 * 1024**2
    # Named, so the refusal and the acceptance agree about which box was judged.
    assert body["checked_against"] == node_id
    assert body["state"] == "pulling"
    assert providers.pulled == [("openrouter", "qwen2.5:0.5b")]


def test_a_pull_too_big_for_the_machine_is_refused_with_both_figures():
    """A pull is minutes of transfer onto a box that may have a gigabyte free.

    The download total in the stream's first frames is the only moment anything
    can judge it -- before that there is no size, after it the card is filling.
    """
    deps, _providers, node_id = _pull_setup(size=3 * GIB, free_bytes=GIB)
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/api/providers/openrouter/pull", json={"model": "llama3.2:3b"}
        )
    assert reply.status_code == 409
    body = reply.json()["error"]
    assert body["code"] == "pull_over_memory"
    # Both numbers and the way out, or the operator has to guess past it.
    assert "3.0 GiB" in body["message"] and "1.0 GiB" in body["message"]
    assert node_id in body["message"]
    assert "allow_over_memory" in body["message"]


def test_an_over_memory_pull_proceeds_when_it_is_asked_for_explicitly():
    deps, providers, _ = _pull_setup(size=3 * GIB, free_bytes=GIB)
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/api/providers/openrouter/pull",
            json={"model": "llama3.2:3b", "allow_over_memory": True},
        )
    assert reply.status_code == 202
    assert providers.pulled == [("openrouter", "llama3.2:3b")]


def test_a_pull_onto_a_machine_that_never_joined_is_not_refused():
    """derate does not own that box. No reading is unknown, not full.

    The same rule the fit gate follows for a node with no live figure: absence
    degrades to unjudged, it never refuses.
    """
    deps, providers, _ = _pull_setup(
        size=99 * GIB, free_bytes=GIB, address="10.9.9.9"
    )
    # The provider points at 10.9.9.9; the roster's only node is elsewhere.
    deps.registry = FakeRegistry([])
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/api/providers/openrouter/pull", json={"model": "huge:latest"}
        )
    assert reply.status_code == 202
    assert reply.json()["free_bytes"] == 0
    assert providers.pulled == [("openrouter", "huge:latest")]


def test_the_reply_says_whether_a_gate_actually_ran():
    """`budget_bytes: 0` cannot tell an unjudged pull from a judged one.

    A box that never joined and a box with nothing free both report zero. The
    first went unweighed; the second was weighed and had no room. Reporting
    them identically means the screen either invents a measurement that never
    happened or hides one that did -- and the unmeasured case is the common
    one, since the kind's own default base_url is loopback, which can never
    match a roster entry.
    """
    deps, _providers, node_id = _pull_setup(size=100 * 1024**2, free_bytes=2 * GIB)
    with TestClient(create_app(deps)) as client:
        judged = client.post(
            "/api/providers/openrouter/pull", json={"model": "small:latest"}
        ).json()
    assert judged["gated"] is True
    assert judged["checked_against"] == node_id

    # Same route, same shape of reply, no measurement behind it.
    deps, _providers, _ = _pull_setup(
        size=99 * GIB, free_bytes=GIB, address="10.9.9.9"
    )
    deps.registry = FakeRegistry([])
    with TestClient(create_app(deps)) as client:
        unjudged = client.post(
            "/api/providers/openrouter/pull", json={"model": "huge:latest"}
        ).json()
    assert unjudged["gated"] is False
    assert unjudged["free_bytes"] == 0 and unjudged["budget_bytes"] == 0


def test_pulling_onto_a_kind_that_hosts_nothing_says_so():
    from control_plane.providers.errors import PullUnsupportedError

    providers = PullableProviders(
        [make_provider()],
        raises=PullUnsupportedError("OpenRouter does not host its own weights"),
    )
    deps = build_deps(registry=FakeRegistry(), providers=providers)
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/api/providers/openrouter/pull", json={"model": "anything"}
        )
    assert reply.status_code == 400
    assert reply.json()["error"]["code"] == "pull_unsupported"


def test_a_pull_needs_a_model_name():
    deps, _, _ = _pull_setup(size=0, free_bytes=GIB)
    with TestClient(create_app(deps)) as client:
        reply = client.post("/api/providers/openrouter/pull", json={"model": "  "})
    assert reply.status_code == 400
    assert reply.json()["error"]["code"] == "model_required"


# -- /api/activity ---------------------------------------------------------
#
# What is arriving. Before this endpoint a pull was one number in a 202 and
# then silence, and a launch was up to READY_TIMEOUT_S of `launching` with no
# figure anywhere -- while the rail beside it said "Nothing is being served."


def _reset_activity():
    """The registry is process-local, so tests must not leak into each other."""
    from control_plane.gateway import internal_api

    internal_api._DOWNLOADS.clear()
    internal_api._LAUNCH_SEEN.clear()


def test_activity_is_empty_when_nothing_is_arriving():
    _reset_activity()
    deps, _, _ = _pull_setup(size=0, free_bytes=GIB)
    with TestClient(create_app(deps)) as client:
        reply = client.get("/api/activity")
    assert reply.status_code == 200
    assert reply.json() == {"downloads": [], "launches": []}


def test_a_running_transfer_reports_how_far_along_it_is():
    """The whole point: a number that moves, from frames that were discarded."""
    _reset_activity()
    deps, providers, _ = _pull_setup(size=4096, free_bytes=GIB)
    providers.frames = [
        {"status": "pulling manifest"},
        {"status": "pulling sha256:ab", "total": 4096, "completed": 1024},
    ]
    providers.gate.clear()  # hold the transfer open
    with TestClient(create_app(deps)) as client:
        accepted = client.post(
            "/api/providers/openrouter/pull", json={"model": "qwen2.5:0.5b"}
        )
        assert accepted.status_code == 202, accepted.text
        pull_id = accepted.json()["pull_id"]
        assert pull_id, "an accepted transfer must be addressable"

        body = client.get("/api/activity").json()

    assert len(body["downloads"]) == 1
    row = body["downloads"][0]
    assert row["pull_id"] == pull_id
    assert row["model"] == "qwen2.5:0.5b"
    assert row["completed"] == 1024
    assert row["total"] == 4096
    # The upstream's own sentence, not a paraphrase of it.
    assert row["status"] == "pulling sha256:ab"
    assert row["done"] is False
    assert row["error"] is None


def test_a_transfer_with_no_size_yet_reports_no_total_rather_than_zero():
    """A download nothing has measured is not a download of nothing.

    `total: 0` would draw a full, solid, empty bar and state a measurement that
    never happened. The UI's ProportionBar draws null and 0 differently on
    exactly this rule.
    """
    _reset_activity()
    deps, providers, _ = _pull_setup(size=4096, free_bytes=GIB)
    providers.frames = [{"status": "pulling manifest"}]
    providers.gate.clear()
    with TestClient(create_app(deps)) as client:
        client.post("/api/providers/openrouter/pull", json={"model": "m"})
        row = client.get("/api/activity").json()["downloads"][0]
    # The gate saw a size, so this one has a total. What must never appear is a
    # completed count invented before a frame reported one.
    assert row["completed"] == 0
    assert row["status"] == "pulling manifest"


def test_a_pull_that_downloaded_nothing_is_not_listed_as_a_transfer():
    """Already upstream means no transfer, so there is nothing to show."""
    _reset_activity()
    deps, _, _ = _pull_setup(size=0, free_bytes=GIB)
    with TestClient(create_app(deps)) as client:
        accepted = client.post("/api/providers/openrouter/pull", json={"model": "m"})
        assert accepted.json()["state"] == "present"
        assert accepted.json()["pull_id"] is None
        assert client.get("/api/activity").json()["downloads"] == []


def test_a_refused_pull_never_appears():
    """A refusal downloaded nothing. A bar for it would be a bar for a
    transfer that was stopped at its first byte."""
    _reset_activity()
    deps, _, _ = _pull_setup(size=3 * GIB, free_bytes=GIB)
    with TestClient(create_app(deps)) as client:
        refused = client.post("/api/providers/openrouter/pull", json={"model": "m"})
        assert refused.status_code == 409
        assert client.get("/api/activity").json()["downloads"] == []


def test_a_failed_transfer_says_so_instead_of_disappearing():
    """The case that cost somebody four minutes of debugging their own code.

    A download that stops has to say it stopped. Vanishing silently is the
    behaviour this whole surface exists to end.
    """
    _reset_activity()
    deps, providers, _ = _pull_setup(size=4096, free_bytes=GIB)

    async def explode(provider_id, model, *, on_size=None, on_progress=None):
        on_size(4096)
        raise RuntimeError("connection reset by peer")

    providers.pull = explode
    with TestClient(create_app(deps)) as client:
        assert client.post(
            "/api/providers/openrouter/pull", json={"model": "m"}
        ).status_code == 202
        rows = client.get("/api/activity").json()["downloads"]

    assert len(rows) == 1
    assert rows[0]["done"] is True
    assert "connection reset by peer" in rows[0]["error"]


def test_a_transfer_cut_off_partway_is_not_reported_as_finished():
    """Found live, and the reason this check exists at all.

    A truncated stream ends CLEANLY: the upstream closes the connection, the
    frame loop runs out of lines, and the background task returns with no
    exception. Treating "it did not raise" as "the weights arrived" filled the
    bar to 4.00/4.00 GiB for a transfer that was watched stopping at 0.30 GiB
    -- a completed download that never happened, which is the exact invented
    figure this whole surface exists to avoid printing.
    """
    _reset_activity()
    deps, providers, _ = _pull_setup(size=4 * GIB, free_bytes=100 * GIB)

    async def cut_off(provider_id, model, *, on_size=None, on_progress=None):
        on_size(4 * GIB)
        on_progress({"status": "pulling sha256:ab", "total": 4 * GIB,
                     "completed": 300 * 1024**2})
        # and then the far end simply stops talking. No exception.
        return {"provider_id": provider_id, "model": model, "digest": ""}

    providers.pull = cut_off
    with TestClient(create_app(deps)) as client:
        client.post("/api/providers/openrouter/pull", json={"model": "m"})
        row = client.get("/api/activity").json()["downloads"][0]

    assert row["error"], "a transfer that stopped partway must say so"
    assert "0.3 GiB of 4.0 GiB" in row["error"]
    # The figure it actually reached, never rounded up to the total.
    assert row["completed"] == 300 * 1024**2


def test_a_last_frame_a_few_bytes_short_still_counts_as_finished():
    """The other half of the same judgement. Reported sizes jitter, and a bar
    frozen at 99.98% forever reads as a hang."""
    _reset_activity()
    deps, providers, _ = _pull_setup(size=4 * GIB, free_bytes=100 * GIB)

    async def almost(provider_id, model, *, on_size=None, on_progress=None):
        on_size(4 * GIB)
        on_progress({"total": 4 * GIB, "completed": 4 * GIB - 512})
        return {"provider_id": provider_id, "model": model, "digest": "sha256:x"}

    providers.pull = almost
    with TestClient(create_app(deps)) as client:
        client.post("/api/providers/openrouter/pull", json={"model": "m"})
        row = client.get("/api/activity").json()["downloads"][0]

    assert row["error"] is None
    assert row["completed"] == row["total"]


def test_a_provider_that_reports_only_a_total_is_not_called_truncated():
    """No `completed` frame at all is no evidence either way, and must not be
    read as evidence of a transfer that was cut off."""
    _reset_activity()
    deps, providers, _ = _pull_setup(size=4 * GIB, free_bytes=100 * GIB)

    async def silent(provider_id, model, *, on_size=None, on_progress=None):
        on_size(4 * GIB)
        on_progress({"status": "pulling"})
        return {"provider_id": provider_id, "model": model, "digest": "sha256:x"}

    providers.pull = silent
    with TestClient(create_app(deps)) as client:
        client.post("/api/providers/openrouter/pull", json={"model": "m"})
        row = client.get("/api/activity").json()["downloads"][0]

    assert row["error"] is None


def _every_state():
    """One deployment per lifecycle state, so the filter is asked about all of
    them rather than the two that happened to be in a fixture."""
    return FakeDeployments(
        [
            make_deployment(f"d-{state.value}", state.value,
                            backend_url="http://spark-01:8000/v1", state=state)
            for state in DeploymentState
        ]
    )


def test_only_a_model_that_is_arriving_counts_as_activity():
    """A launch is up to half an hour with no number on any screen -- but a
    model that is serving, stopping or finished is not arriving, and the rail's
    other sections already describe those."""
    _reset_activity()
    deps = build_deps(deployments=_every_state())
    with TestClient(create_app(deps)) as client:
        body = client.get("/api/activity").json()

    listed = {row["deployment_id"] for row in body["launches"]}
    arriving = {DeploymentState.PLANNED, DeploymentState.LAUNCHING}
    for state in DeploymentState:
        dep_id = f"d-{state.value}"
        if state in arriving:
            assert dep_id in listed, f"{state.value} is arriving and must be listed"
        else:
            assert dep_id not in listed, f"{state.value} is not arriving"
    # A degraded model is up and serving badly, which the plan and routing
    # sections already report. Pinned because it is the tempting mistake.
    assert "d-degraded" not in listed


def test_a_launch_carries_no_percentage_of_its_own():
    """A deployment record measures nothing, and this endpoint invents nothing.

    The phase fields below come from the manager reading the launcher and the
    backend's log. A port that does not offer them -- every stub, and this
    fake -- reports no phase and no fraction, which is what the screen drew
    before there was one. What must never appear is a figure derived here from
    a record that contains no measurement.
    """
    _reset_activity()
    deps = build_deps(deployments=_every_state())
    with TestClient(create_app(deps)) as client:
        launches = client.get("/api/activity").json()["launches"]
    assert launches, "the fixture is expected to have something launching"
    for row in launches:
        assert "completed" not in row
        assert "total" not in row
        assert "progress" not in row
        assert row["phase"] is None
        assert row["status"] == ""
        assert row["fraction"] is None
        # `since` is when this coordinator first saw it, not when it launched:
        # Deployment.started_at is None until READY, so there is no launch
        # timestamp to report and the field does not claim to be one.
        assert row["since"] > 0


def test_a_launch_reports_the_phase_the_manager_read():
    """The other half: when the manager has read one, it reaches the screen.

    Verbatim, including the runtime's own shard count -- that pair is the one
    measurement anything in a launch reports, and re-deriving or rounding it
    here would replace a fact with an estimate.
    """
    _reset_activity()
    deployments = _every_state()
    launching = "d-%s" % DeploymentState.LAUNCHING.value
    deployments.progress = lambda: {
        launching: {
            "phase": "loading",
            "status": "Loading safetensors checkpoint shards:  50% Completed | 1/2",
            "fraction": 0.5,
            "source": "runtime",
        }
    }
    with TestClient(create_app(build_deps(deployments=deployments))) as client:
        rows = client.get("/api/activity").json()["launches"]

    row = next(r for r in rows if r["deployment_id"] == launching)
    assert row["phase"] == "loading"
    assert row["status"] == "Loading safetensors checkpoint shards:  50% Completed | 1/2"
    assert row["fraction"] == 0.5
    # A deployment the manager said nothing about is not given somebody else's
    # phase.
    others = [r for r in rows if r["deployment_id"] != launching]
    assert others, "the fixture is expected to have more than one arriving"
    assert all(r["phase"] is None for r in others)


def test_the_log_route_says_which_source_answered():
    """The sheet has to know what it may poll.

    `buffer` is the coordinator repeating lines it is already streaming and
    costs nothing; `read` is one bounded `sparkrun logs`, which follows and
    has to be cut off, so it is fetched on a click and never on a timer. A
    route that returned lines without saying which would get the expensive one
    polled every two seconds.
    """
    _reset_activity()
    deployments = _every_state()
    launching = "d-%s" % DeploymentState.LAUNCHING.value
    deployments.log_tail = lambda deployment_id, limit=500: (
        {"lines": ["[2/6] Building image", "Capturing CUDA graphs"], "source": "buffer"}
        if deployment_id == launching
        else {"lines": [], "source": "none"}
    )
    with TestClient(create_app(build_deps(deployments=deployments))) as client:
        body = client.get("/api/deployments/%s/logs" % launching).json()
        missing = client.get("/api/deployments/d-nope/logs")

    assert body["source"] == "buffer"
    assert body["lines"][0] == "[2/6] Building image"
    # A deployment that is not here is a 404, not an empty log -- which would
    # read as a backend that printed nothing.
    assert missing.status_code == 404


def test_a_control_plane_that_cannot_read_a_log_says_so():
    """`unavailable` and `none` are different answers.

    One is a stub port with no way to reach a container; the other is a real
    one with nothing to show. Collapsing them would tell somebody their
    backend printed nothing when in fact nobody looked.
    """
    _reset_activity()
    deployments = _every_state()  # no log_tail on this fake
    with TestClient(create_app(build_deps(deployments=deployments))) as client:
        body = client.get(
            "/api/deployments/d-%s/logs" % DeploymentState.LAUNCHING.value
        ).json()
    assert body == {"lines": [], "source": "unavailable"}


def test_a_manager_that_cannot_report_a_phase_does_not_break_the_endpoint():
    """The activity rail is the one screen that draws during an incident.

    `progress` is reached through a getattr, so a port without it degrades --
    and one that raises must degrade the same way rather than take the whole
    payload down, including the downloads beside it.
    """
    _reset_activity()
    deployments = _every_state()

    def angry():
        raise RuntimeError("the manager is wedged")

    deployments.progress = angry
    with TestClient(create_app(build_deps(deployments=deployments))) as client:
        body = client.get("/api/activity").json()

    assert body["launches"], "a wedged progress read must not empty the rail"
    assert all(row["phase"] is None for row in body["launches"])


def test_activity_is_reachable_even_when_the_ui_is_mounted(tmp_path):
    """The trap every new route has to be tested against: a StaticFiles mount
    at "/" answers index.html for anything registered below it."""
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<!doctype html><title>derate</title>")
    app = create_app(
        build_deps(),
        settings=GatewaySettings(cluster_id="c-test", ui_dir=str(ui)),
    )
    with TestClient(app) as client:
        reply = client.get("/api/activity")
    assert reply.status_code == 200
    assert reply.headers["content-type"].startswith("application/json")
    assert "downloads" in reply.json()


def test_the_native_api_is_addressed_by_stripping_the_openai_shim():
    """One address for the box. Two would eventually name two machines."""
    from control_plane.providers.kinds import join_url, native_base

    for given in ("http://pi:11434/v1", "http://pi:11434/v1/", "http://pi:11434"):
        assert join_url(native_base(given), "api/pull") == "http://pi:11434/api/pull"


def test_provider_kinds_tell_the_form_what_each_kind_needs():
    """The add-provider form restated this table and drifted from it.

    Two ways, both of which reached an operator as an unexplained failure: it
    offered Anthropic, which this build has no adapter for and rejects at POST
    time, and it could not say that Ollama needs no key or where its default
    base_url points.
    """
    with TestClient(create_app(build_deps())) as client:
        reply = client.get("/api/providers/kinds")
    assert reply.status_code == 200
    by_kind = {k["kind"]: k for k in reply.json()}

    ollama = by_kind["ollama"]
    assert ollama["requires_key"] is False
    # Shown so it can be corrected. Dialled from the coordinator, this default
    # is the coordinator itself -- almost never the machine that was meant.
    assert ollama["base_url"] == "http://localhost:11434/v1"

    assert by_kind["openrouter"]["requires_key"] is True
    assert by_kind["custom"]["requires_base_url"] is True
    # A kind this build cannot talk to says so, in its own words.
    assert by_kind["anthropic"]["unsupported_reason"]


def test_provider_kinds_needs_no_ports_and_is_cacheable():
    """A static table compiled into the server. It must answer with nothing
    wired, and must not be re-fetched on every render of the settings page."""
    with TestClient(create_app(GatewayDeps())) as bare:
        reply = bare.get("/api/providers/kinds")
    assert reply.status_code == 200
    assert len(reply.json()) >= 5
    assert "max-age" in reply.headers.get("cache-control", "")


def test_provider_kinds_carries_no_key_material():
    """It is served to every browser that opens Settings."""
    with TestClient(create_app(build_deps())) as client:
        body = client.get("/api/providers/kinds").text
    assert "api_key" not in body and "sk-" not in body


# ── The provider mark on the cluster screen ─────────────────────────────────


def _logo_app(monkeypatch, tmp_path, body=b"\x89PNG\r\n\x1a\n", content_type="image/png"):
    """A gateway whose logo fetches are answered from memory, not the network."""
    from control_plane.providers import logos

    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path))
    logos_seen = []

    async def fake_fetch(provider):
        logos_seen.append(provider.provider_id)
        return (body, content_type) if body else None

    monkeypatch.setattr(logos, "fetch_logo", fake_fetch)
    import control_plane.gateway.internal_api as api

    monkeypatch.setattr(api, "_LOGOS", logos.LogoCache(tmp_path / "logos"))
    return create_app(build_deps(providers=FakeProviders([make_provider()]))), logos_seen


def test_a_provider_logo_is_served_by_the_coordinator(monkeypatch, tmp_path):
    """The browser is frequently the one machine with no egress -- a laptop on
    the lab LAN pointed at a coordinator that has it. So the coordinator
    fetches and this route serves, rather than the page reaching out."""
    app, seen = _logo_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        # Cold: an immediate 404 while the fetch is kicked off behind it. A
        # screen never waits on a picture.
        first = client.get("/api/providers/openrouter/logo")
        assert first.status_code == 404
        for _ in range(50):
            reply = client.get("/api/providers/openrouter/logo")
            if reply.status_code == 200:
                break
            time.sleep(0.02)
    assert reply.status_code == 200, reply.text
    assert reply.headers["content-type"].startswith("image/png")
    assert reply.content == b"\x89PNG\r\n\x1a\n"
    assert "max-age" in reply.headers.get("cache-control", "")
    assert seen == ["openrouter"], "one fetch, not one per request"


def test_a_provider_with_no_mark_stays_a_404_and_is_not_refetched(monkeypatch, tmp_path):
    """404 is an ordinary answer here: the Cluster tab draws a monogram tile
    underneath, so a miss is simply never painted over. Without the negative
    cache it would also be a network request per repaint."""
    app, seen = _logo_app(monkeypatch, tmp_path, body=None)
    with TestClient(app) as client:
        for _ in range(30):
            client.get("/api/providers/openrouter/logo")
            time.sleep(0.01)
        final = client.get("/api/providers/openrouter/logo")
    assert final.status_code == 404
    assert len(seen) == 1, f"a miss must be remembered, saw {len(seen)} fetches"


def test_an_unknown_provider_logo_is_a_404_naming_the_provider(monkeypatch, tmp_path):
    app, _ = _logo_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        reply = client.get("/api/providers/nope/logo")
    assert reply.status_code == 404
    assert reply.json()["error"]["code"] == "provider_not_found"


def test_the_logo_route_is_reachable_even_when_the_ui_is_mounted(tmp_path):
    """The trap every new route has to be tested against: a StaticFiles mount
    at "/" answers index.html for anything registered below it -- and an <img>
    handed an HTML document renders as a broken picture, not as an error."""
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<!doctype html><title>derate</title>")
    app = create_app(
        build_deps(providers=FakeProviders([make_provider()])),
        settings=GatewaySettings(cluster_id="c-test", ui_dir=str(ui)),
    )
    with TestClient(app) as client:
        reply = client.get("/api/providers/openrouter/logo")
    assert reply.status_code == 404
    assert not reply.headers["content-type"].startswith("text/html")


def test_the_logo_route_declares_a_path_parameter_not_a_query_field():
    """The deferred-import convention in this file has demoted a path param to
    a query field before, which reaches an operator as a 404 that looks exactly
    like a provider that does not exist."""
    with TestClient(create_app(build_deps())) as client:
        spec = client.get("/api/openapi.json").json()
    params = spec["paths"]["/api/providers/{provider_id}/logo"]["get"]["parameters"]
    assert [p["in"] for p in params] == ["path"]


def test_the_logo_route_answers_without_any_ports_wired():
    """The day-0 stub provider store has no `get`. This route must resolve a
    provider the way the rest of the surface does, not assume a richer port."""
    with TestClient(create_app(GatewayDeps())) as bare:
        reply = bare.get("/api/providers/openrouter/logo")
    assert reply.status_code == 404
    assert reply.status_code != 500


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


def test_a_local_request_is_forwarded_byte_for_byte():
    """No model rewrite is needed for a local target, so the client's own
    bytes should reach the backend unchanged rather than being re-serialized
    from the parsed dict -- which would reformat this body's spacing and the
    trailing zero on 1.0."""
    backend = FakeBackend()
    with RunningBackend(backend) as running:
        deps = build_deps(
            deployments=FakeDeployments(
                [make_deployment("d-1", "llama-3.3-70b", backend_url=running.base_url)]
            )
        )
        sent = (
            b'{"model":  "llama-3.3-70b", "temperature": 1.0, '
            b'"messages": [{"role": "user", "content": "hello"}]}'
        )
        with TestClient(create_app(deps)) as client:
            reply = client.post(
                "/v1/chat/completions",
                content=sent,
                headers={"content-type": "application/json"},
            )
    assert reply.status_code == 200
    assert backend.raw_bodies[0] == sent


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
# audio endpoints
# ---------------------------------------------------------------------------


def test_speech_is_proxied_and_the_audio_comes_back_untouched():
    """The bytes a client gets are the bytes the runtime sent.

    This is the whole reason /v1/audio/speech needed no new response path: the
    proxy streams aiter_raw() and forwards the upstream's own headers, so an
    MP3 survives it. Asserting byte equality rather than a status code is what
    proves nothing on the way through decoded or re-encoded it.
    """
    backend = FakeBackend()
    with RunningBackend(backend) as running:
        deps = build_deps(
            deployments=FakeDeployments(
                [
                    make_deployment(
                        "d-1",
                        "kokoro",
                        backend_url=running.base_url,
                        modality=Modality.SPEECH,
                    )
                ]
            )
        )
        with TestClient(create_app(deps)) as client:
            reply = client.post(
                "/v1/audio/speech",
                json={"model": "kokoro", "input": "hello", "voice": "af"},
            )
    assert reply.status_code == 200
    assert reply.content == SPEECH_BYTES
    assert reply.headers["content-type"] == "audio/mpeg"
    # The trace id every /v1 response carries. An audio response is not exempt.
    assert reply.headers["X-Request-Id"].startswith("r-")


def test_speech_requests_carry_no_stream_field():
    """A TTS body has no `stream`, and a strict upstream 400s on an unknown one.

    The provider path used to add `stream` to every forwarded body; this pins
    that it no longer does for audio, which is the difference between /v1/audio
    working against a real provider and not.
    """
    backend = FakeBackend()
    with RunningBackend(backend) as running:
        deps = build_deps(
            deployments=FakeDeployments(
                [
                    make_deployment(
                        "d-1",
                        "kokoro",
                        backend_url=running.base_url,
                        modality=Modality.SPEECH,
                    )
                ]
            )
        )
        with TestClient(create_app(deps)) as client:
            client.post(
                "/v1/audio/speech", json={"model": "kokoro", "input": "hi"}
            )
    assert "stream" not in backend.requests[0]


def test_voices_are_listed_from_the_deployment_that_has_them():
    """The one question a caller cannot guess the answer to.

    A voice is a `<name>.wav` beside a `<name>.txt`, an unknown name is
    refused by design, and until this route existed nothing outside the
    container could enumerate them -- so the refusal was unanswerable. The
    runtime's envelope comes back whole, `skipped` included, because that
    array is the server explaining which clips it declined.
    """
    backend = FakeBackend()
    with RunningBackend(backend) as running:
        deps = build_deps(
            deployments=FakeDeployments(
                [
                    make_deployment(
                        "d-1",
                        "kokoro",
                        backend_url=running.base_url,
                        modality=Modality.SPEECH,
                    )
                ]
            )
        )
        with TestClient(create_app(deps)) as client:
            reply = client.get("/v1/audio/voices", params={"model": "kokoro"})
    assert reply.status_code == 200
    body = reply.json()
    assert [v["id"] for v in body["data"]] == ["af", "narrator"]
    # Not summarised, not dropped: the reason a clip was declined is the only
    # thing that tells somebody how to fix it.
    assert body["skipped"] == ["ambient.wav: no ambient.txt beside it"]
    assert backend.voice_requests and backend.voice_requests[0].endswith(
        "/v1/audio/voices"
    )


def test_voices_needs_a_model_and_refuses_a_text_one():
    """Both halves of "which deployment", asked before anything is sent.

    The name is a query parameter here rather than a body field, which is the
    one thing about this route that is not like the others -- so the missing
    case has to be its own refusal rather than falling out of JSON parsing.
    And a text model gets the same `wrong_modality` it would get on
    /v1/audio/speech: the guard is the endpoint's, not the body's.
    """
    backend = FakeBackend()
    with RunningBackend(backend) as running:
        deps = build_deps(
            deployments=FakeDeployments(
                [
                    make_deployment("d-1", "qwen", backend_url=running.base_url),
                    make_deployment(
                        "d-2",
                        "kokoro",
                        backend_url=running.base_url,
                        modality=Modality.SPEECH,
                    ),
                ]
            )
        )
        with TestClient(create_app(deps)) as client:
            missing = client.get("/v1/audio/voices")
            wrong = client.get("/v1/audio/voices", params={"model": "qwen"})
            unknown = client.get("/v1/audio/voices", params={"model": "nope"})

    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "missing_model"

    assert wrong.status_code == 400
    err = wrong.json()["error"]
    assert err["code"] == "wrong_modality"
    # It names the endpoint that would have worked, which is the whole point
    # of this refusal existing rather than a bare 400.
    assert err["correct_endpoint"] == "/v1/chat/completions"

    # A name nothing serves is still a 404 that lists what does, exactly as it
    # is on every other route. Nothing was reached to find that out.
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "model_not_found"
    assert not backend.voice_requests


def test_a_providers_speech_model_has_no_voice_library_to_list():
    """Refused with the mechanism, not with somebody else's 404.

    No provider implements this route. Forwarding to one would return whatever
    their API says about an unknown path, which tells the reader nothing about
    why derate could not answer -- and the answer is a real one: the clips
    live on a node, so a model served only from a provider has none here.
    """
    provider = make_provider(
        served_name="tts-1", upstream_id="tts-1", modality=Modality.SPEECH
    )
    deps = build_deps(providers=FakeProviders([provider]))
    with TestClient(create_app(deps)) as client:
        reply = client.get("/v1/audio/voices", params={"model": "tts-1"})

    assert reply.status_code == 400
    err = reply.json()["error"]
    assert err["code"] == "no_local_target"
    # It says what to do instead, both ways: deploy it here, or send no voice
    # at all -- which is a real request, and the model speaks in its own.
    assert "without one" in err["message"]


def test_a_speech_model_is_refused_on_the_chat_endpoint():
    """Naming a TTS model in a chat request is a mistake worth explaining.

    "No such model" would be a lie -- it exists and is serving -- so the
    refusal has to name the endpoint that would have worked, or the caller is
    left thinking they mistyped the model name.
    """
    deps = build_deps(
        deployments=FakeDeployments(
            [
                make_deployment(
                    "d-1",
                    "kokoro",
                    backend_url="http://backend.invalid/v1",
                    modality=Modality.SPEECH,
                )
            ]
        )
    )
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/v1/chat/completions",
            json={"model": "kokoro", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert reply.status_code == 400
    error = reply.json()["error"]
    assert error["code"] == "wrong_modality"
    assert "/v1/audio/speech" in error["message"]
    assert error["correct_endpoint"] == "/v1/audio/speech"


def test_a_chat_model_is_refused_on_the_speech_endpoint():
    """The guard runs in both directions, and refuses before anything is sent."""
    backend = FakeBackend()
    with RunningBackend(backend) as running:
        deps = build_deps(
            deployments=FakeDeployments(
                [make_deployment("d-1", "llama-3.3-70b", backend_url=running.base_url)]
            )
        )
        with TestClient(create_app(deps)) as client:
            reply = client.post(
                "/v1/audio/speech",
                json={"model": "llama-3.3-70b", "input": "hello"},
            )
    assert reply.status_code == 400
    assert reply.json()["error"]["code"] == "wrong_modality"
    # Refused by us, so the runtime never heard about it.
    assert backend.requests == []


def test_a_text_model_still_serves_embeddings():
    """Text and embeddings are one family here and must stay one.

    One vLLM server answers both from the same weights, so a modality check
    that treated them as exclusive would refuse requests that work today. Only
    audio is a real split.
    """
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


def test_models_listing_says_what_each_model_answers():
    """Without this a client cannot tell a TTS model from a chat model, which
    is exactly the confusion that made a provider's whisper-1 show up in the
    chat picker."""
    deps = build_deps(
        deployments=FakeDeployments(
            [
                make_deployment("d-1", "llama-3.3-70b", backend_url="http://a.invalid/v1"),
                make_deployment(
                    "d-2",
                    "kokoro",
                    backend_url="http://b.invalid/v1",
                    modality=Modality.SPEECH,
                ),
            ]
        )
    )
    with TestClient(create_app(deps)) as client:
        rows = {m["id"]: m for m in client.get("/v1/models").json()["data"]}
    assert rows["llama-3.3-70b"]["modality"] == "text"
    assert rows["kokoro"]["modality"] == "speech"


def test_transcription_upload_reaches_the_runtime_byte_for_byte():
    """The upload is forwarded, not re-encoded.

    This is the whole reason the proxy grew a raw-content path: it used to send
    every request as `json=body` under a hardcoded application/json, which a
    multipart upload cannot survive. Asserting the exact bytes -- and the exact
    content-type, boundary included -- is what proves nothing rebuilt the form.
    """
    backend = FakeBackend()
    audio = b"ID3\x03\x00" + bytes(range(256)) * 8  # binary, and contains \r\n
    with RunningBackend(backend) as running:
        deps = build_deps(
            deployments=FakeDeployments(
                [
                    make_deployment(
                        "d-1",
                        "whisper",
                        backend_url=running.base_url,
                        modality=Modality.TRANSCRIPTION,
                    )
                ]
            )
        )
        with TestClient(create_app(deps)) as client:
            reply = client.post(
                "/v1/audio/transcriptions",
                data={"model": "whisper", "language": "en"},
                files={"file": ("clip.mp3", audio, "audio/mpeg")},
            )
            sent_type = reply.request.headers["content-type"]
            sent_body = reply.request.read()

    assert reply.status_code == 200
    assert reply.json()["text"] == "hello from the cluster"
    assert backend.uploads[0] == sent_body
    assert backend.headers[-1]["content-type"] == sent_type
    # The audio survived intact, \r\n runs and all.
    assert audio in backend.uploads[0]


def test_transcription_needs_a_model_field():
    """The model arrives as a form field here, not a JSON key, so the missing
    -parameter refusal has to come from reading the form."""
    deps = build_deps()
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/v1/audio/transcriptions", files={"file": ("clip.mp3", b"x", "audio/mpeg")}
        )
    assert reply.status_code == 400
    assert reply.json()["error"]["code"] == "missing_model"


def test_transcription_refuses_a_body_that_is_not_multipart():
    """A JSON body here is a client that meant a different endpoint. Say so
    rather than failing somewhere further in on a boundary that never existed."""
    deps = build_deps()
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/v1/audio/transcriptions", json={"model": "whisper"}
        )
    assert reply.status_code == 400
    assert reply.json()["error"]["code"] == "invalid_content_type"


def test_an_oversized_upload_is_refused_rather_than_buffered():
    """The cap is a real limit, not a formality: this body is read whole and
    held for the life of the call so a failed target stays retryable."""
    settings = GatewaySettings(max_audio_upload_bytes=1024)
    deps = build_deps(settings=settings)
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/v1/audio/transcriptions",
            data={"model": "whisper"},
            files={"file": ("big.mp3", b"\x00" * 4096, "audio/mpeg")},
        )
    assert reply.status_code == 413
    assert reply.json()["error"]["code"] == "payload_too_large"


def test_an_oversized_json_body_is_refused_rather_than_buffered():
    """The JSON endpoints get the same cap as the audio upload, for the same
    reason: a multimodal chat body is not reliably smaller than an upload."""
    settings = GatewaySettings(max_json_body_bytes=1024)
    deps = build_deps(settings=settings)
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/v1/chat/completions",
            content=b'{"model": "llama-3.3-70b", "pad": "' + b"x" * 4096 + b'"}',
            headers={"content-type": "application/json"},
        )
    assert reply.status_code == 413
    assert reply.json()["error"]["code"] == "payload_too_large"


def test_a_transcription_model_is_refused_on_the_speech_endpoint():
    """The two audio families are distinct from each other, not just from text."""
    deps = build_deps(
        deployments=FakeDeployments(
            [
                make_deployment(
                    "d-1",
                    "whisper",
                    backend_url="http://backend.invalid/v1",
                    modality=Modality.TRANSCRIPTION,
                )
            ]
        )
    )
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/v1/audio/speech", json={"model": "whisper", "input": "hi"}
        )
    assert reply.status_code == 400
    error = reply.json()["error"]
    assert error["code"] == "wrong_modality"
    assert error["correct_endpoint"] == "/v1/audio/transcriptions"


def test_transcription_records_no_invented_token_count():
    """An audio upload has no tokens, and the trace must not claim otherwise.

    _NoAccounting exists for exactly this: a `data:` frame count over binary
    audio would be fiction, and a 0 would read as a request that produced
    nothing.
    """
    backend = FakeBackend()
    with RunningBackend(backend) as running:
        deps = build_deps(
            deployments=FakeDeployments(
                [
                    make_deployment(
                        "d-1",
                        "whisper",
                        backend_url=running.base_url,
                        modality=Modality.TRANSCRIPTION,
                    )
                ]
            )
        )
        with TestClient(create_app(deps)) as client:
            reply = client.post(
                "/v1/audio/transcriptions",
                data={"model": "whisper"},
                files={"file": ("clip.mp3", b"audio", "audio/mpeg")},
            )
    assert reply.status_code == 200
    assert reply.headers["X-Request-Id"].startswith("r-")


def test_a_remote_transcription_gets_the_provider_name_for_the_model():
    """A provider calls the model something else, and on this path the name is
    inside the body rather than in a dict we can copy.

    So the field is spliced in place. The test asserts both halves of that: the
    upstream sees its own id, and the audio part beside the field is untouched.
    """
    backend = FakeBackend()
    audio = b"ID3\x03\x00" + bytes(range(256)) * 4
    with RunningBackend(backend) as running:
        provider = make_provider(
            "openai",
            base_url=running.base_url,
            served_name="whisper",
            upstream_id="whisper-1",
            modality=Modality.TRANSCRIPTION,
        )
        deps = build_deps(providers=FakeProviders([provider]))
        with TestClient(create_app(deps)) as client:
            reply = client.post(
                "/v1/audio/transcriptions",
                data={"model": "whisper"},
                files={"file": ("clip.mp3", audio, "audio/mpeg")},
            )

    assert reply.status_code == 200
    sent = backend.uploads[0]
    assert b'name="model"\r\n\r\nwhisper-1\r\n' in sent
    assert b"whisper-1-1" not in sent  # spliced, not appended to
    assert audio in sent


# ---------------------------------------------------------------------------
# the round trip, against real models
#
# Everything above proves the gateway forwards an upload without touching it.
# None of it proves a word ever comes back: the backend is a fixture that
# answers a constant, and it would answer the same constant if the runtime on
# the other end could not decode audio at all -- which, until
# docker/audio.Dockerfile, is exactly what the pinned vLLM image could not do.
#
# So this one uses no fixture. derate's own tts runtime says a sentence,
# Whisper reads it back, and both are launched through the ordinary API by the
# ordinary planner and fit gate. The reference clip is generated rather than
# committed, which is why there is no .wav in this repository: the two audio
# endpoints are each other's test data.
# ---------------------------------------------------------------------------

#: Common words, and the project's own line. A rarer sentence would test the
#: vocabulary of whatever checkpoint is loaded rather than the path through
#: the gateway.
SPOKEN = "the link is measured, not assumed"

#: Where a coordinator is, if one is running. Same convention as the UI
#: verifiers' DERATE_CHECK_ORIGIN: this test drives the real HTTP API the way
#: an operator would, rather than assembling a control plane in-process.
E2E_ORIGIN = os.environ.get("DERATE_E2E_ORIGIN", "http://localhost:8088")

#: A clip kept between runs, so a reroll of the Whisper half does not pay for
#: the tts launch again. Delete it to re-record.
E2E_AUDIO = os.environ.get("DERATE_TEST_AUDIO", "")


def _words(text: str) -> set[str]:
    """Casefolded words, punctuation stripped.

    The assertion is a word set and not an equality: Whisper capitalises and
    punctuates to its own taste, and the reference here is synthetic speech.
    Comparing strings would make this a test of transcript formatting, which
    is not the claim -- the claim is that the words survived the round trip.
    """
    return {w.strip(".,!?;:\"'").casefold() for w in text.split()} - {""}


def _deployments(origin: str) -> list[dict]:
    reply = httpx.get(origin + "/api/deployments", timeout=30)
    reply.raise_for_status()
    return reply.json()


#: A launch that died because the cluster could not get the image. Narrow on
#: purpose: this is the one launch failure that is a fact about the machines
#: rather than about the code, and everything else must stay a failure. Both
#: model images derate publishes itself are pinned by tag and neither is on a
#: node until somebody pushes them -- see docker/README.md -- so on a cluster
#: where that has not happened yet, this test has nothing to say.
_IMAGE_MISSING = ("manifest unknown", "image distribution failed",
                  "failed to ensure local image", "pull access denied")


def _wait_ready(origin: str, deployment_id: str, timeout: float) -> dict:
    """Poll until READY, or fail with the record's own last_error.

    A launch is minutes and the interesting failures all happen inside it, so
    the refusal a person needs is the one the deployment recorded -- not
    "timed out".
    """
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        rows = [d for d in _deployments(origin) if d["deployment_id"] == deployment_id]
        if rows:
            last = rows[0]
            if last["state"] == "ready":
                return last
            if last["state"] in ("failed", "stopped"):
                why = last.get("last_error") or ""
                if any(m in why.lower() for m in _IMAGE_MISSING):
                    pytest.skip("this cluster cannot get the image for %s: %s"
                                % (deployment_id, why.strip()[:400]))
                pytest.fail("%s went %s: %s" % (deployment_id, last["state"], why))
        time.sleep(5)
    pytest.fail("%s never became ready in %.0fs; last state %r"
                % (deployment_id, timeout, last.get("state")))


def _launch(origin: str, model_id: str, runtime: str) -> dict:
    """Launch, or skip saying why. A refusal here is not a failing test.

    The fit gate refusing for want of memory on a machine somebody else is
    using is the expected answer, not a defect: this is one GB10 and the
    launch is competing with whatever else is on it.
    """
    reply = httpx.post(origin + "/api/deployments", timeout=300, json={
        "model_id": model_id, "runtime": runtime, "concurrency": 1,
    })
    if reply.status_code >= 400:
        error = reply.json().get("error", {})
        code = error.get("code", reply.status_code)
        if code in ("live_memory_insufficient", "wont_fit", "launch_failed"):
            pytest.skip("cannot launch %s here: %s" % (model_id, error.get("message")))
        pytest.fail("launching %s: %s" % (model_id, reply.text[:2000]))
    return reply.json()


def _stop(origin: str, deployment_id: str) -> None:
    try:
        httpx.delete(origin + "/api/deployments/" + deployment_id, timeout=120)
    except Exception:  # teardown must not mask the reason the test failed
        pass


def _require_coordinator() -> str:
    origin = E2E_ORIGIN.rstrip("/")
    try:
        httpx.get(origin + "/v1/models", timeout=5).raise_for_status()
    except Exception as exc:
        pytest.skip("no coordinator on %s (%s); set DERATE_E2E_ORIGIN"
                    % (origin, type(exc).__name__))
    return origin


def _speak(origin: str, sentence: str) -> bytes:
    """The reference clip: derate's own tts runtime, through the gateway.

    wav rather than the default mp3 -- both ends read it natively and an
    encoder in the middle is a second thing that can fail.
    """
    if E2E_AUDIO and os.path.exists(E2E_AUDIO):
        return open(E2E_AUDIO, "rb").read()

    launched = _launch(origin, "Audio8/Audio8-TTS-Preview-0.6b", "tts")
    dep = launched["deployment_id"]
    try:
        record = _wait_ready(origin, dep, timeout=1800)
        reply = httpx.post(origin + "/v1/audio/speech", timeout=300, json={
            "model": record["served_name"], "input": sentence,
            "response_format": "wav",
        })
        reply.raise_for_status()
        clip = reply.content
    finally:
        # Sequentially, not side by side. This is one machine, and holding two
        # checkpoints resident to save a few minutes is how the second launch
        # gets refused by the fit gate for memory the first one is holding.
        _stop(origin, dep)

    assert clip.startswith(b"RIFF"), "the speech endpoint did not return a WAV"
    if E2E_AUDIO:
        open(E2E_AUDIO, "wb").write(clip)
    return clip


@pytest.mark.slow
def test_whisper_transcribes_what_the_tts_runtime_just_said():
    """The words go out through one audio endpoint and come back through the
    other, and nothing in between is a fixture.

    This is the test that would have caught an image with no audio decoder.
    Every gate in this project passed that image: the architecture is in
    vLLM's support table, the fit gate said it fit, the health check said the
    server was serving and `/v1/models` said it answered transcription. The
    only thing that fails is asking it to transcribe.
    """
    origin = _require_coordinator()
    clip = _speak(origin, SPOKEN)

    launched = _launch(origin, "openai/whisper-base.en", "vllm")
    dep = launched["deployment_id"]
    try:
        record = _wait_ready(origin, dep, timeout=1800)
        assert record["modality"] == "transcription", (
            "launched as %r, so the gateway would keep it off the transcription "
            "route entirely" % record["modality"]
        )
        reply = httpx.post(
            origin + "/v1/audio/transcriptions", timeout=300,
            data={"model": record["served_name"]},
            files={"file": ("spoken.wav", clip, "audio/wav")},
        )
    finally:
        _stop(origin, dep)

    assert reply.status_code == 200, reply.text[:2000]
    text = reply.json()["text"]
    assert text.strip(), "a 200 with no text is not a transcription"

    heard, said = _words(text), _words(SPOKEN)
    assert said <= heard, (
        "Whisper heard %r; the words it missed from %r were %s"
        % (text, SPOKEN, sorted(said - heard))
    )


# ---------------------------------------------------------------------------
# realtime
# ---------------------------------------------------------------------------


def test_realtime_url_is_derived_from_the_provider_base_url():
    """Providers publish one base URL for everything, so the realtime address
    is derived rather than configured separately."""
    from control_plane.gateway.realtime import realtime_url

    assert (
        realtime_url("https://api.openai.com/v1", "gpt-4o-realtime")
        == "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime"
    )
    # http downgrades to ws, for a box on the LAN.
    assert realtime_url("http://h:8000/v1", "m").startswith("ws://")
    # A trailing slash on the path must not produce "//realtime", and a query
    # already on the base URL is kept rather than replaced.
    assert realtime_url("https://x.co/v1/?a=1", "m") == "wss://x.co/v1/realtime?a=1&model=m"


def test_realtime_needs_a_model():
    deps = build_deps()
    with TestClient(create_app(deps)) as client:
        with pytest.raises(WebSocketDisconnect) as caught:
            with client.websocket_connect("/v1/realtime") as ws:
                ws.receive()
    assert caught.value.code == 1008


def test_realtime_refuses_an_unknown_model():
    deps = build_deps()
    with TestClient(create_app(deps)) as client:
        with pytest.raises(WebSocketDisconnect) as caught:
            with client.websocket_connect("/v1/realtime?model=nope") as ws:
                ws.receive()
    assert caught.value.code == 1008


def test_realtime_refuses_a_local_model_and_says_why():
    """A local runtime does not speak this protocol, and composing a session
    from local STT + chat + TTS is a different project.

    The refusal has to say that: a session that opened and then produced no
    audio would look like a bug in the client.
    """
    deps = build_deps(
        deployments=FakeDeployments(
            [make_deployment("d-1", "llama-3.3-70b", backend_url="http://h.invalid/v1")]
        )
    )
    with TestClient(create_app(deps)) as client:
        with pytest.raises(WebSocketDisconnect) as caught:
            with client.websocket_connect("/v1/realtime?model=llama-3.3-70b") as ws:
                ws.receive()
    assert caught.value.code == 1008
    assert "served locally" in (caught.value.reason or "")


def test_realtime_relays_both_directions_over_a_real_socket():
    """The relay's actual job, measured over real sockets in both directions.

    TestClient cannot stand in here: this needs a genuine WebSocket upstream,
    and the property under test is that frames cross unmodified -- text as
    text, binary as binary. Collapsing the two would corrupt either the JSON
    event stream or the audio, and only a round trip catches it.
    """
    import websockets

    received: list = []

    async def upstream(conn):
        await conn.send(json.dumps({"type": "session.created"}))
        async for frame in conn:
            received.append(frame)
            if isinstance(frame, bytes):
                await conn.send(b"PCM:" + frame)
            else:
                await conn.send(json.dumps({"type": "echo", "of": json.loads(frame)["type"]}))

    async def scenario():
        port = _free_port()
        server = await websockets.serve(upstream, "127.0.0.1", port)
        provider = make_provider(
            "openai",
            base_url=f"http://127.0.0.1:{port}/v1",
            served_name="voice",
            upstream_id="gpt-4o-realtime",
        )
        deps = build_deps(providers=FakeProviders([provider]))
        with RunningServer(create_app(deps)) as gateway:
            url = f"ws://127.0.0.1:{gateway.port}/v1/realtime?model=voice"
            async with websockets.connect(url) as ws:
                hello = json.loads(await asyncio.wait_for(ws.recv(), 10))
                await ws.send(json.dumps({"type": "session.update"}))
                echoed = json.loads(await asyncio.wait_for(ws.recv(), 10))
                audio = bytes(range(256)) * 4
                await ws.send(audio)
                back = await asyncio.wait_for(ws.recv(), 10)
        server.close()
        await server.wait_closed()
        return hello, echoed, audio, back

    hello, echoed, audio, back = asyncio.run(scenario())
    assert hello["type"] == "session.created"
    assert echoed == {"type": "echo", "of": "session.update"}
    # Binary survived as binary, and byte-identical.
    assert isinstance(back, bytes) and back == b"PCM:" + audio
    assert audio in received


def test_the_speech_route_is_reachable_even_when_the_ui_is_mounted(tmp_path):
    """A Starlette mount at "/" catches everything not matched by an EARLIER
    route, so a route registered below it silently serves index.html instead.

    Asked by mounting a UI and calling, not by inspecting app.router.routes:
    included routers are wrapped in objects with no .path, so introspection
    would assert on an implementation detail rather than the property that
    matters. A 400 here is a pass -- it means openai_api answered, since
    index.html would have been a 200 with HTML.
    """
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<!doctype html><title>derate</title>")

    app = create_app(
        GatewayDeps(),
        settings=GatewaySettings(cluster_id="c-test", ui_dir=str(ui)),
    )
    with TestClient(app) as client:
        reply = client.post("/v1/audio/speech", json={"input": "no model named"})
        root = client.get("/")

    assert reply.headers["content-type"].startswith("application/json")
    assert reply.json()["error"]["code"] == "missing_model"
    # The mount is genuinely there, so the test above proves ordering.
    assert root.status_code == 200
    assert "<!doctype html>" in root.text


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


def make_router(
    *, deployments=None, providers=None, registry=None, settings=None, breaker=None
):
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
        breaker=breaker,
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


def test_deleting_a_routing_override_restores_the_auto_policy():
    """PUT is not its own inverse; only DELETE gets ``auto_selected`` back.

    Anything that pins a policy for the duration of a run -- a load test that
    wants the router to stop taking turns -- has to be able to put it back
    exactly, and re-PUTting the resolved policy does not do that.
    """
    with TestClient(create_app(build_deps(deployments=two_unequal_replicas()))) as client:

        def config():
            return next(
                c for c in client.get("/api/routing").json()
                if c["served_name"] == "llama-3.3-70b"
            )

        before = config()
        assert before["auto_selected"] is True

        pinned = client.put("/api/routing/llama-3.3-70b", json={"policy": "round_robin"})
        assert pinned.status_code == 200
        assert pinned.json()["policy"] == "round_robin"
        assert pinned.json()["auto_selected"] is False

        # What a caller without DELETE is reduced to: restating the policy the
        # model resolved to before. It leaves the override behind, which is the
        # whole reason this route exists.
        restated = client.put(
            "/api/routing/llama-3.3-70b", json={"policy": before["policy"]}
        )
        assert restated.json()["policy"] == before["policy"]
        assert restated.json()["auto_selected"] is False

        cleared = client.delete("/api/routing/llama-3.3-70b")
        assert cleared.status_code == 200
        assert cleared.json()["auto_selected"] is True
        assert cleared.json()["policy"] == before["policy"]
        assert cleared.json()["auto_reason"] == before["auto_reason"]
        assert config()["auto_selected"] is True


def test_deleting_a_routing_override_is_idempotent_and_404s_on_an_unknown_model():
    with TestClient(create_app(build_deps(deployments=two_unequal_replicas()))) as client:
        first = client.delete("/api/routing/llama-3.3-70b")
        assert first.status_code == 200
        assert first.json()["auto_selected"] is True
        assert client.delete("/api/routing/llama-3.3-70b").status_code == 200
        gone = client.delete("/api/routing/nope")
        assert gone.status_code == 404
        assert "nope" in gone.json()["error"]["message"]


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
        began = time.monotonic()
        reply = client.post(
            "/v1/chat/completions",
            json={"model": "llama-3.3-70b", "messages": [{"role": "user", "content": "x"}]},
        )
        elapsed = time.monotonic() - began
    assert reply.status_code == 502
    assert reply.json()["error"]["code"] == "upstream_unreachable"
    # The canary for the retry loop's exclusion set: with only one target there
    # is nowhere to fail over to, and retrying the same dead port would show up
    # here as latency rather than as a wrong answer.
    assert elapsed < 1.0, f"retried a target it had already tried ({elapsed:.2f}s)"


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
        assert set(payload) == {"ts", "cluster", "nodes", "deployments", "remotes"}
        assert payload["deployments"][0]["deployment_id"] == "d-a"
        assert payload["nodes"][0]["node_id"] == "spark-01"


def test_a_provider_model_reports_throughput_on_the_same_frame_as_a_deployment():
    """The cluster screen draws a remote-served name with the same band as a
    local one and reads both figures off this frame. Joining /api/topology's
    copy instead would tick five times slower on the same drawing."""
    from control_plane.gateway.metrics import MetricsHub
    from control_plane.gateway.stats import StatsRegistry

    stats = StatsRegistry()
    stats.get("d-a").complete(tokens=100, duration_s=1.0)
    stats.get("openrouter:qwen/qwen3-30b-a3b").complete(tokens=40, duration_s=1.0)

    hub = MetricsHub(
        registry=FakeRegistry(),
        deployments=FakeDeployments(
            [make_deployment("d-a", "llama-3.3-70b", backend_url="http://a/v1")]
        ),
        stats=stats,
        settings=GatewaySettings(),
        providers=FakeProviders([make_provider()]),
    )
    event = hub.snapshot()

    remote = event["remotes"][0]
    assert remote["target_id"] == "openrouter:qwen/qwen3-30b-a3b"
    assert remote["provider_id"] == "openrouter"
    assert remote["served_name"] == "qwen3-30b-a3b"
    assert remote["state"] == "healthy"
    # The same counters under the same names, so one reader can read both.
    assert set(remote) == (set(event["deployments"][0]) - {"deployment_id"}) | {
        "target_id",
        "provider_id",
        "served_name",
    }


def test_a_provider_model_nobody_has_routed_to_is_not_on_the_frame():
    """An un-allowlisted key is several hundred models. This payload goes out
    once a second, and a target with no counter has served nothing."""
    from control_plane.gateway.metrics import MetricsHub
    from control_plane.gateway.stats import StatsRegistry

    hub = MetricsHub(
        registry=FakeRegistry(),
        deployments=FakeDeployments(),
        stats=StatsRegistry(),
        settings=GatewaySettings(),
        providers=FakeProviders([make_provider()]),
    )
    assert hub.snapshot()["remotes"] == []


def test_a_coordinator_with_no_provider_port_reports_no_remotes_rather_than_none():
    """Null is the degraded path -- the source raised -- and must stay
    distinguishable from a hub wired without providers at all."""
    from control_plane.gateway.metrics import MetricsHub
    from control_plane.gateway.stats import StatsRegistry

    hub = MetricsHub(
        registry=FakeRegistry(),
        deployments=FakeDeployments(),
        stats=StatsRegistry(),
        settings=GatewaySettings(),
    )
    assert hub.snapshot()["remotes"] is None


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


# ---------------------------------------------------------------------------
# failover: a node that dies mid-request is somebody else's to answer
# ---------------------------------------------------------------------------


def two_replicas(url_a, url_b, served="llama-3.3-70b"):
    return FakeDeployments(
        [
            make_deployment("d-a", served, backend_url=url_a),
            make_deployment("d-b", served, backend_url=url_b),
        ]
    )


CHAT = {"model": "llama-3.3-70b", "messages": [{"role": "user", "content": "x"}]}


def test_a_dead_replica_is_retried_on_a_live_one_and_the_client_never_learns():
    """The promise is that a client never learns which node answered. A node
    dying has to be held to the same promise."""
    live = FakeBackend()
    with RunningBackend(live) as running_live:
        dead_port = _free_port()  # nothing is listening here
        deps = build_deps(
            deployments=two_replicas(f"http://127.0.0.1:{dead_port}/v1", running_live.base_url)
        )
        with TestClient(create_app(deps)) as client:
            reply = client.post("/v1/chat/completions", json=CHAT)

    assert reply.status_code == 200
    assert reply.json()["choices"][0]["message"]["content"] == "hello"
    assert len(live.requests) == 1
    # Nothing in the answer hints that a node died.
    assert "d-a" not in reply.text and "unreachable" not in reply.text


def test_a_5xx_is_retried_on_another_target_but_a_4xx_is_not():
    broken = FakeBackend(status=500, error_body={"error": "cuda oom"})
    good = FakeBackend()
    with RunningBackend(broken) as a, RunningBackend(good) as b:
        deps = build_deps(deployments=two_replicas(a.base_url, b.base_url))
        with TestClient(create_app(deps)) as client:
            assert client.post("/v1/chat/completions", json=CHAT).status_code == 200
    assert len(broken.requests) == 1 and len(good.requests) == 1

    refusing = FakeBackend(status=422, error_body={"error": "bad request"})
    spare = FakeBackend()
    with RunningBackend(refusing) as a, RunningBackend(spare) as b:
        deps = build_deps(deployments=two_replicas(a.base_url, b.base_url))
        with TestClient(create_app(deps)) as client:
            reply = client.post("/v1/chat/completions", json=CHAT)
    assert reply.status_code == 422
    # A 4xx is the backend working correctly. Asking somebody else is wrong.
    assert len(spare.requests) == 0


def test_an_exhausted_chain_returns_the_last_upstream_5xx_verbatim():
    """Rule 2 has to survive the end of the chain, not just the first hop."""
    first = FakeBackend(status=500, error_body={"error": "first node"})
    second = FakeBackend(status=503, error_body={"object": "error", "message": "second node"})
    with RunningBackend(first) as a, RunningBackend(second) as b:
        deps = build_deps(deployments=two_replicas(a.base_url, b.base_url))
        with TestClient(create_app(deps)) as client:
            reply = client.post("/v1/chat/completions", json=CHAT)
    assert reply.status_code == 503
    assert reply.json() == {"object": "error", "message": "second node"}
    # Nothing of ours is bolted onto a preserved backend error.
    assert "retry-after" not in {k.lower() for k in reply.headers}


def test_an_exhausted_transport_chain_returns_502_naming_how_many_were_tried():
    ports = [_free_port(), _free_port()]
    deps = build_deps(
        deployments=two_replicas(*[f"http://127.0.0.1:{p}/v1" for p in ports])
    )
    with TestClient(create_app(deps)) as client:
        reply = client.post("/v1/chat/completions", json=CHAT)
    assert reply.status_code == 502
    body = reply.json()["error"]
    assert body["code"] == "upstream_unreachable"
    assert "2 targets" in body["message"]


def test_kv_commitments_are_released_after_every_failed_attempt():
    """A chain that commits per attempt and releases none would 429 the one
    healthy node it was trying to reach."""
    live = FakeBackend()
    with RunningBackend(live) as running:
        deps = build_deps(
            deployments=two_replicas(f"http://127.0.0.1:{_free_port()}/v1", running.base_url)
        )
        app = create_app(deps)
        with TestClient(app) as client:
            assert client.post("/v1/chat/completions", json=CHAT).status_code == 200
            admission = app.state.ctx.admission
            assert admission.committed("d-a") == 0
            assert admission.committed("d-b") == 0


def test_a_provider_key_is_never_carried_into_a_retry_against_a_local_backend():
    """The remote branch rewrites the body and sets a bearer token. Neither may
    survive into the next target's turn."""
    local = FakeBackend()
    with RunningBackend(local) as running:
        provider = make_provider(
            base_url=f"http://127.0.0.1:{_free_port()}/v1",  # dead, so it fails over
            served_name="llama-3.3-70b",
            upstream_id="meta/llama-3.3-70b-instruct",
        )
        deps = build_deps(
            deployments=FakeDeployments(
                [make_deployment("d-a", "llama-3.3-70b", backend_url=running.base_url)]
            ),
            providers=FakeProviders([provider]),
        )
        app = create_app(deps)
        with TestClient(app) as client:
            app.state.ctx.router.set_policy("llama-3.3-70b", RoutingPolicy.COST_AWARE)
            reply = client.post("/v1/chat/completions", json=CHAT)

    assert reply.status_code == 200
    seen = json.dumps(local.headers) + json.dumps(local.requests)
    assert SECRET_KEY not in seen
    assert "Bearer" not in json.dumps(local.headers)
    # The provider's name for the model must not reach a vLLM either.
    assert local.requests[0]["model"] == "llama-3.3-70b"


def test_a_backend_that_dies_before_its_first_chunk_is_retried():
    """Holding the response line until the first byte is what makes a node
    dying during prefill recoverable."""
    dying = FakeBackend(die_before_first_chunk=True)
    live = FakeBackend()
    with RunningBackend(dying) as a, RunningBackend(live) as b:
        deps = build_deps(deployments=two_replicas(a.base_url, b.base_url))
        with RunningServer(create_app(deps)) as gateway, httpx.Client(timeout=30) as client:
            reply = client.post(
                f"{gateway.url}/v1/chat/completions", json={**CHAT, "stream": True}
            )
    assert reply.status_code == 200
    assert b"tok0" in reply.content
    assert len(dying.requests) == 1 and len(live.requests) == 1


def test_headers_are_released_when_the_first_chunk_outlasts_the_hold_budget():
    """A long prefill is not a failure. Past the budget the status goes out and
    the stream still completes -- the timeout must not cancel the read."""
    settings = GatewaySettings()
    settings.upstream_header_hold_s = 0.2
    slow = FakeBackend(chunks=3, first_chunk_delay=0.8)
    with RunningBackend(slow) as running:
        deps = build_deps(
            deployments=FakeDeployments(
                [make_deployment("d-1", "llama-3.3-70b", backend_url=running.base_url)]
            ),
            settings=settings,
        )
        with RunningServer(create_app(deps, settings=settings)) as gateway, httpx.Client(
            timeout=30
        ) as client:
            began = time.monotonic()
            with client.stream(
                "POST", f"{gateway.url}/v1/chat/completions", json={**CHAT, "stream": True}
            ) as response:
                headers_at = time.monotonic() - began
                assert response.status_code == 200
                payload = b"".join(response.iter_raw())

    assert headers_at < 0.7, f"headers held for {headers_at:.2f}s past the budget"
    assert payload.count(b"data:") == 4  # three frames plus [DONE]


def test_a_disconnected_stream_settles_its_outstanding_count():
    """Audit H-1. GeneratorExit and CancelledError derive from BaseException,
    so a client hanging up used to skip settle() entirely: the in-flight count
    never came down and the KV commitment was never released."""
    backend = FakeBackend(chunk_delay=0.3, chunks=20)
    with RunningBackend(backend) as running:
        deps = build_deps(
            deployments=FakeDeployments(
                [make_deployment("d-1", "llama-3.3-70b", backend_url=running.base_url)]
            )
        )
        app = create_app(deps)
        with RunningServer(app) as gateway, httpx.Client(timeout=30) as client:
            with client.stream(
                "POST", f"{gateway.url}/v1/chat/completions", json={**CHAT, "stream": True}
            ) as response:
                next(response.iter_raw())  # one chunk, then walk away
            deadline = time.monotonic() + 5
            ctx = app.state.ctx
            while time.monotonic() < deadline:
                if ctx.stats.outstanding("d-1") == 0 and ctx.admission.committed("d-1") == 0:
                    break
                time.sleep(0.05)

    assert ctx.stats.outstanding("d-1") == 0, "in-flight count leaked on disconnect"
    assert ctx.admission.committed("d-1") == 0, "KV commitment leaked on disconnect"


# ---------------------------------------------------------------------------
# circuit breaker: stop feeding a target the gateway watched fail
# ---------------------------------------------------------------------------


class Clock:
    """A hand-cranked clock, so cooldowns are tested without sleeping."""

    def __init__(self, t=0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def test_consecutive_transport_failures_bench_a_target_and_a_probe_brings_it_back():
    from control_plane.gateway.breaker import CLOSED, HALF_OPEN, OPEN, CircuitBreaker

    clock = Clock()
    breaker = CircuitBreaker(failure_threshold=3, cooldown_s=30.0, clock=clock)

    breaker.record_transport_failure("d-a")
    breaker.record_transport_failure("d-a")
    assert breaker.state("d-a") == CLOSED, "one failure is a coincidence"

    breaker.record_transport_failure("d-a")
    assert breaker.state("d-a") == OPEN and breaker.is_open("d-a")

    clock.advance(30.1)
    assert breaker.state("d-a") == HALF_OPEN
    assert not breaker.is_open("d-a"), "half open stays selectable, to be probed"
    assert breaker.begin("d-a") is True
    assert breaker.begin("d-a") is False, "exactly one probe, however many ask"

    breaker.record_success("d-a")
    assert breaker.state("d-a") == CLOSED


def test_a_failed_probe_buys_another_full_cooldown():
    from control_plane.gateway.breaker import HALF_OPEN, OPEN, CircuitBreaker

    clock = Clock()
    breaker = CircuitBreaker(failure_threshold=1, cooldown_s=10.0, clock=clock)
    breaker.record_transport_failure("d-a")
    clock.advance(10.1)
    assert breaker.state("d-a") == HALF_OPEN
    breaker.begin("d-a")
    breaker.record_transport_failure("d-a")
    assert breaker.state("d-a") == OPEN
    clock.advance(9.0)
    assert breaker.state("d-a") == OPEN, "a flapping node is not retried every tick"


def test_a_benched_target_is_excluded_from_selection_and_returns_on_recovery():
    from control_plane.gateway.breaker import CircuitBreaker

    clock = Clock()
    breaker = CircuitBreaker(failure_threshold=2, cooldown_s=30.0, clock=clock)
    router, _, _ = make_router(
        deployments=two_replicas("http://a/v1", "http://b/v1"), breaker=breaker
    )
    breaker.record_transport_failure("d-a")
    breaker.record_transport_failure("d-a")
    for _ in range(5):
        assert router.select("llama-3.3-70b").target.target_id == "d-b"

    breaker.record_success("d-a")
    # Nothing is in flight, so least-outstanding breaks the tie on target_id:
    # d-a winning again is exactly the proof that it is back in the running.
    assert router.select("llama-3.3-70b").target.target_id == "d-a"


def test_polling_the_routing_api_does_not_consume_the_half_open_probe():
    """Router._refresh_live serves GET /api/routing as well as real requests.
    A breaker read there that claimed the probe would let an open UI eat every
    one of them, and no benched target would ever come back."""
    from control_plane.gateway.breaker import CircuitBreaker

    clock = Clock()
    breaker = CircuitBreaker(failure_threshold=1, cooldown_s=10.0, clock=clock)
    deps = build_deps(deployments=two_replicas("http://a/v1", "http://b/v1"))
    app = create_app(deps)
    with TestClient(app) as client:
        app.state.ctx.router._breaker = breaker
        breaker.record_transport_failure("d-a")
        clock.advance(10.1)
        for _ in range(20):
            assert client.get("/api/routing").status_code == 200
        assert breaker.begin("d-a") is True, "the UI ate the probe"


def test_a_5xx_does_not_bench_a_target():
    """One malformed request that every node rejects with a 500 must not take
    the whole cluster out of rotation."""
    broken = FakeBackend(status=500)
    spare = FakeBackend(status=500)
    with RunningBackend(broken) as a, RunningBackend(spare) as b:
        deps = build_deps(deployments=two_replicas(a.base_url, b.base_url))
        app = create_app(deps)
        with TestClient(app) as client:
            for _ in range(6):
                client.post("/v1/chat/completions", json=CHAT)
            assert app.state.ctx.breaker.opened_targets() == {}


def test_a_benched_target_that_leaves_the_index_does_not_come_back_benched():
    """Stopping and relaunching a deployment under the same id would otherwise
    return it permanently benched, with nothing left to clear it."""
    from control_plane.gateway.breaker import CircuitBreaker

    breaker = CircuitBreaker(failure_threshold=1, cooldown_s=300.0)
    deployments = two_replicas("http://a/v1", "http://b/v1")
    router, _, _ = make_router(deployments=deployments, breaker=breaker)
    breaker.record_transport_failure("d-a")
    assert breaker.is_open("d-a")

    deployments.deployments[0].state = DeploymentState.FAILED
    router.rebuild(force_scores=True)
    deployments.deployments[0].state = DeploymentState.READY
    router.rebuild(force_scores=True)

    assert not breaker.is_open("d-a")
    assert router.select("llama-3.3-70b").target.target_id == "d-a"


def test_the_routing_api_says_why_a_target_is_benched():
    from control_plane.gateway.breaker import CircuitBreaker

    breaker = CircuitBreaker(failure_threshold=1, cooldown_s=300.0)
    deps = build_deps(deployments=two_replicas("http://a/v1", "http://b/v1"))
    app = create_app(deps)
    with TestClient(app) as client:
        app.state.ctx.router._breaker = breaker
        app.state.ctx.breaker = breaker
        breaker.record_transport_failure("d-a")
        targets = {t["target_id"]: t for t in client.get("/api/routing").json()[0]["targets"]}
    assert targets["d-a"]["circuit"] == "open"
    assert targets["d-b"]["circuit"] == "closed"
    assert targets["d-a"]["healthy"] is False


def test_the_retry_budget_caps_amplification_under_a_deterministic_500():
    """A 500 the backend will give every node -- because the request is what it
    objects to -- must not fan out across the fleet indefinitely."""
    first = FakeBackend(status=500)
    second = FakeBackend(status=500)
    with RunningBackend(first) as a, RunningBackend(second) as b:
        deps = build_deps(deployments=two_replicas(a.base_url, b.base_url))
        app = create_app(deps)
        with TestClient(app) as client:
            for _ in range(20):
                assert client.post("/v1/chat/completions", json=CHAT).status_code == 500
            budget = app.state.ctx.retry_budget.snapshot()

    upstream_calls = len(first.requests) + len(second.requests)
    assert upstream_calls < 20 * 1.5, f"{upstream_calls} upstream calls for 20 requests"
    assert budget["refused"] > 0


# ---------------------------------------------------------------------------
# parking: hold a request briefly when its node has just gone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason", ["memory_critical", "draining", "rate_limited"]
)
def test_a_blocked_target_is_refused_instantly_rather_than_parked(reason):
    """Load shedding is a decision, not an outage. Queueing behind one hides
    exactly what the operator needs to see."""
    backend = FakeBackend()
    with RunningBackend(backend) as running:
        deps = build_deps(
            deployments=FakeDeployments(
                [make_deployment("d-a", "llama-3.3-70b", backend_url=running.base_url)]
            )
        )
        app = create_app(deps)
        with TestClient(app) as client:
            assert client.post("/v1/chat/completions", json=CHAT).status_code == 200
            app.state.ctx.admission.block("d-a", reason)
            began = time.monotonic()
            reply = client.post("/v1/chat/completions", json=CHAT)
            elapsed = time.monotonic() - began

    assert reply.status_code == 503
    assert reply.json()["error"]["code"] == "no_target_admitting"
    assert elapsed < 1.0, f"parked a deliberate refusal for {elapsed:.2f}s"


def test_a_parked_request_is_dispatched_when_its_replica_recovers():
    backend = FakeBackend()
    with RunningBackend(backend) as running:
        deployments = FakeDeployments(
            [make_deployment("d-a", "llama-3.3-70b", backend_url=running.base_url)]
        )
        deps = build_deps(deployments=deployments)
        with TestClient(create_app(deps)) as client:
            assert client.post("/v1/chat/completions", json=CHAT).status_code == 200

            deployments.deployments[0].state = DeploymentState.FAILED
            recover = threading.Timer(
                0.6,
                lambda: setattr(deployments.deployments[0], "state", DeploymentState.READY),
            )
            recover.start()
            began = time.monotonic()
            reply = client.post("/v1/chat/completions", json=CHAT)
            elapsed = time.monotonic() - began
            recover.join()

    assert reply.status_code == 200, "a request held over a restart should be answered"
    assert 0.5 < elapsed < 5.0
    assert len(backend.requests) == 2


def test_a_parked_request_gives_up_at_the_deadline_rather_than_hanging():
    settings = GatewaySettings()
    settings.park_grace_s = 1.0
    backend = FakeBackend()
    with RunningBackend(backend) as running:
        deployments = FakeDeployments(
            [make_deployment("d-a", "llama-3.3-70b", backend_url=running.base_url)]
        )
        deps = build_deps(deployments=deployments, settings=settings)
        with TestClient(create_app(deps, settings=settings)) as client:
            assert client.post("/v1/chat/completions", json=CHAT).status_code == 200
            deployments.deployments[0].state = DeploymentState.FAILED
            began = time.monotonic()
            reply = client.post("/v1/chat/completions", json=CHAT)
            elapsed = time.monotonic() - began

    assert reply.status_code == 503
    assert reply.json()["error"]["code"] == "no_target_admitting"
    assert 0.9 < elapsed < 4.0, f"gave up after {elapsed:.2f}s"


def test_parking_is_bounded_and_a_full_lot_refuses_immediately():
    from control_plane.gateway.parking import ParkingLot

    settings = GatewaySettings()
    settings.park_max_waiters = 2
    settings.park_max_per_model = 1
    settings.park_max_body_bytes = 100
    lot = ParkingLot(settings)

    assert lot.accepts("llama-3.3-70b", 50)
    assert not lot.accepts("llama-3.3-70b", 101), "an enormous prompt is not held"

    settings.park_grace_s = 0.0
    assert not ParkingLot(settings).accepts("llama-3.3-70b", 1), "zero grace disables it"


def test_parking_never_applies_to_a_model_that_never_served():
    """Unknown and still-launching both already have an honest answer."""
    deps = build_deps(deployments=FakeDeployments([]))
    with TestClient(create_app(deps)) as client:
        began = time.monotonic()
        reply = client.post("/v1/chat/completions", json={"model": "nope", "messages": []})
        elapsed = time.monotonic() - began
    assert reply.status_code == 404
    assert elapsed < 1.0


# ---------------------------------------------------------------------------
# node admission: join, admit, remove (H-5)
# ---------------------------------------------------------------------------


def test_join_with_a_bad_token_is_rejected_with_403():
    registry = FakeJoinableRegistry(reject_join=True)
    deps = build_deps(registry=registry)
    profile = make_node_profile()
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/api/nodes/join",
            json={
                "token": "wrong-token",
                "profile": {
                    "node_id": profile.node_id,
                    "hostname": profile.hostname,
                    "address": profile.address,
                    "device_class": profile.device_class.value,
                    "gpu_name": profile.gpu_name,
                    "gpu_count": profile.gpu_count,
                    "total_memory": profile.total_memory,
                    "addressable_memory": profile.addressable_memory,
                    "memory_bandwidth_gbps": profile.memory_bandwidth_gbps,
                    "compute_capability": profile.compute_capability,
                    "driver_version": profile.driver_version,
                },
                "agent_url": "http://192.168.11.99:8770",
            },
        )
    assert reply.status_code == 403
    assert reply.json()["error"]["code"] == "join_rejected"


def test_join_with_no_token_passes_through_the_candidate_status():
    registry = FakeJoinableRegistry()
    deps = build_deps(registry=registry)
    profile = make_node_profile()
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/api/nodes/join",
            json={
                "profile": {
                    "node_id": profile.node_id,
                    "hostname": profile.hostname,
                    "address": profile.address,
                    "device_class": profile.device_class.value,
                    "gpu_name": profile.gpu_name,
                    "gpu_count": profile.gpu_count,
                    "total_memory": profile.total_memory,
                    "addressable_memory": profile.addressable_memory,
                    "memory_bandwidth_gbps": profile.memory_bandwidth_gbps,
                    "compute_capability": profile.compute_capability,
                    "driver_version": profile.driver_version,
                },
                "agent_url": "http://192.168.11.99:8770",
            },
        )
    # Passed through as-is: 200, no cluster_id, "candidate" -- never rejected.
    assert reply.status_code == 200
    body = reply.json()
    assert body["status"] == "candidate"
    assert body["node_id"] == profile.node_id
    token, sent_profile, agent_url = registry.joins[0]
    assert token is None
    assert sent_profile.node_id == profile.node_id
    assert agent_url == "http://192.168.11.99:8770"


def test_join_with_a_malformed_body_is_a_400_not_a_500():
    deps = build_deps(registry=FakeJoinableRegistry())
    with TestClient(create_app(deps)) as client:
        reply = client.post("/api/nodes/join", json={"agent_url": "http://x"})
    assert reply.status_code == 400
    assert reply.json()["error"]["code"] == "invalid_join_body"


def test_join_gracefully_degrades_when_the_registry_has_no_handle_join():
    deps = build_deps(registry=FakeRegistry())  # no handle_join at all
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/api/nodes/join",
            json={"profile": {"node_id": "x"}, "agent_url": "http://x"},
        )
    assert reply.status_code == 501
    assert reply.json()["error"]["code"] == "not_implemented"


def test_admit_unknown_candidate_is_404():
    deps = build_deps(registry=FakeJoinableRegistry())
    with TestClient(create_app(deps)) as client:
        reply = client.post("/api/nodes/no-such-node/admit")
    assert reply.status_code == 404
    assert reply.json()["error"]["code"] == "node_not_found"


def test_admit_a_known_candidate_succeeds():
    registry = FakeJoinableRegistry()
    deps = build_deps(registry=registry)
    node_id = registry.states[0].profile.node_id
    with TestClient(create_app(deps)) as client:
        reply = client.post(f"/api/nodes/{node_id}/admit")
    assert reply.status_code == 200
    assert reply.json()["profile"]["node_id"] == node_id
    assert registry.admitted == [node_id]


def test_remove_unknown_node_is_404():
    # Exercises the route's own contract (remove_node -> NodeNotFound -> 404).
    # See FakeJoinableRegistry's docstring: the real Registry.remove_node does
    # not raise this today (it pops silently and the route would 200), which
    # is a registry.py gap outside this package's ownership, not a claim that
    # this test proves 404 against the real Registry as it stands.
    deps = build_deps(registry=FakeJoinableRegistry())
    with TestClient(create_app(deps)) as client:
        reply = client.delete("/api/nodes/no-such-node")
    assert reply.status_code == 404
    assert reply.json()["error"]["code"] == "node_not_found"


def test_remove_a_known_node_succeeds():
    registry = FakeJoinableRegistry()
    deps = build_deps(registry=registry)
    node_id = registry.states[0].profile.node_id
    with TestClient(create_app(deps)) as client:
        reply = client.delete(f"/api/nodes/{node_id}")
    assert reply.status_code == 200
    assert reply.json() == {"removed": node_id}
    assert registry.removed == [node_id]


def test_the_default_stub_registry_wires_the_same_admission_surface():
    """gateway/stubs.py's StubRegistry must expose the real method names and
    exceptions, not just the RegistryPort trio (H-5)."""
    with TestClient(create_app()) as client:
        assert client.get("/api/nodes/candidates").json() == []
        admit_reply = client.post("/api/nodes/spark-01/admit")
        assert admit_reply.status_code == 200
        missing = client.post("/api/nodes/does-not-exist/admit")
        assert missing.status_code == 404
        removed = client.delete("/api/nodes/spark-01")
        assert removed.status_code == 200
        join_reply = client.post(
            "/api/nodes/join",
            json={"profile": {"node_id": "x"}, "agent_url": "http://x"},
        )
        # The stub genuinely cannot accept a join; degrades to 501 rather
        # than crashing with an unhandled NotImplementedError.
        assert join_reply.status_code == 501


# ---------------------------------------------------------------------------
# provider admin: PATCH, DELETE, refresh of an unknown provider (M-21)
# ---------------------------------------------------------------------------


def test_patch_unknown_provider_is_404_not_501():
    deps = build_deps(providers=FakeProviders([make_provider()]))
    with TestClient(create_app(deps)) as client:
        reply = client.patch("/api/providers/no-such-provider", json={"priority": 1})
    assert reply.status_code == 404
    assert reply.json()["error"]["code"] == "provider_not_found"


def test_patch_a_known_provider_updates_it():
    providers = FakeProviders([make_provider(priority=10)])
    deps = build_deps(providers=providers)
    with TestClient(create_app(deps)) as client:
        reply = client.patch("/api/providers/openrouter", json={"priority": 5})
    assert reply.status_code == 200
    assert reply.json()["priority"] == 5


def test_delete_unknown_provider_is_404_not_501():
    deps = build_deps(providers=FakeProviders([make_provider()]))
    with TestClient(create_app(deps)) as client:
        reply = client.delete("/api/providers/no-such-provider")
    assert reply.status_code == 404
    assert reply.json()["error"]["code"] == "provider_not_found"


def test_delete_a_known_provider_removes_it():
    providers = FakeProviders([make_provider()])
    deps = build_deps(providers=providers)
    with TestClient(create_app(deps)) as client:
        reply = client.delete("/api/providers/openrouter")
    assert reply.status_code == 200
    assert client.get("/api/providers").json() == []


def test_refresh_unknown_provider_is_404():
    deps = build_deps(providers=FakeProviders([make_provider()]))
    with TestClient(create_app(deps)) as client:
        reply = client.post("/api/providers/no-such-provider/refresh")
    assert reply.status_code == 404
    assert reply.json()["error"]["code"] == "provider_not_found"


def test_the_default_stub_providers_support_patch_and_delete():
    with TestClient(create_app()) as client:
        assert client.patch(
            "/api/providers/openrouter", json={"priority": 1}
        ).status_code == 200
        assert client.delete("/api/providers/openrouter").status_code == 200
        assert client.delete("/api/providers/openrouter").status_code == 404


# ---------------------------------------------------------------------------
# adding a provider over HTTP: the key travels one way, in the request body
# ---------------------------------------------------------------------------


def _real_providers(tmp_path, env=None):
    """A real ProviderService over a mocked upstream.

    ``FakeProviders.add`` raises NotImplementedError, which is why POST
    /api/providers had no HTTP-level coverage at all: neither its 201 body nor
    its error wrapping was reachable through the fake.
    """
    from control_plane.providers import ProviderService, SecretStore

    def handler(request):
        if request.method == "GET" and request.url.path.endswith("/models"):
            return httpx.Response(
                200, json={"data": [{"id": "qwen/qwen3-30b-a3b", "context_length": 32768}]}
            )
        return httpx.Response(404, json={"error": {"message": "nope"}})

    return ProviderService(
        data_path=tmp_path,
        secrets=SecretStore(tmp_path / "secrets.json", env=env or {}),
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def test_posting_a_pasted_key_stores_it_and_answers_with_the_reference(tmp_path, caplog):
    """The key goes up in the body, into secrets.json, and no further."""
    import logging

    caplog.set_level(logging.DEBUG)
    providers = _real_providers(tmp_path)
    with TestClient(create_app(build_deps(providers=providers))) as client:
        reply = client.post(
            "/api/providers", json={"kind": "openrouter", "api_key": SECRET_KEY}
        )

    assert reply.status_code == 201
    body = reply.json()
    assert body["api_key"] == "***"
    assert body["api_key_ref"] == "DERATE_OPENROUTER_API_KEY"

    assert SECRET_KEY not in reply.text
    assert SECRET_KEY not in caplog.text
    stored = json.loads((tmp_path / "secrets.json").read_text())
    assert stored["DERATE_OPENROUTER_API_KEY"] == SECRET_KEY
    assert SECRET_KEY not in (tmp_path / "providers.json").read_text()


def test_posting_a_key_into_the_reference_field_is_a_400_naming_the_field_that_works(tmp_path):
    """The refusal an operator actually hits. Tested at the service layer since
    it was written; the sentence the route wraps around it was not."""
    providers = _real_providers(tmp_path)
    with TestClient(create_app(build_deps(providers=providers))) as client:
        reply = client.post(
            "/api/providers", json={"kind": "openrouter", "api_key_ref": SECRET_KEY}
        )

    assert reply.status_code == 400
    error = reply.json()["error"]
    assert error["code"] == "provider_add_failed"
    assert error["message"].startswith("Could not add provider.")
    assert "NAME of an environment variable" in error["message"]
    assert "api_key" in error["message"]
    # The refusal cannot carry the key it is refusing.
    assert SECRET_KEY not in reply.text


def test_patching_a_key_into_the_reference_field_is_a_400_not_a_500(tmp_path):
    """POST has caught this since it was written; PATCH had no handler at all,
    so the same mistake surfaced as a framework 500 with the sentence buried in
    a traceback instead of a 400 carrying it."""
    providers = _real_providers(tmp_path)
    with TestClient(create_app(build_deps(providers=providers))) as client:
        added = client.post(
            "/api/providers", json={"kind": "openrouter", "api_key": SECRET_KEY}
        )
        assert added.status_code == 201
        reply = client.patch(
            "/api/providers/openrouter", json={"api_key_ref": SECRET_KEY}
        )

    assert reply.status_code == 400
    error = reply.json()["error"]
    assert error["code"] == "provider_update_failed"
    assert "NAME of an environment variable" in error["message"]
    assert SECRET_KEY not in reply.text


def test_secret_refs_lists_names_and_never_values(tmp_path):
    """Names are the one part of a secret that is safe to show -- they are
    already rendered in every provider listing -- and they are what makes
    naming a reference a choice from what exists."""
    providers = _real_providers(tmp_path)
    with TestClient(create_app(build_deps(providers=providers))) as client:
        assert client.get("/api/providers/secret-refs").json() == {"refs": []}
        client.post("/api/providers", json={"kind": "openrouter", "api_key": SECRET_KEY})
        reply = client.get("/api/providers/secret-refs")

    assert reply.status_code == 200
    assert reply.json() == {"refs": ["DERATE_OPENROUTER_API_KEY"]}
    assert SECRET_KEY not in reply.text


def test_secret_refs_degrades_when_the_port_keeps_no_secrets():
    """The stub port has no secret store. A form that cannot autocomplete is
    not a reason to 500."""
    with TestClient(create_app()) as client:
        reply = client.get("/api/providers/secret-refs")
    assert reply.status_code == 200
    assert reply.json() == {"refs": []}


def test_the_stub_surface_answers_a_pasted_key_with_a_reference_too():
    """The day-0 build is where somebody first meets the paste field.

    The stub has no secrets file and writes nothing, but it must do the visible
    half. Dropping ``api_key`` answered 201 with an empty reference and a
    key_state of "not_needed" -- a paste path that reads as having silently
    done nothing, on the one surface where nobody has a coordinator to check.
    """
    with TestClient(create_app()) as client:
        reply = client.post(
            "/api/providers",
            json={"kind": "openrouter", "api_key": SECRET_KEY},
        )

    assert reply.status_code == 201
    body = reply.json()
    assert body["api_key_ref"] == f"DERATE_{body['provider_id'].upper().replace('-', '_')}_API_KEY"
    assert body["key_state"] == "set"
    assert body["api_key"] == "***"
    assert SECRET_KEY not in reply.text


def test_the_stub_surface_refuses_a_key_in_the_reference_field_in_the_same_words():
    """The screen, not just the storage. A stub that accepts what the real
    coordinator refuses teaches the add form the opposite lesson: the field
    learns what it takes from the 400 it comes back as."""
    with TestClient(create_app()) as client:
        reply = client.post(
            "/api/providers",
            json={"kind": "openrouter", "api_key_ref": SECRET_KEY},
        )

    assert reply.status_code == 400
    error = reply.json()["error"]
    assert error["code"] == "provider_add_failed"
    assert "NAME of an environment variable" in error["message"]
    assert SECRET_KEY not in reply.text


def test_the_stub_surface_takes_the_kinds_default_base_url():
    """"Leave it blank for the default" is what the add form offers, and the
    stub read spec["base_url"] -- so the form's own default path came back as a
    400 whose message was the word KeyError."""
    with TestClient(create_app()) as client:
        reply = client.post("/api/providers", json={"kind": "openrouter"})

    assert reply.status_code == 201
    assert reply.json()["base_url"] == "https://openrouter.ai/api/v1"


# ---------------------------------------------------------------------------
# plan and deployments: fit_unavailable, runtime_unsupported, launch ValueError
# ---------------------------------------------------------------------------


def test_create_deployment_refuses_to_launch_unchecked_when_fit_is_unavailable():
    """H-3: no fit port answer is never treated as an implicit pass."""

    class NoFit:
        def check(self, req, nodes):
            return None

    deployments = FakeDeployments()
    deps = build_deps(deployments=deployments)
    deps.fit = NoFit()
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/api/deployments",
            json={"model_id": "meta-llama/Llama-3.3-70B-Instruct"},
        )
    assert reply.status_code == 503
    assert reply.json()["error"]["code"] == "fit_unavailable"
    assert deployments.launched == []


def test_plan_endpoint_tolerates_fit_being_unavailable():
    """/api/plan may still answer with fit: null (H-3 only guards launch)."""

    class NoFit:
        def check(self, req, nodes):
            return None

    deps = build_deps()
    deps.fit = NoFit()
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/api/plan",
            json={"model_id": "meta-llama/Llama-3.3-70B-Instruct"},
        )
    assert reply.status_code == 200
    assert reply.json()["fit"] is None


def test_create_deployment_refuses_an_unsupported_runtime():
    """M-11: resolver.supported_by is consulted when the port exposes it."""

    class UnsupportingResolver(StubResolver):
        def supported_by(self, shape, runtime):
            return False, f"{runtime} has no adapter for this architecture"

    deployments = FakeDeployments()
    deps = build_deps(deployments=deployments)
    deps.resolver = UnsupportingResolver()
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/api/deployments",
            json={
                "model_id": "meta-llama/Llama-3.3-70B-Instruct",
                "runtime": "sglang",
            },
        )
    assert reply.status_code == 400
    assert reply.json()["error"]["code"] == "runtime_unsupported"
    assert reply.json()["error"]["message"] == "sglang has no adapter for this architecture"
    assert deployments.launched == []


def test_plan_reflects_an_unsupported_runtime_before_any_launch_is_attempted():
    """The dry run has to agree with the launch it is drawing a button for.

    Before this, `/api/plan` never asked `supported_by` at all, so the fit box
    could say "fits, click Serve" about an architecture the runtime does not
    implement -- and the click always failed with `runtime_unsupported`. The
    UI has no other signal: `Verdict.tsx` reads exactly `serve.allowed`.
    """

    class UnsupportingResolver(StubResolver):
        def supported_by(self, shape, runtime):
            return False, f"{runtime} has no adapter for this architecture"

    deps = build_deps()
    deps.resolver = UnsupportingResolver()
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/api/plan",
            json={"model_id": "meta-llama/Llama-3.3-70B-Instruct", "runtime": "sglang"},
        )
    assert reply.status_code == 200
    body = reply.json()
    assert body["serve"]["allowed"] is False
    assert body["serve"]["reason"] == "sglang has no adapter for this architecture"
    # Not overridable: no tick on this screen ever unblocks a runtime that
    # cannot load the architecture at all.
    assert body["serve"]["overrides"] == []
    assert body["serve"]["override_required"] is False
    # Independent of the fit verdict, which the stub resolver's shape still
    # passes -- an unsupported architecture is a refusal fit can't see.
    assert body["fit"]["verdict"] == "fits"


def test_create_deployment_refuses_a_sharded_plan_on_a_runtime_that_cannot_shard():
    """The tts runtime is one process holding one checkpoint.

    A plan carrying TP or PP passes the fit gate -- per-rank arithmetic makes
    a sharded model fit MORE easily -- so nothing before this point objects.
    Past it the machines are committed and the same fact becomes a FAILED
    record with a health timeout in front of it, which is why the refusal is
    here and is a 400.
    """
    deployments = FakeDeployments()
    deps = build_deps(deployments=deployments)
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/api/deployments",
            # No degrees named: the plan the planner itself chose is PP=2,
            # which is the case that matters. Nothing the caller typed is
            # wrong -- the runtime simply cannot run what was planned.
            json={
                "model_id": "meta-llama/Llama-3.3-70B-Instruct",
                "runtime": "tts",
            },
        )
    assert reply.status_code == 400, reply.text
    assert reply.json()["error"]["code"] == "runtime_cannot_shard"
    assert "cannot shard" in reply.json()["error"]["message"]
    assert deployments.launched == []


def test_create_deployment_maps_a_launch_value_error_to_400():
    """M-11: a launch-time input validation error is a 400, not a 502."""

    class RejectingDeployments(FakeDeployments):
        def launch(self, shape, plan, fit, runtime, ctx, max_seqs, *, modality=None, extra_args=(), custom_command=()):
            raise ValueError("model id is not a safe command argument")

    deployments = RejectingDeployments()
    deps = build_deps(deployments=deployments)
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/api/deployments",
            json={"model_id": "meta-llama/Llama-3.3-70B-Instruct"},
        )
    assert reply.status_code == 400
    assert reply.json()["error"]["code"] == "invalid_request"
    assert "not a safe command argument" in reply.json()["error"]["message"]
    assert deployments.launched == []


def test_a_launch_onto_a_node_already_running_the_model_is_a_409():
    """The deployment rule, as the caller sees it.

    502 was the old answer, because DuplicateDeployment reached the catch-all:
    an operator picking a machine that was already busy with this model got
    "Launch failed" and a server-error type, as though the coordinator had
    broken. It is a conflict with what is running, it names its own way out,
    and an unchanged retry works once that deployment is stopped -- which is
    the same shape as the live-memory 409 beside it.
    """
    from control_plane.deploy import DuplicateDeployment

    running = make_deployment(
        "d-running", "llama-3.3-70b", backend_url="http://n1:8000/v1"
    )

    class OccupiedDeployments(FakeDeployments):
        def launch(self, shape, plan, fit, runtime, ctx, max_seqs, *, modality=None, extra_args=(), custom_command=()):
            raise DuplicateDeployment(running, clash="model")

    deps = build_deps(deployments=OccupiedDeployments())
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/api/deployments",
            json={"model_id": "meta-llama/Llama-3.3-70B-Instruct"},
        )
    assert reply.status_code == 409
    body = reply.json()
    assert body["error"]["code"] == "already_deployed"
    assert body["error"]["type"] == "invalid_request_error"
    # The manager's sentence, unedited -- it is the only thing on the page that
    # says which deployment to stop.
    assert body["error"]["message"] == str(DuplicateDeployment(running, clash="model"))
    assert "spark-01" in body["error"]["message"]
    assert body["clash"] == "model"
    # And the deployment in the way, so the screen can link to it.
    assert body["conflict"]["deployment_id"] == "d-running"
    assert body["conflict"]["node_ids"] == ["spark-01"]
    assert body["plan"] and body["fit"]


def test_a_launch_that_really_did_fail_is_still_a_502():
    """The 409 is keyed on the conflict, not on any exception reaching here."""

    class BrokenDeployments(FakeDeployments):
        def launch(self, shape, plan, fit, runtime, ctx, max_seqs, *, modality=None, extra_args=(), custom_command=()):
            raise RuntimeError("sparkrun exited 1")

    deps = build_deps(deployments=BrokenDeployments())
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/api/deployments",
            json={"model_id": "meta-llama/Llama-3.3-70B-Instruct"},
        )
    assert reply.status_code == 502
    assert reply.json()["error"]["code"] == "launch_failed"


def test_plan_and_deployment_use_resolve_full_when_the_resolver_exposes_it():
    """M-9/H-10: resolve_full wins, carries its warnings, and its measured
    weight bytes reach the fit request."""
    from control_plane.resolver.support import build_verdict
    from control_plane.resolver.types import ParamSource, QuantSource, Resolution

    shape = MODEL_SHAPES["llama-3.3-70b"]
    resolution = Resolution(
        shape=shape,
        revision="main",
        param_source=ParamSource.SAFETENSORS_HEADERS,
        quant_source=QuantSource.QUANT_CONFIG,
        support=build_verdict((), shape.dtype),
        warnings=["least reliable source: config estimate"],
        weight_bytes=int(shape.total_params * shape.bytes_per_param()) + 3 * GIB,
    )

    class ResolveFullResolver(StubResolver):
        def __init__(self):
            self.seen_weight_bytes = None

        def resolve_full(self, model_id, dtype=None):
            return resolution

    seen_fit_requests = []

    class RecordingFit(StubFit):
        def check(self, req, nodes):
            seen_fit_requests.append(req)
            return fits()

    deps = build_deps()
    deps.resolver = ResolveFullResolver()
    deps.fit = RecordingFit()
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/api/plan", json={"model_id": "meta-llama/Llama-3.3-70B-Instruct"}
        )
    assert reply.status_code == 200
    assert reply.json()["resolver_warnings"] == ["least reliable source: config estimate"]
    assert seen_fit_requests[0].weight_bytes == resolution.weight_bytes


def test_plan_reports_no_resolver_warnings_when_the_port_lacks_resolve_full():
    deps = build_deps()  # StubResolver has no resolve_full
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/api/plan", json={"model_id": "meta-llama/Llama-3.3-70B-Instruct"}
        )
    assert reply.status_code == 200
    assert reply.json()["resolver_warnings"] == []


# ---------------------------------------------------------------------------
# _plan_and_fit extras reach the ports (M-1), and the ports run off the event
# loop (M-14). A design-review probe found both genuinely implemented but
# unguarded by any test in this file -- close that gap directly.
# ---------------------------------------------------------------------------


def test_plan_forwards_context_length_and_kv_dtype_to_the_planner():
    seen_calls = []

    class RecordingPlanner(StubPlanner):
        def plan(self, shape, nodes, link, target, concurrency, *,
                  context_length=None, kv_dtype=None):
            seen_calls.append((context_length, kv_dtype))
            return super().plan(
                shape, nodes, link, target, concurrency,
                context_length=context_length, kv_dtype=kv_dtype,
            )

    deps = build_deps()
    deps.planner = RecordingPlanner()
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/api/plan",
            json={
                "model_id": "meta-llama/Llama-3.3-70B-Instruct",
                "context": 32768,
                "kv_dtype": "fp8",
            },
        )
    assert reply.status_code == 200
    assert seen_calls == [(32768, "fp8")]


def test_plan_and_fit_port_calls_run_off_the_event_loop():
    # asyncio.to_thread's default executor names its workers "asyncio_N"
    # (thread_name_prefix='asyncio' in BaseEventLoop.run_in_executor) --
    # distinct from both pytest's MainThread and the event-loop thread
    # TestClient/anyio runs the ASGI app on. Recording the thread name from
    # inside each port call is a direct check that M-14 wrapped it in
    # to_thread, not just a check that it avoided one specific thread.
    seen_threads = {}

    class RecordingResolver(StubResolver):
        def resolve(self, model_id, dtype=None):
            seen_threads["resolver"] = threading.current_thread().name
            return super().resolve(model_id, dtype)

    class RecordingPlanner(StubPlanner):
        def plan(self, shape, nodes, link, target, concurrency, **kw):
            seen_threads["planner"] = threading.current_thread().name
            return super().plan(shape, nodes, link, target, concurrency, **kw)

    class RecordingFit(StubFit):
        def check(self, req, nodes):
            seen_threads["fit"] = threading.current_thread().name
            return super().check(req, nodes)

    deps = build_deps()
    deps.resolver = RecordingResolver()
    deps.planner = RecordingPlanner()
    deps.fit = RecordingFit()
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/api/plan", json={"model_id": "meta-llama/Llama-3.3-70B-Instruct"}
        )
    assert reply.status_code == 200
    assert seen_threads.keys() == {"resolver", "planner", "fit"}
    for name in seen_threads.values():
        assert name.startswith("asyncio_"), seen_threads


# ---------------------------------------------------------------------------
# routing payload extras: flow, auto_selected, auto_reason, zero_weight_reason,
# node_ids (H-8)
# ---------------------------------------------------------------------------


def test_routing_payload_reports_auto_selection_and_its_reason():
    deps = build_deps(
        deployments=FakeDeployments(
            [
                make_deployment("d-a", "llama-3.3-70b", backend_url="http://a/v1"),
                make_deployment("d-b", "llama-3.3-70b", backend_url="http://b/v1"),
            ]
        )
    )
    with TestClient(create_app(deps)) as client:
        config = next(
            c for c in client.get("/api/routing").json()
            if c["served_name"] == "llama-3.3-70b"
        )
        assert config["auto_selected"] is True
        assert config["policy"] == "least_outstanding"
        assert config["auto_reason"]
        assert config["flow"] is None  # not LOCAL_FIRST

        put = client.put(
            "/api/routing/llama-3.3-70b", json={"policy": "round_robin"}
        ).json()
        assert put["auto_selected"] is False
        assert put["auto_reason"] is None


def test_routing_payload_reports_flow_for_local_first_after_a_selection():
    router, stats, _ = make_router(
        deployments=FakeDeployments(
            [
                make_deployment(
                    "d-a", "qwen3-30b-a3b", backend_url="http://a/v1",
                    max_concurrent_seqs=2,
                ),
            ]
        ),
        providers=FakeProviders([make_provider()]),
    )
    assert router.config_for("qwen3-30b-a3b").policy is RoutingPolicy.LOCAL_FIRST
    # No selection made yet: flow is null even under LOCAL_FIRST.
    assert router.flow("qwen3-30b-a3b", RoutingPolicy.LOCAL_FIRST) is None

    selection = router.select("qwen3-30b-a3b")
    assert selection.target.kind is TargetKind.LOCAL
    assert router.flow("qwen3-30b-a3b", RoutingPolicy.LOCAL_FIRST) == "local"

    stats.get("d-a").outstanding = 2  # saturate the only local target
    selection = router.select("qwen3-30b-a3b")
    assert selection.target.kind is TargetKind.REMOTE
    assert router.flow("qwen3-30b-a3b", RoutingPolicy.LOCAL_FIRST) == "spilled"
    # Reading /api/routing must never itself change the tracker (pure read).
    for _ in range(3):
        router.config_for("qwen3-30b-a3b")
    assert router.flow("qwen3-30b-a3b", RoutingPolicy.LOCAL_FIRST) == "spilled"


def test_routing_target_carries_node_ids_and_zero_weight_reason():
    deps = build_deps(
        deployments=two_unequal_replicas(strong_tps=100.0, weak_tps=1.0),
    )
    with TestClient(create_app(deps)) as client:
        config = next(
            c for c in client.get("/api/routing").json()
            if c["served_name"] == "llama-3.3-70b"
        )
    targets = {t["target_id"]: t for t in config["targets"]}
    assert targets["d-spark"]["node_ids"] == ["spark-01", "spark-02"]
    assert targets["d-3090"]["node_ids"] == ["ws-3090"]
    # The 3090 replica is far enough below the strong pair to be floored.
    assert targets["d-3090"]["weight"] == 0.0
    assert targets["d-3090"]["zero_weight_reason"] is not None
    assert "15%" in targets["d-3090"]["zero_weight_reason"]
    assert targets["d-spark"]["zero_weight_reason"] is None


def test_remote_targets_never_carry_a_zero_weight_reason_or_node_ids():
    deps = build_deps(
        deployments=FakeDeployments(
            [make_deployment("d-local", "qwen3-30b-a3b", backend_url="http://a/v1")]
        ),
        providers=FakeProviders([make_provider()]),
    )
    with TestClient(create_app(deps)) as client:
        config = next(
            c for c in client.get("/api/routing").json()
            if c["served_name"] == "qwen3-30b-a3b"
        )
    remote = next(t for t in config["targets"] if t["kind"] == "remote")
    assert remote["zero_weight_reason"] is None
    assert remote["node_ids"] == []


# ---------------------------------------------------------------------------
# node eligibility (H-8)
# ---------------------------------------------------------------------------


def test_node_payload_reports_eligible_when_healthy_and_identified():
    with TestClient(create_app()) as client:
        node = client.get("/api/nodes").json()[0]
    assert node["eligible"] is True
    assert node["ineligible_reason"] is None


def test_node_payload_is_ineligible_when_unhealthy():
    from tests.fixtures import node_state, NODE_PROFILES

    unhealthy = node_state(NODE_PROFILES["spark-01"], healthy=False)
    deps = build_deps(registry=FakeRegistry([unhealthy]))
    with TestClient(create_app(deps)) as client:
        node = client.get("/api/nodes").json()[0]
    assert node["eligible"] is False
    assert node["ineligible_reason"] == "node is unhealthy"


def test_node_payload_is_ineligible_when_device_class_is_unrecognized():
    from tests.fixtures import node_state

    unknown_hw = make_node_profile(device_class=DeviceClass.UNKNOWN)
    deps = build_deps(registry=FakeRegistry([node_state(unknown_hw)]))
    with TestClient(create_app(deps)) as client:
        node = client.get("/api/nodes").json()[0]
    assert node["eligible"] is False
    assert node["ineligible_reason"] == (
        "device class is not recognized; cannot confirm this hardware is "
        "eligible to join the pool"
    )


def test_a_node_with_no_gpu_is_eligible_rather_than_unconfirmable():
    """A Raspberry Pi is identified hardware, and the roster used to hedge.

    It reported `device_class: unknown`, which the gateway turned into "cannot
    confirm this hardware is eligible to join the pool" -- said about a machine
    the probe had in fact identified, next to real host telemetry it was
    reporting at the time. A CPU node is eligible: it joins, it is admitted, it
    can front a provider. What it cannot do is carry a rank, and that refusal
    belongs to `addressable_memory == 0` and is made where a placement is
    actually attempted, naming the real reason.
    """
    from tests.fixtures import node_state

    deps = build_deps(registry=FakeRegistry([node_state(_no_gpu_profile())]))
    with TestClient(create_app(deps)) as client:
        node = client.get("/api/nodes").json()[0]
    assert node["eligible"] is True
    assert node["ineligible_reason"] is None
    assert node["device_class"] == "cpu"
    # Unchanged, and the reason the tick is still withheld on the Models board.
    assert node["addressable_memory"] == 0


def _no_gpu_profile():
    """What probe_local returns on a machine it found no GPU on: zeroed, never
    partial. addressable_memory stays 0, so the fit gate still refuses it.

    DeviceClass.CPU and not UNKNOWN: the probe looked for nvidia-smi, for an
    Apple chip, and for NVIDIA hardware the driver would admit to without
    nvidia-smi, and came back with a fact rather than a gap. UNKNOWN is still
    what a machine gets when one of those looks was inconclusive.
    """
    from dataclasses import replace

    return replace(
        make_node_profile(device_class=DeviceClass.CPU),
        gpu_name="",
        gpu_count=0,
        total_memory=0,
        addressable_memory=0,
        memory_bandwidth_gbps=0.0,
        compute_capability="",
        driver_version="",
    )


def test_node_payload_reports_power_as_unknown_when_there_is_no_gpu():
    """A Pi in the roster read 0 W, which looks like a measured idle GPU.

    The node reports real host temperature, memory and CPU utilisation, but
    there is no GPU power draw to read on a board that has no GPU and no
    portable host equivalent -- so power goes out as unknown, not as zero.
    """
    from tests.fixtures import node_state

    no_gpu = _no_gpu_profile()
    state = node_state(no_gpu)
    state.power_watts = 0.0
    state.temperature_c = 47.5
    state.utilization_pct = 12.5
    deps = build_deps(registry=FakeRegistry([state]))
    with TestClient(create_app(deps)) as client:
        node = client.get("/api/nodes").json()[0]

    assert node["power_w"] is None
    # Real readings from the host, and they must survive as themselves.
    assert node["temp_c"] == 47.5
    assert node["util_pct"] == 12.5


def test_every_surface_agrees_that_a_gpu_less_node_has_no_power_reading():
    """/api/nodes said null and the live frame said 0 W, for the same machine.

    The UI prefers the frame while it is fresh, so the roster rendered the
    measured-looking zero the /api/nodes rule exists to suppress. Found live on
    a worker container started without --gpus: 0 W beside a real 50 C.
    """
    from control_plane.gateway.metrics import MetricsHub
    from control_plane.gateway.stats import StatsRegistry
    from tests.fixtures import node_state

    state = node_state(_no_gpu_profile())
    state.power_watts = 0.0
    state.temperature_c = 50.1
    state.utilization_pct = 6.0
    registry = FakeRegistry([state])

    with TestClient(create_app(build_deps(registry=registry))) as client:
        node = client.get("/api/nodes").json()[0]
        topo = client.get("/api/topology").json()["nodes"][0]

    frame = MetricsHub(
        registry=registry,
        deployments=FakeDeployments([]),
        stats=StatsRegistry(),
        settings=GatewaySettings(),
    ).snapshot()["nodes"][0]

    assert node["power_w"] is None
    assert topo["power_w"] is None
    assert frame["power_w"] is None
    # And the host readings survive on all three, unchanged.
    assert [node["temp_c"], topo["temp_c"], frame["temp_c"]] == [50.1] * 3
    assert [node["util_pct"], topo["util_pct"], frame["util_pct"]] == [6.0] * 3


def test_the_cluster_power_total_does_not_count_a_node_with_no_gpu():
    """Zero either way today, since a GPU-less node reports 0.0 W. The total
    is summed from the same reading the rows show so it cannot start
    disagreeing with them if that ever stops being true."""
    from tests.fixtures import node_state

    spark = node_state(NODE_PROFILES["spark-01"])
    spark.power_watts = 91.0
    no_gpu = node_state(_no_gpu_profile())
    no_gpu.power_watts = 4.5  # a stale or fabricated figure, not a measurement
    deps = build_deps(registry=FakeRegistry([spark, no_gpu]))

    with TestClient(create_app(deps)) as client:
        summary = client.get("/api/cluster").json()["summary"]

    assert summary["total_power_w"] == 91.0


def test_the_topology_payload_carries_gpu_count():
    """The cluster plate labels its utilisation line from this, and the layout
    is built from /api/topology -- so a plate can be on screen before
    /api/cluster answers, and would otherwise call host CPU "GPU"."""
    from tests.fixtures import node_state

    deps = build_deps(registry=FakeRegistry([node_state(_no_gpu_profile())]))
    with TestClient(create_app(deps)) as client:
        assert client.get("/api/topology").json()["nodes"][0]["gpu_count"] == 0


def test_node_payload_measures_memory_against_the_live_total_when_there_is_no_gpu():
    """Otherwise the memory readout is a permanent em dash.

    profile.total_memory is 0 on a machine with no GPU and must stay 0 -- it is
    summed into cluster-wide totals, where host RAM no model can reach does not
    belong. The denominator comes from the node's own sample instead.
    """
    from tests.fixtures import node_state

    state = node_state(_no_gpu_profile())
    state.memory_used = 2 * GIB
    state.memory_total = 8 * GIB
    deps = build_deps(registry=FakeRegistry([state]))
    with TestClient(create_app(deps)) as client:
        node = client.get("/api/nodes").json()[0]

    assert node["memory_used_pct"] == 25.0
    assert node["memory_total"] == 8 * GIB
    # The GPU figure stays absent rather than borrowing the host's.
    assert node["total_memory"] == 0
    assert node["addressable_memory"] == 0


def test_node_payload_memory_is_unknown_until_something_is_sampled():
    from tests.fixtures import node_state

    state = node_state(_no_gpu_profile())
    state.memory_used = 0
    state.memory_total = 0
    deps = build_deps(registry=FakeRegistry([state]))
    with TestClient(create_app(deps)) as client:
        node = client.get("/api/nodes").json()[0]
    assert node["memory_used_pct"] is None


def test_node_payload_reports_temperature_as_unknown_only_when_absent():
    """A running board does not sit at exactly 0.0 C; that is a missing sensor."""
    from tests.fixtures import node_state

    no_gpu = _no_gpu_profile()
    state = node_state(no_gpu)
    state.temperature_c = 0.0
    deps = build_deps(registry=FakeRegistry([state]))
    with TestClient(create_app(deps)) as client:
        node = client.get("/api/nodes").json()[0]
    assert node["temp_c"] is None


def test_candidate_payload_reports_eligibility_by_device_class():
    from control_plane.gateway import serialize

    eligible = serialize.candidate_payload(
        {"node_id": "spark-05", "device_class": "gb10"}
    )
    assert eligible["eligible"] is True
    assert eligible["ineligible_reason"] is None

    ineligible = serialize.candidate_payload(
        {"node_id": "mystery-box", "device_class": "unknown"}
    )
    assert ineligible["eligible"] is False
    assert ineligible["ineligible_reason"]


# ---------------------------------------------------------------------------
# link honesty metadata (M-7) and measurement failure (M-8)
# ---------------------------------------------------------------------------


def test_link_payload_carries_honesty_metadata_when_annotated():
    from control_plane.gateway import serialize
    from control_plane.links.record import LinkAnnotation, annotate

    bare = LINKS[("spark-01", "spark-02")]
    annotated = annotate(
        bare,
        LinkAnnotation(
            estimated=True,
            raw_gbps=24.3,
            scale_factor=0.42,
            notes=("scaled from ib_write_bw",),
        ),
    )
    payload = serialize.link_payload(annotated)
    assert payload["estimated"] is True
    assert payload["raw_gbps"] == 24.3
    assert payload["scale_factor"] == 0.42
    assert payload["notes"] == ["scaled from ib_write_bw"]


def test_link_payload_omits_honesty_fields_for_a_bare_measurement():
    from control_plane.gateway import serialize

    payload = serialize.link_payload(LINKS[("spark-01", "spark-02")])
    for key in ("estimated", "raw_gbps", "scale_factor", "notes"):
        assert key not in payload


def test_link_measure_returns_503_when_every_rung_fails():
    class AlwaysFailsLinks(StubLinks):
        def measure(self, a, b):
            return None

    deps = build_deps()
    deps.links = AlwaysFailsLinks()
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/api/links/measure", json={"a": "spark-01", "b": "spark-02"}
        )
    assert reply.status_code == 503
    assert reply.json()["error"]["code"] == "measurement_failed"
    assert "spark-01" in reply.json()["error"]["message"]
    assert "spark-02" in reply.json()["error"]["message"]


# ---- a dtype sizes, it does not launch --------------------------------------
#
# `_plan_and_fit` accepts a `dtype` and hands it to the resolver as a
# quantization override. That is legitimate for /api/plan, which starts
# nothing. It is not legitimate for /api/deployments: neither serve command
# template in deploy/flags.py carries --quantization, and the only model
# identifier either interpolates is {model}, filled from shape.model_id. So a
# honoured dtype would size for 4-bit weights and then start the repository's
# real 16-bit ones -- the out-of-memory kill the fit gate exists to refuse.


def test_plan_still_accepts_a_dtype_because_it_starts_nothing():
    """The sizing question stays askable: "what would this cost at q4_k_m"."""
    deps = build_deps()
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/api/plan",
            json={
                "model_id": "meta-llama/Llama-3.3-70B-Instruct",
                "dtype": "q4_k_m",
            },
        )
    # 200, not the 400 the launch path gives. Whether the *stub* resolver
    # honours the override is its own business -- what is being pinned here is
    # that the plan path does not refuse the question.
    assert reply.status_code == 200
    assert "plan" in reply.json()


def test_launch_refuses_a_dtype_that_would_size_one_model_and_start_another():
    deps = build_deps()
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/api/deployments",
            json={
                "model_id": "meta-llama/Llama-3.3-70B-Instruct",
                "dtype": "q4_k_m",
            },
        )
    assert reply.status_code == 400
    error = reply.json()["error"]
    assert error["code"] == "dtype_not_launchable"
    # The message has to name the missing flag, or the reader is left thinking
    # they passed the wrong dtype rather than that the mechanism is absent.
    assert "--quantization" in error["message"]
    assert "different repository" in error["message"]


def test_launch_refuses_the_dtype_before_it_resolves_anything():
    """The guard is a cheap pre-check, not a late refusal.

    Ordering matters: resolving first would spend a hub round trip, and on a
    slow or unreachable hub the caller would get a 502 about metadata rather
    than the 400 that tells them what they actually did wrong.
    """

    class CountingResolver(StubResolver):
        def __init__(self):
            self.calls = 0

        def resolve(self, model_id, dtype=None):
            self.calls += 1
            return super().resolve(model_id, dtype)

    deps = build_deps()
    deps.resolver = CountingResolver()
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/api/deployments",
            json={"model_id": "meta-llama/Llama-3.3-70B-Instruct", "dtype": "q4_k_m"},
        )
    assert reply.status_code == 400
    assert deps.resolver.calls == 0


def test_launch_without_a_dtype_is_untouched_by_the_guard():
    """An empty or absent dtype is not a dtype; the guard must not catch it."""
    deps = build_deps()
    with TestClient(create_app(deps)) as client:
        for body in (
            {"model_id": "meta-llama/Llama-3.3-70B-Instruct"},
            {"model_id": "meta-llama/Llama-3.3-70B-Instruct", "dtype": None},
            {"model_id": "meta-llama/Llama-3.3-70B-Instruct", "dtype": ""},
        ):
            reply = client.post("/api/deployments", json=body)
            assert reply.status_code != 400 or (
                reply.json()["error"]["code"] != "dtype_not_launchable"
            ), body


def test_launch_with_extra_args_reaches_the_deployment_manager():
    """Unlike dtype, extra_args is launch-only but not sizing-relevant, so it
    is expected to flow straight through to a successful launch."""
    deployments = FakeDeployments()
    deps = build_deps(deployments=deployments)
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/api/deployments",
            json={
                "model_id": "meta-llama/Llama-3.3-70B-Instruct",
                "extra_args": "--quantization modelopt_fp4 --trust-remote-code",
            },
        )
    assert reply.status_code == 201, reply.text
    assert reply.json()["extra_args"] == [
        "--quantization", "modelopt_fp4", "--trust-remote-code",
    ]


def test_launch_without_extra_args_is_untouched_by_the_field():
    deps = build_deps()
    with TestClient(create_app(deps)) as client:
        for body in (
            {"model_id": "meta-llama/Llama-3.3-70B-Instruct"},
            {"model_id": "meta-llama/Llama-3.3-70B-Instruct", "extra_args": None},
            {"model_id": "meta-llama/Llama-3.3-70B-Instruct", "extra_args": ""},
        ):
            reply = client.post("/api/deployments", json=body)
            assert reply.status_code == 201, body
            assert reply.json()["extra_args"] == []


def test_launch_refuses_a_non_string_extra_args():
    deps = build_deps()
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/api/deployments",
            json={
                "model_id": "meta-llama/Llama-3.3-70B-Instruct",
                "extra_args": ["--quantization", "modelopt_fp4"],
            },
        )
    assert reply.status_code == 400
    assert reply.json()["error"]["code"] == "invalid_request"


def test_launch_refuses_unparsable_extra_args():
    """Unbalanced quoting is a 400 naming the parse failure, not a 500."""
    deps = build_deps()
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/api/deployments",
            json={
                "model_id": "meta-llama/Llama-3.3-70B-Instruct",
                "extra_args": "--foo 'unterminated",
            },
        )
    assert reply.status_code == 400
    assert reply.json()["error"]["code"] == "invalid_request"


def test_launch_forwards_extra_args_to_the_deployment_manager_which_may_reject_them():
    """The M-22 allowlist itself lives in deploy/recipes.py and is exercised
    against the real DeploymentManager in test_deploy.py; here only the
    plumbing is pinned -- a ValueError the manager raises over a token it
    rejects reaches the caller as a 400 naming it, the same path
    test_create_deployment_maps_a_launch_value_error_to_400 pins generically."""

    class RejectingOnExtraArgs(FakeDeployments):
        def launch(self, shape, plan, fit, runtime, ctx, max_seqs, *, extra_args=(), **kw):
            assert extra_args == ("--foo;curl",)
            raise ValueError("extra_args[0] is not safe to substitute into the launch command")

    deps = build_deps(deployments=RejectingOnExtraArgs())
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/api/deployments",
            json={
                "model_id": "meta-llama/Llama-3.3-70B-Instruct",
                "extra_args": "--foo;curl",
            },
        )
    assert reply.status_code == 400
    assert reply.json()["error"]["code"] == "invalid_request"
    assert "extra_args" in reply.json()["error"]["message"]


def test_launch_forwards_custom_command_to_the_deployment_manager():
    """Same plumbing as extra_args, mirrored for the field that replaces the
    generated command rather than appending to it."""

    class RecordingDeployments(FakeDeployments):
        def launch(self, shape, plan, fit, runtime, ctx, max_seqs, *, custom_command=(), **kw):
            assert custom_command == ("--gpu-memory-utilization", "0.5")
            return super().launch(shape, plan, fit, runtime, ctx, max_seqs, custom_command=custom_command, **kw)

    deps = build_deps(deployments=RecordingDeployments())
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/api/deployments",
            json={
                "model_id": "meta-llama/Llama-3.3-70B-Instruct",
                "custom_command": "--gpu-memory-utilization 0.5",
            },
        )
    assert reply.status_code == 201, reply.text
    assert reply.json()["custom_command"] == ["--gpu-memory-utilization", "0.5"]


def test_sending_extra_args_and_custom_command_together_is_a_400():
    deps = build_deps(deployments=FakeDeployments())
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/api/deployments",
            json={
                "model_id": "meta-llama/Llama-3.3-70B-Instruct",
                "extra_args": "--quantization modelopt_fp4",
                "custom_command": "--gpu-memory-utilization 0.5",
            },
        )
    assert reply.status_code == 400
    assert reply.json()["error"]["code"] == "invalid_request"
    assert "mutually exclusive" in reply.json()["error"]["message"]


def test_a_gguf_model_is_refused_at_launch_by_the_runtime_support_gate():
    """Pins the second layer, which is currently load-bearing by accident.

    recipes.py's _COMMAND_SAFE allows "hf://owner/repo/file.gguf" through --
    every character is in its allowlist and it starts with a letter. What
    actually stops a GGUF launch is support.py marking every gguf key
    UNSUPPORTED on sglang and UNVERIFIED on vllm, neither of which is
    RuntimeSupport.ok. That is one dict edit away from silently opening a path
    to a runtime that cannot load the file, so it gets a test.
    """
    from dataclasses import replace

    from control_plane.resolver.support import build_verdict
    from control_plane.resolver.types import ParamSource, QuantSource, Resolution

    base = MODEL_SHAPES["llama-3.3-70b"]
    gguf_shape = replace(base, model_id="unsloth/Llama-3.3-70B-GGUF", dtype="q4_k_m")

    class GGUFResolver(StubResolver):
        def resolve(self, model_id, dtype=None):
            return gguf_shape

        def resolve_full(self, model_id, dtype=None):
            return Resolution(
                shape=gguf_shape,
                revision="main",
                param_source=ParamSource.GGUF_TENSORS,
                quant_source=QuantSource.GGUF_FILE_TYPE,
                support=build_verdict(("LlamaForCausalLM",), gguf_shape.dtype),
            )

        def supported_by(self, shape, runtime):
            return build_verdict(
                ("LlamaForCausalLM",), shape.dtype
            ).for_runtime(runtime).as_tuple()

    deps = build_deps()
    deps.resolver = GGUFResolver()
    with TestClient(create_app(deps)) as client:
        reply = client.post(
            "/api/deployments",
            json={"model_id": "unsloth/Llama-3.3-70B-GGUF", "runtime": "sglang"},
        )
    assert reply.status_code == 400
    assert reply.json()["error"]["code"] == "runtime_unsupported"


# ---------------------------------------------------------------------------
# Manual placement over HTTP
#
# The invariant that governs every one of these: a request carrying none of the
# new fields must produce exactly the answer it produced before they existed.
# Everything else is opt-in.
# ---------------------------------------------------------------------------


PLACEMENT_BODY = {
    "model_id": "meta-llama/Llama-3.3-70B-Instruct",
    "context": 8192,
    "concurrency": 4,
    "target": "throughput",
}


def _real_planner_deps(**kw):
    """The fixture cluster, planned by the real planner.

    `build_deps` wires `StubPlanner`, which has no `plan_for` and no
    `alternatives`. That is a case worth testing on its own (below), but the
    operator-degree path needs a planner that can actually author a reason.
    """
    deps = build_deps(**kw)
    deps.planner = Planner()
    return deps


class TestManualPlacement:
    def test_a_body_without_placement_is_todays_answer(self):
        """The compatibility pin. If this fails, the feature is not additive."""
        with TestClient(create_app(build_deps())) as client:
            body = client.post("/api/plan", json=PLACEMENT_BODY).json()

        assert body["placement"]["mode"] == "planner"
        assert body["placement"]["requested_node_ids"] is None
        assert body["placement"]["mixed_hardware"] is False
        assert body["degrees"]["source"] == "planner"
        assert body["serve"]["overrides"] == []

    def test_plan_uses_exactly_the_machines_named(self):
        with TestClient(create_app(build_deps())) as client:
            body = client.post(
                "/api/plan", json={**PLACEMENT_BODY, "node_ids": ["spark-01"]}
            ).json()

        assert body["plan"]["node_ids"] == ["spark-01"]
        assert body["placement"]["mode"] == "operator"
        assert body["placement"]["requested_node_ids"] == ["spark-01"]

    def test_an_unknown_machine_names_the_ones_that_exist(self):
        with TestClient(create_app(build_deps())) as client:
            response = client.post(
                "/api/plan", json={**PLACEMENT_BODY, "node_ids": ["nope"]}
            )

        assert response.status_code == 400
        body = response.json()
        assert body["error"]["code"] == "unknown_node"
        assert body["error"]["param"] == "node_ids"
        assert "spark-01" in body["error"]["message"]

    def test_an_empty_selection_is_not_the_same_request_as_no_selection(self):
        """Silently reading `[]` as "the planner picks" would substitute a
        placement the caller did not ask for."""
        with TestClient(create_app(build_deps())) as client:
            response = client.post(
                "/api/plan", json={**PLACEMENT_BODY, "node_ids": []}
            )
        assert response.status_code == 400

    def test_operator_degrees_win_and_the_recommendation_comes_back_with_them(self):
        """One round trip carries both: what will launch, and what was advised."""
        with TestClient(create_app(_real_planner_deps())) as client:
            body = client.post(
                "/api/plan",
                json={
                    **PLACEMENT_BODY,
                    "node_ids": ["spark-01", "spark-02"],
                    "parallelism": {"tensor_parallel": 2},
                },
            ).json()

        assert body["degrees"]["source"] == "operator"
        assert body["plan"]["tensor_parallel"] == 2
        assert body["plan"]["pipeline_parallel"] == 1
        # The planner still says what it would have done, in its own words.
        assert body["recommended_plan"]["pipeline_parallel"] == 2
        assert body["recommended_plan"]["reason"]

    def test_illegal_degrees_carry_the_planners_sentence_and_the_legal_ones(self):
        with TestClient(create_app(_real_planner_deps())) as client:
            response = client.post(
                "/api/plan",
                json={
                    **PLACEMENT_BODY,
                    "node_ids": ["spark-01", "spark-02"],
                    "parallelism": {"tensor_parallel": 3},
                },
            )

        assert response.status_code == 400
        body = response.json()
        assert body["error"]["code"] == "illegal_parallelism"
        assert body["error"]["param"] == "parallelism.tensor_parallel"
        assert "not divisible by 3" in body["error"]["message"]
        assert body["legal_degrees"]["tensor_parallel"] == [1, 2]

    def test_a_named_machine_that_would_carry_no_rank_is_refused(self):
        """sparkrun will not catch this -- it launches the degrees it is given
        against the hosts it is given, without complaint."""
        with TestClient(create_app(_real_planner_deps())) as client:
            response = client.post(
                "/api/plan",
                json={
                    **PLACEMENT_BODY,
                    "node_ids": ["spark-01", "spark-02"],
                    "parallelism": {},
                },
            )

        assert response.status_code == 400
        body = response.json()
        assert body["error"]["code"] == "placement_underfilled"
        assert body["unused_node_ids"] == ["spark-02"]

    def test_a_planner_without_plan_for_degrades_rather_than_fabricating(self):
        """A gateway-authored `reason` would be rendered verbatim and persisted
        on the deployment forever. 501 is the honest answer."""
        with TestClient(create_app(build_deps())) as client:
            response = client.post(
                "/api/plan",
                json={**PLACEMENT_BODY, "parallelism": {"tensor_parallel": 2}},
            )
            still_fine = client.post(
                "/api/plan", json={**PLACEMENT_BODY, "node_ids": ["spark-01"]}
            )

        assert response.status_code == 501
        assert response.json()["error"]["code"] == "manual_degrees_unsupported"
        # Naming machines needs nothing beyond the frozen port.
        assert still_fine.status_code == 200


class TestMixedHardwareGate:
    def test_a_dry_run_plans_the_mix_and_reports_the_gate(self):
        """/api/plan must not refuse: the operator has to be able to see what
        they are agreeing to before agreeing to it."""
        with TestClient(create_app(build_deps())) as client:
            body = client.post(
                "/api/plan",
                json={**PLACEMENT_BODY, "node_ids": ["spark-01", "ws-3090"]},
            ).json()

        assert body["placement"]["mixed_hardware"] is True
        assert body["plan"]["node_ids"] == ["spark-01", "ws-3090"]
        gates = [g["param"] for g in body["serve"]["overrides"]]
        assert "allow_mixed_hardware" in gates
        reason = next(
            g["reason"]
            for g in body["serve"]["overrides"]
            if g["param"] == "allow_mixed_hardware"
        )
        assert "the slowest node the tail latency" in reason

    def test_a_launch_is_refused_until_the_named_param_is_sent(self):
        with TestClient(create_app(build_deps())) as client:
            response = client.post(
                "/api/deployments",
                json={
                    **PLACEMENT_BODY,
                    "node_ids": ["spark-01", "ws-3090"],
                    "runtime": "vllm",
                },
            )

        assert response.status_code == 400
        body = response.json()
        assert body["error"]["code"] == "mixed_hardware_not_allowed"
        assert body["error"]["param"] == "allow_mixed_hardware"
        assert body["override"]["value_required"] is True

    def test_alike_machines_never_raise_the_gate(self):
        with TestClient(create_app(build_deps())) as client:
            body = client.post(
                "/api/plan",
                json={**PLACEMENT_BODY, "node_ids": ["spark-01", "spark-02"]},
            ).json()

        assert body["placement"]["mixed_hardware"] is False
        assert body["serve"]["overrides"] == []

    def test_the_default_cluster_is_mixed_and_must_not_start_refusing(self):
        """The guard that keeps this feature additive.

        The fixture cluster spans two hardware groups. Without the
        "only when node_ids was named" condition on the mixed check, every
        request that names no machines would begin failing here.
        """
        with TestClient(create_app(build_deps())) as client:
            response = client.post("/api/plan", json=PLACEMENT_BODY)

        assert response.status_code == 200
        assert response.json()["placement"]["mixed_hardware"] is False


def test_a_machine_with_no_memory_cannot_be_named_as_a_serving_node():
    """Found live: a Raspberry Pi in the roster, named by hand, reached the
    scorer and produced `plan_failed: ZeroDivisionError`. The fit gate already
    drops zero-memory machines from the live budget; naming one deserves the
    same answer said out loud."""
    spark = NODE_PROFILES["spark-01"]
    unprobed = dataclasses.replace(
        spark,
        node_id="unprobed",
        hostname="unprobed",
        gpu_name="",
        gpu_count=0,
        addressable_memory=0,
        memory_bandwidth_gbps=0.0,
    )
    registry = FakeRegistry([node_state(spark), node_state(unprobed)])
    deps = build_deps(registry=registry)
    deps.planner = Planner()

    with TestClient(create_app(deps)) as client:
        response = client.post(
            "/api/plan",
            json={**PLACEMENT_BODY, "node_ids": [spark.node_id, "unprobed"]},
        )

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "node_has_no_memory"
    assert body["unusable_node_ids"] == ["unprobed"]


def _catalogue_providers(tmp_path, *, count=4):
    """A real ProviderService whose upstream publishes several models.

    `_real_providers` publishes one and resolves no key, which is right for the
    add-form tests it was written for and useless for an allowlist: there has
    to be more on offer than you switch on, or "only the chosen ones" is not
    something the test can tell apart from "all of them".
    """
    from control_plane.providers import ProviderService, SecretStore

    ref, key = "OPENROUTER_API_KEY", "sk-or-v1-catalogueproviderstestkey0123456789"

    def handler(request):
        if request.method == "GET" and request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={"data": [
                    {"id": f"vendor/model-{i}", "context_length": 32768}
                    for i in range(count)
                ]},
            )
        return httpx.Response(404, json={"error": {"message": "nope"}})

    return ProviderService(
        data_path=tmp_path,
        secrets=SecretStore(tmp_path / "secrets.json", env={ref: key}),
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def test_adding_a_provider_adds_no_servable_names_until_two_are_enabled(tmp_path):
    """The whole feature, end to end over HTTP.

    Adding a provider used to make its entire catalogue servable in one call.
    Now nothing is, until somebody picks -- and then exactly what they picked.
    """
    providers = _catalogue_providers(tmp_path)
    with TestClient(create_app(build_deps(providers=providers))) as client:
        before = {m["id"] for m in client.get("/v1/models").json()["data"]}
        added = client.post(
            "/api/providers", json={"kind": "openrouter", "api_key_ref": "OPENROUTER_API_KEY"}
        )
        assert added.status_code == 201
        provider_id = added.json()["provider_id"]

        # Nothing new is servable, and the listing the UI reads shows no models.
        assert {m["id"] for m in client.get("/v1/models").json()["data"]} == before
        listed = client.get("/api/providers").json()[0]
        assert listed["models"] == []
        assert listed["model_count"] == 0

        # The catalogue is still offered, every row switched off.
        catalogue = client.get(f"/api/providers/{provider_id}/models").json()
        assert len(catalogue) >= 2
        assert not any(m["enabled"] for m in catalogue)
        assert listed["catalogue_count"] == len(catalogue)

        chosen = [m["upstream_id"] for m in catalogue[:2]]
        patched = client.patch(
            f"/api/providers/{provider_id}", json={"enabled_models": chosen}
        )
        assert patched.status_code == 200

        # Exactly two new servable names, with no restart.
        after = {m["id"] for m in client.get("/v1/models").json()["data"]}
        served = {m["served_name"] for m in catalogue if m["upstream_id"] in chosen}
        assert after - before == served
        assert len(after - before) == 2

        # And nothing else from that provider appears in the listing the UI reads.
        listed = client.get("/api/providers").json()[0]
        assert {m["upstream_id"] for m in listed["models"]} == set(chosen)
        assert listed["model_count"] == 2
        enabled = {m["upstream_id"] for m in
                   client.get(f"/api/providers/{provider_id}/models").json() if m["enabled"]}
        assert enabled == set(chosen)


def test_models_chosen_reaches_the_wire_through_the_spend_allowlist(tmp_path):
    """A field not named in `ui_detail._SPEND_KEYS` never arrives.

    That tuple is a copy-by-name allowlist, and `aliases` is the cautionary
    case: settable and persisted for months while every screen showed nothing,
    because nothing listed it. This pins the new boolean against that.

    It says what the two counts cannot. `model_count == catalogue_count` holds
    both for a record that predates the allowlist and for one whose operator
    switched everything on, and only one of those deserves to be told that
    nobody ever chose.
    """
    providers = _catalogue_providers(tmp_path)
    with TestClient(create_app(build_deps(providers=providers))) as client:
        added = client.post(
            "/api/providers", json={"kind": "openrouter", "api_key_ref": "OPENROUTER_API_KEY"}
        )
        provider_id = added.json()["provider_id"]

        listed = client.get("/api/providers").json()[0]
        # Chosen, even though the choice was "nothing at all": `add()` writes
        # an empty frozenset, which is a decision and not the absence of one.
        assert listed["models_chosen"] is True
        assert listed["model_count"] == 0

        catalogue = client.get(f"/api/providers/{provider_id}/models").json()
        client.patch(
            f"/api/providers/{provider_id}",
            json={"enabled_models": [m["upstream_id"] for m in catalogue]},
        )
        listed = client.get("/api/providers").json()[0]
        assert listed["models_chosen"] is True
        # Everything on -- the counts now say exactly what a legacy record's
        # counts say, and the boolean is the only thing that still differs.
        assert listed["model_count"] == listed["catalogue_count"] == len(catalogue)


def test_enabling_a_model_the_provider_does_not_publish_is_a_400(tmp_path):
    providers = _catalogue_providers(tmp_path)
    with TestClient(create_app(build_deps(providers=providers))) as client:
        added = client.post(
            "/api/providers", json={"kind": "openrouter", "api_key_ref": "OPENROUTER_API_KEY"}
        )
        reply = client.patch(
            f"/api/providers/{added.json()['provider_id']}",
            json={"enabled_models": ["nobody/such-model"]},
        )

    assert reply.status_code == 400
    assert "nobody/such-model" in reply.json()["error"]["message"]


# ---------------------------------------------------------------------------
# upstream connection lifetime
#
# The bug this section exists for: on 2026-09-08 the gateway leaked 256 upstream
# sockets -- exactly `upstream_pool_limit` -- into CLOSE-WAIT and then could not
# reach any backend at all. Nothing in the suite could see it, because the
# accounting was correct the whole time: `settle()` is synchronous and ran, so
# `outstanding` read 0 while every connection was held.
# ---------------------------------------------------------------------------


def test_an_upstream_is_closed_even_from_inside_a_cancelled_scope():
    """The invariant the leak turned on, stated on its own.

    Starlette cancels the anyio scope a StreamingResponse body runs in the
    moment the client hangs up, and an anyio cancel scope is *level*-triggered:
    every later `await` inside it raises CancelledError at once. So the bare
    `await response.aclose()` that used to sit in proxy.py's `finally` never
    ran, httpx never got the connection back, and the socket stayed open for
    the life of the process.

    The first half of this test is the bug; the second is the fix. If anybody
    ever unwraps `close_quietly`, the first assertion is what tells them what
    they just re-broke.
    """
    import anyio

    from control_plane.gateway.proxy import close_quietly

    class Upstream:
        def __init__(self):
            self.closed = False

        async def aclose(self):
            await anyio.lowlevel.checkpoint()
            self.closed = True

    async def scenario():
        naive, shielded = Upstream(), Upstream()

        with anyio.CancelScope() as scope:
            scope.cancel()
            try:
                await naive.aclose()
            except anyio.get_cancelled_exc_class():
                pass
            await close_quietly(shielded)

        return naive.closed, shielded.closed

    naive_closed, shielded_closed = anyio.run(scenario)
    assert not naive_closed, (
        "an unshielded aclose inside a cancelled scope is exactly the leak; "
        "if this now passes, anyio's semantics changed and the shield's "
        "reasoning needs rechecking rather than deleting"
    )
    assert shielded_closed, "close_quietly must survive the cancelled scope"


def test_close_quietly_never_raises():
    """It runs on paths already carrying an exception worth more than its own."""
    import anyio

    from control_plane.gateway.proxy import close_quietly

    class Broken:
        async def aclose(self):
            raise RuntimeError("the transport is gone")

    anyio.run(close_quietly, Broken())


def test_the_janitor_closes_an_upstream_whose_body_was_never_read():
    """The second leak path: a generator that never started has no `finally`.

    Starlette sends the response line before it first iterates a
    StreamingResponse, so a client that vanishes in between leaves the body
    generator constructed and never started -- and an unstarted async generator
    runs no cleanup when it is collected. Shielding cannot help there, because
    there is nothing to shield.
    """
    import anyio

    from control_plane.gateway.proxy import UpstreamProxy
    from control_plane.gateway.settings import GatewaySettings

    class Upstream:
        def __init__(self):
            self.closed = False

        async def aclose(self):
            self.closed = True

    cancelled: list[bool] = []

    class Peek:
        def cancel(self):
            cancelled.append(True)

    proxy = UpstreamProxy(GatewaySettings())
    stranded, streaming = Upstream(), Upstream()
    proxy._track(stranded).pending = Peek()
    proxy._track(streaming).started = True

    # Not yet: inside the grace period nothing is touched at all.
    anyio.run(proxy._reap)
    assert not stranded.closed, "a young response may simply not have begun yet"

    for entry in proxy._open.values():
        entry.opened_at -= 60.0
    anyio.run(proxy._reap)

    assert stranded.closed, "nothing else was ever going to close this one"
    assert cancelled == [True], (
        "the first-chunk peek outlives the header hold and is only ever awaited "
        "inside the generator; reaping without cancelling it leaves a task "
        "holding the response and retiring with an exception nobody retrieves"
    )
    assert not streaming.closed, (
        "a running stream is never reaped on age -- that would be a read "
        "timeout by the back door, and upstream_read_timeout_s is None on "
        "purpose so a slow decode can take as long as it takes"
    )


def test_pool_exhaustion_is_not_a_verdict_about_the_backend():
    """A PoolTimeout must not bench a target, and must not claim it is down.

    This is the failure that took the whole gateway out on 2026-09-08. One
    model's leaked connections filled the shared pool; every other model then
    got a PoolTimeout, which `forward` counted as a transport failure. Three of
    those benched a backend answering /health in under a millisecond, and the
    half-open probe died on the same empty pool and re-benched it -- for as long
    as anybody kept asking.

    Both halves are asserted here, because either one alone is still broken: a
    503 that names the real condition, and a circuit that never opens.
    """
    settings = GatewaySettings()
    settings.upstream_per_origin_limit = 1  # one slot, so the second request waits
    settings.upstream_connect_timeout_s = 0.3  # ...and gives up quickly

    backend = FakeBackend()
    backend.hold = threading.Event()

    with RunningBackend(backend) as running:
        deps = build_deps(
            settings=settings,
            deployments=FakeDeployments(
                [make_deployment("d-a", "llama-3.3-70b", backend_url=running.base_url)]
            ),
        )
        app = create_app(deps)
        with TestClient(app) as client:
            held = {
                "model": "llama-3.3-70b",
                "messages": [{"role": "user", "content": HOLD_MARKER}],
            }
            payload = {
                "model": "llama-3.3-70b",
                "messages": [{"role": "user", "content": "x"}],
            }

            def occupy():
                client.post("/v1/chat/completions", json=held)

            worker = threading.Thread(target=occupy)
            worker.start()
            try:
                time.sleep(0.3)  # let it take the only connection

                statuses = []
                for _ in range(4):
                    reply = client.post("/v1/chat/completions", json=payload)
                    statuses.append(reply.status_code)
                    body = reply.json()

                assert statuses == [503] * 4, statuses
                assert body["error"]["code"] == "upstream_pool_exhausted", body
                assert "not implicated" in body["error"]["message"], (
                    "the refusal must not send whoever reads it to a backend "
                    "that is answering perfectly well"
                )

                breaker = app.state.ctx.breaker
                assert breaker.opened_targets() == {}, (
                    "four pool timeouts, well past the failure threshold, and "
                    "the backend was healthy for every one of them"
                )
            finally:
                backend.hold.set()
                worker.join(timeout=15)


def test_one_origin_cannot_exhaust_another_origins_pool():
    """The 30B's leak is why the 0.5B stopped answering 65 ms later.

    One `AsyncClient` for every target made `max_connections` a cluster-wide
    resource, so a single backend could spend all of it. A client per origin is
    what keeps the blast radius to the backend that earned it.
    """
    settings = GatewaySettings()
    settings.upstream_per_origin_limit = 1
    settings.upstream_connect_timeout_s = 0.3

    busy, quiet = FakeBackend(), FakeBackend()
    busy.hold = threading.Event()

    with RunningBackend(busy) as busy_running, RunningBackend(quiet) as quiet_running:
        deps = build_deps(
            settings=settings,
            deployments=FakeDeployments(
                [
                    make_deployment("d-busy", "busy", backend_url=busy_running.base_url),
                    make_deployment("d-quiet", "quiet", backend_url=quiet_running.base_url),
                ]
            ),
        )
        with TestClient(create_app(deps)) as client:
            def occupy():
                client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "busy",
                        "messages": [{"role": "user", "content": HOLD_MARKER}],
                    },
                )

            worker = threading.Thread(target=occupy)
            worker.start()
            try:
                time.sleep(0.3)

                blocked = client.post(
                    "/v1/chat/completions",
                    json={"model": "busy", "messages": [{"role": "user", "content": "x"}]},
                )
                assert blocked.status_code == 503, "the busy origin is out of slots"

                spared = client.post(
                    "/v1/chat/completions",
                    json={"model": "quiet", "messages": [{"role": "user", "content": "x"}]},
                )
                assert spared.status_code == 200, (
                    "a backend that leaked nothing must not go down with the "
                    "one that did"
                )
            finally:
                busy.hold.set()
                worker.join(timeout=15)


# ---------------------------------------------------------------------------
# recovery is decided by bytes, not by a clock
#
# `upstream_header_hold_s` bounds how long the response line is held for the
# first chunk. Past it the gateway used to give up on failover entirely -- the
# comment said the attempt "stops being retryable from here" -- even though the
# client had seen a status line and zero body bytes. On the live cluster that
# timer fired 130,815 times, every one of them the 30B whose prefill is 76-130s
# and none of them the 0.5B which answers in 5s: the fast model was always
# protected and the slow one never was.
# ---------------------------------------------------------------------------


def _hold(seconds: float) -> GatewaySettings:
    settings = GatewaySettings()
    settings.upstream_header_hold_s = seconds
    return settings


def test_a_committed_response_is_finished_by_another_target():
    """Headers already sent, zero body bytes, upstream dies -- and it recovers.

    This is the case the 10s timer used to abandon. Nothing the client has seen
    is contradicted by streaming somebody else's body underneath the same status
    line, so the request is still recoverable and the client never learns.
    """
    stalls = FakeBackend(first_chunk_delay=1.0, die_before_first_chunk=True)
    live = FakeBackend()

    with RunningBackend(stalls) as a, RunningBackend(live) as b:
        deps = build_deps(
            settings=_hold(0.2),  # commit the headers long before it dies
            deployments=two_replicas(a.base_url, b.base_url),
        )
        with TestClient(create_app(deps)) as client:
            reply = client.post(
                "/v1/chat/completions", json={**CHAT, "stream": True}
            )

    assert reply.status_code == 200, reply.text
    assert len(stalls.requests) == 1 and len(live.requests) == 1, (
        "both targets should have been asked: the first died, the second finished"
    )
    # The whole stream, from the replacement, under the first target's headers.
    assert "tok0" in reply.text and "tok4" in reply.text
    assert "[DONE]" in reply.text
    assert "died" not in reply.text and "d-a" not in reply.text, (
        "the client must not learn that a node died under it"
    )


def test_a_stream_the_client_has_begun_reading_is_never_swapped():
    """The counterpart, and the rule that must not bend.

    Once a body byte has reached the client the response is committed in a way
    a status line is not: finishing it from another target would splice two
    different completions into one. Rule 1 -- never rewrite a body already
    being read -- outranks recovery.
    """
    dies_midway = FakeBackend(chunks=3, chunk_delay=0.05)
    live = FakeBackend()

    async def _truncated(self):
        yield b'data: {"choices":[{"delta":{"content":"tok0"}}]}\n\n'
        raise RuntimeError("died after the client saw a byte")

    dies_midway._sse = _truncated.__get__(dies_midway, FakeBackend)

    with RunningBackend(dies_midway) as a, RunningBackend(live) as b:
        deps = build_deps(
            settings=_hold(5.0),
            deployments=two_replicas(a.base_url, b.base_url),
        )
        with TestClient(create_app(deps)) as client:
            try:
                reply = client.post(
                    "/v1/chat/completions", json={**CHAT, "stream": True}
                )
                body = reply.text
            except Exception:
                body = "tok0"  # a broken stream is an acceptable outcome here

    assert "tok0" in body
    assert "[DONE]" not in body, "a truncated stream must stay truncated"
    assert not live.requests, (
        "the second target must never be asked to finish a body the client "
        "has already begun reading"
    )


# ---------------------------------------------------------------------------
# hedging
#
# Off by default. It buys latency with a whole extra inference, so it is worth
# it only between targets of comparable strength, and the loser must be given
# back or every hedged request leaks what the 2026-09-08 outage leaked.
# ---------------------------------------------------------------------------


def test_hedging_is_off_unless_asked_for():
    """Nobody pays for a second inference they did not ask for."""
    assert GatewaySettings().hedge_after_s is None

    slow = FakeBackend(first_chunk_delay=0.6)
    idle = FakeBackend()
    with RunningBackend(slow) as a, RunningBackend(idle) as b:
        deps = build_deps(deployments=two_replicas(a.base_url, b.base_url))
        with TestClient(create_app(deps)) as client:
            assert client.post(
                "/v1/chat/completions", json={**CHAT, "stream": True}
            ).status_code == 200

    assert not idle.requests, "the second target must not be touched by default"


def test_a_stalled_leader_is_raced_and_the_loser_is_given_back():
    """The whole point, and the part that must not leak.

    The loser's body generator is never started, so nothing would settle its
    accounting, release its KV commitment or return its connection. If
    `Attempt.discard` is not called, `outstanding` stays above zero forever.
    """
    settings = GatewaySettings()
    settings.hedge_after_s = 0.15
    settings.upstream_header_hold_s = 5.0

    slow = FakeBackend(first_chunk_delay=2.0)
    quick = FakeBackend()

    with RunningBackend(slow) as a, RunningBackend(quick) as b:
        deps = build_deps(
            settings=settings, deployments=two_replicas(a.base_url, b.base_url)
        )
        app = create_app(deps)
        with TestClient(app) as client:
            reply = client.post(
                "/v1/chat/completions", json={**CHAT, "stream": True}
            )
            assert reply.status_code == 200, reply.text
            assert "tok0" in reply.text and "[DONE]" in reply.text

            stats = app.state.ctx.stats
            deadline = time.time() + 5
            while time.time() < deadline:
                if not stats.outstanding("d-a") and not stats.outstanding("d-b"):
                    break
                time.sleep(0.05)

    assert len(slow.requests) == 1 and len(quick.requests) == 1, (
        "both targets should have been asked -- that is what a hedge is"
    )
    assert stats.outstanding("d-a") == 0 and stats.outstanding("d-b") == 0, (
        "the hedged loser was never given back; its commitment is stranded"
    )


def test_a_hedge_is_refused_where_it_would_be_a_spill_decision():
    """LOCAL_FIRST's remote is the overflow valve, not a speed option.

    Hedging onto it would turn "the cluster is saturated" into "the cluster was
    slow this once", and on a metered provider that is a doubled bill.
    """
    from control_plane.contracts import (
        RouteTarget,
        RoutingConfig,
        RoutingPolicy,
        TargetKind,
    )

    from control_plane.gateway import policies

    def target(tid, kind, strength):
        return RouteTarget(
            target_id=tid, kind=kind, backend_url="http://x/v1", weight=1.0,
            outstanding=0, healthy=True, admitting=True, strength=strength,
            cost_per_mtok=None,
        )

    local = target("d-a", TargetKind.LOCAL, 1.0)
    remote = target("p:m", TargetKind.REMOTE, 1.0)
    settings = GatewaySettings()

    spill = RoutingConfig(
        served_name="m", policy=RoutingPolicy.LOCAL_FIRST,
        targets=[local, remote], sticky_ttl_s=0.0,
    )
    assert policies.hedge_candidate(spill, local, settings) is None

    balanced = RoutingConfig(
        served_name="m", policy=RoutingPolicy.LEAST_OUTSTANDING,
        targets=[local, remote], sticky_ttl_s=0.0,
    )
    assert policies.hedge_candidate(balanced, local, settings) is remote


def test_a_hedge_is_refused_onto_a_target_that_cannot_win():
    """Below the weak-target floor a hedge loses every race for free."""
    from control_plane.contracts import (
        RouteTarget,
        RoutingConfig,
        RoutingPolicy,
        TargetKind,
    )

    from control_plane.gateway import policies

    def target(tid, strength):
        return RouteTarget(
            target_id=tid, kind=TargetKind.LOCAL, backend_url="http://x/v1",
            weight=1.0, outstanding=0, healthy=True, admitting=True,
            strength=strength, cost_per_mtok=None,
        )

    settings = GatewaySettings()
    strong = target("d-a", 1.0)
    feeble = target("d-b", settings.weak_target_floor / 2)

    config = RoutingConfig(
        served_name="m", policy=RoutingPolicy.LEAST_OUTSTANDING,
        targets=[strong, feeble], sticky_ttl_s=0.0,
    )
    assert policies.hedge_candidate(config, strong, settings) is None, (
        "the floor that holds a weak replica as failover-only is the same "
        "line that says it is not worth racing"
    )
    # And the same rule has to be askable about a target somebody else chose.
    # `_claim` runs the policy and the breaker, so the target routing actually
    # hands back need not be the one `hedge_candidate` picked -- gating only
    # the pick would let this one through the floor it exists to enforce.
    assert not policies.may_hedge(config, strong, feeble, settings)
    assert policies.may_hedge(config, strong, target("d-c", 1.0), settings)
