"""Role resolution and startup.

Browse, join if somebody is already coordinating, otherwise coordinate. No
election, no failover: first one wins and the role is sticky for the process
lifetime. If the coordinator dies, workers keep running and the UI goes dark
until it comes back. That is a deliberate scope decision, not an oversight.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import random
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
from .identity import load_or_create_identity
from .net import normalize_agent_url
from .serde import profile_to_dict

log = logging.getLogger(__name__)

JOIN_TIMEOUT_S = 5.0

# How often a worker-in-waiting (found a coordinator, not admitted yet) polls
# join again. Jittered so a fleet of candidates admitted around the same time
# does not all hammer the coordinator on the same tick. A wrong, non-empty
# token backs off much slower -- that is much more likely a foreign cluster on
# the same subnet than a typo about to fix itself, and hammering it teaches us
# nothing new each time.
REJOIN_INTERVAL_S = 15.0
REJOIN_JITTER_S = 5.0
REJOIN_WRONG_TOKEN_INTERVAL_S = 60.0


@dataclass(frozen=True)
class RoleDecision:
    role: str
    coordinator_url: str | None
    joined: bool
    reason: str
    cluster_id: str | None = None
    # "candidate" or "member" (a token the coordinator accepted), "rejected"
    # (a coordinator answered but the token did not match), or None (nobody
    # to join at all), as the coordinator saw it.
    status: str | None = None
    # Set only when we were admitted on an enrollment token: the permanent
    # cluster token, handed over once so this node keeps working after the
    # enrollment token expires. The caller must persist it.
    cluster_token: str | None = None
    # Which of the credentials this node holds the coordinator actually took.
    # None when no join happened at all.
    token_used: str | None = None

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


async def join_with_held_credentials(
    join: Callable[..., Awaitable[dict]],
    url: str,
    config: RegistryConfig,
    profile: NodeProfile,
    agent_url: str,
) -> tuple[dict, str | None]:
    """Present what this machine holds, and let the coordinator pick.

    A node can legitimately be holding two credentials at once: the enrollment
    token it was installed with -- which ``install.sh`` bakes into the container
    environment permanently, so it is presented on every restart forever -- and
    the permanent cluster token the coordinator handed it in exchange for that
    one. After the first hour the enrollment token is spent, and because
    ``handle_join`` checks the token *before* it checks membership, presenting
    it 403s the node out of a cluster it is already a member of.

    Preferring the stored token instead would break the opposite case, which is
    just as real: a *fresh* enrollment token carried to this machine to re-home
    it onto a different cluster, where the stored one is the wrong cluster's and
    will be refused. Nothing on this side can tell those apart -- so try the
    deliberate one first and fall back to the held one, and let the far end
    settle it.

    Returns the join response and the token that earned it, so the caller can
    stop paying for the rejection next time.
    """
    try:
        return await join(url, config.token, profile, agent_url), config.token
    except JoinRejected:
        fallback = config.fallback_token
        if not fallback or fallback == config.token:
            raise
        log.info(
            "%s rejected the token we were given; retrying with the permanent "
            "cluster token this node adopted earlier",
            url,
        )
        return await join(url, fallback, profile, agent_url), fallback


def adopt_cluster_token(config: RegistryConfig, result: dict | None) -> RegistryConfig:
    """Keep the permanent token a coordinator handed back, and use it from now on.

    A node admitted on an enrollment token holds a credential that expires
    within the hour. ``Registry.handle_join`` therefore returns the permanent
    cluster token with the admission, exactly once. Persisting it here is not
    housekeeping: ``handle_join`` checks the token *before* it checks
    membership, so a worker that kept presenting a spent enrollment token
    would eventually 403 itself out of a cluster it is already a member of.

    Returns the config to use from here on -- the same object when there was
    nothing to adopt. Writing goes through ``load_or_create_identity``, which
    already prefers an explicit token and creates the file at 0600.
    """
    token = (result or {}).get("cluster_token")
    if not token or token == config.token:
        return config
    load_or_create_identity(
        config.data_dir,
        cluster_id=(result or {}).get("cluster_id") or config.cluster_id,
        token=str(token),
    )
    log.info(
        "adopted the cluster token from the coordinator; the enrollment token "
        "this node installed with is no longer needed"
    )
    return dataclasses.replace(config, token=str(token))


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
            ROLE_COORDINATOR, None, False, "DERATE_ROLE=coordinator"
        )

    # An explicit join address is a deliberate instruction for a node on another
    # subnet. Skip discovery, and fail loudly rather than quietly starting a
    # second cluster next to the one the operator named.
    if config.join_address:
        url = normalize_agent_url(config.join_address, config.coordinator_port)
        try:
            result, token_used = await join_with_held_credentials(
                join, url, config, profile, agent_url
            )
        except JoinRejected as exc:
            # A rejection proves the NAMED coordinator exists and answered --
            # the same fact the discovered path treats as "stay a worker and
            # keep retrying", and it must be treated the same way here.
            # Crashing instead is not the loud failure the comment above
            # wants: on first boot the join races our own agent app (the
            # coordinator's probe-back lands before /agent/* is listening),
            # so even a perfectly configured node 403s once. The rejoin loop
            # retries after the agent is up; a wrong token stays visible in
            # the log on every retry. ProbeFailed (the named address not
            # answering at all) still fails loudly -- that is a typo, not a
            # cluster.
            reason = (
                f"DERATE_JOIN={config.join_address} answered but rejected "
                f"the join ({exc}); staying a worker and retrying rather than "
                "dying -- the agent app was not yet listening for the "
                "probe-back on a first boot, and admission or a token fix "
                "makes the next attempt succeed"
            )
            log.warning("role resolution: %s", reason)
            return RoleDecision(ROLE_WORKER, url, False, reason, status="rejected")
        return RoleDecision(
            ROLE_WORKER,
            url,
            True,
            f"DERATE_JOIN={config.join_address}",
            cluster_id=(result or {}).get("cluster_id"),
            status=(result or {}).get("status"),
            cluster_token=(result or {}).get("cluster_token"),
            token_used=token_used,
        )

    peers = await browse(MDNS_BROWSE_SECONDS, profile.node_id)
    coordinators = [p for p in peers if p.is_coordinator]
    rejected_url: str | None = None

    for peer in coordinators:
        url = f"http://{peer.address}:{config.coordinator_port}"
        try:
            result, token_used = await join_with_held_credentials(
                join, url, config, profile, agent_url
            )
            return RoleDecision(
                ROLE_WORKER,
                url,
                True,
                f"joined coordinator {peer.node_id} at {url}",
                cluster_id=(result or {}).get("cluster_id") or peer.cluster_id,
                status=(result or {}).get("status"),
                cluster_token=(result or {}).get("cluster_token"),
                token_used=token_used,
            )
        except JoinRejected:
            # Someone else's cluster on the same subnet -- or the same
            # cluster with a token we do not (yet) have. Either way there IS
            # a coordinator here, so this must never fall through to us
            # starting our own: that would split-brain a subnet that already
            # has one. Keep browsing the rest in case another peer accepts us.
            rejected_url = url
            log.warning(
                "coordinator %s at %s rejected our token; not our cluster (or "
                "we have not been admitted with it yet)",
                peer.node_id,
                url,
            )
        except ProbeFailed as exc:
            log.warning("coordinator %s advertised but did not answer: %s", peer.node_id, exc)

    if rejected_url is not None:
        # A coordinator exists and answered. Stay a worker-in-waiting: keep
        # serving /agent/*, keep advertising, and let the caller re-poll join
        # on an interval so that once a human fixes the token or admits us
        # from the other side, the very next attempt succeeds.
        reason = (
            "a coordinator responded but our token did not match; staying a "
            "worker rather than starting a second cluster on this subnet -- "
            "will keep retrying"
        )
        log.warning("role resolution: %s", reason)
        return RoleDecision(ROLE_WORKER, rejected_url, False, reason, status="rejected")

    if config.role == ROLE_WORKER:
        # Forced worker with nobody to serve. Stay a worker and keep serving
        # /agent/*, so a coordinator starting later can find and admit us.
        return RoleDecision(
            ROLE_WORKER,
            None,
            False,
            "DERATE_ROLE=worker but no coordinator answered; waiting to be found",
        )

    if coordinators:
        reason = "coordinators advertised but none answered; becoming coordinator"
    else:
        reason = f"no coordinator found in {MDNS_BROWSE_SECONDS:.0f}s; becoming coordinator"
    log.info("role resolution: %s", reason)
    return RoleDecision(ROLE_COORDINATOR, None, False, reason)


def resolve_role_sync(*args, **kwargs) -> RoleDecision:
    """Blocking wrapper, for a container entrypoint that has no loop yet."""
    return asyncio.run(resolve_role(*args, **kwargs))


@dataclass(frozen=True)
class Announcement:
    """The outcome of one re-announcement. Never a role decision."""

    ok: bool
    coordinator_url: str | None
    result: dict | None
    reason: str
    token_used: str | None = None


async def reannounce(
    config: RegistryConfig,
    profile: NodeProfile,
    agent_url: str,
    coordinator_url: str | None,
    browse: Callable[..., Awaitable[list[DiscoveredPeer]]] = browse_async,
    join: Callable[..., Awaitable[dict]] = post_join,
) -> Announcement:
    """Tell the coordinator where we are and what we are, again.

    ``resolve_role`` runs once and the role is sticky, which is deliberate --
    there is no election here and this function does not add one. It is the
    other missing half: after a node is admitted, steady state was entirely
    coordinator-pull, so a member had no way to say anything ever again. Three
    ordinary events therefore had no recovery path:

    * the node's address changed, and the coordinator kept dialling the old one
      until the node's container happened to restart;
    * the coordinator was restarted with an empty roster, and every worker sat
      there healthy and unlisted;
    * the coordinator was replaced by another machine, and the cluster had to be
      rebuilt by hand.

    All three are the same fix: re-run the join. ``Registry.handle_join`` with a
    valid token already moves ``agent_url``, refreshes the profile, clears the
    miss counter and persists, and it is idempotent -- so this needs nothing new
    on the coordinator side.

    It is not a way to fake health, either. ``handle_join`` probes back before it
    accepts anything, so a node that can reach the coordinator but cannot be
    reached *back* has its announcement rejected and stays unhealthy. The
    direction that gets asserted is the direction that was proved.

    Tries the coordinator we know about first, then anything mDNS can find. It
    never decides to coordinate: a worker whose coordinator is gone waits for one
    to come back rather than splitting the subnet in two.
    """
    tried: list[str] = []

    async def attempt(url: str) -> Announcement | None:
        tried.append(url)
        try:
            result, token_used = await join_with_held_credentials(
                join, url, config, profile, agent_url
            )
        except JoinRejected as exc:
            log.warning("re-announcement to %s was rejected: %s", url, exc)
            return None
        except ProbeFailed as exc:
            log.debug("re-announcement to %s did not land: %s", url, exc)
            return None
        return Announcement(
            True, url, result, f"re-announced to {url}", token_used=token_used
        )

    if coordinator_url:
        landed = await attempt(coordinator_url)
        if landed is not None:
            return landed

    # The known coordinator did not answer, or turned us away. Look for one:
    # this is how a *replacement* coordinator is found, which is the whole
    # reason the browse is here rather than just retrying one address.
    try:
        peers = await browse(MDNS_BROWSE_SECONDS, profile.node_id)
    except Exception as exc:  # a browse failure is not a reason to stop trying
        log.debug("re-announcement browse failed: %s", exc)
        peers = []

    for peer in peers:
        if not peer.is_coordinator:
            continue
        url = f"http://{peer.address}:{config.coordinator_port}"
        if url in tried:
            continue
        landed = await attempt(url)
        if landed is not None:
            return landed

    return Announcement(
        False,
        coordinator_url,
        None,
        f"no coordinator accepted a re-announcement (tried {', '.join(tried) or 'nothing'})",
    )


async def rejoin_until_admitted(
    decision: RoleDecision,
    config: RegistryConfig,
    profile: NodeProfile,
    agent_url: str,
    join: Callable[..., Awaitable[dict]] = post_join,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    jitter: Callable[[], float] = random.random,
    should_continue: Callable[[], bool] = lambda: True,
) -> dict | None:
    """The worker-in-waiting loop: keep polling one coordinator until it lets
    us in, then return the join response that finally carried "member".

    Called after ``resolve_role`` comes back a worker that is not yet a
    member (``status`` is "candidate" or "rejected") -- the agent app is
    already up and advertising by then, so admission from the UI (or a fixed
    token) needs nothing more from this node than the next poll landing.
    A wrong token backs off much slower than a plain "not admitted yet", and
    logs loudly every attempt, but it retries forever rather than ever
    deciding to coordinate its own cluster: this loop's only exit is a
    coordinator saying "member", or the caller stopping the node.
    """
    url = decision.coordinator_url
    if url is None:
        return None
    while should_continue():
        try:
            result, token_used = await join_with_held_credentials(
                join, url, config, profile, agent_url
            )
        except JoinRejected as exc:
            log.warning(
                "still not admitted by %s (%s); retrying in %.0fs",
                url,
                exc,
                REJOIN_WRONG_TOKEN_INTERVAL_S,
            )
            await sleep(REJOIN_WRONG_TOKEN_INTERVAL_S)
            continue
        except ProbeFailed as exc:
            log.debug("coordinator %s unreachable (%s); retrying", url, exc)
            await sleep(REJOIN_INTERVAL_S + jitter() * REJOIN_JITTER_S)
            continue

        # Before the status check: an admission carries the permanent token
        # with it, and the next iteration of this loop must present that one.
        # Same for a credential that only worked as the fallback -- there is no
        # reason to keep paying for the rejection that got us here.
        if token_used and token_used != config.token:
            config = dataclasses.replace(config, token=token_used)
        config = adopt_cluster_token(config, result)

        if (result or {}).get("status") == "member":
            log.info(
                "admitted into cluster %s; no longer waiting",
                (result or {}).get("cluster_id"),
            )
            return result
        await sleep(REJOIN_INTERVAL_S + jitter() * REJOIN_JITTER_S)
    return None
