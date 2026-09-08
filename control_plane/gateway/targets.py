"""Build the routable view of the cluster.

Local deployments (Agent F) and remote provider models (Agent I) are merged
into one target list per ``served_name``. A model served both locally and
remotely is one entry with several targets behind it; the client never learns
which answered.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from control_plane.contracts import (
    Deployment,
    Modality,
    NodeProfile,
    NodeState,
    Provider,
    ProviderModel,
    RouteTarget,
    TargetKind,
)

from . import states
from .settings import GatewaySettings
from .stats import StatsRegistry
from .strength import (
    StrengthScore,
    local_strength,
    normalize_strength,
    remote_strength,
)

# Deployment states that can answer a request. Anything else is not a target.
# ``states`` is the gateway's one spelling of this; the name stays because the
# routing code reads better for it.
ROUTABLE_STATES = states.SERVING


def local_cost_per_mtok(
    power_watts: float | None, decode_tps: float | None, rate_usd_per_kwh: float
) -> float | None:
    """Price local generation from measured power draw.

    Defaults to zero, which makes local free and therefore always the cheapest
    option under COST_AWARE -- the right default for someone who already owns
    the hardware. Returns None only when a rate is set but we have no measured
    throughput to divide by, because guessing would be worse than skipping.
    """
    if rate_usd_per_kwh <= 0:
        return 0.0
    if not power_watts or not decode_tps or decode_tps <= 0:
        return None
    kwh_per_hour = power_watts / 1000.0
    tokens_per_hour = decode_tps * 3600.0
    return (kwh_per_hour * rate_usd_per_kwh) / tokens_per_hour * 1_000_000.0


def remote_cost_per_mtok(model: ProviderModel) -> float | None:
    """Decode dominates, so output price is the honest single number.

    None stays None: a target with unknown cost is skipped by COST_AWARE
    rather than assumed free.
    """
    if model.output_cost_per_mtok is not None:
        return model.output_cost_per_mtok
    return model.input_cost_per_mtok


@dataclass
class TargetIndex:
    """One rebuild's worth of routable state."""

    targets: dict[str, list[RouteTarget]] = field(default_factory=dict)
    # target_id -> the deployment behind it (local only)
    deployments: dict[str, Deployment] = field(default_factory=dict)
    # target_id -> (provider, model) behind it (remote only)
    remotes: dict[str, tuple[Provider, ProviderModel]] = field(default_factory=dict)
    # target_id -> raw strength score, before normalization
    raw_strength: dict[str, StrengthScore] = field(default_factory=dict)
    capacity: dict[str, int] = field(default_factory=dict)
    remote_priority: dict[str, int] = field(default_factory=dict)
    # served_name -> context length advertised on /v1/models
    context_length: dict[str, int] = field(default_factory=dict)
    # served_name -> which endpoint family answers for it. A name served both
    # locally and remotely is one entry, so the first target to claim it sets
    # the modality and the rest agree by construction: two models that answer
    # different endpoints are two different served_names.
    modality: dict[str, Modality] = field(default_factory=dict)
    # served_name -> deployments that exist but cannot serve yet, for 503 bodies
    pending: dict[str, list[Deployment]] = field(default_factory=dict)
    # target_id -> why the 15% weak-target floor benched it, None otherwise.
    # Local targets only; a remote is never floored. Filled in by
    # ``apply_scores`` alongside strength/weight.
    zero_weight_reason: dict[str, str | None] = field(default_factory=dict)

    def served_names(self) -> list[str]:
        return sorted(set(self.targets) | set(self.pending))

    def kinds(self) -> dict[str, TargetKind]:
        return {
            t.target_id: t.kind
            for targets in self.targets.values()
            for t in targets
        }

    def node_ids_for(self, target_id: str) -> list[str]:
        """The deployment's plan node_ids for a local target, else []."""
        dep = self.deployments.get(target_id)
        if dep is None or dep.plan is None:
            return []
        return list(dep.plan.node_ids)


