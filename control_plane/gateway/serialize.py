"""Contract types to JSON.

Provider serialization is an explicit allowlist rather than a dataclass dump.
Provider API keys are the one unrecoverable mistake available here: this is a
tool people screenshot. A field can only reach a response if it is named below.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any

from control_plane.contracts.quant import quant_info
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

# Never rendered, whatever a port puts in the object. Imported rather than
# re-typed: the secret store decides what a key renders as, and two modules
# each holding their own "***" is two things to change on the day it stops
# being one. Imported here rather than referenced through `config.` because
# this module's own callers reach for `serialize.REDACTED`.
from control_plane.providers.config import REDACTED
# Re-exported: these read a NodeState, which the registry owns, and the
# registry's own payloads have to apply the same rule. Callers here reach
# for `serialize.power_reading`, so the names stay available from this
# module too.
from control_plane.registry.serde import power_reading, temp_reading
from control_plane.version import same_build

from . import ui_detail


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
INELIGIBLE_DEVICE_CLASS = (
    "device class is not recognized; cannot confirm this hardware is "
    "eligible to join the pool"
)


INELIGIBLE_BUILD_SKEW = (
    "this node is running an older build of derate than the coordinator, so "
    "what it reports about its own hardware may be out of date; re-run the "
    "installer on that machine"
)


def _eligibility(
    healthy: bool,
    device_class: DeviceClass,
    skewed: bool = False,
) -> tuple[bool, str | None]:
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

    What it no longer covers is a machine with no GPU. That used to land here
    too, and it is the one case where the hedge was simply wrong: a Raspberry
    Pi is not hardware we failed to identify, it is hardware we identified as
    having no GPU. ``probe.probe_local`` now says DeviceClass.CPU for it, and
    a CPU node is eligible by the rule this function already had -- eligible
    unless there is a concrete reason not to be. It still cannot carry a rank,
    but that refusal is `addressable_memory == 0`'s to make, said where the
    placement is actually attempted, and it names the real reason.
    """
    if not healthy:
        return False, _INELIGIBLE_UNHEALTHY
    if device_class is DeviceClass.UNKNOWN:
        # Build skew outranks the device class, the same way unhealthy
        # outranks both. The order is the order an operator should act in, and
        # this pair had the order wrong in the only way that matters: a
        # Raspberry Pi whose image predated the CPU probe reported UNKNOWN,
        # and the roster blamed the hardware. The machine was fine. Upgrading
        # the node is the first thing to try, and until it is tried nothing it
        # says about its own hardware is worth investigating.
        if skewed:
            return False, INELIGIBLE_BUILD_SKEW
        return False, INELIGIBLE_DEVICE_CLASS
    return True, None


def memory_used_pct(state: NodeState) -> float | None:
    """How full this node's memory is, or unknown when nothing can say.

    Physical GPU memory first, matching the architecture doc's topology
    payload. A machine with no GPU has none and falls back to the live host
    total its own sample carries -- without a denominator its memory readout is
    a permanent em dash, which reads as broken rather than as absent.

    Shared for the same reason ``power_reading`` is: the topology payload
    computed its own version that dropped the host-total fallback, so a GPU-less
    node answered a real percentage on /api/nodes and `null` on /api/topology at
    the same instant, for the same node, from the same sample.

    ``registry.serde.memory_used_pct`` divides by *addressable* memory instead,
    and that difference is intended: it is the planning view, and on a GB10 the
    addressable pool is a slice of the physical one. Two questions, two
    answers -- not a duplicate to collapse.
    """
    total = state.profile.total_memory or state.memory_total or 0
    if not total:
        return None
    return round(state.memory_used / total * 100.0, 1)


