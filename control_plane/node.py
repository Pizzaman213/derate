"""The composition root. ``python3 -m control_plane.node``

This is the process docker/entrypoint.sh execs into on every container,
worker and coordinator alike (00-architecture.md section 2 and section 3).
There is exactly one image; role is decided at runtime by
``registry.startup.start_node``, not by this file.

Every node:

1. Brings itself up via ``start_node`` -- preflight, hardware probe, mDNS
   role resolution, identity, the node agent. Returns a ``NodeRuntime``.
2. Serves ``runtime.agent_app()`` on ``config.agent_port``. That is the
   entire worker.

A coordinator additionally:

3. Builds the real ports -- registry, links, resolver, fit, planner,
   deployments, providers -- with ``strict=True`` so a missing wire is a
   startup crash, never a silent stub (the day-0 failure mode this package
   exists to close, audit C-1).
4. Serves the real gateway (``control_plane.gateway.app.create_app``) on
   ``config.coordinator_port``, passing it ``runtime.identity.cluster_id``
   (never the settings default) and ``runtime.telemetry`` -- the one
   Telemetry bundle ``start_node`` already opened for this process, not a
   second one racing it over the same journal/archive files.

Both servers run as ``uvicorn.Server`` instances awaited concurrently in one
event loop; SIGTERM/SIGINT flip ``should_exit`` on both, and
``runtime.stop()`` always runs on the way out.

The worker path must stay light: nothing from ``control_plane.gateway``,
``control_plane.deploy``, ``control_plane.providers``, ``control_plane.fit``,
``control_plane.planner``, ``control_plane.links`` or
``control_plane.resolver`` is imported unless this process is the
coordinator. Every one of those imports lives inside :func:`build_gateway_deps`
or :func:`_serve_coordinator`, never at module scope.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from typing import Callable

import uvicorn

from control_plane.registry import NodeRuntime, RegistryConfig, start_node
from control_plane.registry.config import ROLE_COORDINATOR

log = logging.getLogger("control_plane.node")


def build_gateway_deps(runtime: NodeRuntime, config: RegistryConfig):
    """The real ``GatewayDeps`` for the coordinator role.

    Only ever called when ``runtime.role`` is coordinator. Imports every
    gateway/deploy/providers/planner/fit/links/resolver module lazily, right
    here, so a worker process that never calls this function never pulls any
    of them in.

    ``strict=True`` on the returned deps: ``GatewayDeps.__post_init__`` raises
    if any port is missing rather than silently backfilling
    ``control_plane.gateway.stubs`` fixtures. That silent backfill on a real
    deployment is audit C-1 -- a stub surface wearing a real gateway's
    clothes -- so it must never be reachable from this composition root.
    """
    from control_plane.deploy.manager import DeploymentManager
    from control_plane.fit.calculator import FitCalculator
    from control_plane.gateway.deps import GatewayDeps
    from control_plane.gateway.settings import GatewaySettings
    from control_plane.links.service import LinkService
    from control_plane.planner.planner import Planner
    from control_plane.providers.service import ProviderService
    from control_plane.resolver.resolver import ModelResolver

    registry = runtime.registry
    if registry is None:
        raise RuntimeError(
            "build_gateway_deps called with no registry; runtime.role must "
            "be coordinator"
        )

    links = LinkService(registry=registry, local_node_id=runtime.profile.node_id)
    resolver = ModelResolver()
    fit = FitCalculator()
    # Never Planner(fit=FitCalculator()): the planner's fit argument is its
    # own FitHelpers protocol (min_nodes_required/kv_bytes_per_token), which
    # FitCalculator does not implement. The no-arg default resolves to
    # control_plane.fit's module-level helpers when they exist.
    planner = Planner()
    deployments = DeploymentManager(registry=registry, state_dir=config.data_dir)
    providers = ProviderService(data_path=config.data_dir)

    # runtime.identity.cluster_id is the real minted (or joined) cluster id.
    # GatewaySettings.cluster_id defaults to the day-0 fixture "c-local";
    # leaving that default in place here would mean /api/cluster and
    # /api/topology report a fixture id from a real, composed coordinator --
    # the exact species of bug this composition root exists to prevent.
    cluster_id = getattr(runtime.identity, "cluster_id", None) or GatewaySettings.cluster_id
    settings = GatewaySettings(
        host=os.environ.get("DERATE_HOST", "0.0.0.0"),
        port=config.coordinator_port,
        cluster_id=cluster_id,
        # The composition root KNOWS which node is the coordinator -- itself.
        # Left None, the lifespan guesses registry.list_nodes()[0], and a
        # persisted roster that leads with a worker makes /api/cluster and
        # /api/topology name that worker as coordinator (verifier N-8).
        coordinator_node_id=runtime.profile.node_id,
        electricity_rate_usd_per_kwh=float(
            os.environ.get("DERATE_ELECTRICITY_RATE", "0")
        ),
        # ui_dir is left to GatewaySettings' own default_factory, which reads
        # DERATE_UI_DIR -- the same thing gateway/main.py relies on.
    )

    return GatewayDeps(
        registry=registry,
        links=links,
        resolver=resolver,
        fit=fit,
        planner=planner,
        deployments=deployments,
        providers=providers,
        strict=True,
        settings=settings,
    )


def _make_server(app, *, host: str, port: int) -> uvicorn.Server:
    config = uvicorn.Config(app, host=host, port=port, log_config=None)
    return uvicorn.Server(config)


def _install_shutdown(
    servers: list[uvicorn.Server],
) -> Callable[[], None]:
    """Build the signal callback: flip ``should_exit`` on every server.

    Split out from :func:`_run_with_signals` so the shutdown behaviour is
    exercisable directly in a test, with plain fake objects, without sending
    a real OS signal or opening a real socket.
    """

    def _shutdown() -> None:
        log.info("shutdown signal received")
        for server in servers:
            server.should_exit = True

    return _shutdown


async def _run_with_signals(servers: list[uvicorn.Server]) -> None:
    """Serve every server concurrently until SIGTERM/SIGINT stops them."""
    loop = asyncio.get_running_loop()
    shutdown = _install_shutdown(servers)
    installed: list[signal.Signals] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, shutdown)
            installed.append(sig)
        except (NotImplementedError, RuntimeError):
            # Not every host/thread can install a signal handler (Windows,
            # or an event loop that is not the main thread's). Best effort:
            # the process can still be stopped by cancellation.
            pass
    try:
        await asyncio.gather(*(server.serve() for server in servers))
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)


async def _serve_worker(runtime: NodeRuntime, config: RegistryConfig) -> None:
    """The entire worker: the node agent, on the agent port. Nothing else."""
    server = _make_server(runtime.agent_app(), host="0.0.0.0", port=config.agent_port)
    await _run_with_signals([server])


async def _serve_coordinator(runtime: NodeRuntime, config: RegistryConfig) -> None:
    """Worker duties, plus the real gateway on the coordinator port."""
    from control_plane.gateway.app import create_app

    deps = build_gateway_deps(runtime, config)
    # runtime.telemetry is the one Telemetry bundle start_node already built
    # and started for this process. create_app(), given no telemetry, builds
    # its own from the environment -- fine for the day-0 stub gateway, but on
    # a real composed coordinator that means two Journal writer threads on
    # one journal.db, two Archives on one archive.db, and two Collectors
    # racing cursors over the same local journal. Passing the runtime's
    # bundle through is the one-bundle composition root the telemetry
    # package's own module docstring asks for.
    gateway_app = create_app(deps, settings=deps.settings, telemetry=runtime.telemetry)

    agent_server = _make_server(
        runtime.agent_app(), host="0.0.0.0", port=config.agent_port
    )
    gateway_server = _make_server(
        gateway_app, host=deps.settings.host, port=deps.settings.port
    )
    await _run_with_signals([agent_server, gateway_server])


async def run(config: RegistryConfig | None = None) -> None:
    config = config or RegistryConfig.from_env()
    runtime = await start_node(config)
    try:
        if runtime.role == ROLE_COORDINATOR:
            await _serve_coordinator(runtime, config)
        else:
            await _serve_worker(runtime, config)
    finally:
        await runtime.stop()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("DERATE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger().setLevel(os.environ.get("DERATE_LOG_LEVEL", "INFO"))
    asyncio.run(run())


if __name__ == "__main__":
    main()
