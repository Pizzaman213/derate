"""Contract types to JSON.

Provider serialization is an explicit allowlist rather than a dataclass dump.
Provider API keys are the one unrecoverable mistake available here: this is a
tool people screenshot. A field can only reach a response if it is named below.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any

from control_plane.contracts import (
    Deployment,
    DeviceClass,
    FitResult,
    LinkMeasurement,
    ModelShape,
    NodeState,
    ParallelismPlan,
    Provider,
    ProviderModel,
    RoutingConfig,
)

# Never rendered, whatever a port puts in the object.
REDACTED = "***"


def plain(value: Any) -> Any:
    """Recursive dataclass/enum to JSON-safe conversion."""
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [plain(v) for v in value]
    return value


_INELIGIBLE_UNHEALTHY = "node is unhealthy"
_INELIGIBLE_DEVICE_CLASS = (
    "device class is not recognized; cannot confirm this hardware is "
    "eligible to join the pool"
)


def _eligibility(healthy: bool, device_class: DeviceClass) -> tuple[bool, str | None]:
    """Conservative and honest: eligible unless there is a concrete reason not
    to be. Unhealthy always wins over an unrecognized device class as the
    reported reason, since an operator fixes reachability before hardware ID.

    DeviceClass.UNKNOWN covers two situations this function cannot tell
    apart -- a genuinely zeroed unknown_profile() and a profile whose
    device_class string merely failed to parse (registry/serde.py's
    fallback) while other fields (e.g. addressable_memory) may be entirely
    real. Nothing downstream (planner/fit) actually filters placement on
    device class today, so the reason says "cannot confirm eligible" rather
    than asserting an exclusion the system does not enforce -- conservative
    about admitting an unidentified node without overclaiming.
    """
    if not healthy:
        return False, _INELIGIBLE_UNHEALTHY
    if device_class is DeviceClass.UNKNOWN:
        return False, _INELIGIBLE_DEVICE_CLASS
    return True, None


def node_payload(state: NodeState) -> dict:
    profile = state.profile
    # Reported against physical memory, which is the basis Agent A's telemetry
    # and the topology payload in the architecture doc both use. Admission
    # control deliberately uses addressable memory instead: that is the slice
    # the GPU can actually reach and the one Agent D budgets a fit against.
    total = profile.total_memory or 0
    eligible, ineligible_reason = _eligibility(state.healthy, profile.device_class)
    return {
        "node_id": profile.node_id,
        "hostname": profile.hostname,
        "address": profile.address,
        "device_class": profile.device_class.value,
        "gpu_name": profile.gpu_name,
        "gpu_count": profile.gpu_count,
        "total_memory": profile.total_memory,
        "addressable_memory": profile.addressable_memory,
        "memory_bandwidth_gbps": profile.memory_bandwidth_gbps,
        "compute_capability": profile.compute_capability,
        "driver_version": profile.driver_version,
        "healthy": state.healthy,
        "last_seen": state.last_seen,
        "memory_used": state.memory_used,
        "memory_used_pct": round(state.memory_used / total * 100.0, 1)
        if total
        else None,
        "power_w": state.power_watts,
        "temp_c": state.temperature_c,
        "util_pct": state.utilization_pct,
        "eligible": eligible,
        "ineligible_reason": ineligible_reason,
    }


def candidate_payload(candidate: dict) -> dict:
    """A discovered-not-yet-admitted node. No health telemetry exists for a
    candidate yet, so eligibility rests on device class alone.
    """
    payload = plain(candidate)
    device_class_raw = str(payload.get("device_class", DeviceClass.UNKNOWN.value))
    try:
        device_class = DeviceClass(device_class_raw.lower())
    except ValueError:
        device_class = DeviceClass.UNKNOWN
    eligible, ineligible_reason = _eligibility(True, device_class)
    payload["eligible"] = eligible
    payload["ineligible_reason"] = ineligible_reason
    return payload


def link_payload(link: LinkMeasurement) -> dict:
    payload = {
        "src": link.src,
        "dst": link.dst,
        "all_reduce_gbps": link.all_reduce_gbps,
        "sendrecv_gbps": link.sendrecv_gbps,
        "latency_us": link.latency_us,
        "gpudirect_rdma": link.gpudirect_rdma,
        "measured_at": link.measured_at,
        "method": link.method,
    }
    # AnnotatedLink (control_plane.links.record) carries the honesty metadata
    # a plain contract LinkMeasurement cannot hold. Present only when there is
    # one: a bare LinkMeasurement (the stub, a hand-built fixture) says nothing
    # about estimation rather than implying "measured, not estimated".
    annotation = getattr(link, "annotation", None)
    if annotation is not None:
        payload["estimated"] = annotation.estimated
        payload["raw_gbps"] = annotation.raw_gbps
        payload["scale_factor"] = annotation.scale_factor
        payload["notes"] = list(annotation.notes)
    return payload


def shape_payload(shape: ModelShape) -> dict:
    return plain(shape)


def plan_payload(plan: ParallelismPlan) -> dict:
    data = plain(plan)
    data["world_size"] = plan.world_size
    return data


def fit_payload(fit: FitResult) -> dict:
    data = plain(fit)
    data["breakdown"]["total"] = fit.breakdown.total
    data["ok"] = fit.ok
    return data


def deployment_payload(deployment: Deployment) -> dict:
    return {
        "deployment_id": deployment.deployment_id,
        "served_name": deployment.served_name,
        "model_id": deployment.shape.model_id if deployment.shape else None,
        "runtime": deployment.runtime,
        "state": deployment.state.value,
        "backend_url": deployment.backend_url,
        "context_length": deployment.context_length,
        "max_concurrent_seqs": deployment.max_concurrent_seqs,
        "started_at": deployment.started_at,
        "last_error": deployment.last_error,
        "node_ids": list(deployment.plan.node_ids) if deployment.plan else [],
        "plan": plan_payload(deployment.plan) if deployment.plan else None,
        "fit": fit_payload(deployment.fit) if deployment.fit else None,
    }


def provider_model_payload(model: ProviderModel) -> dict:
    return {
        "served_name": model.served_name,
        "upstream_id": model.upstream_id,
        "context_length": model.context_length,
        "supports_streaming": model.supports_streaming,
        "supports_tools": model.supports_tools,
        "input_cost_per_mtok": model.input_cost_per_mtok,
        "output_cost_per_mtok": model.output_cost_per_mtok,
    }


def provider_payload(provider: Provider) -> dict:
    """Allowlist. ``api_key_ref`` is a reference -- an env var name or secret
    key -- and is safe to show; it is what the UI needs to tell the user which
    variable to set. Any resolved key material is rendered as ``***`` and
    nothing else, and no other field is emitted at all.
    """
    return {
        "provider_id": provider.provider_id,
        "kind": provider.kind.value,
        "display_name": provider.display_name,
        "base_url": provider.base_url,
        "api_key_ref": provider.api_key_ref,
        "api_key": REDACTED,
        "enabled": provider.enabled,
        "priority": provider.priority,
        "healthy": provider.healthy,
        "last_error": provider.last_error,
        "last_refreshed": provider.last_refreshed,
        "models": [provider_model_payload(m) for m in provider.models],
    }


def routing_payload(
    config: RoutingConfig,
    sources: dict[str, str] | None = None,
    circuits: dict[str, str] | None = None,
    *,
    auto_selected: bool = False,
    auto_reason: str | None = None,
    flow: str | None = None,
    zero_weight_reasons: dict[str, str | None] | None = None,
    node_ids: dict[str, list[str]] | None = None,
) -> dict:
    sources = sources or {}
    circuits = circuits or {}
    zero_weight_reasons = zero_weight_reasons or {}
    node_ids = node_ids or {}
    return {
        "served_name": config.served_name,
        "policy": config.policy.value,
        "sticky_ttl_s": config.sticky_ttl_s,
        # "local" | "spilled" | null; null unless this config is LOCAL_FIRST
        # and at least one real selection has been made (the caller derives
        # this from the router's own dispatch-time tracker, never recomputed
        # here from current saturation).
        "flow": flow,
        # No explicit override on this served_name: the policy above is
        # whatever auto_policy picked, and auto_reason says why.
        "auto_selected": auto_selected,
        "auto_reason": auto_reason,
        "targets": [
            {
                "target_id": t.target_id,
                "kind": t.kind.value,
                "backend_url": t.backend_url,
                "weight": round(t.weight, 4),
                "outstanding": t.outstanding,
                "healthy": t.healthy,
                "admitting": t.admitting,
                "strength": round(t.strength, 4),
                "strength_source": sources.get(t.target_id),
                # Why an otherwise-live target is not taking traffic. Without
                # this the UI shows a deployment the deploy manager calls READY
                # sitting at healthy=false with nothing to explain it.
                "circuit": circuits.get(t.target_id, "closed"),
                "cost_per_mtok": t.cost_per_mtok,
                # Why the 15% weak-target floor benched a local replica; None
                # for a remote (never floored) or a local target above it.
                "zero_weight_reason": zero_weight_reasons.get(t.target_id),
                # The deployment's plan node_ids for a local target, else [].
                "node_ids": node_ids.get(t.target_id, []),
            }
            for t in config.targets
        ],
    }
