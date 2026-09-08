"""Composition root: what the container entrypoint calls.

Order matters. Refuse a bridge network before anything else, because every
later step would appear to work and then quietly discover nothing. Probe the
hardware next, because the profile is what we advertise and what we join with.
Only then decide the role.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from control_plane.contracts import NodeProfile

from .agent import NodeAgent, create_agent_app
from .bootstrap import (
    RoleDecision,
    adopt_cluster_token,
    reannounce,
    rejoin_until_admitted,
    resolve_role,
)
from .config import (
    REANNOUNCE_MAX_RETRY_S,
    REANNOUNCE_RETRY_S,
    REANNOUNCE_TICK_S,
    REANNOUNCE_UNPOLLED_S,
    ROLE_COORDINATOR,
    RegistryConfig,
)
from .discovery import Advertiser
from .identity import ClusterIdentity, banner, load_or_create_identity, read_identity
from .net import require_host_networking
from .nodeident import load_or_create_node_id
from .probe import probe_local
from .registry import Registry
from . import shell_config

log = logging.getLogger(__name__)


@dataclass
class NodeRuntime:
    """Everything one container is running. Coordinators have a registry."""

    config: RegistryConfig
    profile: NodeProfile
    decision: RoleDecision
    identity: ClusterIdentity
    node_agent: NodeAgent
    advertiser: Advertiser
    registry: Registry | None = None
    telemetry: "object | None" = None
    probe_server: "object | None" = None
    _stopped: bool = field(default=False, repr=False)
    _rejoin_task: "asyncio.Task | None" = field(default=None, repr=False)
    _announce_task: "asyncio.Task | None" = field(default=None, repr=False)

    @property
    def role(self) -> str:
        return self.decision.role

    @property
    def agent_url(self) -> str:
        # Read off the agent's profile, not the boot-time copy: the agent
        # re-probes its own hardware on a timer, and a machine that changed
        # address must not keep handing out the one it started with.
        return f"http://{self.node_agent.profile.address}:{self.config.agent_port}"

    def agent_app(self):
        return create_agent_app(self.node_agent)

    def _apply_admission(self, result: dict) -> None:
        """Learn our cluster identity once a coordinator finally says member.

        Runs once, from the rejoin loop, the moment a human admits a
        candidate (or fixes a wrong token) that started this process life as
        a worker-in-waiting.
        """
        cluster_id = result.get("cluster_id") or ""
        # An admission on an enrollment token carries the permanent cluster
        # token with it. Take it before building the identity, or this node
        # keeps the credential that is about to expire.
        self.config = adopt_cluster_token(self.config, result)
        self.identity = ClusterIdentity(cluster_id=cluster_id, token=self.config.token or "")
        self.node_agent.cluster_id = cluster_id
        self.node_agent.set_token(self.config.token)
        self.decision = dataclasses.replace(
            self.decision, joined=True, status="member", cluster_id=cluster_id
        )
        log.info("%s: admitted into cluster %s", self.profile.node_id, cluster_id)

    async def _rejoin_loop(self) -> None:
        result = await rejoin_until_admitted(
            self.decision,
            self.config,
            self.profile,
            self.agent_url,
            should_continue=lambda: not self._stopped,
        )
        if result is not None:
            self._apply_admission(result)

    def _sync_profile(self) -> bool:
        """Adopt the agent's live profile. True when it had moved on.

        The agent re-probes on a timer and replaces its own ``profile``; this
        is where the rest of the runtime notices. Also keeps the mDNS record
        honest, for both roles: a coordinator that changes address and keeps
        advertising the old one is worse than a worker doing the same, because
        that is the record every joining node browses for.
        """
        live = self.node_agent.profile
        if live == self.profile:
            return False
        moved = live.address != self.profile.address
        self.profile = live
        if moved:
            self.advertiser.readvertise(live.address)
        return True

    def _announce_trigger(self, since_boot: float) -> str | None:
        """Why we should re-announce right now, or None to stay quiet."""
        moved = self._sync_profile()
        if self.role == ROLE_COORDINATOR:
            # A coordinator has nobody to announce itself to, but it still has
            # to keep its mDNS record true -- that record is what every joining
            # node browses for. _sync_profile above has already re-registered
            # it; there is nothing further to do here.
            return None
        if self._rejoin_task is not None and not self._rejoin_task.done():
            # A node waiting to be admitted is `_rejoin_loop`'s conversation.
            # Both loops would fire -- a candidate is not polled, so the silence
            # trigger below is true for it from boot -- and the second would
            # only duplicate the first's join.
            #
            # The test is whether that loop is *running*, not what the status
            # says. It exits for good the first time it is admitted, and the
            # status can go back to "candidate" afterwards: a coordinator that
            # is rebuilt from nothing has never met this node, so it re-offers
            # it as a candidate. Gating on the status there would leave nobody
            # driving the conversation and the node stranded outside a cluster
            # it can see.
            return None
        if moved:
            return f"our profile changed (address {self.profile.address})"
        # Silence from the coordinator is the signal. It polls every member
        # every heartbeat, so nobody asking means nobody is holding a usable
        # record of us. `None` -- never polled at all -- is measured from boot
        # instead, which is what a node that restarted while the coordinator
        # was down looks like: it never got a rejoin loop, because it was never
        # a candidate, and before this it would have waited forever.
        quiet = self.node_agent.seconds_since_poll()
        if quiet is None:
            quiet = since_boot
        if quiet >= REANNOUNCE_UNPOLLED_S:
            return f"nothing has polled this node in {quiet:.0f}s"
        return None

    async def _announce_loop(
        self,
        tick: float = REANNOUNCE_TICK_S,
        sleep=None,
        clock=None,
    ) -> None:
        """Keep this node correctly registered, for as long as it runs.

        Deliberately not a role loop: it only ever re-joins. A worker whose
        coordinator has gone waits for one to come back rather than promoting
        itself, so this adds no election and cannot split a subnet -- see
        `bootstrap.reannounce`.
        """
        sleep = sleep or asyncio.sleep
        clock = clock or time.monotonic
        started = clock()
        last_attempt = 0.0
        floor = REANNOUNCE_RETRY_S
        announced = False
        while not self._stopped:
            await sleep(tick)
            if self._stopped:
                return
            now = clock()
            try:
                reason = self._announce_trigger(now - started)
            except Exception as exc:
                log.debug("re-announcement check failed: %s", exc)
                continue
            if reason is None:
                continue
            # The trigger stays true for as long as we are forgotten, so the
            # floor is what stops this becoming a hot loop against a dead
            # address. It backs off while nobody answers and resets the moment
            # somebody does.
            if announced and now - last_attempt < floor:
                continue
            last_attempt = now
            announced = True
            log.info("re-announcing %s: %s", self.profile.node_id, reason)
            try:
                result = await reannounce(
                    self.config,
                    self.profile,
                    self.agent_url,
                    self.decision.coordinator_url,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("re-announcement failed: %s", exc)
                floor = min(floor * 2, REANNOUNCE_MAX_RETRY_S)
                continue
            if not result.ok:
                log.warning("%s; retrying in %.0fs", result.reason, floor)
                floor = min(floor * 2, REANNOUNCE_MAX_RETRY_S)
                continue
            floor = REANNOUNCE_RETRY_S
            self._apply_announcement(result)

    def _apply_announcement(self, result) -> None:
        """Take what a landed re-announcement told us."""
        payload = result.result or {}
        self.decision = dataclasses.replace(
            self.decision, coordinator_url=result.coordinator_url, joined=True
        )
        if result.token_used and result.token_used != self.config.token:
            self.config = dataclasses.replace(self.config, token=result.token_used)
        # An admission carries the permanent token, and a coordinator we have
        # just met for the first time may be handing it to us now.
        self.config = adopt_cluster_token(self.config, payload)
        self.node_agent.set_token(self.config.token)
        status = payload.get("status")
        cluster_id = payload.get("cluster_id") or ""
        if status == "member" and (
            self.decision.status != "member" or cluster_id != self.identity.cluster_id
        ):
            self.identity = ClusterIdentity(
                cluster_id=cluster_id or self.identity.cluster_id,
                token=self.config.token or "",
            )
            self.node_agent.cluster_id = self.identity.cluster_id
            self.advertiser.readvertise(self.profile.address)
        if status:
            self.decision = dataclasses.replace(self.decision, status=status)
        log.info("%s: %s (status=%s)", self.profile.node_id, result.reason, status)

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        for task in (self._announce_task, self._rejoin_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await self.node_agent.stop()  # also withdraws the advertisement
        if self.registry is not None:
            await self.registry.stop()
        if self.telemetry is not None:
            await self.telemetry.stop()
        if self.probe_server is not None:
            self.probe_server.close()


def recover_credentials(config: RegistryConfig) -> RegistryConfig:
    """Fill in the cluster credential this machine already holds.

    A worker admitted on an enrollment token is handed the permanent cluster
    token once and persists it (``bootstrap.adopt_cluster_token``) -- and
    nothing ever read it back. A worker's token came from ``DERATE_TOKEN`` and
    nowhere else, which made that write dead and its docstring untrue.

    What that cost, concretely: ``handle_join`` checks the token *before* it
    checks membership, so a container restarted with a spent ``ej_`` enrollment
    token still in its environment presented the spent one and was rejected --
    the node 403'ing itself out of a cluster it was already a member of, which
    is the exact failure ``adopt_cluster_token`` exists to prevent. A native
    install restarted without the variable was worse in a quieter way: it
    joined tokenless, got "member" back with its routing state frozen, and its
    agent refused every credentialed request because it held no token at all.

    An environment token still wins. It is the deployment's deliberate
    instruction and it is how a machine is re-homed onto another cluster.
    Reading is all this does: a worker must never *mint* a cluster identity,
    which is why it goes through ``read_identity`` and not
    ``load_or_create_identity``.
    """
    stored = read_identity(config.data_dir)
    if stored is None:
        return config
    if not config.token:
        config = dataclasses.replace(config, token=stored.token)
    elif config.token != stored.token:
        # Two credentials, and from here there is no way to tell which one the
        # coordinator will accept. The environment's is tried first because it
        # is the deliberate instruction -- a fresh enrollment token carried to
        # this machine to re-home it -- and the stored one is the fallback,
        # because the common case is the opposite: the container is simply
        # restarting with the spent enrollment token install.sh baked into its
        # environment, and the permanent token it adopted is the one that works.
        config = dataclasses.replace(config, fallback_token=stored.token)
    if not config.cluster_id and stored.cluster_id:
        # So a worker that restarts while the coordinator is down still
        # advertises the cluster it belongs to instead of an empty string.
        config = dataclasses.replace(config, cluster_id=stored.cluster_id)
    return config


def _open_probe_sink():
    """The TCP sink the link ladder's last rung measures against.

    ``links/measure.py``'s ladder degrades nccl-tests -> ib_write_bw -> a plain
    TCP blast, and that last rung dials this port on the *peer*. Nothing ever
    started it: ``start_probe_server`` was written, exported from
    ``links/__init__.py``, documented as "the node agent starts the sink", and
    then not called -- so the rung only ever worked between two machines that
    both happened to have iperf3 installed and reachable over ssh.

    It matters more now than it did. A machine with no CUDA cannot run
    nccl-tests and has no RDMA verbs, so TCP is the *only* rung it can take
    part in at all; without a sink, every link to a Mac or a Windows box is
    permanently unmeasured rather than coarsely measured.

    Best effort by construction: a port already in use is somebody else's sink
    or a stale process, and neither is a reason for the node not to boot. The
    ladder already reports an unmeasured link honestly.
    """
    try:
        from control_plane.links.probe_server import start_probe_server

        return start_probe_server()
    except OSError as exc:
        log.warning(
            "link probe sink not listening (%s); links to this node fall back "
            "to whatever iperf3 is reachable, or stay unmeasured",
            exc,
        )
        return None


def _open_telemetry(config: RegistryConfig, profile: NodeProfile, role: str):
    """This node's telemetry.

    A worker gets a journal and no archive: it has nothing to collect from
    anyone, and the coordinator is where the cluster's history is assembled.
    Its rows leave over /agent/journal. The journal still matters on a worker,
    and matters most there -- there is no coordinator failover, so a worker
    that recorded nothing loses everything the coordinator was down for.
    """
    from control_plane.telemetry.service import Telemetry

    return Telemetry.from_env(
        root=config.data_dir, node_id=profile.node_id
    ) if role == ROLE_COORDINATOR else _journal_only(config, profile)


def _journal_only(config: RegistryConfig, profile: NodeProfile):
    from control_plane.telemetry import config as tconfig
    from control_plane.telemetry.service import Telemetry

    if not tconfig.enabled():
        return Telemetry.disabled("DERATE_TELEMETRY is off")
    root = Path(config.data_dir)
    if not root.is_dir():
        return Telemetry.disabled(f"{root} does not exist")
    return Telemetry.open(root, node_id=profile.node_id, coordinator=False)


async def start_node(
    config: RegistryConfig | None = None, telemetry: "object" = None
) -> NodeRuntime:
    """Bring up this node. Raises BridgeNetworkError before doing any work.

    *telemetry* is the node's Telemetry bundle. A worker builds its own if it
    is not given one, because a worker is where most of the cluster's activity
    happens and it is the only thing that remembers it while the coordinator
    is down.
    """
    config = config or RegistryConfig.from_env()

    require_host_networking(allow_bridge=config.allow_bridge)

    # If the shell is switched on, make sure this machine has a key before any
    # route exists to present one to. An enabled shell with no key refuses every
    # session, so without this `DERATE_SHELL=1` alone produces a terminal that
    # is advertised as available and turns away every attempt -- which reads as
    # a broken feature rather than as a missing secret.
    #
    # Here rather than in `create_agent_app`: that is a factory, twenty-odd
    # tests call it, and generating a secret and creating a data directory is a
    # process-startup side effect that has no business firing under pytest.
    if shell_config.enabled():
        shell_config.bootstrap_key(config.data_dir)

    # Seed-once-and-keep, rather than re-derived from the hostname every boot.
    # A machine that is renamed, re-imaged or handed a new name by DHCP stays
    # the same node to the rest of the cluster; see nodeident.py for what that
    # was costing.
    node_id = load_or_create_node_id(config.data_dir, config.node_id)
    profile = probe_local(node_id=node_id)
    if profile.device_class.value == "unknown":
        log.warning(
            "no GPU could be probed on %s; this node will be visible but the "
            "planner will skip it",
            profile.node_id,
        )

    agent_url = f"http://{profile.address}:{config.agent_port}"

    config = recover_credentials(config)

    decision = await resolve_role(config, profile, agent_url)
    # If the coordinator took the fallback rather than the credential we led
    # with, lead with that one from now on: there is no reason to keep paying
    # for the rejection on every later join.
    if decision.token_used and decision.token_used != config.token:
        config = dataclasses.replace(config, token=decision.token_used)
    # A node admitted on an enrollment token was handed the permanent cluster
    # token in the join response. Persist it now, before anything derives an
    # identity from `config.token`.
    config = adopt_cluster_token(
        config,
        {"cluster_token": decision.cluster_token, "cluster_id": decision.cluster_id},
    )

    if decision.role == ROLE_COORDINATOR:
        identity = load_or_create_identity(
            config.data_dir, cluster_id=config.cluster_id, token=config.token
        )
    else:
        # A worker never mints a cluster identity -- that would be inventing a
        # cluster nobody asked for. Its token is whatever survived above (env,
        # then the copy it adopted on an earlier run) and its cluster id comes
        # from the coordinator's join response, falling back to the one on disk
        # when there was nobody to ask.
        identity = ClusterIdentity(
            cluster_id=decision.cluster_id or config.cluster_id or "",
            token=config.token or "",
        )

    advertiser = Advertiser(
        node_id=profile.node_id,
        role=decision.role,
        cluster_id=identity.cluster_id,
        address=profile.address,
        port=config.agent_port,
    )
    if telemetry is None:
        telemetry = _open_telemetry(config, profile, decision.role)

    node_agent = NodeAgent(
        profile=profile,
        role=decision.role,
        cluster_id=identity.cluster_id,
        port=config.agent_port,
        advertiser=advertiser,
        sink=telemetry.sink,
        token=identity.token,
        data_root=config.data_dir,
    )
    await node_agent.start()
    await telemetry.start(node_id=profile.node_id)
    probe_server = _open_probe_sink()

    registry: Registry | None = None
    if decision.role == ROLE_COORDINATOR:
        registry = Registry(
            config=config,
            local_profile=profile,
            role=ROLE_COORDINATOR,
            identity=identity,
        )
        await registry.start()
        print(banner(identity, f"http://{profile.address}:{config.coordinator_port}"))
    else:
        log.info(
            "worker %s: %s (status=%s)",
            profile.node_id,
            decision.reason,
            decision.status or "unknown",
        )

    runtime = NodeRuntime(
        config=config,
        profile=profile,
        decision=decision,
        identity=identity,
        node_agent=node_agent,
        advertiser=advertiser,
        registry=registry,
        telemetry=telemetry,
        probe_server=probe_server,
    )

    # Every node, both roles: keeps the mDNS record true when this machine's
    # address changes, and -- for a worker that is a member -- keeps it
    # registered with whichever coordinator is actually running. It stands down
    # while `_rejoin_loop` below is still waiting to be admitted, and picks the
    # node up again the moment it is.
    runtime._announce_task = asyncio.create_task(
        runtime._announce_loop(), name="registry-announce"
    )

    if decision.role != ROLE_COORDINATOR and decision.status in ("candidate", "rejected"):
        # Worker-in-waiting: the agent app above is already up and
        # advertising. Keep polling join in the background so that once a
        # human admits us (or fixes the token) from the other side, this
        # process picks up its cluster identity without a restart.
        runtime._rejoin_task = asyncio.create_task(
            runtime._rejoin_loop(), name="registry-rejoin"
        )

    return runtime