def build_index(
    deployments: list[Deployment],
    providers: list[Provider],
    nodes: list[NodeState],
    stats: StatsRegistry,
    settings: GatewaySettings,
    is_blocked,
) -> TargetIndex:
    """Assemble every routable target. Pure given its inputs."""
    index = TargetIndex()
    profiles: dict[str, NodeProfile] = {n.profile.node_id: n.profile for n in nodes}
    power: dict[str, float] = {n.profile.node_id: n.power_watts for n in nodes}

    for dep in deployments:
        if dep.state not in ROUTABLE_STATES or not dep.backend_url:
            if dep.state not in states.TERMINAL:
                index.pending.setdefault(dep.served_name, []).append(dep)
            continue

        tid = dep.deployment_id
        st = stats.peek(tid)
        score = local_strength(dep, stats, profiles, settings)
        node_power = sum(power.get(n, 0.0) for n in (dep.plan.node_ids if dep.plan else []))
        cost = local_cost_per_mtok(
            node_power,
            st.decode_tps if st else None,
            settings.electricity_rate_usd_per_kwh,
        )

        index.deployments[tid] = dep
        index.raw_strength[tid] = score
        index.capacity[tid] = dep.max_concurrent_seqs
        index.zero_weight_reason[tid] = None  # filled in by apply_scores
        index.context_length.setdefault(dep.served_name, dep.context_length)
        index.modality.setdefault(dep.served_name, dep.modality)
        index.targets.setdefault(dep.served_name, []).append(
            RouteTarget(
                target_id=tid,
                kind=TargetKind.LOCAL,
                backend_url=dep.backend_url,
                weight=0.0,  # filled in by the router
                outstanding=stats.outstanding(tid),
                # DEGRADED is up but impaired, and is listed in /v1/models,
                # so it must stay routable. Memory pressure is expressed
                # through `admitting`, not by making it invisible.
                healthy=dep.state in ROUTABLE_STATES,
                admitting=not is_blocked(tid),
                strength=0.0,  # filled in by the router
                cost_per_mtok=cost,
            )
        )

    for provider in providers:
        if not provider.enabled:
            continue
        for model in provider.models:
            tid = f"{provider.provider_id}:{model.upstream_id}"
            index.remotes[tid] = (provider, model)
            index.raw_strength[tid] = remote_strength(tid, stats, settings)
            index.remote_priority[tid] = provider.priority
            index.zero_weight_reason[tid] = None  # remotes are never floored
            index.context_length.setdefault(model.served_name, model.context_length)
            index.modality.setdefault(model.served_name, model.modality)
            index.targets.setdefault(model.served_name, []).append(
                RouteTarget(
                    target_id=tid,
                    kind=TargetKind.REMOTE,
                    backend_url=provider.base_url,
                    weight=0.0,
                    outstanding=stats.outstanding(tid),
                    healthy=provider.healthy,
                    admitting=provider.healthy and not is_blocked(tid),
                    strength=0.0,
                    cost_per_mtok=remote_cost_per_mtok(model),
                )
            )

    return index


def apply_scores(index: TargetIndex, settings: GatewaySettings) -> None:
    """Fill in ``strength`` (relative to the strongest target anywhere in the
    cluster, which is what the topology view draws) and ``weight`` (traffic
    share within one served_name, summing to 1)."""
    from .strength import SOURCE_DEFAULT, compute_weights

    raw = {k: v.raw for k, v in index.raw_strength.items()}
    strengths = normalize_strength(raw)

    for targets in index.targets.values():
        group_raw = {t.target_id: raw.get(t.target_id, 0.0) for t in targets}
        group_kinds = {t.target_id: t.kind for t in targets}

        # An unmeasured remote has no comparable score: its placeholder is not
        # in the same units as a local target's tok/s. Weighting it against
        # them would starve or flood it for no reason, so it is treated as
        # neutral -- the average of the local targets serving the same model --
        # until enough requests complete to measure it.
        local_values = [
            v
            for tid, v in group_raw.items()
            if group_kinds[tid] is TargetKind.LOCAL and v > 0
        ]
        if local_values:
            neutral = sum(local_values) / len(local_values)
            for t in targets:
                score = index.raw_strength.get(t.target_id)
                if t.kind is TargetKind.REMOTE and score and score.source == SOURCE_DEFAULT:
                    group_raw[t.target_id] = neutral

        weights = compute_weights(group_raw, group_kinds, settings)

        # Mirrors the floor condition inside compute_weights exactly (same
        # group_raw, same group_kinds, same weak_target_floor), so that a
        # target this determines was floored is always the same target that
        # actually landed at weight 0.0 above. Kept here rather than in
        # strength.py so compute_weights stays a pure weights-only function.
        local_group_raw = [
            v for tid, v in group_raw.items() if group_kinds[tid] is TargetKind.LOCAL
        ]
        strongest_local = max(local_group_raw) if local_group_raw else 0.0
        floor = strongest_local * settings.weak_target_floor

        for t in targets:
            t.strength = strengths.get(t.target_id, 0.0)
            t.weight = weights.get(t.target_id, 0.0)
            if (
                t.kind is TargetKind.LOCAL
                and strongest_local > 0
                and group_raw.get(t.target_id, 0.0) < floor
            ):
                index.zero_weight_reason[t.target_id] = (
                    f"strength is below {settings.weak_target_floor:.0%} of the "
                    "strongest local replica serving this model; held as "
                    "failover only"
                )
            else:
                index.zero_weight_reason[t.target_id] = None
