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
from dataclasses import dataclass

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
from .settings import GatewaySettings
from .stats import StatsRegistry
from .strength import strength_spread
from .targets import ROUTABLE_STATES, TargetIndex, apply_scores, build_index

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
    ) -> None:
        self._deployments = deployments
        self._providers = providers
        self._registry = registry
        self._stats = stats
        self._admission = admission
        self._settings = settings

        self._index = TargetIndex()
        self._index_at = 0.0
        self._scored_at = 0.0
        # target_id -> (strength, weight), held between weight refreshes
        self._scores: dict[str, tuple[float, float]] = {}
        self._overrides: dict[str, RoutingPolicy] = {}
        self._state: dict[str, PolicyState] = {}
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
        except Exception:
            log.exception("routing source unavailable: %s", getattr(fn, "__name__", fn))
            return default

    def rebuild(self, *, force_scores: bool = False) -> TargetIndex:
        now = time.monotonic()
        deployments = self._safe(self._deployments.list, [])
        providers = self._safe(self._providers.list, [])
        nodes = self._safe(self._registry.list_nodes, [])

        index = build_index(
            deployments=deployments,
            providers=providers,
            nodes=nodes,
            stats=self._stats,
            settings=self._settings,
            is_blocked=self._admission.is_blocked,
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
                t.target_id: (t.strength, t.weight)
                for targets in index.targets.values()
                for t in targets
            }
            self._scored_at = now
        else:
            for targets in index.targets.values():
                for t in targets:
                    t.strength, t.weight = self._scores.get(t.target_id, (0.0, 0.0))

        self._index = index
        self._index_at = now
        for name in index.targets:
            state = self._state.setdefault(name, PolicyState())
            state.capacity = index.capacity
            state.remote_priority = index.remote_priority
        return index

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
                t.admitting = t.healthy and not blocked
            else:
                entry = self._index.remotes.get(t.target_id)
                provider = entry[0] if entry else None
                t.healthy = bool(provider and provider.healthy)
                t.admitting = t.healthy and not blocked

    # -- policy ------------------------------------------------------------

    def policy_for(self, served_name: str, targets: list[RouteTarget]) -> RoutingPolicy:
        override = self._overrides.get(served_name)
        if override is not None:
            return override
        return self.auto_policy(targets)

    def auto_policy(self, targets: list[RouteTarget]) -> RoutingPolicy:
        """Defaults, in the order the architecture states them.

        LOCAL_FIRST wins over WEIGHTED_CAPACITY when both apply: a mixed
        local/remote fleet is a spill decision before it is a balance decision.
        """
        has_local = any(t.kind is TargetKind.LOCAL for t in targets)
        has_remote = any(t.kind is TargetKind.REMOTE for t in targets)
        if has_local and has_remote:
            return RoutingPolicy.LOCAL_FIRST

        raw = {
            t.target_id: self._index.raw_strength[t.target_id].raw
            for t in targets
            if t.target_id in self._index.raw_strength
        }
        kinds = {t.target_id: t.kind for t in targets}
        if strength_spread(raw, kinds) > self._settings.auto_weighted_spread:
            return RoutingPolicy.WEIGHTED_CAPACITY
        return RoutingPolicy.LEAST_OUTSTANDING

    def set_policy(self, served_name: str, policy: RoutingPolicy) -> RoutingConfig:
        self._overrides[served_name] = policy
        # Reset selection state so the new policy starts from a clean rotation.
        self._state[served_name] = PolicyState(
            capacity=self._index.capacity,
            remote_priority=self._index.remote_priority,
        )
        config = self.config_for(served_name)
        if config is None:
            raise KeyError(served_name)
        return config

    def clear_policy(self, served_name: str) -> None:
        self._overrides.pop(served_name, None)

    def overrides(self) -> dict[str, RoutingPolicy]:
        return dict(self._overrides)

    # -- reads -------------------------------------------------------------

    def config_for(self, served_name: str) -> RoutingConfig | None:
        index = self.index()
        targets = index.targets.get(served_name)
        if targets is None:
            return None
        self._refresh_live(targets)
        return RoutingConfig(
            served_name=served_name,
            policy=self.policy_for(served_name, targets),
            targets=targets,
            sticky_ttl_s=self._settings.sticky_ttl_s,
        )

    def configs(self) -> list[RoutingConfig]:
        index = self.index()
        return [
            c
            for c in (self.config_for(name) for name in sorted(index.targets))
            if c is not None
        ]

    # -- selection ---------------------------------------------------------

    def select(self, served_name: str, prefix_key: str | None = None) -> Selection | None:
        config = self.config_for(served_name)
        if config is None:
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
        remote = self._index.remotes.get(target.target_id)
        return Selection(
            config=config,
            target=target,
            deployment=self._index.deployments.get(target.target_id),
            provider=remote[0] if remote else None,
            model=remote[1] if remote else None,
        )
