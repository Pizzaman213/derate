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
``runtime.stop()`` always runs on the way out. With ``DERATE_EDGE_PORT`` set
the gateway server listens on a second socket too -- the edge cloudflared is
pointed at -- under the same app and one lifespan; ``gateway/authz.py`` tells
the two apart per connection from ``scope["server"]``.

The worker path must stay light: nothing from ``control_plane.gateway``,
``control_plane.deploy``, ``control_plane.providers``, ``control_plane.fit``,
``control_plane.planner``, ``control_plane.links`` or
``control_plane.resolver`` is imported unless this process is the
coordinator. Every one of those imports lives inside :func:`build_gateway_deps`
or :func:`_serve_coordinator`, never at module scope.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import ipaddress
import logging
import os
import signal
import socket
from typing import Callable

import uvicorn

from control_plane import logfiles
from control_plane.redaction import Redactor, SecretRedactingFilter
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
    from control_plane.deploy.flags import RUNTIMES as RUNTIME_SPECS
    from control_plane.deploy.recipes import container_image
    from control_plane.resolver import imageprobe
    from control_plane.resolver.resolver import ModelResolver

    registry = runtime.registry
    if registry is None:
        raise RuntimeError(
            "build_gateway_deps called with no registry; runtime.role must "
            "be coordinator"
        )

    links = LinkService(registry=registry, local_node_id=runtime.profile.node_id)
    # The composition root is the only thing that knows both which images
    # this build launches and which component needs to ask them what they
    # can load. Handing them over here is what lets the support table stop
    # being a hand-copied list of somebody else's architectures -- see
    # resolver/imageprobe.py. An image that is not on this machine is not
    # pulled and not waited for; the static table answers instead.
    resolver = ModelResolver(
        runtime_images={
            name: container_image(spec)
            for name, spec in RUNTIME_SPECS.items()
            if name in imageprobe.SCRIPTS
        }
    )
    fit = FitCalculator()
    # Never Planner(fit=FitCalculator()): the planner's fit argument is its
    # own FitHelpers protocol (min_nodes_required/kv_bytes_per_token), which
    # FitCalculator does not implement. The no-arg default resolves to
    # control_plane.fit's module-level helpers when they exist.
    planner = Planner()
    deployments = DeploymentManager(registry=registry, state_dir=config.data_dir)
    providers = ProviderService(data_path=config.data_dir)
    # The log files opened in main() started with a fresh Redactor, which knows
    # the vendor key shapes but no actual value. This is the first moment a
    # thing that RESOLVES keys exists, so the file handlers are pointed at its
    # redactor -- the same instance telemetry/service.py shares, for the same
    # reason: a Redactor only scrubs what it has been told to remember.
    logfiles.adopt_redactor(providers.redactor)

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


#: Seconds in-flight requests get to finish after a shutdown signal.
SHUTDOWN_GRACE_S = 30
#: Websocket frames buffered per connection (each up to uvicorn's 16 MiB).
WS_MAX_QUEUE = 4


#: What uvicorn's websocket protocol, and the websockets library through the
#: logger uvicorn hands it, say about every socket: at INFO, on
#: ``uvicorn.error``, with the caller's raw target in it and no budget.
#: ``gateway.access`` already records each socket once, capped, so these are
#: only a way for an internet caller to write unbounded lines into node.log.
_WEBSOCKET_CHATTER = frozenset(
    {
        '%s - "WebSocket %s" 403',
        '%s - "WebSocket %s" [accepted]',
        '%s - "WebSocket %s" %d',
        "connection rejected (%d %s)",
        "connection open",
        "connection closed",
    }
)


class _WebsocketChatterFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.msg not in _WEBSOCKET_CHATTER


_WEBSOCKET_FILTER = _WebsocketChatterFilter()


def _quiet_websocket_chatter() -> None:
    """Drop :data:`_WEBSOCKET_CHATTER` from ``uvicorn.error``. Idempotent: one
    filter, however many configs are built. On the logger, not a handler,
    because the records are emitted by that logger itself and a handler added
    later (the journal's, the log files') must not see them either."""
    logger = logging.getLogger("uvicorn.error")
    if _WEBSOCKET_FILTER not in logger.filters:
        logger.addFilter(_WEBSOCKET_FILTER)


def _config(app, *, host: str, port: int) -> uvicorn.Config:
    from control_plane.gateway.settings import uvicorn_proxy_kwargs

    _quiet_websocket_chatter()
    # access_log=False: gateway.access (authz.on_request_done) is the access
    # line now -- request id, principal, UTC -- and uvicorn's would double it.
    return uvicorn.Config(
        app,
        host=host,
        port=port,
        log_config=None,
        access_log=False,
        # No "server: uvicorn": the version banner tells a scanner what to try.
        server_header=False,
        # A client holding a stream or a realtime socket open must not hold a
        # restart open with it: uvicorn's default waits for every connection
        # forever, which an internet caller can now arrange at will.
        timeout_graceful_shutdown=SHUTDOWN_GRACE_S,
        # Frames a websocket may buffer while its upstream is slow. The
        # default 32 x 16 MiB lets one realtime session hold 512 MiB.
        ws_max_queue=WS_MAX_QUEUE,
        **uvicorn_proxy_kwargs(),
    )


