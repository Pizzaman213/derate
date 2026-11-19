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
from urllib.parse import urlsplit

from control_plane.contracts import NodeProfile

from .agent import NodeAgent, create_agent_app
from .bootstrap import (
    RoleDecision,
    adopt_credentials,
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
from .identity import (
    ClusterIdentity,
    agent_token,
    banner,
    load_or_create_identity,
    read_identity,
)
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
    # Set by the first landed re-announcement: a member from before per-member
    # tokens has asked for its own once in this process life.
    _migration_asked: bool = field(default=False, repr=False)

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

    def _agent_credential(self, payload: dict | None = None) -> str | None:
        """What this node's agent accepts on its gated routes.

        See ``worker_agent_credential``.
        """
        return worker_agent_credential(self.config, self.profile.node_id, payload)

    def _note_coordinator(self, url: str | None) -> None:
        """Tell the agent which address is the coordinator's (N-11): a read
        presenting this agent's token from anywhere else leaves a row."""
        host = urlsplit(url).hostname if url else None
        if host:
            self.node_agent.set_coordinator(host)

    def _apply_admission(self, result: dict) -> None:
        """Learn our cluster identity once a coordinator finally says member.

        Runs once, from the rejoin loop, the moment a human admits a
        candidate (or fixes a wrong token) that started this process life as
        a worker-in-waiting.
        """
        cluster_id = result.get("cluster_id") or ""
        # An admission carries this member's own agent token. Take it before
        # anything else, or the next join presents a credential the
        # coordinator no longer accepts from a member.
        self.config = adopt_credentials(self.config, result)
        self.identity = ClusterIdentity(cluster_id=cluster_id, token=self.config.token or "")
        self.node_agent.cluster_id = cluster_id
        self.node_agent.set_token(self._agent_credential(result))
        self._note_coordinator(self.decision.coordinator_url)
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
        if (
            self.decision.status == "member"
            and not self.config.agent_token
            and self.config.token
            and not self._migration_asked
        ):
            # A member from before per-member tokens. The coordinator keeps
            # polling it with the derived token, so silence never comes; one
            # re-join collects its own token, after which the cluster token
            # can be rotated. Once per process life: an older coordinator
            # hands nothing back, and asking again changes nothing.
            return "this member holds no agent token of its own yet"
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
        # A member's answer carries its own agent token -- the first one, on
        # the one-time migration, or the same one again.
        self._migration_asked = True
        self.config = adopt_credentials(self.config, payload)
        self.node_agent.set_token(self._agent_credential(payload))
        self._note_coordinator(result.coordinator_url)
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
    """Fill in the credential this machine already holds.

    A member's ``cluster.json`` holds its own agent token
    (``identity.save_agent_credential``), handed over by the coordinator. That
    is the only credential a member's re-join is accepted on, so a restarted
    worker that did not read it back would lock itself out of its cluster. A
    worker from before per-member tokens still has the cluster token there
    instead; it is used as ``token`` when the environment names none, which is
    what the coordinator's one-time migration accepts.

    The environment's ``DERATE_TOKEN`` stays ``token`` and is the fallback
    ``join_with_held_credentials`` tries when the agent token is refused -- a
    fresh enrollment token carried to this machine to re-home it. Reading is
    all this does: a worker must never *mint* a cluster identity, which is why
    it goes through ``read_identity`` and not ``load_or_create_identity``.
    """
    stored = read_identity(config.data_dir)
    if stored is None:
        return config
    if stored.agent_token and not config.agent_token:
        config = dataclasses.replace(config, agent_token=stored.agent_token)
    if not config.token and stored.token:
        config = dataclasses.replace(config, token=stored.token)
    if not config.cluster_id and stored.cluster_id:
        # So a worker that restarts while the coordinator is down still
        # advertises the cluster it belongs to instead of an empty string.
        config = dataclasses.replace(config, cluster_id=stored.cluster_id)
    return config


def worker_agent_credential(
    config: RegistryConfig, node_id: str, payload: dict | None = None
) -> str | None:
    """What a worker's agent accepts on its gated routes.

    The coordinator presents each member its own token and no other: the one
    an acknowledged join just delivered, else the one this node holds in
    ``cluster.json``. Only a member that has never been handed one -- a worker
    from before per-member tokens, not yet re-joined -- falls back to the value
    derived from the cluster token, which is what the coordinator still
    presents to it until it migrates.
    """
    delivered = (payload or {}).get("agent_token")
    if delivered:
        return str(delivered)
    if config.agent_token:
        return config.agent_token
    if not config.token:
        return None
    return agent_token(config.token, node_id)


def _open_probe_sink(node_agent: NodeAgent | None = None):
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

        return start_probe_server(
            allowed=node_agent.trusted_peers if node_agent is not None else (lambda: ())
        )
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

    root = Path(config.data_dir)
    if not root.is_dir():
        return Telemetry.disabled(f"{root} does not exist")
    # DERATE_TELEMETRY=0 switches metrics off, not this agent's record of what
    # it did and refused: that still journals, for the coordinator to collect
    # (RecordsOnlySink, EV-2).
    return Telemetry.open(
        root,
        node_id=profile.node_id,
        coordinator=False,
        records_only=not tconfig.enabled(),
    )


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
        #
        # A member's acked answer carried its own agent token: persist it now,
        # before the agent is armed with it.
        config = adopt_credentials(
            config,
            {"agent_token": decision.agent_token, "cluster_id": decision.cluster_id},
        )
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

    # One instance, both roles. The node-side half (probe health, throttle,
    # disk, its own re-probe) is emitted by the agent on every node; the roster
    # half is emitted by the coordinator's Registry below. A worker gets the
    # first and never constructs the second.
    from control_plane.telemetry.events import RegistryEvents

    registry_events = RegistryEvents(sink=telemetry.sink, node_id=profile.node_id)

    node_agent = NodeAgent(
        profile=profile,
        role=decision.role,
        cluster_id=identity.cluster_id,
        port=config.agent_port,
        advertiser=advertiser,
        sink=telemetry.sink,
        # A worker's own credential. The coordinator's agent is armed below,
        # once its registry exists and has minted the local node's token.
        token=(
            None
            if decision.role == ROLE_COORDINATOR
            else worker_agent_credential(config, profile.node_id)
        ),
        data_root=config.data_dir,
        events=registry_events,
    )
    if decision.role != ROLE_COORDINATOR and decision.coordinator_url:
        host = urlsplit(decision.coordinator_url).hostname
        if host:
            node_agent.set_coordinator(host)
    await node_agent.start()
    await telemetry.start(node_id=profile.node_id)
    probe_server = _open_probe_sink(node_agent)

    registry: Registry | None = None
    if decision.role == ROLE_COORDINATOR:
        registry = Registry(
            config=config,
            local_profile=profile,
            role=ROLE_COORDINATOR,
            identity=identity,
            events=registry_events,
        )
        # The coordinator presents the local node its own minted token, like
        # every other member; and it is the only reader its own agent has.
        node_agent.set_token(registry.agent_token(profile.node_id))
        node_agent.set_coordinator(profile.address)
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
