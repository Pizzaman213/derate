"""Package P-NODE: the composition root that closes audit C-1.

Every port a coordinator built here is real: the same Registry, LinkService,
ModelResolver, FitCalculator, Planner, DeploymentManager and ProviderService
node.py wires in production. mDNS is never exercised: DERATE_ROLE is
always set explicitly, and ``resolve_role`` returns immediately for
``coordinator`` without a browse (control_plane/registry/bootstrap.py), so
these tests build a real Registry directly rather than going through the
full ``start_node`` -> mDNS-advertiser -> telemetry-service pipeline, which
buys speed without giving up a single real component the gateway actually
depends on.

No port here is ever a real, bound socket: ``create_app`` is exercised
through FastAPI's ``TestClient`` (in-process ASGI transport) and the signal/
serve-loop plumbing is exercised against fake ``uvicorn.Server`` stand-ins.
"""

from __future__ import annotations

import asyncio
import inspect
import shutil
import socket
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import control_plane.node as node
from control_plane.gateway import GatewayDeps, create_app
from control_plane.gateway import stubs as gw_stubs
from control_plane.registry.config import ROLE_COORDINATOR, ROLE_WORKER, RegistryConfig
from control_plane.registry.identity import ClusterIdentity
from control_plane.registry.probe import probe_local
from control_plane.registry.registry import Registry
from control_plane.registry.serde import profile_to_dict
from control_plane.telemetry.service import Telemetry

RESOLVER_DATA = Path(__file__).parent / "resolver_data"

