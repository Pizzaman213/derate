"""Wire encoding for the contract dataclasses this package sends over HTTP.

Kept explicit rather than generic. These payloads cross a version boundary
between two containers that may not be the same build, so an unexpected field
must be ignorable and a missing one must have a defined default.
"""

from __future__ import annotations

from dataclasses import asdict

from control_plane.contracts import DeviceClass, GpuProcess, NodeProfile, NodeState

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
    # Addressable first: on a GPU node that is the pool anything can be planned
    # into. A machine with no GPU has none, and falls back to the live host
    # total from its own sample -- otherwise its memory readout is a permanent
    # 0 that looks like an idle machine rather than an unmeasured one.
    denominator = state.profile.addressable_memory or state.memory_total
    if denominator <= 0:
        return 0.0
    pct = 100.0 * state.memory_used / denominator
    return round(min(pct, 100.0), 1)  # GB10: pool total can exceed addressable, so cap the reported figure at 100


def state_to_dict(state: NodeState) -> dict:
    return {
        "node_id": state.profile.node_id,
        "profile": profile_to_dict(state.profile),
        "healthy": state.healthy,
        "last_seen": state.last_seen,
        # None, not 0.0: "never sampled" and "sampled at the epoch" are
        # different answers and only one of them is real.
        "sample_ts": state.sample_ts or None,
        "memory_used": state.memory_used,
        "memory_total": state.memory_total,
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


def processes_to_dict(
    node_id: str, processes: list[GpuProcess] | None
) -> dict:
    """The resident-process payload, with its own reason for being empty.

    ``available: false`` and ``processes: []`` are different answers and the UI
    renders them differently. An unreadable nvidia-smi is not an idle GPU, and a
    container without ``--pid=host`` sees no compute apps at all -- telling an
    operator "nothing is resident" in either case is how they conclude the
    memory is free when it is not.
    """
    if processes is None:
        return {
            "node_id": node_id,
            "processes": [],
            "available": False,
            "reason": (
                "nvidia-smi did not answer on this node, so what is holding GPU "
                "memory could not be read."
            ),
        }
    if not processes:
        return {
            "node_id": node_id,
            "processes": [],
            "available": True,
            "reason": (
                "No compute contexts are resident. If a model is running here, "
                "the container is missing --pid=host and cannot see it."
            ),
        }
    return {
        "node_id": node_id,
        "processes": [p.as_dict() for p in processes],
        "available": True,
        "reason": None,
    }


def storage_to_dict(payload: dict | None, node_id: str) -> dict:
    """The storage payload, with its own reason for being empty.

    Same shape and same rule as ``processes_to_dict`` above: ``available:
    false`` is a different answer from an empty estate, and a storage screen
    that renders "0 bytes free" for a node it could not reach is how somebody
    concludes a disk is full when it is fine.
    """
    if payload is None:
        return {
            "node_id": node_id,
            "filesystems": [],
            "estate": [],
            "unreadable": [],
            "available": False,
            "reason": (
                "The data root could not be read on this node, so its disk "
                "usage is unknown."
            ),
        }
    out = dict(payload)
    out["node_id"] = node_id
    out["available"] = True
    out["reason"] = None
    return out
