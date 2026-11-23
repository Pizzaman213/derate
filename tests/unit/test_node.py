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
import logging
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

RESOLVER_DATA = Path(__file__).resolve().parents[1] / "resolver_data"

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
                    "proof": {"nonce": "00", "ts": 0, "mac": "00"},
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
    def test_70b_at_1m_context_is_refused_by_the_real_fit_calculator(self, tmp_path, monkeypatch):
        # Local model directories resolve only under a configured root.
        monkeypatch.setenv("DERATE_MODEL_DIRS", str(tmp_path))
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

    def test_a_second_signal_forces_the_exit(self):
        """The grace period waits for open streams; asking twice must not."""
        servers = [SimpleNamespace(should_exit=False, force_exit=False)]
        shutdown = node._install_shutdown(servers)
        shutdown()
        assert (servers[0].should_exit, servers[0].force_exit) == (True, False)
        shutdown()
        assert servers[0].force_exit is True

    def test_one_real_sigterm_runs_every_lifespan_shutdown(self):
        """uvicorn's own signal handlers fired beside derate's, so ONE SIGTERM
        looked like two: force_exit on the first, and the gateway's lifespan
        shutdown -- audit flush, store close -- skipped. The fakes above cannot
        see that; a real signal, in a child so it can never reach pytest, can."""
        import os
        import subprocess

        script = (
            "import asyncio, os, signal\n"
            "from control_plane import node\n"
            "done = []\n"
            "def app_named(name):\n"
            "    async def app(scope, receive, send):\n"
            "        while True:\n"
            "            m = await receive()\n"
            "            if m['type'] == 'lifespan.startup':\n"
            "                await send({'type': 'lifespan.startup.complete'})\n"
            "            elif m['type'] == 'lifespan.shutdown':\n"
            "                done.append(name)\n"
            "                await send({'type': 'lifespan.shutdown.complete'})\n"
            "                return\n"
            "    return app\n"
            "async def main():\n"
            "    servers = [node._make_server(app_named(n), host='127.0.0.1', port=0) for n in ('agent', 'gateway')]\n"
            "    async def kick():\n"
            "        while not all(s.started for s in servers):\n"
            "            await asyncio.sleep(0.05)\n"
            "        os.kill(os.getpid(), signal.SIGTERM)\n"
            "    task = asyncio.get_running_loop().create_task(kick())\n"
            "    await node._run_with_signals(servers)\n"
            "    print([s.force_exit for s in servers], sorted(done))\n"
            "asyncio.run(main())\n"
        )
        root = Path(__file__).resolve().parents[2]
        out = subprocess.run([sys.executable, "-c", script], cwd=root, capture_output=True,
                             text=True, timeout=60, env={**os.environ, "PYTHONPATH": str(root)})
        assert out.returncode == 0, out.stderr[-2000:]
        assert out.stdout.strip().splitlines()[-1] == "[False, False] ['agent', 'gateway']"

    def test_a_second_sigterm_exits_while_a_response_is_still_streaming(self):
        """force_exit stops uvicorn waiting on connections and tasks, but it
        still awaits wait_closed(), which holds until every accepted socket is
        gone -- so a client being streamed to kept the process up for the whole
        grace. Asking twice has to close those sockets."""
        import os
        import subprocess

        script = (
            "import asyncio, os, signal, time\n"
            "from control_plane import node\n"
            "async def app(scope, receive, send):\n"
            "    if scope['type'] == 'lifespan':\n"
            "        while True:\n"
            "            m = await receive()\n"
            "            if m['type'] == 'lifespan.startup':\n"
            "                await send({'type': 'lifespan.startup.complete'})\n"
            "            elif m['type'] == 'lifespan.shutdown':\n"
            "                await send({'type': 'lifespan.shutdown.complete'})\n"
            "                return\n"
            "    await send({'type': 'http.response.start', 'status': 200, 'headers': []})\n"
            "    while True:\n"
            "        await send({'type': 'http.response.body', 'body': b'x', 'more_body': True})\n"
            "        await asyncio.sleep(1)\n"
            "async def main():\n"
            "    sock = node._bind('127.0.0.1', 0)\n"
            "    port = sock.getsockname()[1]\n"
            "    server = node._Server(node._config(app, host='127.0.0.1', port=0), sockets=[sock])\n"
            "    first = []\n"
            "    async def client():\n"
            "        while not server.started:\n"
            "            await asyncio.sleep(0.05)\n"
            "        reader, writer = await asyncio.open_connection('127.0.0.1', port)\n"
            "        writer.write(b'GET / HTTP/1.1\\r\\nHost: x\\r\\n\\r\\n')\n"
            "        await reader.readuntil(b'\\r\\n\\r\\n')\n"
            "        await reader.read(1)\n"
            "        first.append(time.monotonic())\n"
            "        os.kill(os.getpid(), signal.SIGTERM)\n"
            "        await asyncio.sleep(0.5)\n"
            "        os.kill(os.getpid(), signal.SIGTERM)\n"
            "        while await reader.read(1024):\n"
            "            pass\n"
            "    task = asyncio.get_running_loop().create_task(client())\n"
            "    await node._run_with_signals([server])\n"
            "    print(round(time.monotonic() - first[0], 2))\n"
            "asyncio.run(main())\n"
        )
        root = Path(__file__).resolve().parents[2]
        out = subprocess.run([sys.executable, "-c", script], cwd=root, capture_output=True,
                             text=True, timeout=60, env={**os.environ, "PYTHONPATH": str(root)})
        assert out.returncode == 0, out.stderr[-2000:]
        assert "Exception in callback" not in out.stderr, out.stderr[-2000:]
        assert float(out.stdout.strip().splitlines()[-1]) < 5.0

    def test_refused_websockets_write_nothing_to_uvicorn_error(self, tmp_path, monkeypatch):
        """uvicorn logs every websocket refused before accept, raw target and
        all, at INFO on uvicorn.error -- and the websockets library logs its
        rejection and close through the same logger -- with no budget, so an
        internet caller could fill node.log. gateway.access already has the row.
        TestClient never runs uvicorn's protocol, so this is a real server."""
        from control_plane.gateway.settings import GatewaySettings

        monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path))
        # A handler on the logger itself, not caplog's on the root: another
        # test's logging.config (disable_existing_loggers) can disable it or
        # stop it propagating, and then caplog sees nothing at all. Filters on
        # the logger run before its handlers, so this sees what node.log would.
        logger = logging.getLogger("uvicorn.error")
        records: list[logging.LogRecord] = []

        class Keep(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        keep = Keep(level=logging.INFO)
        monkeypatch.setattr(logger, "disabled", False)
        # setLevel, not the attribute: it clears isEnabledFor's cache, which
        # otherwise keeps answering for the level an earlier server left.
        level = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(keep)
        edge = node._bind("127.0.0.1", 0)
        main = node._bind("127.0.0.1", 0)
        edge_port = edge.getsockname()[1]
        app = create_app(GatewayDeps(), settings=GatewaySettings(edge_port=edge_port))
        server = node._Server(node._config(app, host="127.0.0.1", port=0), sockets=[main, edge])

        async def scenario() -> bytes:
            task = asyncio.create_task(server.serve())
            try:
                while not server.started:
                    assert not task.done(), task.exception()
                    await asyncio.sleep(0.05)
                reader, writer = await asyncio.open_connection("127.0.0.1", edge_port)
                writer.write(
                    b"GET /api/x HTTP/1.1\r\n"
                    b"Host: 127.0.0.1\r\n"
                    b"Upgrade: websocket\r\n"
                    b"Connection: Upgrade\r\n"
                    b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                    b"Sec-WebSocket-Version: 13\r\n"
                    b"CF-Connecting-IP: 203.0.113.9\r\n\r\n"
                )
                status = await asyncio.wait_for(reader.readuntil(b"\r\n"), 10)
                # Until the server closes: the close is logged too.
                await asyncio.wait_for(reader.read(), 10)
                writer.close()
                return status
            finally:
                server.should_exit = True
                await asyncio.wait_for(task, 30)

        try:
            status = asyncio.run(scenario())
        finally:
            logger.removeHandler(keep)
            logger.setLevel(level)

        assert status.startswith(b"HTTP/1.1 403")
        said = [r.msg for r in records]
        assert not set(said) & node._WEBSOCKET_CHATTER, said
        assert "Application startup complete." in said

    def test_an_open_connection_cannot_hold_a_restart_forever(self):
        """uvicorn's defaults wait on every connection with no deadline and
        let one websocket buffer 32 x 16 MiB; an internet caller controls both."""
        config = node._config(lambda *a: None, host="127.0.0.1", port=18000)
        assert config.timeout_graceful_shutdown == node.SHUTDOWN_GRACE_S
        assert config.ws_max_queue == node.WS_MAX_QUEUE
        assert config.ws_max_queue * config.ws_max_size <= 64 * 1024 * 1024
        logger = logging.getLogger("uvicorn.error")
        node._config(lambda *a: None, host="127.0.0.1", port=18001)
        assert logger.filters.count(node._WEBSOCKET_FILTER) == 1

    def test_the_edge_listener_is_not_handed_to_child_processes(self):
        """The shell relay forks and execs; an inheritable listener would go
        with every shell, and outlive the coordinator in anything left running."""
        sock = node._bind("127.0.0.1", 0)
        try:
            assert sock.get_inheritable() is False
        finally:
            sock.close()

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
