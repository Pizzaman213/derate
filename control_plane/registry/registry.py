"""The cluster's picture of itself.

Members are nodes a human admitted. Candidates are nodes discovery found and
nobody has accepted yet. They live in separate collections and are exposed
separately, so the UI can say "found on your network" without implying that
anything is going to be scheduled onto it. Discovery proposes; a human accepts.

Health and telemetry never delete. A node that stops answering is marked
unhealthy and keeps its last known numbers, because greyed-out real values with
a fault marker tell an operator more than a row that vanished.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from typing import AsyncIterator, Callable

from control_plane.contracts import (
    DEFAULT_GUARDRAIL,
    DeviceClass,
    NodeProfile,
    NodeState,
)

from .client import AgentClient, HttpAgentClient
from .config import (
    HEARTBEAT_INTERVAL_S,
    HEARTBEAT_MISSES_UNHEALTHY,
    HEARTBEAT_TIMEOUT_S,
    ROLE_COORDINATOR,
    RegistryConfig,
    TELEMETRY_INTERVAL_S,
)
from .enrollment import (
    DEFAULT_TTL_S,
    DEFAULT_USES,
    EnrollmentStore,
    EnrollmentToken,
)
from .errors import JoinRejected, NodeNotFound, ProbeFailed
from .identity import ClusterIdentity, load_or_create_identity
from .labels import normalize_label
from .net import normalize_agent_url
from .probe import probe_local
from .reach import (
    COORDINATOR,
    PEER_REACH_TIMEOUT_S,
    REACH_TIMEOUT_S,
    ReachLeg,
    dial,
    self_leg,
    summarize,
)
from .roster import load_roster, save_roster
from .serde import memory_used_pct, profile_from_dict, profile_to_dict, state_to_dict
from .telemetry import (
    TelemetryStore,
    TelemetrySample,
    allocatable_bytes,
    read_telemetry,
)

log = logging.getLogger(__name__)

SOURCE_MDNS = "mdns"
SOURCE_JOIN = "join"
SOURCE_MANUAL = "manual"
# Arrived holding an enrollment token, so it was admitted on arrival. The
# candidate record it labels exists only for the instant between the
# probe-back and the promotion -- `admit` pops it, and the member roster
# keeps profile and agent_url only -- so this never reaches disk. It is here
# so the record is not mislabelled as a plain "join" on the way past.
SOURCE_ENROLL = "enroll"


class Registry:
    """Implements RegistryPort, plus discovery, admission and telemetry.

    ``add_node`` and ``handle_join`` are async because both perform a bounded
    HTTP probe of the far side before they will believe anything it said. The
    three RegistryPort methods are synchronous in-memory reads, as frozen.
    """

    def __init__(
        self,
        config: RegistryConfig | None = None,
        local_profile: NodeProfile | None = None,
        role: str = ROLE_COORDINATOR,
        client: AgentClient | None = None,
        clock: Callable[[], float] = time.time,
        identity: ClusterIdentity | None = None,
        enrollment: EnrollmentStore | None = None,
    ) -> None:
        self.config = config or RegistryConfig()
        self._clock = clock
        self._role = role
        self._client = client or HttpAgentClient()

        self._identity = identity or load_or_create_identity(
            self.config.data_dir,
            cluster_id=self.config.cluster_id,
            token=self.config.token,
        )
        # Short-lived credentials the UI mints so a new machine can be
        # installed with one command and be a member when it finishes.
        # Shares the data dir with the identity, and the same 0600 rule.
        self._enrollment = enrollment or EnrollmentStore(
            self.config.data_dir, clock=self._clock
        )

        self._members: dict[str, NodeState] = {}
        self._candidates: dict[str, dict] = {}
        self._agent_urls: dict[str, str] = {}
        # node_id -> operator-chosen display name. Sparse on purpose: a node
        # nobody renamed has no entry, not an entry equal to its node_id.
        self._labels: dict[str, str] = {}
        self._misses: dict[str, int] = {}
        self._telemetry = TelemetryStore()
        self._tasks: list[asyncio.Task] = []
        self._running = False

        self._load_roster()

        self.local_profile = local_profile
        self.local_node_id = local_profile.node_id if local_profile else None
        if local_profile is not None:
            # We are a member of our own cluster from the first instant. Nobody
            # has to admit the machine they are looking at.
            self._members[local_profile.node_id] = NodeState(
                profile=local_profile,
                healthy=True,
                last_seen=self._clock(),
                memory_used=0,
                power_watts=0.0,
                temperature_c=0.0,
                utilization_pct=0.0,
            )
            self._agent_urls[local_profile.node_id] = (
                f"http://{local_profile.address}:{self.config.agent_port}"
            )

    # ------------------------------------------------------------------
    # RegistryPort
    # ------------------------------------------------------------------

    def list_nodes(self) -> list[NodeState]:
        return list(self._members.values())

    def get_node(self, node_id: str) -> NodeState | None:
        return self._members.get(node_id)

    def healthy_nodes(self) -> list[NodeState]:
        return [s for s in self._members.values() if s.healthy]

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    def role(self) -> str:
        """"coordinator" or "worker". Sticky for the process lifetime."""
        return self._role

    def cluster_token(self) -> str:
        return self._identity.token

    def cluster_id(self) -> str:
        return self._identity.cluster_id

    @property
    def identity(self) -> ClusterIdentity:
        return self._identity

    def agent_url(self, node_id: str) -> str | None:
        return self._agent_urls.get(node_id)

    def agent_urls(self, include_local: bool = False) -> dict[str, str]:
        """node_id -> agent URL, for every member with one.

        The telemetry collector uses this to find journals to drain. The local
        node is excluded by default: the coordinator reads its own journal
        directly rather than over HTTP, and collecting it twice would give it
        two cursors.
        """
        return {
            node_id: url
            for node_id, url in self._agent_urls.items()
            if url and (include_local or node_id != self.local_node_id)
        }

    # ------------------------------------------------------------------
    # Display names
    # ------------------------------------------------------------------

    def node_label(self, node_id: str) -> str | None:
        """The operator's name for this node, or None if nobody renamed it."""
        return self._labels.get(node_id)

    def node_labels(self) -> dict[str, str]:
        """Every label that is set. The gateway sends this with each payload."""
        return dict(self._labels)

    def set_node_label(self, node_id: str, label: object) -> str | None:
        """Rename a node for display. Returns the stored label, or None.

        Only members can be renamed: a candidate is a proposal, and naming one
        would leave the name stranded if it is never admitted. `node_id` is
        untouched -- deployments, links and routing keep referring to the node
        by the id they were written against, and this changes nothing but what
        the UI calls it.

        Raises NodeNotFound for an unknown id and ValueError for a name that
        cannot be rendered.
        """
        if node_id not in self._members:
            raise NodeNotFound(f"no member {node_id!r}")
        clean = normalize_label(label)
        previous = self._labels.get(node_id)
        if clean is None:
            self._labels.pop(node_id, None)
        else:
            self._labels[node_id] = clean
        if clean != previous:
            log.info(
                "node %s renamed: %s -> %s",
                node_id,
                previous or "(no name)",
                clean or "(no name)",
            )
            self._persist_roster()
        return clean

    # ------------------------------------------------------------------
    # Roster persistence (M-12)
    # ------------------------------------------------------------------

    def _load_roster(self) -> None:
        """Restore members and candidates from the last run, if any.

        Only static facts (profile, agent URL) come back this way. Health and
        telemetry are live questions the health/telemetry loops answer fresh
        within one round of restarting, so a restored member starts healthy
        and unmeasured rather than carrying a stale number as if it were current.
        """
        data = load_roster(self.config.data_dir)
        for node_id, entry in data["members"].items():
            if not isinstance(entry, dict):
                continue
            try:
                profile = profile_from_dict(entry["profile"])
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("dropping unreadable persisted member %s: %s", node_id, exc)
                continue
            self._members[node_id] = NodeState(
                profile=profile,
                healthy=True,
                last_seen=self._clock(),
                memory_used=0,
                power_watts=0.0,
                temperature_c=0.0,
                utilization_pct=0.0,
            )
            self._agent_urls[node_id] = str(entry.get("agent_url", ""))
            # A bad label on disk loses the name, never the node.
            try:
                label = normalize_label(entry.get("label"))
            except ValueError as exc:
                log.warning("dropping unusable label for %s: %s", node_id, exc)
                label = None
            if label:
                self._labels[node_id] = label
        self._candidates = {
            node_id: record
            for node_id, record in data["candidates"].items()
            if isinstance(record, dict)
        }
        if self._members or self._candidates:
            log.info(
                "restored %d member(s) and %d candidate(s) from %s",
                len(self._members),
                len(self._candidates),
                self.config.data_dir,
            )

    def _persist_roster(self) -> None:
        members = {
            node_id: {
                "profile": profile_to_dict(state.profile),
                "agent_url": self._agent_urls.get(node_id, ""),
                # Omitted rather than null when unset: an absent key reads as
                # "never renamed" to any build, including one older than this
                # field, which is the whole reason the roster is written key
                # by key instead of as a dumped dataclass.
                **(
                    {"label": self._labels[node_id]}
                    if self._labels.get(node_id)
                    else {}
                ),
            }
            for node_id, state in self._members.items()
        }
        save_roster(self.config.data_dir, members, self._candidates)

    # ------------------------------------------------------------------
    # Admission
    # ------------------------------------------------------------------

    def candidates(self) -> list[dict]:
        """Discovered, not yet admitted. Never scheduled onto."""
        return list(self._candidates.values())

    def _candidate_record(
        self, profile: NodeProfile, agent_url: str, source: str
    ) -> dict:
        now = self._clock()
        existing = self._candidates.get(profile.node_id)
        return {
            "node_id": profile.node_id,
            "hostname": profile.hostname,
            "address": profile.address,
            "gpu_name": profile.gpu_name,
            "gpu_count": profile.gpu_count,
            "device_class": profile.device_class.value,
            "addressable_memory": profile.addressable_memory,
            "agent_url": agent_url,
            "source": source,
            "first_seen": existing["first_seen"] if existing else now,
            "last_seen": now,
            "profile": profile_to_dict(profile),
        }

    async def handle_join(
        self, token: str | None, profile: NodeProfile, agent_url: str
    ) -> dict:
        """Coordinator side of the join protocol. Async: it probes back.

        A wrong, non-empty token is rejected before touching any state, so a
        bad joiner leaves no trace anywhere (Agent G maps JoinRejected to
        403). An absent token is different on purpose: with nothing to check
        it against, there is nothing to reject -- the joiner is routed into
        ``offer_candidate``, the same "discovered, not yet admitted" path
        mDNS uses. That is the whole mechanism behind the README's zero-config
        demo: two containers, same command, no shared secret between them
        yet, and the second one shows up for a human to admit instead of
        either being turned away or founding a second cluster.

        The admitted set is consulted before any of that: a node already a
        member gets "member" status back (with the cluster id) whether or
        not it presented a token, because that is how a node admitted with
        no token learns its cluster identity on its next poll. But a
        tokenless caller claiming to already be a member never gets to
        rewrite where that member's traffic is routed -- node_ids are
        slugified hostnames, advertised in the clear over mDNS, so they are
        not a secret an unauthenticated prober can be assumed not to know.
        Only a token holder can move the agent_url / profile on file for an
        existing member; a tokenless "re-join" is a status check, not a
        write.

        A third credential exists: an *enrollment* token from
        ``enrollment.EnrollmentStore``, minted in the UI for one install and
        expiring within the hour. It is the one thing that admits a new node
        without a click, because a human minted it and carried it to that
        machine deliberately -- the same position ``add_node`` already takes
        for a human typing an address. The response then carries
        ``cluster_token`` once, so the node keeps working after the enrollment
        token expires.
        """
        expected = self.cluster_token()
        has_token = bool(token)
        # Two credentials are accepted here and they mean different things. The
        # cluster token is permanent and buys candidacy, exactly as before. An
        # enrollment token is short-lived, was minted by a human in the UI for
        # this specific install, and buys membership outright -- minting it was
        # the admission decision. Anything that is neither is still rejected
        # with the same sentence, so this cannot be used to tell an expired
        # enrollment token apart from a wrong one.
        enrollment: EnrollmentToken | None = None
        if has_token and not hmac.compare_digest(str(token), expected):
            enrollment = self._enrollment.verify(token)
            if enrollment is None:
                log.warning(
                    "rejected join from %s (%s): wrong cluster token",
                    profile.node_id,
                    agent_url,
                )
                raise JoinRejected("invalid cluster token")

        # Believe the profile only after the far side answers as itself,
        # token or not. A joiner can claim any hardware it likes in the body.
        try:
            confirmed = await self.probe_remote(agent_url)
        except ProbeFailed as exc:
            log.warning("rejected join from %s: probe-back failed (%s)", agent_url, exc)
            raise JoinRejected("probe-back failed") from exc

        if confirmed.node_id != profile.node_id:
            log.warning(
                "rejected join from %s: claimed %s, answered as %s",
                agent_url,
                profile.node_id,
                confirmed.node_id,
            )
            raise JoinRejected("node_id mismatch on probe-back")

        if confirmed.node_id in self._members:
            if not has_token:
                # Confirming membership is fine -- that is the mechanism a
                # no-token-admitted worker uses to learn its cluster id --
                # but nothing here proves this caller IS that member, only
                # that it can answer a probe at the node_id it claims (which
                # anyone on the subnet can do once they have sniffed the
                # mDNS advertisement). Do not touch routing state: no
                # agent_url rewrite, no profile overwrite, no persistence.
                log.info(
                    "member %s re-confirmed with no token from %s "
                    "(routing state unchanged, valid token required to move it)",
                    confirmed.node_id,
                    agent_url,
                )
                return {
                    "node_id": confirmed.node_id,
                    "cluster_id": self.cluster_id(),
                    "status": "member",
                }
            # A restarted member, or a waiting candidate that was just
            # admitted, re-joining with the real token. Refresh what it
            # told us, keep it.
            self._agent_urls[confirmed.node_id] = agent_url
            state = self._members[confirmed.node_id]
            state.profile = confirmed
            state.last_seen = self._clock()
            state.healthy = True
            self._misses[confirmed.node_id] = 0
            self._persist_roster()
            result = {
                "node_id": confirmed.node_id,
                "cluster_id": self.cluster_id(),
                "status": "member",
            }
            if enrollment is not None:
                # It is still holding the enrollment token, which means the
                # cluster token we handed back last time never reached disk.
                # Hand it over again rather than let this member 403 itself
                # out of the cluster when the enrollment token expires.
                result["cluster_token"] = expected
            return result

        if not has_token:
            # No token at all: never a rejection, only a candidate -- and a
            # deliberately thin response. No cluster id, no member state:
            # a machine nobody has looked at yet cannot infer either.
            self.offer_candidate(confirmed, agent_url, source=SOURCE_JOIN)
            log.info(
                "candidate %s joined from %s with no token, awaiting admission",
                confirmed.node_id,
                agent_url,
            )
            return {"node_id": confirmed.node_id, "status": "candidate"}

        if enrollment is not None and enrollment.auto_admit:
            # Straight to member. `admit` is the same promotion the UI button
            # calls, reached through the candidate record so there is exactly
            # one place that builds a NodeState from a profile.
            self._candidates[confirmed.node_id] = self._candidate_record(
                confirmed, agent_url, SOURCE_ENROLL
            )
            self.admit(confirmed.node_id)
            self._enrollment.consume(enrollment.token_id)
            log.info(
                "admitted %s from %s on enrollment token %s",
                confirmed.node_id,
                agent_url,
                enrollment.token_id,
            )
            return {
                "node_id": confirmed.node_id,
                "cluster_id": self.cluster_id(),
                "status": "member",
                # The permanent token, handed over once, so this node survives
                # the enrollment token expiring. It is the only response that
                # ever carries it, and only to a node we just admitted.
                "cluster_token": expected,
            }

        self._candidates[confirmed.node_id] = self._candidate_record(
            confirmed, agent_url, SOURCE_JOIN
        )
        self._persist_roster()
        log.info("candidate %s joined from %s, awaiting admission", confirmed.node_id, agent_url)
        return {
            "node_id": confirmed.node_id,
            "cluster_id": self.cluster_id(),
            "status": "candidate",
        }

    def offer_candidate(
        self, profile: NodeProfile, agent_url: str, source: str = SOURCE_MDNS
    ) -> dict:
        """Record a peer as a candidate. Never a member.

        This is the "discovery proposes" half. Nothing calls admit for us.
        Used both for mDNS sightings and for a tokenless join (source="join"),
        which is the other production caller this used to be missing.
        """
        if profile.node_id in self._members:
            return self._candidates.get(profile.node_id, {})
        record = self._candidate_record(profile, agent_url, source)
        self._candidates[profile.node_id] = record
        self._persist_roster()
        return record

    def admit(self, node_id: str) -> NodeState:
        """Promote a candidate to a member. One click in the UI."""
        if node_id in self._members:
            return self._members[node_id]
        record = self._candidates.pop(node_id, None)
        if record is None:
            raise NodeNotFound(f"no candidate {node_id!r} to admit")
        profile = profile_from_dict(record["profile"])
        state = NodeState(
            profile=profile,
            healthy=True,
            last_seen=self._clock(),
            memory_used=0,
            power_watts=0.0,
            temperature_c=0.0,
            utilization_pct=0.0,
        )
        self._members[node_id] = state
        self._agent_urls[node_id] = record["agent_url"]
        self._misses[node_id] = 0
        self._persist_roster()
        log.info("admitted %s as a member", node_id)
        return state

    # ------------------------------------------------------------------
    # Enrollment tokens (the curl installer's credential)
    # ------------------------------------------------------------------

    def mint_enrollment(
        self,
        ttl_s: float = DEFAULT_TTL_S,
        uses: int | None = DEFAULT_USES,
        auto_admit: bool = True,
    ) -> EnrollmentToken:
        """Mint a short-lived credential for one install. Raises ValueError."""
        return self._enrollment.mint(ttl_s=ttl_s, uses=uses, auto_admit=auto_admit)

    def enrollments(self) -> list[dict]:
        """Live tokens, without the secrets. Expired ones are pruned on read."""
        return self._enrollment.public_list()

    def revoke_enrollment(self, token_id: str) -> bool:
        return self._enrollment.revoke(token_id)

    async def add_node(self, address: str) -> NodeState:
        """Manual add: probe the address, then admit it directly.

        Discovery will fail for somebody, and a dead end is worse than a form.
        A human typing an address is the admission decision, so this skips the
        candidate step rather than asking them to accept their own input.
        """
        agent_url = normalize_agent_url(address, self.config.agent_port)
        profile = await self.probe_remote(agent_url)
        self._candidates[profile.node_id] = self._candidate_record(
            profile, agent_url, SOURCE_MANUAL
        )
        return self.admit(profile.node_id)

    def remove_node(self, node_id: str) -> None:
        """Forget a node entirely. The only path that drops telemetry.

        Raises NodeNotFound for an unknown id so the gateway's 404 branch is
        reachable; silently 200-ing a typo'd delete hides the operator error.
        """
        was_member = self._members.pop(node_id, None) is not None
        was_candidate = self._candidates.pop(node_id, None) is not None
        if not was_member and not was_candidate:
            raise NodeNotFound(f"no member or candidate {node_id!r}")
        self._agent_urls.pop(node_id, None)
        self._labels.pop(node_id, None)
        self._misses.pop(node_id, None)
        self._telemetry.drop(node_id)
        self._persist_roster()

    def dismiss_candidate(self, node_id: str) -> None:
        """Reject a proposal without admitting it."""
        if self._candidates.pop(node_id, None) is not None:
            self._persist_roster()

    # ------------------------------------------------------------------
    # Probing
    # ------------------------------------------------------------------

    async def probe_remote(self, agent_url: str) -> NodeProfile:
        """GET /agent/profile on a peer. Raises ProbeFailed."""
        payload = await self._client.get_json(
            f"{agent_url.rstrip('/')}/agent/profile", timeout=HEARTBEAT_TIMEOUT_S
        )
        try:
            return profile_from_dict(payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise ProbeFailed(f"{agent_url} returned an unusable profile: {exc}") from exc

    # ------------------------------------------------------------------
    # Reachability
    # ------------------------------------------------------------------

    async def _peer_leg(self, source: str, target: str, url: str) -> ReachLeg:
        """Ask `source`'s agent to dial `target`, and report what it found.

        Two failures are possible and they are not the same finding: we could
        not reach `source` to ask it, or `source` answered that it could not
        reach `target`. The first is reported as a failed leg from the
        coordinator -- blaming source→target for a coordinator→source problem
        would send an operator to look at the wrong cable.
        """
        source_url = self._agent_urls.get(source)
        if not source_url:
            return ReachLeg(
                source=source,
                target=target,
                url=url,
                ok=False,
                error=f"No agent address on file for {source}, so it cannot be asked.",
            )
        try:
            payload = await self._client.post_json(
                f"{source_url.rstrip('/')}/agent/reach",
                {"url": url},
                PEER_REACH_TIMEOUT_S,
                {"X-Derate-Token": self.cluster_token()},
            )
        except Exception as exc:
            return ReachLeg(
                source=COORDINATOR,
                target=source,
                url=source_url,
                ok=False,
                error=(
                    f"Could not ask {source} to dial {target}: {exc}. "
                    f"Whether {source} can reach {target} is still unknown."
                ),
            )
        if not isinstance(payload, dict):
            return ReachLeg(
                source=source, target=target, url=url, ok=False,
                error=f"{source} answered with something that was not a probe result.",
            )
        answered = payload.get("answered_as")
        return ReachLeg(
            source=source,
            target=target,
            url=str(payload.get("url") or url),
            ok=bool(payload.get("ok")),
            ms=payload.get("ms") if payload.get("ok") else None,
            error=payload.get("error") or None,
            answered_as=str(answered) if answered else None,
        )

    async def check_reach(self, a: str, b: str) -> dict:
        """Can these two nodes reach each other? Every direction, separately.

        Cheap and non-disruptive, unlike a link measurement: four health
        checks at most, none of which touches the fabric this is asking about
        in any way a running deployment would notice. That is the point --
        an operator should not have to saturate an interconnect for a minute
        to find out whether a node answers at all.

        Raises NodeNotFound for an id that is not a member, so a typo comes
        back as a 404 rather than as an unreachable machine.
        """
        for node_id in (a, b):
            if node_id not in self._members:
                raise NodeNotFound(f"no member {node_id!r}")

        legs: list[ReachLeg] = []
        for node_id in (a, b):
            url = self._agent_urls.get(node_id, "")
            if node_id == self.local_node_id:
                legs.append(self_leg(node_id, url))
            elif not url:
                legs.append(
                    ReachLeg(
                        source=COORDINATOR, target=node_id, url="", ok=False,
                        error=f"No agent address on file for {node_id}.",
                    )
                )
            else:
                legs.append(
                    await dial(
                        self._client.get_json,
                        COORDINATOR,
                        node_id,
                        url,
                        REACH_TIMEOUT_S,
                    )
                )

        # The node-to-node legs, skipping any whose source is this process --
        # the coordinator's own leg above already IS that dial, and running it
        # twice would report the same probe as two independent findings.
        for source, target in ((a, b), (b, a)):
            if source == self.local_node_id or source == target:
                continue
            url = self._agent_urls.get(target, "")
            if not url:
                legs.append(
                    ReachLeg(
                        source=source, target=target, url="", ok=False,
                        error=f"No agent address on file for {target}.",
                    )
                )
                continue
            legs.append(await self._peer_leg(source, target, url))

        ok, sentence = summarize(legs)
        return {
            "a": a,
            "b": b,
            "ok": ok,
            "summary": sentence,
            "checked_at": self._clock(),
            "legs": [leg.as_dict() for leg in legs],
        }

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def check_health(self, node_id: str) -> bool:
        """One health probe. Healthy means an answer inside the timeout."""
        if node_id == self.local_node_id:
            return True  # we are the process asking
        url = self._agent_urls.get(node_id)
        if not url:
            return False
        try:
            await self._client.get_json(
                f"{url.rstrip('/')}/agent/health", timeout=HEARTBEAT_TIMEOUT_S
            )
            return True
        except ProbeFailed:
            return False

    def record_health(self, node_id: str, ok: bool) -> None:
        """Three consecutive failures marks unhealthy. One success clears it.

        Never deletes the node or its last telemetry.
        """
        state = self._members.get(node_id)
        if state is None:
            return
        if ok:
            self._misses[node_id] = 0
            state.last_seen = self._clock()
            if not state.healthy:
                log.info("node %s is healthy again", node_id)
            state.healthy = True
            return

        misses = self._misses.get(node_id, 0) + 1
        self._misses[node_id] = misses
        if misses >= HEARTBEAT_MISSES_UNHEALTHY and state.healthy:
            state.healthy = False
            log.warning(
                "node %s unhealthy after %d consecutive misses; keeping its last "
                "telemetry from %.0fs ago",
                node_id,
                misses,
                self._clock() - state.last_seen,
            )

    async def health_round(self) -> None:
        """One pass over every member, concurrently."""
        node_ids = list(self._members)
        if not node_ids:
            return
        results = await asyncio.gather(
            *(self.check_health(n) for n in node_ids), return_exceptions=True
        )
        for node_id, ok in zip(node_ids, results):
            self.record_health(node_id, ok is True)

    # ------------------------------------------------------------------
    # Telemetry
    # ------------------------------------------------------------------

    def apply_sample(self, node_id: str, sample: TelemetrySample) -> None:
        state = self._members.get(node_id)
        if state is None:
            return
        previous = self._telemetry.latest(node_id)
        if (
            previous is not None
            and sample.swap_used > previous.swap_used
            and sample.host_memory_total
        ):
            # On unified memory a model that overcommits does not fail to
            # allocate, it pushes the OS into swap and takes throughput with it.
            log.warning(
                "node %s is swapping (%.1f GiB, up %.1f GiB): the unified pool "
                "is overcommitted and decode throughput will collapse",
                node_id,
                sample.swap_used / 1024**3,
                (sample.swap_used - previous.swap_used) / 1024**3,
            )
        self._telemetry.record(node_id, sample)
        state.memory_used = sample.memory_used
        state.power_watts = sample.power_watts
        state.temperature_c = sample.temperature_c
        state.utilization_pct = sample.utilization_pct
        state.last_seen = sample.ts
        state.sample_ts = sample.ts

    async def _sample_node(self, node_id: str) -> None:
        state = self._members.get(node_id)
        if state is None:
            return
        if node_id == self.local_node_id and self.local_profile is not None:
            sample = await read_telemetry(self.local_profile)
            if sample is not None:
                self.apply_sample(node_id, sample)
            return

        url = self._agent_urls.get(node_id)
        if not url:
            return
        try:
            payload = await self._client.get_json(
                f"{url.rstrip('/')}/agent/telemetry", timeout=HEARTBEAT_TIMEOUT_S
            )
        except ProbeFailed:
            return  # health loop owns the verdict; keep the last sample
        if not payload.get("available", True):
            return
        self.apply_sample(
            node_id,
            TelemetrySample(
                ts=float(payload.get("ts") or self._clock()),
                memory_used=int(payload.get("memory_used", 0)),
                memory_total=int(payload.get("memory_total", 0)),
                power_watts=float(payload.get("power_w", 0.0)),
                temperature_c=float(payload.get("temp_c", 0.0)),
                utilization_pct=float(payload.get("util_pct", 0.0)),
                gpu_memory_used=int(payload.get("gpu_memory_used", 0)),
                gpu_process_count=int(payload.get("gpu_process_count", 0)),
                host_memory_total=int(payload.get("host_memory_total", 0)),
                host_memory_available=int(payload.get("host_memory_available", 0)),
                swap_used=int(payload.get("swap_used", 0)),
            ),
        )

    async def telemetry_round(self) -> None:
        node_ids = list(self._members)
        if not node_ids:
            return
        await asyncio.gather(
            *(self._sample_node(n) for n in node_ids), return_exceptions=True
        )

    def available_memory(self, node_id: str, guardrail: float = DEFAULT_GUARDRAIL) -> int:
        """Live allocatable bytes on one node. What Agent D should gate on.

        ``NodeProfile.usable_memory`` is the static ceiling this hardware could
        ever spend. On a Spark that ceiling is not reachable while an operating
        system is running in the same pool, so a fit check against it approves
        models that will not load. This is the same question asked of the
        machine as it is right now.
        """
        state = self._members.get(node_id)
        if state is None:
            return 0
        return allocatable_bytes(
            state.profile,
            self._telemetry.latest(node_id),
            guardrail=guardrail,
            host_reserve=self.config.host_memory_reserve,
        )

    def memory_report(self, node_id: str) -> dict | None:
        """The full memory picture for one node, for the UI and for a refusal.

        Separating these is the point: on GB10 the gap between the pool figure
        and the GPU figure is the operating system, and an operator staring at a
        refusal needs to see that it is the desktop, not the model, that is in
        the way.
        """
        state = self._members.get(node_id)
        if state is None:
            return None
        sample = self._telemetry.latest(node_id)
        profile = state.profile
        return {
            "node_id": node_id,
            "device_class": profile.device_class.value,
            "unified_memory": profile.device_class is DeviceClass.GB10,
            "addressable": profile.addressable_memory,
            "static_ceiling": profile.usable_memory(),
            "pool_used": state.memory_used,
            "gpu_used": sample.gpu_memory_used if sample else 0,
            "host_used": sample.host_memory_used if sample else 0,
            "host_available": sample.host_memory_available if sample else 0,
            "host_reserve": self.config.host_memory_reserve,
            "swap_used": sample.swap_used if sample else 0,
            "allocatable": self.available_memory(node_id),
            "stale": not state.healthy,
        }

    def history(self, node_id: str, seconds: int = 60) -> list[dict]:
        """Recent samples for one node, oldest first."""
        return self._telemetry.history(node_id, seconds, now=self._clock())

    def snapshot(self) -> dict:
        """The current picture, in the shape Agent G wraps into an SSE event.

        Cluster throughput and deployment rows are the gateway's to add; the
        registry contributes nodes and the physical totals it actually measures.
        """
        nodes = []
        total_power = 0.0
        healthy = 0
        for state in self._members.values():
            if state.healthy:
                healthy += 1
                total_power += state.power_watts
            sample = self._telemetry.latest(state.profile.node_id)
            nodes.append(
                {
                    "node_id": state.profile.node_id,
                    "power_w": round(state.power_watts, 1),
                    "temp_c": round(state.temperature_c, 1),
                    "memory_used_pct": memory_used_pct(state),
                    "util_pct": round(state.utilization_pct, 1),
                    "healthy": state.healthy,
                    "last_seen": state.last_seen,
                    "sample_ts": state.sample_ts or None,
                    # The unified-memory numbers. On a discrete node gpu_used
                    # equals the pool figure and allocatable is simply headroom.
                    "gpu_used": sample.gpu_memory_used if sample else 0,
                    "allocatable": self.available_memory(state.profile.node_id),
                    "swap_used": sample.swap_used if sample else 0,
                }
            )
        return {
            "ts": self._clock(),
            "cluster": {
                "total_power_w": round(total_power, 1),
                "node_count": len(self._members),
                "healthy_nodes": healthy,
                "candidates": len(self._candidates),
            },
            "nodes": nodes,
        }

    async def snapshots(self, interval: float = TELEMETRY_INTERVAL_S) -> AsyncIterator[dict]:
        """Yield the current snapshot once per second, forever.

        Deadline-based rather than sleep(1), so a slow consumer does not make
        the stream drift further behind real time with every tick.
        """
        deadline = asyncio.get_running_loop().time()
        while True:
            yield self.snapshot()
            deadline += interval
            delay = deadline - asyncio.get_running_loop().time()
            if delay < 0:
                deadline = asyncio.get_running_loop().time()
                delay = 0
            await asyncio.sleep(delay)

    # ------------------------------------------------------------------
    # Background loops
    # ------------------------------------------------------------------

    async def _loop(self, fn: Callable, interval: float, name: str) -> None:
        while self._running:
            started = asyncio.get_running_loop().time()
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a bad round must not kill the loop
                log.exception("%s round failed: %s", name, exc)
            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(0.0, interval - elapsed))

    async def start(self) -> None:
        """Start the health and telemetry loops."""
        if self._running:
            return
        self._running = True
        # Prime once before the loops start. Otherwise the first snapshot goes
        # out before the first poll lands and the UI opens on a row of zeros,
        # which reads as an idle node rather than an unpolled one.
        try:
            await self.telemetry_round()
        except Exception as exc:  # a failed prime is not a failed start
            log.debug("initial telemetry prime failed: %s", exc)
        self._tasks = [
            asyncio.create_task(
                self._loop(self.health_round, HEARTBEAT_INTERVAL_S, "health"),
                name="registry-health",
            ),
            asyncio.create_task(
                self._loop(self.telemetry_round, TELEMETRY_INTERVAL_S, "telemetry"),
                name="registry-telemetry",
            ),
        ]

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------

    def nodes_payload(self) -> list[dict]:
        """Members, serialised. What GET /api/nodes returns."""
        payload = []
        for state in self._members.values():
            row = state_to_dict(state)
            row["role"] = (
                ROLE_COORDINATOR
                if state.profile.node_id == self.local_node_id
                and self._role == ROLE_COORDINATOR
                else "worker"
            )
            row["agent_url"] = self._agent_urls.get(state.profile.node_id)
            payload.append(row)
        return payload
