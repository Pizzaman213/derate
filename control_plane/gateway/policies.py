"""The seven routing policies. Architecture 4.5.

One rule outranks all of them: a target that is not ``admitting`` -- memory
went critical, it is draining, or it is rate limited -- is excluded from every
policy, round robin included. If nothing is admitting, selection returns None
and the caller answers 503 rather than queueing indefinitely.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from control_plane.contracts import RouteTarget, RoutingConfig, RoutingPolicy, TargetKind

from .settings import GatewaySettings


@dataclass
class RouteContext:
    """Everything a policy may look at besides the targets themselves."""

    served_name: str
    prefix_key: str | None = None  # hash input for cache affinity
    now: float = 0.0


@dataclass
class PolicyState:
    """Mutable per-served_name selection state, owned by the router."""

    rr_cursor: int = 0
    swrr_credits: dict[str, float] = field(default_factory=dict)
    sticky: dict[str, tuple[str, float]] = field(default_factory=dict)
    # target_id -> max_concurrent_seqs, for LOCAL_FIRST saturation
    capacity: dict[str, int] = field(default_factory=dict)
    # target_id -> provider priority, lower preferred among remotes
    remote_priority: dict[str, int] = field(default_factory=dict)


def eligible(targets: list[RouteTarget]) -> list[RouteTarget]:
    """Healthy and admitting. The universal filter."""
    return [t for t in targets if t.healthy and t.admitting]


def _least_outstanding(targets: list[RouteTarget]) -> RouteTarget | None:
    if not targets:
        return None
    # target_id breaks ties so selection is deterministic and testable.
    return min(targets, key=lambda t: (t.outstanding, t.target_id))


def _stable_order(targets: list[RouteTarget]) -> list[RouteTarget]:
    """Locals before remotes, then by id. The failover priority order."""
    return sorted(
        targets, key=lambda t: (0 if t.kind is TargetKind.LOCAL else 1, t.target_id)
    )


def _is_saturated(target: RouteTarget, state: PolicyState) -> bool:
    cap = state.capacity.get(target.target_id)
    if cap is None or cap <= 0:
        return False
    return target.outstanding >= cap


def hedge_candidate(
    config,
    leader: RouteTarget,
    settings,
    exclude: set[str] | None = None,
) -> RouteTarget | None:
    """A target worth racing the leader against, or None to send nothing.

    Hedging spends a whole extra inference to save latency, so it has to be
    refused far more often than it is taken. Two refusals, both deliberate:

    **Never under LOCAL_FIRST.** There the remote is the overflow valve --
    "the cluster is preferred and the remote provider is the overflow valve",
    per `Router.auto_policy_explained`. Spilling is a decision that means the
    local pool is saturated. A hedge would spill on every slow request instead,
    turning a deliberate policy into routine double-dispatch and, on a metered
    provider, into a doubled bill.

    **Never onto a target that cannot win.** `weak_target_floor` already draws
    the line at which a local replica is held as failover only; a hedge below it
    loses every race and burns the capacity to learn nothing. Reusing that
    threshold keeps one definition of "too weak to bother with" rather than
    inventing a second.

    Returns the strongest eligible alternative, which is the only one with a
    real chance of beating a leader that has already had a head start.
    """
    skip = exclude or set()
    pool = [
        t
        for t in eligible(config.targets)
        if t.target_id != leader.target_id and t.target_id not in skip
    ]
    if not pool:
        return None
    best = max(pool, key=lambda t: (t.strength, t.target_id))
    return best if may_hedge(config, leader, best, settings) else None


def may_hedge(config, leader: RouteTarget, candidate: RouteTarget, settings) -> bool:
    """Whether racing *candidate* against *leader* is allowed at all.

    The rule lives here, in one predicate, because it is asked twice: once to
    pick a candidate and once to check the target routing actually claimed --
    which need not be the one picked, since selection runs the policy and the
    breaker, not this. Gating only the first would let the second slip past.
    """
    if config.policy is RoutingPolicy.LOCAL_FIRST:
        return False
    if candidate.target_id == leader.target_id:
        return False
    return candidate.strength >= leader.strength * settings.weak_target_floor


def prefix_hash(prefix_key: str) -> int:
    return int.from_bytes(hashlib.sha256(prefix_key.encode()).digest()[:8], "big")


# --------------------------------------------------------------------------
# policies
# --------------------------------------------------------------------------


def select_least_outstanding(
    targets: list[RouteTarget], state: PolicyState, ctx: RouteContext, s: GatewaySettings
) -> RouteTarget | None:
    return _least_outstanding(eligible(targets))


def select_round_robin(
    targets: list[RouteTarget], state: PolicyState, ctx: RouteContext, s: GatewaySettings
) -> RouteTarget | None:
    pool = _stable_order(eligible(targets))
    if not pool:
        return None
    chosen = pool[state.rr_cursor % len(pool)]
    state.rr_cursor = (state.rr_cursor + 1) % len(pool)
    return chosen


def select_weighted_capacity(
    targets: list[RouteTarget], state: PolicyState, ctx: RouteContext, s: GatewaySettings
) -> RouteTarget | None:
    """Smooth weighted round robin.

    Deterministic rather than random: over N requests the split matches the
    weights closely instead of only in expectation, which is what makes the
    "within 10 percent of the strength ratio" acceptance test meaningful.
    """
    pool = eligible(targets)
    if not pool:
        return None

    weighted = [t for t in pool if t.weight > 0]
    if not weighted:
        # Everything admitting is floored to zero weight. Those targets are
        # failover only, and this is the failover case.
        return _least_outstanding(pool)

    total = sum(t.weight for t in weighted)
    best: RouteTarget | None = None
    best_credit = 0.0
    for t in weighted:
        credit = state.swrr_credits.get(t.target_id, 0.0) + t.weight
        state.swrr_credits[t.target_id] = credit
        if best is None or credit > best_credit or (
            credit == best_credit and best is not None and t.target_id < best.target_id
        ):
            best = t
            best_credit = credit
    if best is not None:
        state.swrr_credits[best.target_id] = best_credit - total
    return best


def select_cache_affinity(
    targets: list[RouteTarget], state: PolicyState, ctx: RouteContext, s: GatewaySettings
) -> RouteTarget | None:
    """Hash the prompt prefix to a target so repeat prefixes land where they
    are already cached. Remote targets are excluded: we cannot reason about a
    remote runtime's cache. Falls back to least-outstanding when the chosen
    target is not admitting.
    """
    locals_all = _stable_order([t for t in targets if t.kind is TargetKind.LOCAL])
    fallback_pool = eligible(targets)

    if not locals_all or not ctx.prefix_key:
        return _least_outstanding(fallback_pool)

    ttl = s.sticky_ttl_s
    if ttl > 0:
        entry = state.sticky.get(ctx.prefix_key)
        if entry and entry[1] > ctx.now:
            pinned = next((t for t in locals_all if t.target_id == entry[0]), None)
            if pinned is not None and pinned.healthy and pinned.admitting:
                return pinned

    # Hash across every local target, admitting or not, so the mapping stays
    # stable as targets come and go from the admitting set.
    chosen = locals_all[prefix_hash(ctx.prefix_key) % len(locals_all)]
    if not (chosen.healthy and chosen.admitting):
        return _least_outstanding(fallback_pool)

    if ttl > 0:
        state.sticky[ctx.prefix_key] = (chosen.target_id, ctx.now + ttl)
    return chosen


def select_failover(
    targets: list[RouteTarget], state: PolicyState, ctx: RouteContext, s: GatewaySettings
) -> RouteTarget | None:
    """Everything to the primary; switch only when it stops admitting."""
    for t in _stable_order(targets):
        if t.healthy and t.admitting:
            return t
    return None


def select_local_first(
    targets: list[RouteTarget], state: PolicyState, ctx: RouteContext, s: GatewaySettings
) -> RouteTarget | None:
    """The cluster is the default and the paid API is the overflow valve.

    Spill to remote only when every local target is saturated, unhealthy, or
    not admitting. Return to local the moment capacity frees, which happens
    for free: this is recomputed per request against live outstanding counts.
    """
    pool = eligible(targets)
    local_pool = [t for t in pool if t.kind is TargetKind.LOCAL]
    remote_pool = [t for t in pool if t.kind is TargetKind.REMOTE]

    unsaturated = [t for t in local_pool if not _is_saturated(t, state)]
    if unsaturated:
        return _least_outstanding(unsaturated)

    if remote_pool:
        # Cheapest hop first among remotes: provider priority, then load.
        return min(
            remote_pool,
            key=lambda t: (
                state.remote_priority.get(t.target_id, 0),
                t.outstanding,
                t.target_id,
            ),
        )

    # Locals are saturated but there is nowhere else to go. Admission control
    # decides whether this request is refused; routing does not 503 on load.
    return _least_outstanding(local_pool)


def select_cost_aware(
    targets: list[RouteTarget], state: PolicyState, ctx: RouteContext, s: GatewaySettings
) -> RouteTarget | None:
    """Cheapest admitting target. Unknown cost is skipped, never assumed free."""
    priced = [t for t in eligible(targets) if t.cost_per_mtok is not None]
    if not priced:
        return None
    return min(priced, key=lambda t: (t.cost_per_mtok, t.outstanding, t.target_id))


SELECTORS = {
    RoutingPolicy.LEAST_OUTSTANDING: select_least_outstanding,
    RoutingPolicy.ROUND_ROBIN: select_round_robin,
    RoutingPolicy.WEIGHTED_CAPACITY: select_weighted_capacity,
    RoutingPolicy.CACHE_AFFINITY: select_cache_affinity,
    RoutingPolicy.FAILOVER: select_failover,
    RoutingPolicy.LOCAL_FIRST: select_local_first,
    RoutingPolicy.COST_AWARE: select_cost_aware,
}


def select(
    config: RoutingConfig,
    state: PolicyState,
    ctx: RouteContext,
    settings: GatewaySettings,
) -> RouteTarget | None:
    selector = SELECTORS.get(config.policy, select_least_outstanding)
    return selector(config.targets, state, ctx, settings)