def node_payload(
    state: NodeState, label: str | None = None, coordinator_build: str | None = None
) -> dict:
    """One node row.

    `label` is the operator's display name and is deliberately a parameter
    rather than something read off the profile: the profile is re-probed from
    the machine on every join and would overwrite a name a human chose. It
    stays keyword-optional so a caller without a registry to ask still emits a
    valid row -- with no name, which is what "nobody renamed it" looks like.
    """
    profile = state.profile
    # Reported against physical memory, which is the basis Agent A's telemetry
    # and the topology payload in the architecture doc both use. Admission
    # control deliberately uses addressable memory instead: that is the slice
    # the GPU can actually reach and the one Agent D budgets a fit against.
    # Only a difference between two builds we can both name. Two unknowns are
    # not agreement, and one unknown is not a difference -- see version.py.
    skewed = not same_build(coordinator_build, state.build)
    eligible, ineligible_reason = _eligibility(
        state.healthy, profile.device_class, skewed
    )
    return {
        "node_id": profile.node_id,
        # The name to show. Null, never the node_id: the UI has to tell
        # "renamed to the same thing" from "never renamed", and only one of
        # those should follow the node_id when the id itself changes.
        "label": label or None,
        "hostname": profile.hostname,
        "address": profile.address,
        "device_class": profile.device_class.value,
        "gpu_name": profile.gpu_name,
        "gpu_count": profile.gpu_count,
        "total_memory": profile.total_memory,
        # Live, from the sample, and 0 until one arrives. Separate from
        # total_memory on purpose: that one is GPU memory and is summed into
        # cluster-wide totals, and host RAM no model can reach must not land
        # in that sum.
        "memory_total": state.memory_total,
        "addressable_memory": profile.addressable_memory,
        "memory_bandwidth_gbps": profile.memory_bandwidth_gbps,
        "compute_capability": profile.compute_capability,
        "driver_version": profile.driver_version,
        "healthy": state.healthy,
        "last_seen": state.last_seen,
        # When the four live readings below were last actually measured, as
        # opposed to when the agent last answered a health check. They diverge
        # whenever a node's telemetry source dies while its agent stays up.
        "sample_ts": state.sample_ts or None,
        "memory_used": state.memory_used,
        "memory_used_pct": memory_used_pct(state),
        # See power_reading/temp_reading: a machine with no GPU has no GPU
        # power draw to read, and 0 W would read as an idle one. Utilisation is
        # left alone -- an idle Pi really is at 0%.
        "power_w": power_reading(state),
        "temp_c": temp_reading(state),
        "util_pct": state.utilization_pct,
        "eligible": eligible,
        "ineligible_reason": ineligible_reason,
        # The machine the coordinator is itself running on. Not the same
        # question as `node_id == settings.coordinator_node_id`, which is what
        # the cluster and topology payloads answer from a setting that can be
        # stale or unset; this comes from the registry's own enrollment of its
        # own host, so it is true from the first instant and needs nothing
        # configured to be right.
        "is_coordinator_host": state.is_local,
        # Which build this node is running, and whether it differs from the
        # coordinator's. `build_skew` is never true on an absent id: a node
        # that has not said is not a node that disagrees.
        "build": state.build or None,
        "build_skew": skewed,
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
        # Provenance for the UI's link-provenance rail (audit M-7): which QSFP
        # cages were up, whose fabric view supplied that count, whether/how
        # GPUDirect RDMA was detected, and how long the probe itself took.
        # Same rule as the four fields above -- absent on a bare LinkMeasurement,
        # never defaulted, because a plain contract type says nothing about how
        # the number was obtained.
        payload["active_ports"] = annotation.active_ports
        payload["total_ports"] = annotation.total_ports
        payload["ports_inspected_on"] = annotation.ports_inspected_on
        payload["gdr_detected_by"] = annotation.gdr_detected_by
        payload["duration_s"] = annotation.duration_s
    return payload


def shape_payload(shape: ModelShape) -> dict:
    return plain(shape)


def _mtp_payload(breakdown: dict, warnings: list) -> dict | None:
    """The multi-token-prediction module, stated rather than silently dropped.

    The resolver excludes MTP parameters from ``total_params`` on purpose: no
    runtime loads that module unless speculative decoding is turned on, and
    charging it would refuse launches that would in fact have fit. But the
    weight index on the hub *does* count it, so a reader comparing our figure
    against the repo's own "N B parameters" finds a discrepancy with nothing to
    explain it. This surfaces the size of that gap and the resolver's own
    sentence about it, so the screen can say so.

    ``None`` when the checkpoint carries no MTP module, which is most of them.
    """
    params = int(breakdown.get("mtp") or 0)
    if params <= 0:
        return None
    note = next((w for w in warnings if "multi-token-prediction" in w), None)
    return {
        "params": params,
        "excluded_from_total": True,
        "note": note,
    }


def _capabilities(shape: Any, breakdown: dict, warnings: list) -> dict:
    """The architectural facts a reader needs, as numbers.

    Numbers only. "GQA 8:1" is typography and belongs to whichever component is
    laying the row out; the ratio itself is arithmetic and belongs here, so no
    client ever divides two head counts and gets a different answer.

    When a capability is absent every sibling is ``None`` rather than ``0``.
    They are not the same claim -- ``sliding_window: 0`` would say every layer
    is windowed -- and the UI already draws a null differently from a zero.
    """
    experts = shape.num_experts or 0
    kv_heads = shape.num_kv_heads or 0
    heads = shape.num_attention_heads or 0
    latent = shape.mla_latent_dim
    window = shape.sliding_window
    vision = shape.vision_params or 0
    mtp_params = int(breakdown.get("mtp") or 0)
    rope = getattr(shape, "effective_mla_rope_dim", 0) or 0

    return {
        "moe": {
            "present": bool(experts),
            "num_experts": experts or None,
            "num_experts_per_token": (shape.num_experts_per_token or None) if experts else None,
            "active_params": shape.effective_active_params if experts else None,
            # Read straight off the shape rather than divided again downstream.
            "active_fraction": (
                (shape.effective_active_params / shape.total_params)
                if experts and shape.total_params
                else None
            ),
        },
        "mla": {
            "present": latent is not None,
            "latent_dim": latent,
            "rope_dim": rope if latent is not None else None,
            # The width actually cached per layer per token. The latent alone
            # is not it, and reading `mla_latent_dim` as if it were understates
            # the KV cache on every DeepSeek checkpoint.
            "cached_width_per_layer": (latent + rope) if latent is not None else None,
        },
        "gqa": {
            "present": bool(kv_heads and heads > kv_heads),
            "num_attention_heads": heads or None,
            "num_kv_heads": kv_heads or None,
            "ratio": (heads / kv_heads) if kv_heads else None,
        },
        "sliding_window": {
            "present": window is not None,
            "window": window,
            # None means every layer caches the full context; 0 means every
            # layer is windowed. Anything between is a real interleave.
            "layers_with_full_attention": shape.layers_with_full_attention,
            "num_layers": shape.num_layers,
        },
        "vision": {"present": bool(vision), "vision_params": vision or None},
        "context": {"max_position_embeddings": None},  # filled by the caller
        "mtp": {
            "present": bool(mtp_params),
            "params": mtp_params or None,
            # The checkpoint carries the module; no runtime loads it unless
            # speculative decoding is on, so it is excluded from total_params.
            # Both figures ship so the gap can be stated instead of discovered.
            "counted_in_total_params": False if mtp_params else None,
            "total_params_with_mtp": (
                shape.total_params + mtp_params if mtp_params else None
            ),
            "note": next(
                (w for w in warnings if "multi-token-prediction" in w), None
            ),
        },
    }


def _launchable(res: Any, shape: Any) -> dict:
    """Whether this can be served here at all, and why not when it cannot.

    Mirrors what ``POST /api/deployments`` will decide, so a Serve button drawn
    from this cannot disagree with the launch it triggers. GGUF is the case
    that matters: both serve command templates take a repository path, not a
    ``.gguf`` file, and nothing in the runtime tables claims to load one.
    """
    model_id = str(shape.model_id or "")
    if model_id.startswith("hf://") or model_id.lower().endswith(".gguf"):
        return {
            "ok": False,
            "reason": (
                "a single GGUF file is not a launchable target: both serve "
                "commands take a repository path, not a file"
            ),
        }
    try:
        family = quant_info(shape.dtype).family
    except Exception:  # an unpriced dtype is the resolver's problem, not ours
        family = ""
    if family == "gguf":
        return {
            "ok": False,
            "reason": (
                f"{shape.dtype} is a llama.cpp format; neither vllm nor sglang "
                "is verified to load it, and there is no llama.cpp runtime here"
            ),
        }
    support = getattr(res, "support", None)
    if support is not None and not support.any_runtime_ok:
        entries = list(support.runtimes)
        return {
            "ok": False,
            # Verbatim: the runtime's own sentence is more precise than a
            # rewrite, and it is what the launch would have said.
            "reason": entries[0].reason if entries else "no runtime can load this",
        }
    return {"ok": True, "reason": ""}


def resolution_payload(res: Any) -> dict:
    """A resolver ``Resolution`` on the wire, with the arithmetic already done.

    Duck-typed on purpose. ``ResolverPort`` (contracts/ports.py) guarantees only
    ``resolve()``; ``resolve_full`` is an extra this gateway probes for with
    ``getattr``, and nothing in ``control_plane/gateway/**`` imports
    ``control_plane.resolver``. Reading attributes off whatever the port handed
    back keeps that decoupling, and keeps a stub resolver serializable.

    Every derived number is computed here rather than on the client. The UI's
    standing rule is that it never recomputes anything the backend owns -- a
    second implementation of a GQA ratio or a byte count is a second answer that
    can disagree with the real one -- so the client gets labels and layout, and
    nothing to calculate.
    """
    shape = res.shape
    breakdown = dict(getattr(res, "param_breakdown", {}) or {})
    warnings = list(getattr(res, "warnings", []) or [])

    # `as_dict()` emits `total` but not `total_with_mtp`; the checkpoint figure
    # is what a hub weight index reports, so both belong on the wire.
    if breakdown and "total_with_mtp" not in breakdown:
        breakdown["total_with_mtp"] = int(breakdown.get("total") or 0) + int(
            breakdown.get("mtp") or 0
        )

    kv_heads = shape.num_kv_heads or 0
    payload: dict[str, Any] = {
        "model_id": shape.model_id,
        "revision": getattr(res, "revision", None),
        "model_type": getattr(res, "model_type", ""),
        "architectures": list(getattr(res, "architectures", ()) or ()),
        # Which endpoint family this model answers on, so the screen offering
        # to serve it can name the route it would be served on. Duck-typed
        # like everything else here: a resolver port that predates it reports
        # None, and the UI treats that as text, which is what every model was
        # before audio existed.
        "modality": getattr(res, "modality", None),
        "max_position_embeddings": getattr(res, "max_position_embeddings", None),
        "shape": shape_payload(shape),
        # Derived, so the client never divides anything.
        "is_moe": shape.is_moe,
        "active_params_effective": shape.effective_active_params,
        "effective_head_dim": shape.effective_head_dim,
        # None rather than a fabricated 1 when a config never said how many KV
        # heads there are: an invented 1:1 ratio reads as MHA, which is a claim.
        "gqa_ratio": (shape.num_attention_heads / kv_heads) if kv_heads else None,
        "bytes_per_param": shape.bytes_per_param(),
        "weight_bytes": getattr(res, "weight_bytes", None),
        "param_breakdown": breakdown,
        "mtp": _mtp_payload(breakdown, warnings),
        "warnings": warnings,
        "from_cache": getattr(res, "from_cache", False),
        "resolved_at": getattr(res, "resolved_at", 0.0),
        "elapsed_ms": getattr(res, "elapsed_ms", 0.0),
    }

    # Provenance. These are enums on the dataclass and plain strings once
    # cached, so normalize rather than assuming either spelling.
    for key in ("param_source", "quant_source"):
        source = getattr(res, key, None)
        payload[key] = getattr(source, "value", source)

    effective = getattr(res, "effective_weight_bytes", None)
    payload["weight_bytes_effective"] = effective() if callable(effective) else None

    support = getattr(res, "support", None)
    if support is not None:
        quant = support.quant
        payload["support"] = {
            "architectures": list(support.architectures or ()),
            "runtimes": [
                {
                    "runtime": entry.runtime,
                    "level": getattr(entry.level, "value", entry.level),
                    "reason": entry.reason,
                    "version": entry.version,
                }
                for entry in support.runtimes
            ],
            "quant": {
                "dtype": quant.dtype,
                "native_compute_capability": quant.native_compute_capability,
                "emulated_below_native": quant.emulated_below_native,
                "note": quant.note,
            },
        }
    else:
        payload["support"] = None

    capabilities = _capabilities(shape, breakdown, warnings)
    capabilities["context"]["max_position_embeddings"] = payload[
        "max_position_embeddings"
    ]
    payload["capabilities"] = capabilities
    payload["launchable"] = _launchable(res, shape)
    payload["effective_mla_rope_dim"] = getattr(shape, "effective_mla_rope_dim", None)

    return payload


def plan_degrees_payload(plan) -> dict:
    """One legal shape, degrees and hosts only -- no prose.

    The compact form of a plan, for the list of alternatives. Deliberately
    carries neither `reason` nor `rejected`: shipping a rejection list for every
    legal shape on a wide cluster is kilobytes of text to populate a hint, and
    the moment one is actually chosen a re-plan returns that shape's full
    planner prose anyway.
    """
    return {
        "kind": plan.kind.value,
        "world_size": plan.world_size,
        "tensor_parallel": plan.tensor_parallel,
        "pipeline_parallel": plan.pipeline_parallel,
        "expert_parallel": plan.expert_parallel,
        "data_parallel": plan.data_parallel,
        "node_ids": list(plan.node_ids),
    }


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
        "modality": deployment.modality.value,
        "started_at": deployment.started_at,
        "last_error": deployment.last_error,
        "node_ids": list(deployment.plan.node_ids) if deployment.plan else [],
        "plan": plan_payload(deployment.plan) if deployment.plan else None,
        "fit": fit_payload(deployment.fit) if deployment.fit else None,
        "extra_args": list(deployment.extra_args),
        "custom_command": list(deployment.custom_command),
    }


