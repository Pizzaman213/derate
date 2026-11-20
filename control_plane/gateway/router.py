"""Routing table: policy resolution, live target state, weight refresh.

Weights are recomputed on a 60 second cadence so a thermally throttling node
sheds share on its own. Outstanding counts and the admitting flag are refreshed
on every selection, so a critical memory event takes effect on the next request
rather than at the next weight refresh.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, replace

from control_plane.contracts import (
    Deployment,
    Provider,
    ProviderModel,
    RouteTarget,
    RoutingConfig,
    RoutingPolicy,
    TargetKind,
)

from . import policies
from .policies import PolicyState, RouteContext
from .routing_store import RoutingState, RoutingStore
from .settings import GatewaySettings
from .stats import StatsRegistry
from .strength import strength_spread
from .targets import ROUTABLE_STATES, TargetIndex, apply_scores, build_index

# The only keys a route limit may set.
LIMIT_KEYS = frozenset({"rpm", "tpm"})
#: The policy whose weights an operator's shares replace.
SHARES_POLICY = RoutingPolicy.WEIGHTED_CAPACITY

log = logging.getLogger("gateway.router")


@dataclass
class Selection:
    config: RoutingConfig
    target: RouteTarget
    deployment: Deployment | None = None
    provider: Provider | None = None
    model: ProviderModel | None = None


class Router:
    def __init__(
        self,
        *,
        deployments,
        providers,
        registry,
        stats: StatsRegistry,
        admission,
        settings: GatewaySettings,
        breaker=None,
        clock=time.monotonic,
        events=None,
        store: RoutingStore | None = None,
    ) -> None:
        self._events = events
        self._deployments = deployments
        self._providers = providers
        self._registry = registry
        self._stats = stats
        self._admission = admission
        self._settings = settings
        self._breaker = breaker
        self._clock = clock

        self._index = TargetIndex()
        self._index_at = 0.0
        self._scored_at = 0.0
        # target_id -> (strength, weight, zero_weight_reason), held between
        # weight refreshes
        self._scores: dict[str, tuple[float, float, str | None]] = {}
        # Operator overrides and route limits. Loaded from routing.json when a
        # store is given, so a restart keeps them; tests run without one.
        self._store = store
        state = store.load() if store is not None else RoutingState.empty()
        self._overrides: dict[str, RoutingPolicy] = dict(state.overrides)
        self._limits: dict[str, dict[str, int]] = {
            k: dict(v) for k, v in state.limits.items() if v
        }
        self._shares: dict[str, dict[str, int]] = {
            k: dict(v) for k, v in state.shares.items() if v
        }
        self._state: dict[str, PolicyState] = {}
        # served_name -> the reason auto_policy gave its most recent verdict.
        # Read-only outside this class; write happens as a side effect of
        # config_for computing the policy it needs anyway.
        self._auto_reasons: dict[str, str] = {}
        # served_name -> "local" | "spilled", the kind of the target the last
        # actual LOCAL_FIRST selection landed on. Only ``select`` writes this;
        # the /api/routing read path only ever reads it.
        self._flow: dict[str, str] = {}
        # served_name -> when it last had at least one eligible target. The
        # parking lot uses this to tell "its node just died" from "it has never
        # served", which get different answers.
        self._last_eligible: dict[str, float] = {}
        self._task: asyncio.Task | None = None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        self.rebuild(force_scores=True)
        self._task = asyncio.create_task(self._refresh_loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _refresh_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._settings.weight_refresh_interval_s)
                self.rebuild(force_scores=True)
            except asyncio.CancelledError:
                raise
            except Exception:  # a bad port must not kill the refresh loop
                log.exception("routing weight refresh failed")

    # -- index -------------------------------------------------------------

    def _safe(self, fn, default):
        """A port that raises degrades that source rather than the gateway."""
        try:
            return fn()
        except Exception as exc:
            name = getattr(fn, "__name__", str(fn))
            log.exception("routing source unavailable: %s", name)
            if self._events is not None:
                self._events.routing_source_failed(name, type(exc).__name__)
            return default

    def rebuild(self, *, force_scores: bool = False) -> TargetIndex:
        now = time.monotonic()
        deployments = self._safe(self._deployments.list, [])
        # `servable` carries only the models the operator switched on, so the
        # allowlist lands in the target index -- and therefore in /v1/models,
        # routing, /api/topology and the chat picker -- at one seam rather than
        # being re-applied at each. Duck-typed like every other optional port
        # operation here: a port without it serves its whole catalogue, which
        # is what every port did before the allowlist existed.
        providers = self._safe(getattr(self._providers, "servable", self._providers.list), [])
        nodes = self._safe(self._registry.list_nodes, [])

        index = build_index(
            deployments=deployments,
            providers=providers,
            nodes=nodes,
            stats=self._stats,
            settings=self._settings,
            is_blocked=self._admission.is_blocked,
            runtime_key=getattr(self._deployments, "runtime_api_key", None),
            remote_block=self._remote_block(providers),
        )

        due = force_scores or (
            now - self._scored_at >= self._settings.weight_refresh_interval_s
        )
        known = set(index.raw_strength)
        # A target we have never scored must be scored now, otherwise a newly
        # launched replica would sit at zero weight for up to a minute.
        if due or not known.issubset(self._scores.keys()):
            apply_scores(index, self._settings)
            self._scores = {
                t.target_id: (
                    t.strength,
                    t.weight,
                    index.zero_weight_reason.get(t.target_id),
                )
                for targets in index.targets.values()
                for t in targets
            }
            self._scored_at = now
        else:
            for targets in index.targets.values():
                for t in targets:
                    strength, weight, reason = self._scores.get(
                        t.target_id, (0.0, 0.0, None)
                    )
                    t.strength = strength
                    t.weight = weight
                    index.zero_weight_reason[t.target_id] = reason
        # A containment switch outranks scoring: a blocked remote says why.
        for tid in index.remotes:
            if index.zero_weight_reason.get(tid) is None:
                block = self._remote_block_reason
                if block is not None:
                    index.zero_weight_reason[tid] = block

        self._index = index
        self._index_at = now
        # A benched target that has left the routing table must not keep its
        # bench. Otherwise stopping and relaunching a deployment under the same
        # id brings it back still benched, with nothing left in the index that
        # could ever clear it.
        if self._breaker is not None:
            self._breaker.retain(
                t.target_id for targets in index.targets.values() for t in targets
            )
        for name in index.targets:
            state = self._state.setdefault(name, PolicyState())
            state.capacity = index.capacity
            state.remote_priority = index.remote_priority
        return index

    def _remote_block(self, providers) -> str | None:
        """``local_only`` or ``daily_spend_cap`` when either holds, else None.

        The spend cap is measured at each rebuild, so the overshoot is bounded
        by ``index_ttl_s``/``weight_refresh_interval_s``, not prevented to the
        cent."""
        block: str | None = None
        if getattr(self._settings, "local_only", False):
            block = "local_only"
        else:
            cap = getattr(self._settings, "daily_spend_cap_usd", None)
            spend_today = getattr(self._providers, "spend_today", None)
            if cap is not None and callable(spend_today):
                spent = sum(
                    self._safe(lambda p=p: spend_today(p.provider_id), 0.0) or 0.0
                    for p in providers
                )
                if spent >= cap:
                    block = "daily_spend_cap"
        self._remote_block_reason = block
        return block

    def index(self) -> TargetIndex:
        if time.monotonic() - self._index_at >= self._settings.index_ttl_s:
            return self.rebuild()
        return self._index

    def _refresh_live(self, targets: list[RouteTarget]) -> None:
        """Per-request refresh of the two fields that must never be stale."""
        for t in targets:
            t.outstanding = self._stats.outstanding(t.target_id)
            blocked = self._admission.is_blocked(t.target_id)
            if t.kind is TargetKind.LOCAL:
                dep = self._index.deployments.get(t.target_id)
                t.healthy = dep is not None and dep.state in ROUTABLE_STATES
            else:
                entry = self._index.remotes.get(t.target_id)
                provider = entry[0] if entry else None
                t.healthy = bool(provider and provider.healthy)
            # The breaker only ever subtracts. Whoever owns the upstream --
            # the deployment manager or the provider service -- stays
            # authoritative for putting a target back, and this cannot
            # contradict them into routing at something already known dead.
            if t.healthy and self._breaker is not None and self._breaker.is_open(t.target_id):
                t.healthy = False
            t.admitting = t.healthy and not blocked
            if t.kind is not TargetKind.LOCAL and getattr(self, "_remote_block_reason", None):
                t.admitting = False

    # -- policy ------------------------------------------------------------

    def policy_for(self, served_name: str, targets: list[RouteTarget]) -> RoutingPolicy:
        override = self._overrides.get(served_name)
        if override is not None:
            self._auto_reasons.pop(served_name, None)
            return override
        policy, reason = self.auto_policy_explained(targets)
        self._auto_reasons[served_name] = reason
        return policy

    def auto_policy(self, targets: list[RouteTarget]) -> RoutingPolicy:
        return self.auto_policy_explained(targets)[0]

    def auto_policy_explained(
        self, targets: list[RouteTarget]
    ) -> tuple[RoutingPolicy, str]:
        """Defaults, in the order the architecture states them, plus the
        reason for the pick -- surfaced through /api/routing as auto_reason
        rather than computed and discarded.

        LOCAL_FIRST wins over WEIGHTED_CAPACITY when both apply: a mixed
        local/remote fleet is a spill decision before it is a balance decision.
        """
        has_local = any(t.kind is TargetKind.LOCAL for t in targets)
        has_remote = any(t.kind is TargetKind.REMOTE for t in targets)
        if has_local and has_remote:
            return RoutingPolicy.LOCAL_FIRST, (
                "served both locally and remotely; the cluster is preferred "
                "and the remote provider is the overflow valve"
            )

        raw = {
            t.target_id: self._index.raw_strength[t.target_id].raw
            for t in targets
            if t.target_id in self._index.raw_strength
        }
        kinds = {t.target_id: t.kind for t in targets}
        spread = strength_spread(raw, kinds)
        if spread > self._settings.auto_weighted_spread:
            return RoutingPolicy.WEIGHTED_CAPACITY, (
                f"local replicas differ in measured capability by {spread:.0%}, "
                f"above the {self._settings.auto_weighted_spread:.0%} threshold "
                "for an even split"
            )
        return RoutingPolicy.LEAST_OUTSTANDING, (
            "targets are close enough in capability that sending each request "
            "to the least-busy one balances load well enough on its own"
        )

    def auto_selected(self, served_name: str) -> bool:
        """Whether nothing overrode the policy for this model."""
        return served_name not in self._overrides

    def auto_reason(self, served_name: str) -> str | None:
        """The reason auto_policy picked its verdict, or None under an
        explicit override -- an override was not auto_policy's idea."""
        if served_name in self._overrides:
            return None
        return self._auto_reasons.get(served_name)

    def flow(self, served_name: str, policy: RoutingPolicy) -> str | None:
        """"local" | "spilled", from the last actual LOCAL_FIRST selection.

        Non-null only while ``policy`` (the config's current, resolved
        policy) is LOCAL_FIRST -- a stale tracker value from a policy that has
        since changed must never be shown. Pure: this only reads the tracker
        that ``select`` writes.
        """
        if policy is not RoutingPolicy.LOCAL_FIRST:
            return None
        return self._flow.get(served_name)

    def validate_policy(
        self,
        served_name: str,
        policy: RoutingPolicy,
        limits: dict | None = None,
        shares: dict | None = None,
    ) -> None:
        """Every check ``set_policy`` makes before it writes, and nothing else.

        Unknown name -> ``KeyError``; a bad limits or shares entry ->
        ``ValueError`` carrying the sentence the API returns verbatim. Share
        keys are not checked against today's targets: a batch may wire the
        target in the same Save, and a share naming nothing is simply unused.
        """
        if served_name not in self.index().targets:
            raise KeyError(served_name)
        if limits is not None:
            for key, value in limits.items():
                if key not in LIMIT_KEYS:
                    raise ValueError("limits may only set rpm and tpm.")
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise ValueError("limits must be whole numbers of 1 or more.")
        if shares:
            if policy is not SHARES_POLICY:
                raise ValueError(
                    f"shares split traffic only under {SHARES_POLICY.value}; "
                    "set that policy to set them."
                )
            for value in shares.values():
                if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
                    raise ValueError("shares must be whole numbers from 0 to 100.")
            if not any(shares.values()):
                raise ValueError("shares must give at least one target more than 0.")

    def set_policy(
        self,
        served_name: str,
        policy: RoutingPolicy,
        *,
        limits: dict | None = None,
        shares: dict | None = None,
    ) -> RoutingConfig:
        """Override the policy for a name and, when given, replace its limits
        and its shares.

        ``None`` leaves the stored value alone; ``{}`` deletes it. Shares only
        mean something under ``SHARES_POLICY``, so moving to another policy
        drops them.
        """
        self.validate_policy(served_name, policy, limits, shares)
        self._overrides[served_name] = policy
        if limits is not None:
            if limits:
                self._limits[served_name] = dict(limits)
            else:
                self._limits.pop(served_name, None)
        if shares is not None:
            if shares:
                self._shares[served_name] = {str(k): int(v) for k, v in shares.items()}
            else:
                self._shares.pop(served_name, None)
        if policy is not SHARES_POLICY:
            self._shares.pop(served_name, None)
        # Reset selection state so the new policy starts from a clean rotation.
        self._state[served_name] = PolicyState(
            capacity=self._index.capacity,
            remote_priority=self._index.remote_priority,
        )
        self._persist()
        config = self.config_for(served_name)
        if config is None:
            raise KeyError(served_name)
        return config

    def clear_policy(self, served_name: str) -> None:
        self._overrides.pop(served_name, None)
        self._limits.pop(served_name, None)
        self._shares.pop(served_name, None)
        self._persist()

    def overrides(self) -> dict[str, RoutingPolicy]:
        return dict(self._overrides)

    def limits_for(self, served_name: str) -> dict[str, int]:
        """The route limits for a name. Cheap: no index build; the proxy calls
        this on every request."""
        return dict(self._limits.get(served_name, {}))

    def shares_for(self, served_name: str) -> dict[str, int]:
        """The operator's traffic shares for a name, ``{}`` when none."""
        return dict(self._shares.get(served_name, {}))

    def _persist(self) -> None:
        if self._store is None:
            return
        self._store.save(
            RoutingState(
                overrides=dict(self._overrides),
                limits=dict(self._limits),
                shares=dict(self._shares),
            )
        )

    # -- reads -------------------------------------------------------------

    def config_for(self, served_name: str) -> RoutingConfig | None:
        index = self.index()
        targets = index.targets.get(served_name)
        if targets is None:
            return None
        self._refresh_live(targets)
        if any(t.healthy and t.admitting for t in targets):
            self._last_eligible[served_name] = self._clock()
        policy = self.policy_for(served_name, targets)
        shares = self.shares_for(served_name)
        if shares and policy is SHARES_POLICY:
            # The operator's split replaces the measured weights, normalized
            # over the targets this name has right now. Copies: the index's
            # targets keep their measured weights for the next refresh.
            total = sum(shares.get(t.target_id, 0) for t in targets)
            if total > 0:
                targets = [replace(t, weight=shares.get(t.target_id, 0) / total) for t in targets]
        return RoutingConfig(
            served_name=served_name,
            policy=policy,
            targets=targets,
            sticky_ttl_s=self._settings.sticky_ttl_s,
            limits=self.limits_for(served_name),
            shares=shares,
        )

    def configs(self) -> list[RoutingConfig]:
        index = self.index()
        return [
            c
            for c in (self.config_for(name) for name in sorted(index.targets))
            if c is not None
        ]

    # -- parking -----------------------------------------------------------

    def parkable(self, served_name: str) -> bool:
        """Whether a request with nowhere to go is worth holding briefly.

        Two conditions, both required.

        Not parkable when any target carries an admission block. Rate limited,
        draining and memory critical are decisions the operator needs to see
        answered, and burying one under a ten second wait would be a worse
        answer than the 503 it replaces.

        Not parkable when the model has not had a live target recently. A model
        that has never served is either unknown or still launching for the
        first time, and both of those already have an honest answer of their
        own. What is left is exactly the case worth waiting on: it was serving
        a moment ago and its node has just gone.
        """
        index = self.index()
        targets = index.targets.get(served_name, [])
        if any(self._admission.is_blocked(t.target_id) for t in targets):
            return False
        # Switched off by the operator, which is the same class of answer as
        # an admission block and needs the same treatment -- but it cannot be
        # caught by the loop above, because a non-serving deployment is not in
        # `targets` at all. It is in `pending`, so the check has to happen
        # here or a deliberate switch-off reads as a ten second stall followed
        # by the 503 it was always going to be.
        if any(not d.serving for d in index.pending.get(served_name, [])):
            return False
        return self.recently_eligible(served_name)

    def recently_eligible(self, served_name: str) -> bool:
        """Whether this model had somewhere to go in the recent past.

        Also what separates "its node died" from "no such model" once a failed
        deployment has left the index entirely: answering 404 for a model that
        was serving a minute ago tells the client something untrue.
        """
        last = self._last_eligible.get(served_name)
        if last is None:
            return False
        return self._clock() - last <= self._settings.park_eligible_memory_s

    # -- selection ---------------------------------------------------------

    def select(
        self,
        served_name: str,
        prefix_key: str | None = None,
        exclude: set[str] | None = None,
        policy: RoutingPolicy | None = None,
    ) -> Selection | None:
        """Pick a target, optionally skipping ones already tried.

        ``exclude`` is applied before the policy runs rather than inside it, so
        the seven policies in ``policies.py`` keep their frozen semantics and
        simply see a smaller field. ``policy`` is a key's or org's routing
        override, substituted the same way: the policy sees the same field
        under a different rule.
        """
        config = self.config_for(served_name)
        if config is None:
            return None
        if exclude or (policy is not None and policy is not config.policy):
            config = RoutingConfig(
                served_name=config.served_name,
                policy=policy if policy is not None else config.policy,
                targets=[
                    t for t in config.targets if not exclude or t.target_id not in exclude
                ],
                sticky_ttl_s=config.sticky_ttl_s,
                limits=config.limits,
                shares=config.shares,
            )
            if not config.targets:
                return None
        state = self._state.setdefault(served_name, PolicyState())
        state.capacity = self._index.capacity
        state.remote_priority = self._index.remote_priority
        ctx = RouteContext(
            served_name=served_name, prefix_key=prefix_key, now=time.monotonic()
        )
        target = policies.select(config, state, ctx, self._settings)
        if target is None:
            return None
        if config.policy is RoutingPolicy.LOCAL_FIRST:
            # A tracker of where the *last* selection actually landed, not a
            # recomputation of the pool's current saturation -- config_for and
            # the /api/routing read path must stay pure, so only a real
            # dispatch through select() may write this.
            self._flow[served_name] = (
                "spilled" if target.kind is TargetKind.REMOTE else "local"
            )
        remote = self._index.remotes.get(target.target_id)
        return Selection(
            config=config,
            target=target,
            deployment=self._index.deployments.get(target.target_id),
            provider=remote[0] if remote else None,
            model=remote[1] if remote else None,
        )
