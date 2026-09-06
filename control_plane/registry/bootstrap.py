"""Role resolution and startup.

Browse, join if somebody is already coordinating, otherwise coordinate. No
election, no failover: first one wins and the role is sticky for the process
lifetime. If the coordinator dies, workers keep running and the UI goes dark
until it comes back. That is a deliberate scope decision, not an oversight.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from control_plane.contracts import NodeProfile

from .client import AgentClient, HttpAgentClient
from .config import (
    MDNS_BROWSE_SECONDS,
    ROLE_AUTO,
    ROLE_COORDINATOR,
    ROLE_WORKER,
    RegistryConfig,
)
from .discovery import DiscoveredPeer, browse_async
from .errors import JoinRejected, ProbeFailed
from .net import normalize_agent_url
from .serde import profile_to_dict

log = logging.getLogger(__name__)

JOIN_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class RoleDecision:
    role: str
    coordinator_url: str | None
    joined: bool
    reason: str
    cluster_id: str | None = None
    status: str | None = None  # "candidate" or "member", as the coordinator saw it

    @property
    def is_coordinator(self) -> bool:
        return self.role == ROLE_COORDINATOR


async def post_join(
    coordinator_url: str,
    token: str | None,
    profile: NodeProfile,
    agent_url: str,
    client: AgentClient | None = None,
) -> dict:
    """POST /api/nodes/join. Raises JoinRejected when refused."""
    client = client or HttpAgentClient()
    payload = {
        "token": token or "",
        "profile": profile_to_dict(profile),
        "agent_url": agent_url,
    }
    try:
        return await client.post_json(
            f"{coordinator_url.rstrip('/')}/api/nodes/join",
            payload,
            timeout=JOIN_TIMEOUT_S,
        )
    except ProbeFailed as exc:
        # 403 and "connection refused" are different problems with different
        # fixes, so keep the distinction in the message rather than flattening.
        if "403" in str(exc):
            raise JoinRejected(f"coordinator rejected our token: {exc}") from exc
        raise


async def resolve_role(
    config: RegistryConfig,
    profile: NodeProfile,
    agent_url: str,
    browse: Callable[..., Awaitable[list[DiscoveredPeer]]] = browse_async,
    join: Callable[..., Awaitable[dict]] = post_join,
) -> RoleDecision:
    """Decide what this process is. ``browse`` and ``join`` are injectable."""

    if config.role == ROLE_COORDINATOR:
        return RoleDecision(
            ROLE_COORDINATOR, None, False, "SPARKPLANE_ROLE=coordinator"
        )

    # An explicit join address is a deliberate instruction for a node on another
    # subnet. Skip discovery, and fail loudly rather than quietly starting a
    # second cluster next to the one the operator named.
    if config.join_address:
        url = normalize_agent_url(config.join_address, config.coordinator_port)
        result = await join(url, config.token, profile, agent_url)
        return RoleDecision(
            ROLE_WORKER,
            url,
            True,
            f"SPARKPLANE_JOIN={config.join_address}",
            cluster_id=(result or {}).get("cluster_id"),
            status=(result or {}).get("status"),
        )

    peers = await browse(MDNS_BROWSE_SECONDS, profile.node_id)
    coordinators = [p for p in peers if p.is_coordinator]
    rejected = False

    for peer in coordinators:
        url = f"http://{peer.address}:{config.coordinator_port}"
        try:
            result = await join(url, config.token, profile, agent_url)
            return RoleDecision(
                ROLE_WORKER,
                url,
                True,
                f"joined coordinator {peer.node_id} at {url}",
                cluster_id=(result or {}).get("cluster_id") or peer.cluster_id,
                status=(result or {}).get("status"),
            )
        except JoinRejected:
            # Someone else's cluster on the same subnet. Not an error for us.
            rejected = True
            log.warning(
                "coordinator %s at %s rejected our token; it is not our cluster",
                peer.node_id,
                url,
            )
        except ProbeFailed as exc:
            log.warning("coordinator %s advertised but did not answer: %s", peer.node_id, exc)

    if config.role == ROLE_WORKER:
        # Forced worker with nobody to serve. Stay a worker and keep serving
        # /agent/*, so a coordinator starting later can find and admit us.
        return RoleDecision(
            ROLE_WORKER,
            None,
            False,
            "SPARKPLANE_ROLE=worker but no coordinator answered; waiting to be found",
        )

    if rejected:
        reason = "a coordinator responded but the token did not match; starting our own cluster"
    elif coordinators:
        reason = "coordinators advertised but none answered; becoming coordinator"
    else:
        reason = f"no coordinator found in {MDNS_BROWSE_SECONDS:.0f}s; becoming coordinator"
    log.info("role resolution: %s", reason)
    return RoleDecision(ROLE_COORDINATOR, None, False, reason)


def resolve_role_sync(*args, **kwargs) -> RoleDecision:
    """Blocking wrapper, for a container entrypoint that has no loop yet."""
    import asyncio

    return asyncio.run(resolve_role(*args, **kwargs))