def provider_model_payload(model: ProviderModel) -> dict:
    return {
        "served_name": model.served_name,
        "upstream_id": model.upstream_id,
        "context_length": model.context_length,
        "supports_streaming": model.supports_streaming,
        "supports_tools": model.supports_tools,
        "modality": model.modality.value,
        "input_cost_per_mtok": model.input_cost_per_mtok,
        "output_cost_per_mtok": model.output_cost_per_mtok,
    }


def provider_payload(
    provider: Provider,
    spend: dict[str, Any] | None = None,
    key: dict[str, Any] | None = None,
) -> dict:
    """Allowlist. ``api_key_ref`` is a reference -- an env var name or secret
    key -- and is safe to show; it is what the UI needs to tell the user which
    variable to set. Any resolved key material is rendered as ``***`` and
    nothing else, and no other field is emitted at all.

    *spend* is this provider's own entry from ``ui_detail.provider_spend`` --
    the call site fetches the whole port once per request and passes one
    provider's slice in here, never the port itself. Absent (the default)
    behaves exactly like a port that does not account: the nine spend keys
    are all ``None``, never ``0``.

    *key* is this provider's ``key_status()`` -- whether its reference
    resolves and which of the environment and secrets.json answered. State and
    provenance only; it carries no key material and could not, since neither
    field is ever a value. Absent leaves both ``None``, which reads as "this
    port cannot say", not as "there is no key".
    """
    payload = {
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
    payload.update(ui_detail.spend_fields(spend))
    # Always both keys, so the shape does not change under a port that has no
    # key_status(): a missing field and a null field read the same to a UI, and
    # only one of them survives a round trip through JSON.
    payload["key_state"] = (key or {}).get("key_state")
    payload["key_source"] = (key or {}).get("key_source")
    return payload


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
    counters: dict[str, dict[str, Any]] | None = None,
    strength_raw: dict[str, float] | None = None,
    admission_blocks: dict[str, list[str]] | None = None,
) -> dict:
    sources = sources or {}
    circuits = circuits or {}
    zero_weight_reasons = zero_weight_reasons or {}
    node_ids = node_ids or {}
    counters = counters or {}
    strength_raw = strength_raw or {}
    admission_blocks = admission_blocks or {}
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
                # Per-target request accounting (None-vs-0 semantics live in
                # ui_detail.target_counters); absent only if the caller never
                # asked for this target at all.
                "counters": counters.get(t.target_id),
                # The un-normalized score behind `strength` above. Present
                # exactly where strength_source is: both come from the same
                # index.raw_strength entry, one field apiece.
                "strength_raw": strength_raw.get(t.target_id),
                # Why this target is not admitting, sorted; null when nothing
                # blocks it.
                "admission_blocks": admission_blocks.get(t.target_id),
            }
            for t in config.targets
        ],
    }
