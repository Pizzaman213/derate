"""Wire encoding for the contract dataclasses this package sends over HTTP.

Kept explicit rather than generic. These payloads cross a version boundary
between two containers that may not be the same build, so an unexpected field
must be ignorable and a missing one must have a defined default.
"""

from __future__ import annotations

from dataclasses import asdict

from control_plane.contracts import DeviceClass, NodeProfile, NodeState

from .telemetry import TelemetrySample


def profile_to_dict(profile: NodeProfile) -> dict:
    data = asdict(profile)
    data["device_class"] = profile.device_class.value
    return data


def profile_from_dict(data: dict) -> NodeProfile:
    """Rebuild a profile from a peer. Unknown keys are dropped, not fatal."""
    try:
        device_class = DeviceClass(str(data.get("device_class", "unknown")).lower())
    except ValueError:
        device_class = DeviceClass.UNKNOWN
    return NodeProfile(
        node_id=str(data["node_id"]),
        hostname=str(data.get("hostname", "")),
        address=str(data.get("address", "")),
        device_class=device_class,
        gpu_name=str(data.get("gpu_name", "")),
        gpu_count=int(data.get("gpu_count", 0)),
        total_memory=int(data.get("total_memory", 0)),
        addressable_memory=int(data.get("addressable_memory", 0)),
        memory_bandwidth_gbps=float(data.get("memory_bandwidth_gbps", 0.0)),
        compute_capability=str(data.get("compute_capability", "")),
        driver_version=str(data.get("driver_version", "")),
    )


def memory_used_pct(state: NodeState) -> float:
    addressable = state.profile.addressable_memory
    if addressable <= 0:
        return 0.0
    return round(100.0 * state.memory_used / addressable, 1)


def state_to_dict(state: NodeState) -> dict:
    return {
        "node_id": state.profile.node_id,
        "profile": profile_to_dict(state.profile),
        "healthy": state.healthy,
        "last_seen": state.last_seen,
        "memory_used": state.memory_used,
        "memory_used_pct": memory_used_pct(state),
        "power_w": round(state.power_watts, 1),
        "temp_c": round(state.temperature_c, 1),
        "util_pct": round(state.utilization_pct, 1),
    }


def telemetry_to_dict(node_id: str, sample: TelemetrySample | None) -> dict:
    if sample is None:
        return {"node_id": node_id, "ts": None, "available": False}
    payload = sample.as_dict()
    payload["node_id"] = node_id
    payload["available"] = True
    return payload
