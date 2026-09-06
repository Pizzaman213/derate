"""Target strength scoring and weight normalization.

Strength, in strict order of preference (architecture 4.5):

1. Measured sustained decode tok/s for this target, once at least
   ``measured_strength_min_requests`` requests have completed.
2. Agent D's ``predicted_decode_tps`` for this shape on this node's profile.
3. ``memory_bandwidth_gbps * gpu_count`` as a last resort.

Never compute strength from a spec sheet once measured throughput exists.
"""

from __future__ import annotations

from dataclasses import dataclass

from control_plane.contracts import Deployment, NodeProfile, TargetKind

from .settings import GatewaySettings
from .stats import StatsRegistry

# Which rung of the ladder a score came from, surfaced through /api/routing so
# the UI can say why the split looks the way it does.
SOURCE_MEASURED = "measured"
SOURCE_PREDICTED = "predicted"
SOURCE_BANDWIDTH = "bandwidth"
SOURCE_DEFAULT = "default"


@dataclass(frozen=True)
class StrengthScore:
    raw: float
    source: str


def local_strength(
    deployment: Deployment,
    stats: StatsRegistry,
    profiles: dict[str, NodeProfile],
    settings: GatewaySettings,
) -> StrengthScore:
    st = stats.peek(deployment.deployment_id)
    if (
        st is not None
        and st.completed >= settings.measured_strength_min_requests
        and st.decode_tps
        and st.decode_tps > 0
    ):
        return StrengthScore(st.decode_tps, SOURCE_MEASURED)

    predicted = deployment.fit.predicted_decode_tps if deployment.fit else None
    if predicted and predicted > 0:
        return StrengthScore(float(predicted), SOURCE_PREDICTED)

    node_ids = list(deployment.plan.node_ids) if deployment.plan else []
    bandwidth = sum(
        profiles[n].memory_bandwidth_gbps * max(1, profiles[n].gpu_count)
        for n in node_ids
        if n in profiles
    )
    if bandwidth > 0:
        return StrengthScore(bandwidth, SOURCE_BANDWIDTH)

    return StrengthScore(1.0, SOURCE_DEFAULT)


def remote_strength(
    target_id: str, stats: StatsRegistry, settings: GatewaySettings
) -> StrengthScore:
    """Remotes have no hardware profile to reason about.

    Measurement still applies once we have enough completions; otherwise a
    neutral score. Remotes are excluded from the weak-target floor because
    that rule is about local hardware.
    """
    st = stats.peek(target_id)
    if (
        st is not None
        and st.completed >= settings.measured_strength_min_requests
        and st.decode_tps
        and st.decode_tps > 0
    ):
        return StrengthScore(st.decode_tps, SOURCE_MEASURED)
    return StrengthScore(settings.remote_default_strength, SOURCE_DEFAULT)


def normalize_strength(raw: dict[str, float]) -> dict[str, float]:
    """Scale so the strongest target is 1.0. This is the ``strength`` field."""
    if not raw:
        return {}
    top = max(raw.values())
    if top <= 0:
        return {k: 0.0 for k in raw}
    return {k: v / top for k, v in raw.items()}


def compute_weights(
    raw: dict[str, float],
    kinds: dict[str, TargetKind],
    settings: GatewaySettings,
) -> dict[str, float]:
    """Traffic shares summing to 1.

    A local target below ``weak_target_floor`` of the strongest local target
    drops to zero weight and is held as failover only: a small share of traffic
    on a very slow node still ruins the tail. Remote targets are exempt.
    """
    if not raw:
        return {}

    local_raw = [v for k, v in raw.items() if kinds.get(k) is TargetKind.LOCAL]
    strongest_local = max(local_raw) if local_raw else 0.0
    floor = strongest_local * settings.weak_target_floor

    eligible: dict[str, float] = {}
    for target_id, value in raw.items():
        is_local = kinds.get(target_id) is TargetKind.LOCAL
        if is_local and strongest_local > 0 and value < floor:
            eligible[target_id] = 0.0
        else:
            eligible[target_id] = max(0.0, value)

    total = sum(eligible.values())
    if total <= 0:
        # Everything was floored out; fall back to an even split so the
        # cluster still serves rather than refusing.
        share = 1.0 / len(raw)
        return {k: share for k in raw}
    return {k: v / total for k, v in eligible.items()}


def strength_spread(raw: dict[str, float], kinds: dict[str, TargetKind]) -> float:
    """Relative spread across local targets, 0.0 when they are identical.

    Drives the automatic switch to WEIGHTED_CAPACITY above 25 percent.
    """
    local = [v for k, v in raw.items() if kinds.get(k) is TargetKind.LOCAL and v > 0]
    if len(local) < 2:
        return 0.0
    top = max(local)
    bottom = min(local)
    if top <= 0:
        return 0.0
    return (top - bottom) / top