_STUB_CLASSES = tuple(
    obj
    for _, obj in inspect.getmembers(gw_stubs, inspect.isclass)
    if obj.__module__ == gw_stubs.__name__
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _coordinator_config(tmp_path) -> RegistryConfig:
    return RegistryConfig(
        role=ROLE_COORDINATOR,
        token="correct-token",
        data_dir=tmp_path,
        agent_port=_free_port(),
        coordinator_port=_free_port(),
    )


def _local_runtime(config: RegistryConfig):
    """A real Registry and a real probed profile -- no mDNS, no node agent.

    ``build_gateway_deps`` reads ``runtime.registry``, ``runtime.profile.node_id``
    and (since the N-2 fix) ``runtime.identity.cluster_id``; ``_serve_coordinator``
    also reads ``runtime.telemetry`` (the N-1 fix). A lightweight stand-in with
    exactly those attributes exercises the same code node.py runs, without
    paying for the advertiser machinery ``start_node`` also brings up.

    ``identity.cluster_id`` is deliberately NOT "c-local" (GatewaySettings'
    fixture default) and NOT the empty string, so a test that forgets to wire
    it through would fail loudly instead of accidentally matching.
    """
    profile = probe_local(node_id="node-under-test")
    identity = ClusterIdentity(cluster_id="c-real-9f3a", token=config.token or "")
    registry = Registry(
        config=config, local_profile=profile, role=ROLE_COORDINATOR, identity=identity
    )
    # Telemetry.disabled(...) is a real Telemetry object -- real .sink,
    # .gateway_events, safe no-op .start()/.stop() -- just with no journal
    # file, which is exactly right for a fast test and irrelevant to what
    # these tests check (that it is THE SAME object create_app receives).
    telemetry = Telemetry.disabled("test: no telemetry needed for this fixture")
    runtime = SimpleNamespace(
        registry=registry,
        profile=profile,
        role=ROLE_COORDINATOR,
        identity=identity,
        telemetry=telemetry,
        agent_app=lambda: object(),
    )
    return runtime, registry


def _seed_local_model(tmp_path: Path, config_name: str) -> Path:
    """A local model directory the real ModelResolver resolves with no network.

    ``ModelResolver.resolve_full`` treats any directory containing a
    ``config.json`` as a fully local resolution (control_plane/resolver/
    resolver.py): no cache, no offline gate, no HTTP. Real resolver, real
    analytic parameter breakdown, real bytes-per-param table -- just no
    HuggingFace hub in the loop.
    """
    model_dir = tmp_path / config_name
    model_dir.mkdir()
    shutil.copyfile(RESOLVER_DATA / f"{config_name}.config.json", model_dir / "config.json")
    return model_dir


# ---------------------------------------------------------------------------
# (a) composition smoke
# ---------------------------------------------------------------------------


class TestCompositionSmoke:
    def test_strict_build_has_no_stubs(self, tmp_path):
        config = _coordinator_config(tmp_path)
        runtime, registry = _local_runtime(config)

        deps = node.build_gateway_deps(runtime, config)

        assert isinstance(deps, GatewayDeps)
        assert deps.strict is True
        assert deps.registry is registry
        for f in ("registry", "links", "resolver", "fit", "planner", "deployments", "providers"):
            value = getattr(deps, f)
            assert value is not None, f"{f} is None"
            assert not isinstance(value, _STUB_CLASSES), (
                f"{f} is a stub ({type(value)!r}) -- exactly the C-1 failure mode"
            )

    def test_missing_registry_raises_instead_of_stubbing(self, tmp_path):
        config = _coordinator_config(tmp_path)
        runtime = SimpleNamespace(registry=None, profile=probe_local(node_id="x"))
        with pytest.raises(RuntimeError):
            node.build_gateway_deps(runtime, config)

    def test_settings_carry_the_real_cluster_id_not_the_fixture_default(self, tmp_path):
        """N-2: GatewaySettings.cluster_id defaults to "c-local". A real
        composed coordinator must never serve that default -- it has a real,
        minted (or joined) cluster id on runtime.identity, and /api/cluster
        and /api/topology read settings.cluster_id straight through.
        """
        config = _coordinator_config(tmp_path)
        runtime, registry = _local_runtime(config)

        deps = node.build_gateway_deps(runtime, config)

        assert deps.settings.cluster_id == runtime.identity.cluster_id
        assert deps.settings.cluster_id == "c-real-9f3a"  # the fixture's real id
        assert deps.settings.cluster_id != "c-local"


# ---------------------------------------------------------------------------
# (b) the real gateway, booted
# ---------------------------------------------------------------------------


class TestCoordinatorApp:
    def test_healthz_cluster_and_join_rejected(self, tmp_path):
        config = _coordinator_config(tmp_path)
        runtime, registry = _local_runtime(config)
        deps = node.build_gateway_deps(runtime, config)
        app = create_app(deps, settings=deps.settings, telemetry=runtime.telemetry)

        with TestClient(app) as client:
            r = client.get("/healthz")
            assert r.status_code == 200
            assert r.json()["ok"] is True

            r = client.get("/api/cluster")
            assert r.status_code == 200
            body = r.json()
            node_ids = [n["node_id"] for n in body["nodes"]]
            assert runtime.profile.node_id in node_ids
            # N-2: the real minted cluster id, never GatewaySettings' "c-local"
            # fixture default -- the surface the UI actually reads.
            assert body["cluster_id"] == runtime.identity.cluster_id
            assert body["cluster_id"] != "c-local"

            r = client.get("/api/topology")
            assert r.status_code == 200
            assert r.json()["cluster_id"] == runtime.identity.cluster_id

            joiner = probe_local(node_id="second-node")
            r = client.post(
                "/api/nodes/join",
                json={
                    "token": "definitely-wrong",
                    "profile": profile_to_dict(joiner),
                    "agent_url": "http://127.0.0.1:1",
                },
            )
            assert r.status_code == 403
            assert r.json()["error"]["code"] == "join_rejected"


# ---------------------------------------------------------------------------
# (c) THE C-1 KILL SHOT
# ---------------------------------------------------------------------------


class TestKillShot:
    def test_70b_at_1m_context_is_refused_by_the_real_fit_calculator(self, tmp_path):
        config = _coordinator_config(tmp_path)
        runtime, registry = _local_runtime(config)
        deps = node.build_gateway_deps(runtime, config)
        app = create_app(deps, settings=deps.settings, telemetry=runtime.telemetry)

        model_dir = _seed_local_model(tmp_path, "llama-3.3-70b")

        # A local-directory resolution is deliberately never cached (a path
        # on disk can change under you) -- but that also means the resolver's
        # own supported_by() has no architecture on record for it yet and
        # reports "unverified", which the gateway treats as not ok before
        # fit is ever consulted. Prime the cache with one real resolve, the
        # same real ModelResolver the gateway itself will call, so the
        # request under test exercises the fit refusal rather than an
        # unrelated runtime_unsupported short-circuit.
        primed = deps.resolver.resolve_full(str(model_dir))
        deps.resolver.cache.put(str(model_dir), "main", None, primed)

        with TestClient(app) as client:
            r = client.post(
                "/api/deployments",
                json={
                    "model_id": str(model_dir),
                    "context": 1048576,
                    "concurrency": 1,
                    "runtime": "vllm",
                },
            )

        # The audit's repro: this used to come back 201 against fixture data.
        # Against the real resolver/planner/fit stack a 70B model at 1M
        # context on one node cannot possibly fit, and must be refused.
        assert r.status_code == 400
        body = r.json()
        assert body["error"]["code"] == "wont_fit"
        assert body["fit"]["limiting_term"] == "weights"
        # fit.reason verbatim (internal_api.py copies FitResult.reason
        # straight into error.message; assert the two never drift apart, and
        # that it is a real, specific sentence naming what to change --
        # never a generic "does not fit" and never a stub's placeholder text.
        message = body["error"]["message"]
        assert message == body["fit"]["reason"]
        assert "weight" in message.lower()
        assert len(message) > 40


# ---------------------------------------------------------------------------
# (d) role resolution: worker never touches the gateway
# ---------------------------------------------------------------------------


class TestRoleResolution:
    def test_worker_never_builds_gateway_deps(self, tmp_path, monkeypatch):
        config = RegistryConfig(
            role=ROLE_WORKER,
            data_dir=tmp_path,
            agent_port=_free_port(),
            coordinator_port=_free_port(),
        )

        fake_agent_app = object()
        worker_runtime = SimpleNamespace(
            role=ROLE_WORKER,
            agent_app=lambda: fake_agent_app,
            stop=AsyncMock(),
        )

        async def fake_start_node(cfg):
            assert cfg is config
            return worker_runtime

        build_calls: list[object] = []

        def fake_build_gateway_deps(runtime, cfg):
            build_calls.append(runtime)
            raise AssertionError("build_gateway_deps must never run for a worker")

        made_servers: list[object] = []

        def fake_make_server(app, *, host, port):
            assert app is fake_agent_app
            fake = SimpleNamespace(should_exit=True, serve=AsyncMock(return_value=None))
            made_servers.append(fake)
            return fake

        monkeypatch.setattr(node, "start_node", fake_start_node)
        monkeypatch.setattr(node, "build_gateway_deps", fake_build_gateway_deps)
        monkeypatch.setattr(node, "_make_server", fake_make_server)

        gateway_mods_before = {m for m in sys.modules if m.startswith("control_plane.gateway")}

        asyncio.run(node.run(config))

        assert build_calls == []
        assert len(made_servers) == 1
        worker_runtime.stop.assert_awaited_once()
        # No new gateway module was pulled in by serving the worker path.
        gateway_mods_after = {m for m in sys.modules if m.startswith("control_plane.gateway")}
        assert gateway_mods_after == gateway_mods_before

    def test_coordinator_path_does_build_gateway_deps(self, tmp_path, monkeypatch):
        config = _coordinator_config(tmp_path)
        base_runtime, registry = _local_runtime(config)
        coordinator_runtime = SimpleNamespace(
            role=ROLE_COORDINATOR,
            registry=registry,
            profile=registry.local_profile,
            identity=base_runtime.identity,
            telemetry=base_runtime.telemetry,
            agent_app=lambda: object(),
            stop=AsyncMock(),
        )

        async def fake_start_node(cfg):
            return coordinator_runtime

        build_calls: list[object] = []
        real_build = node.build_gateway_deps

        def spy_build_gateway_deps(runtime, cfg):
            build_calls.append(runtime)
            return real_build(runtime, cfg)

        def fake_make_server(app, *, host, port):
            return SimpleNamespace(should_exit=True, serve=AsyncMock(return_value=None))

        monkeypatch.setattr(node, "start_node", fake_start_node)
        monkeypatch.setattr(node, "build_gateway_deps", spy_build_gateway_deps)
        monkeypatch.setattr(node, "_make_server", fake_make_server)

        asyncio.run(node.run(config))

        assert build_calls == [coordinator_runtime]
        coordinator_runtime.stop.assert_awaited_once()


# ---------------------------------------------------------------------------
# N-1: the coordinator gateway shares runtime's ONE telemetry bundle
# ---------------------------------------------------------------------------


class TestSharedTelemetry:
    def test_serve_coordinator_passes_runtime_telemetry_into_create_app(
        self, tmp_path, monkeypatch
    ):
        """Without this wire, create_app(deps, settings=...) with no
        ``telemetry=`` kwarg builds its OWN Telemetry.from_env() -- a second
        Journal writer thread and a second Collector racing runtime.telemetry
        over the same journal.db/archive.db files. _serve_coordinator must
        hand its one bundle to create_app rather than let it build another.
        """
        config = _coordinator_config(tmp_path)
        runtime, registry = _local_runtime(config)

        captured: dict = {}

        def fake_create_app(deps, *, settings=None, http_client=None, telemetry=None):
            captured["telemetry"] = telemetry
            captured["deps"] = deps
            captured["settings"] = settings
            return object()

        def fake_make_server(app, *, host, port):
            return SimpleNamespace(should_exit=True, serve=AsyncMock(return_value=None))

        monkeypatch.setattr("control_plane.gateway.app.create_app", fake_create_app)
        monkeypatch.setattr(node, "_make_server", fake_make_server)

        asyncio.run(node._serve_coordinator(runtime, config))

        assert captured["telemetry"] is runtime.telemetry
        assert captured["telemetry"] is not None

    def test_no_second_telemetry_bundle_is_built_for_a_real_boot(self, tmp_path):
        """create_app-level half of the property (NOT end-to-end: this test
        calls create_app itself, so it pins create_app's handling of the
        telemetry kwarg -- node.py dropping the kwarg is caught by
        test_serve_coordinator_passes_runtime_telemetry_into_create_app,
        the mutation-verified regression guard): the FastAPI app's
        ctx.telemetry must be the exact object passed in, not a fresh
        Telemetry.from_env() built inside create_app.
        """
        config = _coordinator_config(tmp_path)
        runtime, registry = _local_runtime(config)
        deps = node.build_gateway_deps(runtime, config)

        app = create_app(deps, settings=deps.settings, telemetry=runtime.telemetry)

        assert app.state.ctx.telemetry is runtime.telemetry


# ---------------------------------------------------------------------------
# (e) signal shutdown
# ---------------------------------------------------------------------------


class TestSignalShutdown:
    def test_install_shutdown_flips_should_exit_on_every_server(self):
        servers = [SimpleNamespace(should_exit=False), SimpleNamespace(should_exit=False)]
        shutdown = node._install_shutdown(servers)
        assert all(s.should_exit is False for s in servers)
        shutdown()
        assert all(s.should_exit is True for s in servers)

    def test_run_with_signals_exits_once_should_exit_flips(self):
        """No real socket, no real OS signal: a fake server whose ``serve()``
        polls ``should_exit`` the way uvicorn.Server really does, driven by
        flipping the flag directly -- the same effect the installed SIGTERM/
        SIGINT handler has in production.
        """

        class FakeServer:
            def __init__(self) -> None:
                self.should_exit = False

            async def serve(self) -> None:
                while not self.should_exit:
                    await asyncio.sleep(0.01)

        async def scenario() -> None:
            servers = [FakeServer(), FakeServer()]
            task = asyncio.create_task(node._run_with_signals(servers))
            await asyncio.sleep(0.03)
            assert not task.done()
            for s in servers:
                s.should_exit = True
            await asyncio.wait_for(task, timeout=2.0)
            assert task.exception() is None

        asyncio.run(scenario())

    def test_run_calls_runtime_stop_after_serve_completes(self, tmp_path, monkeypatch):
        config = RegistryConfig(
            role=ROLE_WORKER,
            data_dir=tmp_path,
            agent_port=_free_port(),
            coordinator_port=_free_port(),
        )
        runtime = SimpleNamespace(
            role=ROLE_WORKER, agent_app=lambda: object(), stop=AsyncMock()
        )

        async def fake_start_node(cfg):
            return runtime

        def fake_make_server(app, *, host, port):
            return SimpleNamespace(should_exit=True, serve=AsyncMock(return_value=None))

        monkeypatch.setattr(node, "start_node", fake_start_node)
        monkeypatch.setattr(node, "_make_server", fake_make_server)

        asyncio.run(node.run(config))

        runtime.stop.assert_awaited_once()

    def test_run_calls_runtime_stop_even_when_serve_raises(self, tmp_path, monkeypatch):
        config = RegistryConfig(
            role=ROLE_WORKER,
            data_dir=tmp_path,
            agent_port=_free_port(),
            coordinator_port=_free_port(),
        )
        runtime = SimpleNamespace(
            role=ROLE_WORKER, agent_app=lambda: object(), stop=AsyncMock()
        )

        async def fake_start_node(cfg):
            return runtime

        def fake_make_server(app, *, host, port):
            return SimpleNamespace(
                should_exit=True, serve=AsyncMock(side_effect=RuntimeError("boom"))
            )

        monkeypatch.setattr(node, "start_node", fake_start_node)
        monkeypatch.setattr(node, "_make_server", fake_make_server)

        with pytest.raises(RuntimeError, match="boom"):
            asyncio.run(node.run(config))

        runtime.stop.assert_awaited_once()
