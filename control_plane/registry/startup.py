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
from dataclasses import dataclass, field
from pathlib import Path

from control_plane.contracts import NodeProfile

from .agent import NodeAgent, create_agent_app
from .bootstrap import RoleDecision, rejoin_until_admitted, resolve_role
from .config import ROLE_COORDINATOR, RegistryConfig
from .discovery import Advertiser
from .identity import ClusterIdentity, banner, load_or_create_identity
from .net import require_host_networking
from .probe import probe_local
from .registry import Registry

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
    _stopped: bool = field(default=False, repr=False)
    _rejoin_task: "asyncio.Task | None" = field(default=None, repr=False)

    @property
    def role(self) -> str:
        return self.decision.role

    @property
    def agent_url(self) -> str:
        return f"http://{self.profile.address}:{self.config.agent_port}"

    def agent_app(self):
        return create_agent_app(self.node_agent)

    def _apply_admission(self, result: dict) -> None:
        """Learn our cluster identity once a coordinator finally says member.

        Runs once, from the rejoin loop, the moment a human admits a
        candidate (or fixes a wrong token) that started this process life as
        a worker-in-waiting.
        """
        cluster_id = result.get("cluster_id") or ""
        self.identity = ClusterIdentity(cluster_id=cluster_id, token=self.config.token or "")
        self.node_agent.cluster_id = cluster_id
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

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._rejoin_task is not None:
            self._rejoin_task.cancel()
            try:
                await self._rejoin_task
            except (asyncio.CancelledError, Exception):
                pass
        await self.node_agent.stop()  # also withdraws the advertisement
        if self.registry is not None:
            await self.registry.stop()
        if self.telemetry is not None:
            await self.telemetry.stop()


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
        return Telemetry.disabled("SPARKPLANE_TELEMETRY is off")
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

    profile = probe_local(node_id=config.node_id)
    if profile.device_class.value == "unknown":
        log.warning(
            "no GPU could be probed on %s; this node will be visible but the "
            "planner will skip it",
            profile.node_id,
        )

    agent_url = f"http://{profile.address}:{config.agent_port}"
    decision = await resolve_role(config, profile, agent_url)

    if decision.role == ROLE_COORDINATOR:
        identity = load_or_create_identity(
            config.data_dir, cluster_id=config.cluster_id, token=config.token
        )
    else:
        # A worker's token came from the environment and its cluster id from the
        # coordinator's join response. Nothing to mint, nothing to persist.
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
    )
    await node_agent.start()
    await telemetry.start(node_id=profile.node_id)

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