def _make_server(app, *, host: str, port: int) -> uvicorn.Server:
    return _Server(_config(app, host=host, port=port))


class _Server(uvicorn.Server):
    """A uvicorn server that serves the sockets it was given: one lifespan
    over several listeners, so the gateway and its edge share one app.

    And one that leaves signals to `_run_with_signals`. uvicorn 0.33 installs
    its own SIGTERM/SIGINT handlers around `serve` (`capture_signals`); with
    derate's loop handler beside them both fired on ONE signal, so derate's
    saw `should_exit` already set and forced the exit on the first SIGTERM --
    skipping the gateway's lifespan shutdown, which is where the audit and
    security rows are flushed and the stores closed."""

    def __init__(self, config: uvicorn.Config, sockets: list[socket.socket] | None = None):
        super().__init__(config)
        self._sockets = sockets

    def capture_signals(self):
        return contextlib.nullcontext()

    async def serve(self, sockets: list[socket.socket] | None = None) -> None:
        await super().serve(sockets=sockets or self._sockets)


def _bind(host: str, port: int) -> socket.socket:
    """A listening-ready socket bound the way ``uvicorn.Config.bind_socket``
    binds one, except not inheritable: nothing here forks workers, and the
    shell relay's ``pty.fork`` + ``execve`` would otherwise hand the edge
    listener to every shell an admin opens and every process left running
    from one, which could then hold the port after the coordinator exits."""
    sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.set_inheritable(False)
    return sock


def _not_inheritable(sock: socket.socket) -> socket.socket:
    sock.set_inheritable(False)
    return sock


def _install_shutdown(
    servers: list[uvicorn.Server],
) -> Callable[[], None]:
    """Build the signal callback: flip ``should_exit`` on every server, and
    ``force_exit`` on a second signal -- the operator asking twice means do
    not wait for the grace period. ``force_exit`` alone does not mean that:
    uvicorn still awaits ``wait_closed()``, which holds until every accepted
    connection is gone, so a client streaming a response kept the process up
    for the full grace. The second signal therefore aborts every open
    connection's transport too.

    Split out from :func:`_run_with_signals` so the shutdown behaviour is
    exercisable directly in a test, with plain fake objects, without sending
    a real OS signal or opening a real socket.
    """

    def _shutdown() -> None:
        log.info("shutdown signal received")
        for server in servers:
            if server.should_exit:
                server.force_exit = True
                _abort_connections(server)
            server.should_exit = True

    return _shutdown


def _abort_connections(server: uvicorn.Server) -> None:
    state = getattr(server, "server_state", None)
    for connection in list(getattr(state, "connections", ())):
        transport = getattr(connection, "transport", None)
        if transport is not None and not transport.is_closing():
            transport.abort()


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


def _is_loopback_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


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
    settings = deps.settings
    if settings.edge_port is None:
        gateway_server = _make_server(gateway_app, host=settings.host, port=settings.port)
    else:
        if settings.edge_port == settings.port:
            raise SystemExit("[derate] DERATE_EDGE_PORT must differ from DERATE_PORT")
        # Bound first, so a taken edge port stops startup before the main
        # listener exists, and says which variable moves it.
        try:
            edge = _bind(settings.edge_host, settings.edge_port)
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
            raise SystemExit(
                f"[derate] DERATE_EDGE_PORT {settings.edge_port} is already in use on "
                f"{settings.edge_host}; pick another one."
            ) from None
        config = _config(gateway_app, host=settings.host, port=settings.port)
        gateway_server = _Server(
            config,
            # bind_socket logs the main listener the way uvicorn always has.
            sockets=[_not_inheritable(config.bind_socket()), edge],
        )
        log.info(
            "edge listener on %s:%d: /v1 only, client address from %s",
            settings.edge_host,
            settings.edge_port,
            settings.edge_client_header,
        )
        if not _is_loopback_host(settings.edge_host):
            log.warning(
                "edge listener is bound to %s, not loopback: anything that reaches "
                "it directly can write %s and choose the address every throttle "
                "and audit row records. Bind 127.0.0.1 unless cloudflared runs "
                "in a container, and then firewall the port to it alone",
                settings.edge_host,
                settings.edge_client_header,
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
        format=logfiles.LOG_FORMAT,
    )
    for handler in logging.getLogger().handlers:
        handler.setFormatter(logfiles.UTCFormatter(logfiles.LOG_FORMAT))
        # Pattern rules now; logfiles.adopt_redactor swaps in the provider
        # service's redactor (remembered values too) once that exists.
        handler.addFilter(SecretRedactingFilter(Redactor()))
    logging.getLogger().setLevel(os.environ.get("DERATE_LOG_LEVEL", "INFO"))
    # Before asyncio.run, so a failure to start a node is in the file rather
    # than only on a stderr that systemd or `docker run -d` swallowed.
    logfiles.install()
    asyncio.run(run())


if __name__ == "__main__":
    main()
