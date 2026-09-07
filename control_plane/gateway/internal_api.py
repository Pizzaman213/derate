"""The internal API. Exactly the surface in architecture section 4.8.

No additions without updating that file: Agent H is coding against it in
parallel. Where a port does not yet expose an operation the HTTP surface
promises, the endpoint degrades with a clear 501 rather than disappearing,
so the UI can be built against the full shape from day 0.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from functools import partial
import time
from pathlib import Path

from fastapi import APIRouter, Query, Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from control_plane.contracts import (
    DeviceClass,
    DeploymentState,
    FitRequest,
    Modality,
    RoutingPolicy,
    Verdict,
)
from control_plane.providers import UnknownProviderError
from control_plane.planner import (
    Candidate,
    IllegalDegrees,
    homogeneous_groups,
    pooling_note,
    valid_ep_degrees,
    valid_pp_degrees,
    valid_tp_degrees,
)
from control_plane.registry import JoinRejected, NodeNotFound
from control_plane.registry import modelcache, storage as registry_storage
from control_plane.registry.serde import profile_from_dict

from control_plane.telemetry import query as tquery

from . import errors, gpu_procs, livefit, serialize, ui_detail
from .deps import GatewayContext

log = logging.getLogger("gateway.api")


#: Named for exactly what it overrides, so the request body says it.
_OVERRIDE_PARAM = "allow_over_live_memory"

#: The second override, same rule: named for exactly what it overrides. The two
#: are independent and neither implies the other -- pooling unlike hardware says
#: nothing about whether the memory is there, and free memory says nothing about
#: whether the machines belong in one pool.
_MIXED_HW_PARAM = "allow_mixed_hardware"

#: Request keys that make placement the operator's rather than the planner's.
_PLACEMENT_PARAM = "node_ids"
_DEGREES_PARAM = "parallelism"

#: The four axes a caller may name. A key omitted from `parallelism` means 1,
#: never "whatever the planner would have picked": defaulting to the
#: recommendation would make the launched shape depend on a recommendation the
#: operator never saw, and which can change between the preview round trip and
#: the launch round trip.
_DEGREE_AXES = (
    "tensor_parallel",
    "pipeline_parallel",
    "expert_parallel",
    "data_parallel",
)


class _PlacementRefused(Exception):
    """A placement the gateway will not plan, with the sentence saying why.

    Deliberately not a ValueError: the generic `except ValueError` in both
    routes turns anything it catches into a bare 400 `invalid_request`, which
    would strip the code, the param and the override block that make these
    refusals actionable.
    """

    def __init__(
        self,
        status: int,
        message: str,
        code: str,
        *,
        param: str | None = None,
        extra: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.param = param
        self.extra = extra or {}

    def response(self) -> JSONResponse:
        body = errors.error_body(
            self.message, "invalid_request_error", self.code, param=self.param
        )
        # Siblings of `error`, not children of it -- the same body shape the
        # live-memory 409 uses, so one client branch reads both.
        body.update(self.extra)
        return JSONResponse(status_code=self.status, content=body)


@dataclasses.dataclass(frozen=True)
class _PlanOutcome:
    """One dry run: both verdicts, the memory picture behind them, and the
    single field the UI reads to decide what the Serve button may do."""

    shape: object
    plan: object
    fit: object | None
    fit_live: object | None
    capacity: dict
    serve: dict
    context_length: int
    concurrency: int
    resolver_warnings: list
    #: Which endpoint family this model answers on, taken from the
    #: resolution that produced the shape rather than looked up again
    #: later: a cold or cleared cache would otherwise silently record a
    #: speech model as text, and the gateway would then refuse the very
    #: requests the deployment exists to serve.
    modality: object = Modality.TEXT
    #: What was placed where, and whose choice it was. Always present; `mode`
    #: is "planner" for a request that named no nodes.
    placement: dict = dataclasses.field(default_factory=dict)
    #: The effective degrees and whether the operator or the planner set them.
    degrees: dict = dataclasses.field(default_factory=dict)
    #: The planner's own pick over the same node set, with its reason and
    #: rejected list intact, so an overruled recommendation is still on screen.
    recommended: object | None = None
    #: Every legal shape on this node set, compact (degrees and kind only, no
    #: prose): enough to say what is legal without shipping a rejection list
    #: per entry.
    alternatives: list = dataclasses.field(default_factory=list)

def _parse_node_ids(payload: dict) -> list[str] | None:
    """The machines the operator named, or None when they named none.

    None and `[]` are different requests. Absent means "the planner picks",
    which is what every request sent before this field existed meant. An
    explicit empty list means "plan on nothing", which has no honest answer --
    quietly treating it as absent would substitute a placement the caller did
    not ask for, which is the whole failure this feature exists to remove.
    """
    if _PLACEMENT_PARAM not in payload:
        return None
    raw = payload.get(_PLACEMENT_PARAM)
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(x, str) and x for x in raw):
        raise ValueError(f"{_PLACEMENT_PARAM} must be a list of node id strings")
    if not raw:
        raise ValueError(
            f"{_PLACEMENT_PARAM} is empty. Name at least one machine, or omit "
            f"the field to let the planner choose."
        )
    seen: set[str] = set()
    for node_id in raw:
        if node_id in seen:
            raise ValueError(f"{_PLACEMENT_PARAM} names {node_id} more than once")
        seen.add(node_id)
    # Order is preserved deliberately: the first node is the pipeline head that
    # sparkrun SSHes to first, so sorting here would silently move it.
    return list(raw)


def _parse_degrees(payload: dict) -> dict[str, int] | None:
    """The degrees the operator set, or None when they set none.

    A key omitted from the object means 1, never "whatever the planner would
    have picked". Defaulting to the recommendation would make the launched
    shape depend on a recommendation the operator never saw, and one that can
    change between the preview round trip and the launch round trip.
    """
    if _DEGREES_PARAM not in payload:
        return None
    raw = payload.get(_DEGREES_PARAM)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"{_DEGREES_PARAM} must be an object of degree names to integers")
    unknown = sorted(set(raw) - set(_DEGREE_AXES))
    if unknown:
        raise ValueError(
            f"{_DEGREES_PARAM} has no axis {', '.join(unknown)}. "
            f"Valid axes: {', '.join(_DEGREE_AXES)}."
        )
    out: dict[str, int] = {}
    for axis in _DEGREE_AXES:
        value = raw.get(axis, 1)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{_DEGREES_PARAM}.{axis} must be an integer")
        out[axis] = value
    return out


def _legal_degrees(shape, node_count: int) -> dict[str, list[int]]:
    """What each axis could legally have been, so a refusal is actionable."""
    return {
        "tensor_parallel": sorted(valid_tp_degrees(shape, node_count)),
        "pipeline_parallel": sorted(valid_pp_degrees(shape, node_count)),
        "expert_parallel": sorted(valid_ep_degrees(shape, node_count)),
    }


def _rejection_for(recommended, plan) -> str | None:
    """The recommendation's rejection line for the shape actually planned.

    None when the planner never rejected it -- the shape may simply have ranked
    second, which is not a warning -- or when the plan IS the recommendation.
    """
    if recommended is plan:
        return None
    label = Candidate(
        tp=plan.tensor_parallel,
        pp=plan.pipeline_parallel,
        ep=plan.expert_parallel,
        dp=plan.data_parallel,
    ).label()
    prefix = f"{label}:"
    return next((r for r in recommended.rejected if r.startswith(prefix)), None)


def _rank_plans(
    planner,
    shape,
    nodes,
    link,
    target,
    concurrency,
    *,
    context_length,
    kv_dtype,
    allow_mixed_hardware,
):
    """The ranked plans, best first, from whichever surface this planner has.

    `alternatives` is the planner's own extra API and is what `plan` calls
    internally, so asking for the whole list costs nothing beyond the one call
    that was already happening. A planner exposing only the frozen port still
    works; it just cannot say what it ranked second.
    """
    kwargs = {"context_length": context_length, "kv_dtype": kv_dtype}
    alternatives = getattr(planner, "alternatives", None)
    if callable(alternatives):
        return alternatives(
            shape,
            nodes,
            link,
            target,
            concurrency,
            allow_mixed_hardware=allow_mixed_hardware,
            **kwargs,
        )
    try:
        return [planner.plan(shape, nodes, link, target, concurrency, **kwargs)]
    except TypeError:
        # A port implementation that takes only the frozen five.
        return [planner.plan(shape, nodes, link, target, concurrency)]


def _select_nodes(registry, nodes, requested, warnings: list[str]):
    """The named machines, in the order named, or a refusal saying why not.

    Three cases, told apart with `registry.get_node` so the reason is the real
    one rather than a guess:

      not enrolled  -> 400, naming the machines that are.
      not healthy   -> 409. The request is well formed and legal; the conflict
                       is with machine state, and an unchanged retry succeeds
                       once the machine comes back. Silently dropping it would
                       serve on fewer machines than were asked for.
      device class
      unrecognised  -> allowed, with a warning. `serialize._eligibility` says
                       in as many words that nothing downstream filters
                       placement on device class, and turning that advisory
                       flag into an enforcement point here would block machines
                       whose profile is entirely real -- the registry's parse
                       fallback produces UNKNOWN for a profile it could not
                       label, not for one it could not read.
    """
    healthy = {n.node_id: n for n in nodes}
    chosen = []
    for node_id in requested:
        profile = healthy.get(node_id)
        if profile is None:
            state = None
            try:
                state = registry.get_node(node_id)
            except Exception:
                log.exception("registry lookup failed for %s while planning", node_id)
            if state is None:
                raise _PlacementRefused(
                    400,
                    errors.unknown_node_message(node_id, sorted(healthy)),
                    "unknown_node",
                    param=_PLACEMENT_PARAM,
                    extra={"available_node_ids": sorted(healthy)},
                )
            raise _PlacementRefused(
                409,
                f"The machine '{node_id}' is enrolled but not healthy right "
                f"now, so nothing can be placed on it. Wait for it to come "
                f"back, or deselect it.",
                "node_unhealthy",
                param=_PLACEMENT_PARAM,
                extra={"unhealthy_node_ids": [node_id]},
            )
        # A machine that reports no GPU memory cannot carry a rank. The fit
        # gate already drops these from the live budget so they cannot become
        # the argmin (`livefit.drop_zero_addressable`); naming one explicitly
        # deserves the same answer said out loud, rather than a plan built
        # around a machine that can hold nothing.
        if profile.addressable_memory <= 0:
            raise _PlacementRefused(
                400,
                f"The machine '{node_id}' reports no addressable GPU memory, "
                f"so nothing can be placed on it. It can still be a cluster "
                f"member; it cannot be a serving node.",
                "node_has_no_memory",
                param=_PLACEMENT_PARAM,
                extra={"unusable_node_ids": [node_id]},
            )
        if profile.device_class is DeviceClass.UNKNOWN:
            warnings.append(serialize.INELIGIBLE_DEVICE_CLASS)
        chosen.append(profile)
    return chosen


def _check_every_node_used(plan, nodes, requested_nodes, requested_degrees) -> None:
    """Refuse a selection the chosen degrees cannot fill.

    Only when the operator specified BOTH halves. Naming the machines alone
    leaves the planner's opinion about how many ranks to run on them as advice,
    and it explains itself in its own reason; the shortfall is then reported in
    `placement.unused_node_ids` rather than refused. Naming both and leaving a
    machine rankless is a contradiction inside one request, and sparkrun will
    not catch it -- it launches the degrees it is given against the hosts it is
    given, without complaint.
    """
    if requested_nodes is None or requested_degrees is None:
        return
    used = set(plan.node_ids)
    idle = [n.node_id for n in nodes if n.node_id not in used]
    if not idle:
        return
    degrees = " x ".join(
        f"{label} {value}"
        for label, value in (
            ("TP", plan.tensor_parallel),
            ("PP", plan.pipeline_parallel),
            ("DP", plan.data_parallel),
        )
    )
    raise _PlacementRefused(
        400,
        f"{_PLACEMENT_PARAM} names {len(nodes)} "
        f"{'machine' if len(nodes) == 1 else 'machines'} but the chosen degrees "
        f"are {degrees} = {plan.world_size} "
        f"{'rank' if plan.world_size == 1 else 'ranks'}, so "
        f"{', '.join(idle)} would carry no rank while still being named as a "
        f"serving node. Deselect it, or raise a degree so every named machine "
        f"is used.",
        "placement_underfilled",
        param=_PLACEMENT_PARAM,
        extra={"unused_node_ids": idle},
    )


def _modality_of(architectures) -> Modality:
    """The endpoint family these architectures answer on.

    Taken from the resolution in hand rather than from a later cache lookup:
    `_architectures_for` reaches back into the shape cache, and a cold or
    cleared cache would report no architecture, silently recording a speech
    model as text. The gateway would then refuse the very requests the
    deployment exists to serve, with nothing on screen connecting the two.

    Imported lazily so `resolver/` stays an optional dependency of the gateway,
    exactly as `resolve_full` and `supported_by` already are.
    """
    try:
        from control_plane.resolver.support import modality_for
    except Exception:
        return Modality.TEXT
    try:
        return Modality(modality_for(architectures))
    except ValueError:
        # The resolver knows a family this build has no Modality for. Text
        # keeps it on the routes it was already offered on.
        return Modality.TEXT


#: A node agent that cannot answer a read in this long is treated as down.
_AGENT_TIMEOUT_S = 5.0
#: SIGTERM grace (10s) plus SIGKILL wait (5s) on the agent side, plus slack.
_KILL_TIMEOUT_S = 25.0
#: Removing 182 GiB of blobs is a lot of unlink syscalls. Generous, because
#: the alternative is a timeout on a delete that is actually succeeding.
_DELETE_TIMEOUT_S = 120.0

#: A deployment past these is not serving anything and its weights are fair
#: game. Spelled from the contract enum rather than imported from
#: ``deploy.fsm.TERMINAL``, because ``from control_plane.deploy import fsm``
#: runs the package __init__ and pulls the manager, the sparkrun adapter and
#: the event bus into the gateway's request module to obtain one frozenset.
#: ``test_modelcache`` asserts this set and ``fsm.TERMINAL`` stay equal, so the
#: two cannot drift apart.
_TERMINAL_STATES = frozenset({DeploymentState.FAILED, DeploymentState.STOPPED})
def _agent_detail(res) -> str:
    """The sentence a node agent put in its refusal, however it wrapped it.

    FastAPI's HTTPException nests ours under "detail", and a detail may itself
    be the {code, message} object procs.py raises. Falling back to the raw body
    keeps an unexpected shape readable instead of printing "unknown error".
    """
    try:
        body = res.json()
    except Exception:
        return (res.text or "").strip() or f"HTTP {res.status_code}"
    detail = body.get("detail", body) if isinstance(body, dict) else body
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("detail") or detail)
    return str(detail)


STALE_LINK_AGE_S = 7 * 24 * 3600
_FABRIC_METHODS = {"nccl-tests", "ib_write_bw"}


def _medium(method: str) -> str:
    return "connectx-7" if method in _FABRIC_METHODS else "ethernet"


def _plan_label(plan) -> str:
    if plan is None:
        return "unknown"
    parts = []
    if plan.tensor_parallel > 1:
        parts.append(f"TP {plan.tensor_parallel}")
    if plan.pipeline_parallel > 1:
        parts.append(f"PP {plan.pipeline_parallel}")
    if plan.expert_parallel > 1:
        parts.append(f"EP {plan.expert_parallel}")
    if plan.data_parallel > 1:
        parts.append(f"DP {plan.data_parallel}")
    return " + ".join(parts) if parts else "single node"


def _not_implemented(operation: str, owner: str) -> JSONResponse:
    return errors.error_response(
        501,
        f"{operation} is not wired up yet: the {owner} does not expose it.",
        "server_error",
        "not_implemented",
    )


def create_router(ctx: GatewayContext) -> APIRouter:
    router = APIRouter()
    settings = ctx.settings

    def _nodes():
        try:
            return ctx.deps.registry.list_nodes()
        except Exception:
            log.exception("registry unavailable")
            return []

    def _labels() -> dict[str, str]:
        """node_id -> operator-chosen display name, for the ids that have one.

        Read through a getattr because RegistryPort does not require it: a
        registry build without labels is not an error, it is a cluster where
        nothing has been renamed, and every payload below degrades to the
        node_id on its own.
        """
        labels = getattr(ctx.deps.registry, "node_labels", None)
        if not callable(labels):
            return {}
        try:
            return labels()
        except Exception:
            log.exception("registry labels unavailable")
            return {}

    def _links_for(node_ids: list[str]):
        """Every pair, measured or not. Never emit a bandwidth figure that was
        not measured: an unmeasured pair carries measured=false and no numbers.
        """
        out = []
        for i, a in enumerate(node_ids):
            for b in node_ids[i + 1 :]:
                try:
                    link = ctx.deps.links.get(a, b)
                except Exception:
                    log.exception("link lookup failed for %s/%s", a, b)
                    link = None
                if link is None:
                    out.append({"src": a, "dst": b, "measured": False, "stale": False})
                    continue
                payload = serialize.link_payload(link)
                payload["measured"] = True
                payload["stale"] = (time.time() - link.measured_at) > STALE_LINK_AGE_S
                payload["medium"] = _medium(link.method)
                out.append(payload)
        return out

    # -- cluster and topology ---------------------------------------------

    @router.get("/api/cluster")
    async def cluster() -> JSONResponse:
        nodes = _nodes()
        node_ids = [n.profile.node_id for n in nodes]
        labels = _labels()
        index = ctx.router.index()
        window = settings.metrics_rate_window_s
        return JSONResponse(
            {
                "cluster_id": settings.cluster_id,
                "coordinator": settings.coordinator_node_id,
                "nodes": [
                    serialize.node_payload(n, labels.get(n.profile.node_id))
                    for n in nodes
                ],
                "links": _links_for(node_ids),
                "summary": {
                    "node_count": len(nodes),
                    "healthy_nodes": sum(1 for n in nodes if n.healthy),
                    "total_memory": sum(n.profile.total_memory for n in nodes),
                    "total_power_w": round(sum(n.power_watts or 0.0 for n in nodes), 1),
                    "model_count": len(index.targets),
                    "deployment_count": len(index.deployments),
                    "tokens_per_sec": round(
                        ctx.stats.total_tokens_per_sec(window), 1
                    ),
                    "degraded_startup": ctx.degraded_startup,
                },
            }
        )

    @router.get("/api/topology")
    async def topology() -> JSONResponse:
        nodes = _nodes()
        labels = _labels()
        index = ctx.router.index()

        by_node: dict[str, list[str]] = {}
        strength_by_node: dict[str, float] = {}
        for target_id, deployment in index.deployments.items():
            target = next(
                (
                    t
                    for targets in index.targets.values()
                    for t in targets
                    if t.target_id == target_id
                ),
                None,
            )
            for node_id in (deployment.plan.node_ids if deployment.plan else []):
                by_node.setdefault(node_id, []).append(deployment.deployment_id)
                if target is not None:
                    strength_by_node[node_id] = max(
                        strength_by_node.get(node_id, 0.0), target.strength
                    )

        # Nodes with nothing deployed still need a strength for the UI. Fall
        # back to the last rung of the ladder, normalized across the cluster.
        raw_hw = {
            n.profile.node_id: n.profile.memory_bandwidth_gbps
            * max(1, n.profile.gpu_count)
            for n in nodes
        }
        top_hw = max(raw_hw.values()) if raw_hw else 0.0

        window = settings.metrics_rate_window_s
        node_payloads = []
        for node in nodes:
            node_id = node.profile.node_id
            total_mem = node.profile.total_memory or 0
            strength = strength_by_node.get(node_id)
            if strength is None:
                strength = raw_hw.get(node_id, 0.0) / top_hw if top_hw else 0.0
            node_payloads.append(
                {
                    "node_id": node_id,
                    # Same rule as serialize.node_payload: null when nobody
                    # renamed it, never a copy of the node_id.
                    "label": labels.get(node_id) or None,
                    "hostname": node.profile.hostname,
                    "device_class": node.profile.device_class.value,
                    "gpu_name": node.profile.gpu_name,
                    "state": "healthy" if node.healthy else "unhealthy",
                    "role": "coordinator"
                    if node_id == settings.coordinator_node_id
                    else "worker",
                    "memory_used_pct": round(node.memory_used / total_mem * 100.0, 1)
                    if total_mem
                    else None,
                    "power_w": node.power_watts,
                    "temp_c": node.temperature_c,
                    "util_pct": node.utilization_pct,
                    "sample_ts": node.sample_ts or None,
                    "strength": round(strength, 4),
                    "deployments": by_node.get(node_id, []),
                }
            )

        deployment_payloads = []
        for deployment in index.deployments.values():
            st = ctx.stats.peek(deployment.deployment_id)
            deployment_payloads.append(
                {
                    "deployment_id": deployment.deployment_id,
                    "served_name": deployment.served_name,
                    "node_ids": list(deployment.plan.node_ids)
                    if deployment.plan
                    else [],
                    "state": deployment.state.value,
                    "plan": _plan_label(deployment.plan),
                    "tokens_per_sec": round(st.tokens_per_sec(window), 1) if st else 0.0,
                }
            )

        return JSONResponse(
            {
                "cluster_id": settings.cluster_id,
                "coordinator": settings.coordinator_node_id,
                "nodes": node_payloads,
                "edges": _links_for([n.profile.node_id for n in nodes]),
                "deployments": deployment_payloads,
            }
        )

    # -- nodes -------------------------------------------------------------

    @router.get("/api/nodes")
    async def list_nodes() -> JSONResponse:
        labels = _labels()
        return JSONResponse(
            [
                serialize.node_payload(n, labels.get(n.profile.node_id))
                for n in _nodes()
            ]
        )

    @router.get("/api/nodes/candidates")
    async def node_candidates() -> JSONResponse:
        # Discovery proposes, a human accepts. Agent A owns the candidate set.
        candidates = getattr(ctx.deps.registry, "candidates", None)
        if not callable(candidates):
            return JSONResponse([])
        try:
            return JSONResponse([serialize.candidate_payload(c) for c in candidates()])
        except Exception:
            log.exception("candidate listing failed")
            return JSONResponse([])

    async def _agent_processes(node_id: str) -> tuple[dict | None, Response | None]:
        """The node agent's resident-process payload, annotated. (payload, error).

        The agent is the only thing that can see the machine's PIDs; the
        coordinator can only ask. A node we have no agent URL for is a 404 that
        says so, rather than an empty list that reads as an idle GPU.
        """
        agent_url = None
        lookup = getattr(ctx.deps.registry, "agent_url", None)
        if callable(lookup):
            try:
                agent_url = lookup(node_id)
            except Exception:
                log.exception("agent url lookup failed")
        if not agent_url:
            return None, errors.error_response(
                404,
                f"No agent URL for node '{node_id}'; its resident processes "
                "cannot be read.",
                "invalid_request_error",
                "node_agent_unreachable",
            )
        import httpx

        try:
            async with httpx.AsyncClient(timeout=_AGENT_TIMEOUT_S) as client:
                res = await client.get(f"{agent_url.rstrip('/')}/agent/processes")
                res.raise_for_status()
                payload = res.json()
        except Exception as exc:
            log.warning("process read failed for %s: %s", node_id, exc)
            return None, errors.error_response(
                502,
                f"The node agent on '{node_id}' did not answer: "
                f"{errors.detail(exc, _redactor())}",
                "server_error",
                "node_agent_unreachable",
            )

        try:
            deployments = ctx.deps.deployments.list()
        except Exception:
            log.exception("deployment listing failed")
            deployments = []
        handles_fn = getattr(ctx.deps.deployments, "handles", None)
        handles = None
        if callable(handles_fn):
            try:
                handles = handles_fn()
            except Exception:
                log.exception("handle listing failed")
        payload["processes"] = gpu_procs.attribute(
            payload.get("processes") or [], deployments, handles, node_id
        )
        return payload, None

    @router.get("/api/nodes/{node_id}/processes")
    async def node_processes(node_id: str) -> Response:
        payload, error = await _agent_processes(node_id)
        return error if error is not None else JSONResponse(payload)

    @router.delete("/api/nodes/{node_id}/processes/{pid}")
    async def kill_node_process(node_id: str, pid: int) -> Response:
        payload, error = await _agent_processes(node_id)
        if error is not None:
            return error
        target = gpu_procs.find(payload.get("processes") or [], pid)
        if target is None:
            return errors.error_response(
                404,
                f"PID {pid} is not holding GPU memory on '{node_id}'.",
                "invalid_request_error",
                "not_a_gpu_process",
            )
        if not target.get("killable", True):
            # Refused here rather than on the agent: the agent has no idea what
            # a deployment is, and this is the check that keeps the router from
            # dispatching to a backend somebody killed behind its back.
            return errors.error_response(
                409,
                target.get("not_killable_reason")
                or f"PID {pid} belongs to a running deployment.",
                "invalid_request_error",
                "process_is_managed",
            )

        token = None
        token_fn = getattr(ctx.deps.registry, "cluster_token", None)
        if callable(token_fn):
            try:
                token = token_fn()
            except Exception:
                log.exception("cluster token unavailable")
        if not token:
            return errors.error_response(
                503,
                "No cluster token is available, so the node agent cannot be "
                "asked to kill anything.",
                "server_error",
                "cluster_token_unavailable",
            )

        agent_url = ctx.deps.registry.agent_url(node_id)
        import httpx

        try:
            async with httpx.AsyncClient(timeout=_KILL_TIMEOUT_S) as client:
                res = await client.post(
                    f"{agent_url.rstrip('/')}/agent/processes/{pid}/kill",
                    headers={"X-Derate-Token": token},
                )
        except Exception as exc:
            log.exception("kill request failed")
            return errors.error_response(
                502,
                f"The node agent on '{node_id}' did not answer the kill: "
                f"{errors.detail(exc, _redactor())}",
                "server_error",
                "node_agent_unreachable",
            )
        if res.status_code >= 400:
            # The agent's own sentence, forwarded rather than paraphrased --
            # "the agent may not signal that owner" is a different fix from
            # "that PID is not on the GPU", and only it knows which happened.
            detail = _agent_detail(res)
            return errors.error_response(
                res.status_code if res.status_code != 403 else 502,
                f"The kill was refused on '{node_id}': {detail}",
                "invalid_request_error",
                "kill_refused",
            )
        return JSONResponse(res.json())

    @router.get("/api/nodes/{node_id}")
    async def get_node(node_id: str) -> JSONResponse:
        try:
            node = ctx.deps.registry.get_node(node_id)
        except Exception:
            log.exception("registry unavailable")
            node = None
        if node is None:
            return errors.error_response(
                404, f"No node '{node_id}'.", "invalid_request_error", "node_not_found"
            )
        return JSONResponse(serialize.node_payload(node, _labels().get(node_id)))

    @router.post("/api/nodes/join")
    async def join_node(request: Request) -> Response:
        handle_join = getattr(ctx.deps.registry, "handle_join", None)
        if not callable(handle_join):
            return _not_implemented("Node join", "registry")
        try:
            payload = await request.json()
        except Exception:
            return errors.error_response(
                400, "Join body must be JSON.", "invalid_request_error", "invalid_json"
            )
        try:
            token = payload.get("token")
            profile = profile_from_dict(payload["profile"])
            agent_url = str(payload["agent_url"])
        except (KeyError, TypeError, ValueError) as exc:
            return errors.error_response(
                400, f"Join body malformed: {exc}.",
                "invalid_request_error", "invalid_join_body",
            )
        try:
            result = await handle_join(token, profile, agent_url)
        except JoinRejected as exc:
            # Registry.handle_join raises this for three distinct causes --
            # a wrong token, a failed probe-back (e.g. unreachable agent_url,
            # bridge networking), or a node_id mismatch on probe-back -- and
            # carries which one as str(exc). Surface it rather than assuming
            # "bad token": an operator chasing a probe/network failure with a
            # token-rejected message goes looking for a fault that isn't
            # there. A missing token is not a rejection at all -- it comes
            # back as a "candidate" status below, see Registry.handle_join.
            return errors.error_response(
                403, f"Cluster join rejected: {exc}.",
                "invalid_request_error", "join_rejected",
            )
        except NotImplementedError:
            return _not_implemented("Node join", "registry")
        # Passes through with whatever status handle_join decided -- "member"
        # or "candidate" -- rather than the gateway interpreting it.
        return JSONResponse(serialize.plain(result))

    @router.post("/api/nodes/{node_id}/admit")
    async def admit_node(node_id: str) -> Response:
        admit = getattr(ctx.deps.registry, "admit", None)
        if not callable(admit):
            return _not_implemented("Node admission", "registry")
        try:
            return JSONResponse(serialize.plain(admit(node_id)))
        except NodeNotFound:
            return errors.error_response(
                404, f"No candidate '{node_id}'.",
                "invalid_request_error", "node_not_found",
            )

    @router.put("/api/nodes/{node_id}/label")
    async def rename_node(node_id: str, request: Request) -> Response:
        """Give a node a display name, or clear it with null / "".

        A rename touches nothing but the caption. `node_id` is the key every
        deployment, link measurement and routing target on disk was written
        against, so it stays exactly what it was -- which is also why this is
        a separate route rather than a PATCH on the node: there is no field on
        a node this can be confused with.
        """
        rename = getattr(ctx.deps.registry, "set_node_label", None)
        if not callable(rename):
            return _not_implemented("Renaming a node", "registry")
        try:
            payload = await request.json()
        except Exception:
            return errors.error_response(
                400, 'Body must be {"label": "a name"}.',
                "invalid_request_error", "invalid_json",
            )
        if not isinstance(payload, dict) or "label" not in payload:
            return errors.error_response(
                400, 'Body must be {"label": "a name"}. Send null to clear it.',
                "invalid_request_error", "invalid_request",
            )
        try:
            label = rename(node_id, payload["label"])
        except NodeNotFound:
            return errors.error_response(
                404, f"No node '{node_id}'.", "invalid_request_error", "node_not_found"
            )
        except ValueError as exc:
            # The registry's own sentence, which names what was wrong with the
            # name. Nothing here can improve on it.
            return errors.error_response(
                400, str(exc), "invalid_request_error", "invalid_label"
            )
        return JSONResponse({"node_id": node_id, "label": label})

    @router.delete("/api/nodes/{node_id}")
    async def remove_node(node_id: str) -> Response:
        remove = getattr(ctx.deps.registry, "remove_node", None)
        if not callable(remove):
            return _not_implemented("Node removal", "registry")
        try:
            remove(node_id)
        except NodeNotFound:
            return errors.error_response(
                404, f"No node '{node_id}'.", "invalid_request_error", "node_not_found"
            )
        return JSONResponse({"removed": node_id})

    # -- links -------------------------------------------------------------

    @router.get("/api/links")
    async def list_links() -> JSONResponse:
        return JSONResponse(_links_for([n.profile.node_id for n in _nodes()]))

    @router.post("/api/links/measure")
    async def measure_link(request: Request) -> Response:
        try:
            payload = await request.json()
            a, b = payload["a"], payload["b"]
        except Exception:
            return errors.error_response(
                400, "Body must be {\"a\": node_id, \"b\": node_id}.",
                "invalid_request_error", "invalid_request",
            )
        try:
            measurement = await asyncio.to_thread(ctx.deps.links.measure, a, b)
        except Exception as exc:
            log.exception("link measurement failed")
            return errors.error_response(
                502, f"Measurement failed. {errors.detail(exc, _redactor())}",
                "server_error", "measure_failed",
            )
        if measurement is None:
            # Every rung of the measurement ladder failed. Never fabricate a
            # bandwidth figure -- say plainly that nothing came back.
            return errors.error_response(
                503,
                f"Measurement between '{a}' and '{b}' failed: every "
                "measurement method (NCCL, RDMA probe, manual estimate) "
                "came back empty.",
                "server_error", "measurement_failed",
            )
        return JSONResponse(serialize.link_payload(measurement))

    @router.post("/api/links/reach")
    async def reach_check(request: Request) -> Response:
        """Can these two nodes reach each other? Seconds, not a minute.

        Deliberately a separate route from /api/links/measure rather than a
        mode of it. Measuring saturates the interconnect for about a minute
        and answers "how fast"; this answers "at all, and from which side",
        costs four health checks, and is safe to run against a cluster that is
        serving. Conflating them would put a disruptive operation behind a
        button an operator reasonably expects to be free.
        """
        check = getattr(ctx.deps.registry, "check_reach", None)
        if not callable(check):
            return _not_implemented("Reachability checks", "registry")
        try:
            payload = await request.json()
            a, b = payload["a"], payload["b"]
        except Exception:
            return errors.error_response(
                400, 'Body must be {"a": node_id, "b": node_id}.',
                "invalid_request_error", "invalid_request",
            )
        try:
            result = await check(a, b)
        except NodeNotFound as exc:
            return errors.error_response(
                404, f"{exc}.", "invalid_request_error", "node_not_found"
            )
        except Exception as exc:
            log.exception("reachability check failed")
            return errors.error_response(
                502, f"Reachability check failed. {errors.detail(exc, _redactor())}",
                "server_error", "reach_failed",
            )
        return JSONResponse(serialize.plain(result))

    # -- routing -----------------------------------------------------------

    def _routing_payload(index, config) -> dict:
        sources = {k: v.source for k, v in index.raw_strength.items()}
        circuits = ctx.breaker.opened_targets() if ctx.breaker else {}
        node_ids = {tid: index.node_ids_for(tid) for tid in index.deployments}
        target_ids = [t.target_id for t in config.targets]
        return serialize.routing_payload(
            config,
            sources,
            circuits,
            auto_selected=ctx.router.auto_selected(config.served_name),
            auto_reason=ctx.router.auto_reason(config.served_name),
            flow=ctx.router.flow(config.served_name, config.policy),
            zero_weight_reasons=index.zero_weight_reason,
            node_ids=node_ids,
            counters=ui_detail.target_counters(ctx.stats, target_ids),
            strength_raw=ui_detail.strength_raw(index),
            admission_blocks=ui_detail.admission_blocks(ctx.admission, target_ids),
        )

    @router.get("/api/routing")
    async def list_routing() -> JSONResponse:
        index = ctx.router.index()
        return JSONResponse(
            [_routing_payload(index, c) for c in ctx.router.configs()]
        )

    @router.put("/api/routing/{served_name}")
    async def set_routing(served_name: str, request: Request) -> Response:
        try:
            payload = await request.json()
            policy = RoutingPolicy(payload["policy"])
        except (KeyError, TypeError):
            return errors.error_response(
                400, "Body must be {\"policy\": <routing policy>}.",
                "invalid_request_error", "invalid_request",
            )
        except ValueError:
            return errors.error_response(
                400,
                "Unknown policy. Valid values: "
                + ", ".join(p.value for p in RoutingPolicy)
                + ".",
                "invalid_request_error",
                "invalid_policy",
            )
        except Exception:
            return errors.error_response(
                400, "Body must be JSON.", "invalid_request_error", "invalid_json"
            )
        try:
            config = ctx.router.set_policy(served_name, policy)
        except KeyError:
            return errors.error_response(
                404, f"No model '{served_name}' is being served.",
                "invalid_request_error", "model_not_found",
            )
        index = ctx.router.index()
        return JSONResponse(_routing_payload(index, config))

    # -- providers ---------------------------------------------------------

    @router.get("/api/providers")
    async def list_providers() -> JSONResponse:
        try:
            providers = ctx.deps.providers.list()
        except Exception:
            log.exception("provider listing failed")
            providers = []
        # Computed once per request, then sliced per provider below -- never
        # once per provider, which would repeat the underlying public_list()
        # call for no benefit.
        spend = ui_detail.provider_spend(ctx.deps.providers)
        return JSONResponse(
            [
                serialize.provider_payload(p, spend.get(p.provider_id))
                for p in providers
            ]
        )

    @router.post("/api/providers")
    async def add_provider(request: Request) -> Response:
        try:
            spec = await request.json()
        except Exception:
            return errors.error_response(
                400, "Body must be JSON.", "invalid_request_error", "invalid_json"
            )
        add_async = getattr(ctx.deps.providers, "add_async", None)
        try:
            if callable(add_async):
                provider = await add_async(spec)
            else:
                provider = await asyncio.to_thread(ctx.deps.providers.add, spec)
        except Exception as exc:
            log.exception("provider add failed")
            return errors.error_response(
                400, f"Could not add provider. {errors.detail(exc, _redactor())}",
                "invalid_request_error", "provider_add_failed",
            )
        ctx.router.rebuild(force_scores=True)
        spend = ui_detail.provider_spend(ctx.deps.providers)
        return JSONResponse(
            serialize.provider_payload(provider, spend.get(provider.provider_id)),
            status_code=201,
        )

    @router.patch("/api/providers/{provider_id}")
    async def patch_provider(provider_id: str, request: Request) -> Response:
        update = getattr(ctx.deps.providers, "update", None)
        if not callable(update):
            return _not_implemented("Provider update", "provider store")
        try:
            patch = await request.json()
        except Exception:
            return errors.error_response(
                400, "Body must be JSON.", "invalid_request_error", "invalid_json"
            )
        try:
            provider = update(provider_id, patch)
        except UnknownProviderError:
            return errors.error_response(
                404, f"No provider '{provider_id}'.",
                "invalid_request_error", "provider_not_found",
            )
        ctx.router.rebuild(force_scores=True)
        spend = ui_detail.provider_spend(ctx.deps.providers)
        return JSONResponse(
            serialize.provider_payload(provider, spend.get(provider.provider_id))
        )

    @router.delete("/api/providers/{provider_id}")
    async def delete_provider(provider_id: str) -> Response:
        remove = getattr(ctx.deps.providers, "remove", None)
        if not callable(remove):
            return _not_implemented("Provider removal", "provider store")
        try:
            remove(provider_id)
        except UnknownProviderError:
            return errors.error_response(
                404, f"No provider '{provider_id}'.",
                "invalid_request_error", "provider_not_found",
            )
        ctx.router.rebuild(force_scores=True)
        return JSONResponse({"removed": provider_id})

    @router.post("/api/providers/{provider_id}/refresh")
    async def refresh_provider(provider_id: str) -> Response:
        refresh_async = getattr(ctx.deps.providers, "refresh_async", None)
        try:
            if callable(refresh_async):
                provider = await refresh_async(provider_id)
            else:
                provider = await asyncio.to_thread(ctx.deps.providers.refresh, provider_id)
        except UnknownProviderError:
            return errors.error_response(
                404, f"No provider '{provider_id}'.",
                "invalid_request_error", "provider_not_found",
            )
        except Exception as exc:
            log.exception("provider refresh failed")
            return errors.error_response(
                502, f"Refresh failed. {errors.detail(exc, _redactor())}",
                "server_error", "refresh_failed",
            )
        ctx.router.rebuild(force_scores=True)
        spend = ui_detail.provider_spend(ctx.deps.providers)
        return JSONResponse(
            serialize.provider_payload(provider, spend.get(provider.provider_id))
        )

    @router.get("/api/providers/{provider_id}/models")
    async def provider_models(provider_id: str) -> Response:
        try:
            providers = ctx.deps.providers.list()
        except Exception:
            log.exception("provider listing failed")
            providers = []
        provider = next(
            (p for p in providers if p.provider_id == provider_id), None
        )
        if provider is None:
            return errors.error_response(
                404, f"No provider '{provider_id}'.",
                "invalid_request_error", "provider_not_found",
            )
        return JSONResponse(
            [serialize.provider_model_payload(m) for m in provider.models]
        )

    # -- plan and deployments ---------------------------------------------

    #: Resolver failures are not all the same failure, and a blanket 502 tells
    #: a caller nothing about whether to fix the request or retry it. Matched
    #: on class NAME rather than by importing control_plane.resolver: this
    #: gateway composes ports it does not own, and a third-party resolver
    #: raising its own ModelNotFound deserves the same answer.
    _PLAN_ERROR_STATUS: dict[str, tuple[int, str, str, dict]] = {
        "ModelNotFound": (
            404, "invalid_request_error", "model_not_found",
            {},
        ),
        "MetadataUnavailable": (
            502, "server_error", "metadata_unavailable",
            {"Retry-After": "5"},
        ),
        "UnsupportedArchitecture": (
            400, "invalid_request_error", "unsupported_architecture",
            {},
        ),
    }

    def _redactor():
        """The shared provider redactor, or None. Reached the way the telemetry
        service reaches it -- a fresh Redactor knows no secrets to scrub."""
        return getattr(ctx.deps.providers, "redactor", None)

    def _plan_error_response(exc: Exception, model_id: str | None) -> JSONResponse:
        """The refusal, carrying the reason instead of only the class name."""
        red = _redactor()
        status, type_, code, headers = _PLAN_ERROR_STATUS.get(
            type(exc).__name__, (502, "server_error", "plan_failed", {})
        )
        named = f" '{model_id}'" if model_id else ""
        lead = {
            "model_not_found": f"No model{named} could be found.",
            "metadata_unavailable": (
                f"Could not plan{named}: the model's metadata could not be "
                f"fetched."
            ),
            "unsupported_architecture": (
                f"Could not plan{named}: the architecture is not supported."
            ),
        }.get(code, f"Could not plan{named}.")
        return errors.error_response(
            status,
            f"{lead} {errors.detail(exc, red)}".strip(),
            type_,
            code,
            headers=headers or None,
            exception=type(exc).__name__,
            cause=errors.cause_chain(exc, red),
            model_id=model_id,
        )


    def _capacity_block(
        plan_nodes: list,
        budgets: dict,
        excluded: list,
        fit,
        fit_live,
    ) -> dict:
        """What the memory picture was at the moment the verdict was taken.

        The UI draws the live line from this and explains the refusal from it,
        so every figure is the one the gate actually used -- not a second
        computation that can disagree with it.
        """
        report = getattr(ctx.deps.registry, "memory_report", None)
        nodes_payload = []
        if callable(report):
            for node in plan_nodes:
                try:
                    one = report(node.node_id)
                except Exception:
                    log.exception("memory_report failed for %s", node.node_id)
                    one = None
                if one is not None:
                    nodes_payload.append(one)

        binding_node = None
        if budgets:
            binding_node = min(budgets, key=lambda k: budgets[k])

        return {
            "basis": (fit_live or fit).budget_basis if (fit_live or fit) else None,
            "measured_at": time.time(),
            "allocatable_per_node": (
                budgets[binding_node] if binding_node else None
            ),
            "static_per_node": fit.usable_per_node if fit else None,
            "binding_node": binding_node,
            "nodes": nodes_payload,
            "excluded": excluded,
        }


    async def _plan_and_fit(payload: dict):
        """Resolve, plan, check fit. Launches nothing.

        Every port call here is potentially slow (network resolution, a
        planner search, a fit calculation) and this runs inside an async
        route, so each one is pushed to a worker thread rather than blocking
        the event loop -- and with it every other in-flight request and
        stream (M-14).
        """
        model_id = payload.get("model_id")
        if not isinstance(model_id, str) or not model_id:
            raise ValueError("model_id is required")
        context_length = int(payload.get("context") or 8192)
        concurrency = int(payload.get("concurrency") or 1)
        target = payload.get("target") or "throughput"
        kv_dtype = payload.get("kv_dtype") or settings.default_kv_dtype
        dtype = payload.get("dtype")
        requested_nodes = _parse_node_ids(payload)
        requested_degrees = _parse_degrees(payload)

        # Prefer resolve_full when the port exposes it: it carries warnings
        # worth showing and, on mixed-precision repos, real measured weight
        # bytes that beat total_params * bytes_per_param (H-10).
        resolve_full = getattr(ctx.deps.resolver, "resolve_full", None)
        resolver_warnings: list[str] = []
        weight_bytes: int | None = None
        modality = Modality.TEXT
        if callable(resolve_full):
            resolution = await asyncio.to_thread(resolve_full, model_id, dtype)
            shape = resolution.shape
            resolver_warnings = list(resolution.warnings)
            weight_bytes = resolution.effective_weight_bytes()
            modality = _modality_of(resolution.architectures)
        else:
            shape = await asyncio.to_thread(ctx.deps.resolver.resolve, model_id, dtype)

        try:
            nodes = [n.profile for n in ctx.deps.registry.healthy_nodes()]
        except Exception:
            log.exception("registry unavailable while planning")
            nodes = []

        # Narrow here, before the link lookup, so `worst_all_reduce` measures
        # only the path this deployment will actually cross. Filtering later
        # would leave `plan.measured_link_gbps` describing a link the plan
        # never uses.
        placement_warnings: list[str] = []
        if requested_nodes is not None:
            nodes = _select_nodes(
                ctx.deps.registry, nodes, requested_nodes, placement_warnings
            )

        link = None
        if len(nodes) > 1:
            try:
                link = ctx.deps.links.worst_all_reduce([n.node_id for n in nodes])
            except Exception:
                log.exception("link lookup failed while planning")

        # The operator named the set, so the set is the request: plan across it
        # as given rather than narrowing to the strongest homogeneous group.
        # Whether pooling unlike hardware is *allowed to launch* is a separate
        # question, answered by the serve gate below -- a dry run starts
        # nothing, so it owes an honest answer about the set it was handed.
        pool = requested_nodes is not None
        ranked = await asyncio.to_thread(
            partial(
                _rank_plans,
                ctx.deps.planner,
                shape,
                nodes,
                link,
                target,
                concurrency,
                context_length=context_length,
                kv_dtype=kv_dtype,
                allow_mixed_hardware=pool,
            )
        )
        recommended = ranked[0]

        if requested_degrees is None:
            plan = recommended
        else:
            plan_for = getattr(ctx.deps.planner, "plan_for", None)
            if not callable(plan_for):
                # The honest degrade. This planner cannot author a reason for
                # the operator's shape, and a gateway-written `reason` would be
                # rendered verbatim and persisted on the deployment forever.
                raise _PlacementRefused(
                    501,
                    "this planner cannot plan operator-chosen degrees: it "
                    "exposes only the recommendation. Remove `parallelism` to "
                    "plan with the degrees it chooses.",
                    "manual_degrees_unsupported",
                    param=_DEGREES_PARAM,
                )
            try:
                plan = await asyncio.to_thread(
                    partial(
                        plan_for,
                        shape,
                        nodes,
                        link,
                        target,
                        concurrency,
                        context_length=context_length,
                        kv_dtype=kv_dtype,
                        **requested_degrees,
                    )
                )
            except IllegalDegrees as exc:
                first = exc.refusals[0]
                raise _PlacementRefused(
                    400,
                    str(exc),
                    "illegal_parallelism",
                    param=f"{_DEGREES_PARAM}.{first.axis}",
                    extra={
                        # The planner's own sentences, one per line, so the UI
                        # renders them through the same list it renders every
                        # other rejection through.
                        "rejected": [r.message for r in exc.refusals],
                        "legal_degrees": _legal_degrees(shape, len(nodes)),
                    },
                ) from exc

        _check_every_node_used(plan, nodes, requested_nodes, requested_degrees)

        plan_nodes = [n for n in nodes if n.node_id in set(plan.node_ids)] or nodes
        req = FitRequest(
            shape=shape,
            context_length=context_length,
            max_concurrent_seqs=concurrency,
            kv_dtype=kv_dtype,
            plan=plan,
            weight_bytes=weight_bytes,
        )

        # The live budget. Two checks, not one widened verdict: the breakdown
        # is identical under both budgets and only the budget-dependent terms
        # differ, so two internally consistent FitResults beat one object whose
        # `reason` would have to describe two budgets in one sentence.
        budgets, excluded, live_reason = livefit.allocatable_map(
            ctx.deps.registry, [n.node_id for n in plan_nodes]
        )
        budgets, zero_excluded = livefit.drop_zero_addressable(plan_nodes, budgets)
        excluded = excluded + zero_excluded
        if not budgets and live_reason is None:
            live_reason = "every node was excluded from the live memory budget"

        fit, fit_live, unavailable = await asyncio.to_thread(
            livefit.dual_check, ctx.deps.fit, req, plan_nodes, budgets
        )
        # Pooling unlike hardware is a permission, not a fit failure: the
        # memory can be there and the machines still not belong in one pool.
        # It therefore rides as a serve gate rather than as `allowed: false`,
        # and the plan above was computed across the set as given so the
        # operator can see what they are agreeing to before agreeing to it.
        groups = homogeneous_groups(plan_nodes)
        mixed = requested_nodes is not None and len(groups) > 1
        gates = []
        if mixed:
            gates.append(
                {"param": _MIXED_HW_PARAM, "reason": pooling_note(groups)}
            )

        serve = livefit.serve_decision(
            fit, fit_live, unavailable or live_reason, extra_gates=gates
        )
        capacity = _capacity_block(plan_nodes, budgets, excluded, fit, fit_live)

        used = set(plan.node_ids)
        placement = {
            "mode": "operator" if requested_nodes is not None else "planner",
            "requested_node_ids": requested_nodes,
            "node_ids": list(plan.node_ids),
            # Named but carrying no rank. Only reachable when the operator left
            # the degrees to the planner -- naming both and under-filling is
            # refused outright by `_check_every_node_used`.
            "unused_node_ids": [
                n.node_id for n in nodes if n.node_id not in used
            ],
            "mixed_hardware": mixed,
            "warnings": placement_warnings,
        }
        degrees = {
            "source": "operator" if requested_degrees is not None else "planner",
            "tensor_parallel": plan.tensor_parallel,
            "pipeline_parallel": plan.pipeline_parallel,
            "expert_parallel": plan.expert_parallel,
            "data_parallel": plan.data_parallel,
            # The recommendation's own line about the shape that was chosen
            # instead, matched here because the label vocabulary is the
            # planner's ("TP=2", "TP=2/PP=2", "single node"). A client
            # prefix-matching `rejected` would be a second implementation of
            # that vocabulary, and it would break silently the first time a
            # label changed.
            "rejection": _rejection_for(recommended, plan),
        }

        return _PlanOutcome(
            shape=shape,
            plan=plan,
            fit=fit,
            fit_live=fit_live,
            capacity=capacity,
            serve=serve,
            context_length=context_length,
            concurrency=concurrency,
            resolver_warnings=resolver_warnings,
            modality=modality,
            placement=placement,
            degrees=degrees,
            recommended=recommended,
            alternatives=[serialize.plan_degrees_payload(p) for p in ranked],
        )

    @router.post("/api/plan")
    async def plan_endpoint(request: Request) -> Response:
        try:
            payload = await request.json()
        except Exception:
            return errors.error_response(
                400, "Body must be JSON.", "invalid_request_error", "invalid_json"
            )
        model_id = payload.get("model_id") if isinstance(payload, dict) else None
        try:
            out = await _plan_and_fit(payload)
        except _PlacementRefused as exc:
            return exc.response()
        except ValueError as exc:
            return errors.error_response(
                400, str(exc), "invalid_request_error", "invalid_request"
            )
        except Exception as exc:
            log.exception("planning %s failed", model_id)
            return _plan_error_response(exc, model_id)
        return JSONResponse(
            {
                "shape": serialize.shape_payload(out.shape),
                "plan": serialize.plan_payload(out.plan),
                "fit": serialize.fit_payload(out.fit) if out.fit else None,
                # The live verdict, budgeted against what the nodes can
                # actually hand out right now. None when nothing could read
                # them -- never a fabricated stand-in for the static answer.
                "fit_live": (
                    serialize.fit_payload(out.fit_live) if out.fit_live else None
                ),
                "capacity": out.capacity,
                # The one field the UI reads for the button. Which verdict
                # governs is decided here, not in the client.
                "serve": out.serve,
                "resolver_warnings": out.resolver_warnings,
                # Whose choice the machines and the degrees were.
                "placement": out.placement,
                "degrees": out.degrees,
                # The planner's own pick over the same node set, reason and
                # rejected list intact. Always present, so a client never has
                # to infer what was recommended from what was returned.
                "recommended_plan": (
                    serialize.plan_payload(out.recommended)
                    if out.recommended is not None
                    else None
                ),
                "alternatives": out.alternatives,
            }
        )

    @router.get("/api/deployments")
    async def list_deployments() -> JSONResponse:
        try:
            deployments = ctx.deps.deployments.list()
        except Exception:
            log.exception("deployment listing failed")
            deployments = []
        return JSONResponse([serialize.deployment_payload(d) for d in deployments])

    @router.post("/api/deployments")
    async def create_deployment(request: Request) -> Response:
        try:
            payload = await request.json()
        except Exception:
            return errors.error_response(
                400, "Body must be JSON.", "invalid_request_error", "invalid_json"
            )
        model_id = payload.get("model_id") if isinstance(payload, dict) else None

        # A dtype sizes; it does not launch. `_plan_and_fit` passes it to the
        # resolver as a quantization override, which changes bytes-per-param in
        # the fit arithmetic and nothing else: neither serve command template in
        # deploy/flags.py carries --quantization, and the only model identifier
        # either one interpolates is {model}, filled from shape.model_id
        # (deploy/recipes.py). Honouring a dtype here would budget for 4-bit
        # weights and then start the repo's real 16-bit ones -- the precise
        # out-of-memory kill the fit gate exists to refuse. /api/plan still
        # takes it, because "what would this cost at q4_k_m" is a fair question
        # to ask while nothing is being started.
        if isinstance(payload, dict) and payload.get("dtype"):
            return errors.error_response(
                400,
                "A dtype cannot be launched. It changes only the sizing "
                "arithmetic: neither serve command template carries "
                "--quantization, so the runtime would load this repository's "
                "own weights at their real precision while the fit check "
                "budgeted for something smaller. A quantization variant is a "
                "different repository -- pass that repository's id as model_id.",
                "invalid_request_error",
                "dtype_not_launchable",
            )
        try:
            out = await _plan_and_fit(payload)
        except _PlacementRefused as exc:
            return exc.response()
        except ValueError as exc:
            return errors.error_response(
                400, str(exc), "invalid_request_error", "invalid_request"
            )
        except Exception as exc:
            log.exception("planning %s failed", model_id)
            return _plan_error_response(exc, model_id)

        shape, plan, fit = out.shape, out.plan, out.fit
        context_length, concurrency = out.context_length, out.concurrency

        # No verdict at all is never a launch, checked or not: fail loud
        # rather than let a missing fit port silently mean "assume it fits".
        if fit is None:
            return errors.error_response(
                503,
                "the fit calculator is not wired; refusing to launch unchecked",
                "server_error", "fit_unavailable",
            )

        runtime = payload.get("runtime") or "vllm"
        supported_by = getattr(ctx.deps.resolver, "supported_by", None)
        if callable(supported_by):
            ok, reason = await asyncio.to_thread(supported_by, shape, runtime)
            if not ok:
                return errors.error_response(
                    400, reason, "invalid_request_error", "runtime_unsupported"
                )

        # If the verdict is WONT_FIT the launch is refused with the reason.
        # Nothing is started.
        if fit.verdict is Verdict.WONT_FIT:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": fit.reason,
                        "type": "invalid_request_error",
                        "param": None,
                        "code": "wont_fit",
                    },
                    "plan": serialize.plan_payload(plan),
                    "fit": serialize.fit_payload(fit),
                },
            )

        # Pooling unlike hardware. Refused here rather than at /api/plan: the
        # dry run has to be able to show what the pooled plan looks like, or
        # this permission would be one nobody could see the consequence of
        # before granting. 400 rather than 409 because nothing about the
        # machines will change to make an unchanged retry succeed -- this is a
        # policy question about the request, and only the operator can answer
        # it. Independent of the live-memory override: neither implies the
        # other, and both must be satisfied when both apply.
        if out.placement.get("mixed_hardware") and not bool(
            payload.get(_MIXED_HW_PARAM)
        ):
            gate = next(
                (
                    g
                    for g in out.serve.get("overrides", [])
                    if g.get("param") == _MIXED_HW_PARAM
                ),
                None,
            )
            reason = gate["reason"] if gate else "the named machines are not alike"
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": (
                            f"{reason} Resend with {_MIXED_HW_PARAM}: true to "
                            f"pool them anyway; the fit gate still budgets "
                            f"against the smallest machine."
                        ),
                        "type": "invalid_request_error",
                        "param": _MIXED_HW_PARAM,
                        "code": "mixed_hardware_not_allowed",
                    },
                    "plan": serialize.plan_payload(plan),
                    "fit": serialize.fit_payload(fit),
                    "placement": out.placement,
                    "serve": out.serve,
                    "override": {
                        "param": _MIXED_HW_PARAM,
                        "value_required": True,
                        "overrides": (
                            "the refusal to pool machines unlike each other"
                        ),
                    },
                },
            )

        # It would fit on an idle machine, but not on this one as it is now.
        # 409 rather than 400: the request is well formed and legal, the
        # conflict is with current machine state, and an unchanged retry
        # succeeds once the memory comes back. It also keeps the wont_fit 400
        # above distinguishable from this.
        live = out.fit_live
        allow_over = bool(payload.get(_OVERRIDE_PARAM))
        if live is not None and live.verdict is Verdict.WONT_FIT and not allow_over:
            return JSONResponse(
                status_code=409,
                content={
                    "error": {
                        "message": (
                            f"{live.reason} It would fit on an idle machine: "
                            f"the static ceiling is "
                            f"{fit.usable_per_node / 1024 ** 3:.1f} GiB. Free "
                            f"that memory, or resend with "
                            f"{_OVERRIDE_PARAM}: true."
                        ),
                        "type": "invalid_request_error",
                        "param": _OVERRIDE_PARAM,
                        "code": "live_memory_insufficient",
                    },
                    "plan": serialize.plan_payload(plan),
                    "fit": serialize.fit_payload(fit),
                    "fit_live": serialize.fit_payload(live),
                    "capacity": out.capacity,
                    "serve": out.serve,
                    "override": {
                        "param": _OVERRIDE_PARAM,
                        "value_required": True,
                        "overrides": (
                            "the live allocatable-memory check on "
                            + (out.capacity.get("binding_node") or "this node")
                        ),
                    },
                },
            )

        # The budget the launch was actually gated on is the one recorded, so
        # a later fit_miss compares against a number that existed rather than
        # a ceiling that never did.
        gating_fit = live if live is not None else fit
        if live is not None and live.verdict is Verdict.WONT_FIT and allow_over:
            gating_fit.warnings.append(
                f"launched over a live-memory refusal at the operator's "
                f"instruction ({_OVERRIDE_PARAM}): "
                f"{live.usable_per_node / 1024 ** 3:.1f} GiB allocatable "
                f"against {live.breakdown.total / 1024 ** 3:.1f} GiB needed"
            )

        # Who chose this shape, recorded on the thing that outlives the
        # request. `plan.reason` is planner prose and is persisted on the
        # deployment and rendered verbatim from then on -- without this line an
        # operator-forced shape wears a planner-voiced sentence forever, and
        # nothing on screen connects it to the person who chose it.
        # `FitResult.warnings` is already the channel for exactly this and is
        # already persisted, so it needs no contract change.
        chose = [
            name
            for name, value in (
                (_PLACEMENT_PARAM, out.placement.get("mode") == "operator"),
                (_DEGREES_PARAM, out.degrees.get("source") == "operator"),
            )
            if value
        ]
        if chose:
            note = (
                f"placed and shaped at the operator's instruction "
                f"({', '.join(chose)}): {_plan_label(plan)} on "
                f"{', '.join(plan.node_ids)}"
            )
            if out.recommended is not None and (
                out.recommended.tensor_parallel,
                out.recommended.pipeline_parallel,
                out.recommended.expert_parallel,
                out.recommended.data_parallel,
                tuple(out.recommended.node_ids),
            ) != (
                plan.tensor_parallel,
                plan.pipeline_parallel,
                plan.expert_parallel,
                plan.data_parallel,
                tuple(plan.node_ids),
            ):
                note += (
                    f"; the planner ranked {_plan_label(out.recommended)} on "
                    f"{', '.join(out.recommended.node_ids)} first for this "
                    f"node set"
                )
            gating_fit.warnings.append(note)

        if out.placement.get("mixed_hardware"):
            gating_fit.warnings.append(
                f"pooled machines unlike each other at the operator's "
                f"instruction ({_MIXED_HW_PARAM}); the fit budget is the "
                f"smallest machine's"
            )

        try:
            deployment = await asyncio.to_thread(
                partial(
                    ctx.deps.deployments.launch,
                    shape,
                    plan,
                    gating_fit,
                    runtime,
                    context_length,
                    concurrency,
                    modality=out.modality,
                )
            )
        except ValueError as exc:
            # An input validation error -- e.g. a command-unsafe model id --
            # not a launch that genuinely failed.
            return errors.error_response(
                400, str(exc), "invalid_request_error", "invalid_request"
            )
        except Exception as exc:
            log.exception("launch failed")
            return errors.error_response(
                502, f"Launch failed. {errors.detail(exc, _redactor())}",
                "server_error", "launch_failed",
            )
        ctx.router.rebuild(force_scores=True)
        return JSONResponse(
            serialize.deployment_payload(deployment), status_code=201
        )

    @router.delete("/api/deployments/{deployment_id}")
    async def delete_deployment(deployment_id: str) -> Response:
        try:
            existing = ctx.deps.deployments.get(deployment_id)
        except Exception:
            log.exception("deployment lookup failed")
            existing = None
        if existing is None:
            return errors.error_response(
                404, f"No deployment '{deployment_id}'.",
                "invalid_request_error", "deployment_not_found",
            )
        # Stop admitting immediately; in-flight requests finish on their own.
        ctx.admission.set_draining(deployment_id, True)
        try:
            await asyncio.to_thread(ctx.deps.deployments.stop, deployment_id)
        except Exception as exc:
            log.exception("stop failed")
            return errors.error_response(
                502, f"Stop failed. {errors.detail(exc, _redactor())}",
                "server_error", "stop_failed",
            )
        ctx.router.rebuild(force_scores=True)
        return JSONResponse({"stopping": deployment_id})

    # -- metrics -----------------------------------------------------------

    @router.get("/api/metrics/stream")
    async def metrics_stream() -> StreamingResponse:
        async def events():
            queue = ctx.metrics.subscribe()
            try:
                while True:
                    event = await queue.get()
                    yield f"data: {json.dumps(event)}\n\n".encode()
            finally:
                ctx.metrics.unsubscribe(queue)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                # Stops an intermediate proxy from buffering the stream.
                "X-Accel-Buffering": "no",
            },
        )

    # -- history -----------------------------------------------------------
    #
    # New endpoints, never a widened frame. Section 4.8 fixes the SSE payload
    # and Agent H codes against it; a history surface belongs beside it, not
    # inside it. Every answer says which resolution it used and which parts of
    # the window were trimmed rather than quiet.

    def _archive():
        telemetry = getattr(ctx, "telemetry", None)
        return getattr(telemetry, "archive", None) if telemetry else None

    def _no_history() -> JSONResponse:
        telemetry = getattr(ctx, "telemetry", None)
        reason = getattr(telemetry, "reason", "") or "no archive on this node"
        return errors.error_response(
            503,
            f"No telemetry history is being kept: {reason}.",
            "server_error",
            "history_unavailable",
        )

    async def _history(fn, **kwargs) -> Response:
        archive = _archive()
        if archive is None:
            return _no_history()
        try:
            return JSONResponse(await asyncio.to_thread(fn, archive, **kwargs))
        except Exception as exc:
            log.exception("history query failed")
            return errors.error_response(
                502,
                f"Could not read history. {errors.detail(exc, _redactor())}",
                "server_error",
                "history_failed",
            )

    def _ring_history(node_id: str, from_ts: float, to_ts: float) -> dict | None:
        """The registry's 300-sample ring, when there is no archive.

        Without this, a node with telemetry switched off has five minutes of
        per-node history in RAM that nothing can reach: Registry.history() has
        no caller anywhere in the gateway. It is a fallback inside this handler
        rather than a second route, because two endpoints answering the same
        question with different truthfulness is worse than one that labels
        which it gave you -- hence resolution "ring" and durable false.
        """
        registry = ctx.deps.registry
        history = getattr(registry, "history", None)
        if not callable(history):
            return None
        seconds = int(max(1.0, min(to_ts - from_ts, tquery.config.TELEMETRY_RING_S)))
        if node_id:
            node_ids = [node_id]
        else:
            try:
                node_ids = [n.profile.node_id for n in registry.list_nodes()]
            except Exception:
                return None
        samples: list[dict] = []
        for nid in node_ids:
            try:
                rows = history(nid, seconds) or []
            except Exception:
                continue
            for row in rows:
                if from_ts <= row.get("ts", 0.0) <= to_ts:
                    samples.append({"node_id": nid, **row})
        samples.sort(key=lambda row: row.get("ts", 0.0))
        return {
            "from": from_ts,
            "to": to_ts,
            "resolution": "ring",
            "durable": False,
            "gaps": [],
            "truncated": False,
            "samples": samples,
        }

    @router.get("/api/history/nodes")
    async def history_nodes(
        node_id: str = "",
        from_: str = Query("", alias="from"),
        to: str = "",
        step: str = "auto",
        limit: int = tquery.config.QUERY_MAX_ROWS,
    ) -> Response:
        if _archive() is None:
            frm, to_ts = tquery.resolve_window(from_, to)
            ring = await asyncio.to_thread(_ring_history, node_id, frm, to_ts)
            if ring is not None:
                return JSONResponse(ring)
            return _no_history()
        return await _history(
            tquery.nodes,
            node_id=node_id,
            from_ts=from_,
            to_ts=to,
            step=step,
            limit=limit,
        )

    @router.get("/api/history/requests")
    async def history_requests(
        served_name: str = "",
        target_id: str = "",
        from_: str = Query("", alias="from"),
        to: str = "",
        step: str = "auto",
        limit: int = tquery.config.QUERY_MAX_ROWS,
    ) -> Response:
        return await _history(
            tquery.requests,
            served_name=served_name,
            target_id=target_id,
            from_ts=from_,
            to_ts=to,
            step=step,
            limit=limit,
        )

    @router.get("/api/history/events")
    async def history_events(
        type: str = "",
        deployment_id: str = "",
        source: str = "",
        node_id: str = "",
        from_: str = Query("", alias="from"),
        to: str = "",
        limit: int = 500,
    ) -> Response:
        return await _history(
            tquery.events,
            type=type,
            deployment_id=deployment_id,
            source=source,
            node_id=node_id,
            from_ts=from_,
            to_ts=to,
            limit=limit,
        )

    @router.get("/api/history/logs")
    async def history_logs(
        level: str = "",
        logger: str = "",
        q: str = "",
        node_id: str = "",
        from_: str = Query("", alias="from"),
        to: str = "",
        limit: int = 500,
    ) -> Response:
        return await _history(
            tquery.logs,
            level=level,
            logger=logger,
            q=q,
            node_id=node_id,
            from_ts=from_,
            to_ts=to,
            limit=limit,
        )

    @router.get("/api/history/status")
    async def history_status() -> Response:
        telemetry = getattr(ctx, "telemetry", None)
        if telemetry is None:
            return JSONResponse({"enabled": False, "reason": "not configured"})
        return JSONResponse(await asyncio.to_thread(telemetry.status))

    # -- Storage --------------------------------------------------------

    async def _agent_storage(node_id: str, agent_url: str) -> dict:
        """One node's disk picture, or a row saying why we have none.

        Never raises. A storage screen covering a cluster must not go blank
        because one worker is down -- the node that cannot be read is exactly
        the node an operator is looking for.
        """
        import httpx

        def _unavailable(reason: str) -> dict:
            return {
                "node_id": node_id,
                "filesystems": [],
                "estate": [],
                "unreadable": [],
                "available": False,
                "reason": reason,
            }

        try:
            async with httpx.AsyncClient(timeout=_AGENT_TIMEOUT_S) as client:
                res = await client.get(f"{agent_url.rstrip('/')}/agent/storage")
        except Exception as exc:
            log.warning("storage read failed for %s: %s", node_id, exc)
            return _unavailable(
                f"The node agent on '{node_id}' did not answer: "
                f"{errors.detail(exc, _redactor())}"
            )

        if res.status_code == 404:
            # Every other /agent route this node serves works; only this one is
            # missing. That is a node running a build from before the storage
            # probe existed, which is a rolling upgrade rather than a fault, and
            # it deserves a sentence an operator can act on instead of an
            # HTTP client's stringified 404.
            return _unavailable(
                f"The node agent on '{node_id}' has no storage route, so it is "
                "running a build from before disk was measured. Its disk usage "
                "will appear once it is upgraded."
            )
        try:
            res.raise_for_status()
            payload = res.json()
        except Exception as exc:
            log.warning("storage read failed for %s: %s", node_id, exc)
            return _unavailable(
                f"The node agent on '{node_id}' did not answer: "
                f"{errors.detail(exc, _redactor())}"
            )
        payload["node_id"] = node_id
        payload.setdefault("available", True)
        payload.setdefault("reason", None)
        return payload

    async def _agent_model_cache(node_id: str, agent_url: str) -> dict:
        """One node's downloaded weights, or a row saying why we have none."""
        import httpx

        def _unavailable(reason: str) -> dict:
            return {
                "available": False,
                "path": None,
                "repos": [],
                "total_bytes": None,
                "reason": reason,
            }

        try:
            async with httpx.AsyncClient(timeout=_AGENT_TIMEOUT_S) as client:
                res = await client.get(f"{agent_url.rstrip('/')}/agent/models/cache")
        except Exception as exc:
            log.warning("model cache read failed for %s: %s", node_id, exc)
            return _unavailable(
                f"The node agent on '{node_id}' did not answer: "
                f"{errors.detail(exc, _redactor())}"
            )
        if res.status_code == 404:
            return _unavailable(
                f"The node agent on '{node_id}' has no model cache route, so "
                "it is running a build from before downloaded weights were "
                "measured."
            )
        try:
            res.raise_for_status()
            return res.json()
        except Exception as exc:
            log.warning("model cache read failed for %s: %s", node_id, exc)
            return _unavailable(
                f"The node agent on '{node_id}' did not answer: "
                f"{errors.detail(exc, _redactor())}"
            )

    @router.get("/api/storage")
    async def storage() -> Response:
        """Disk across the cluster, and what this product is spending it on.

        Read on demand and fanned out concurrently. Deliberately not derived
        from the metrics frame: disk is not sampled anywhere, precisely so the
        journal does not carry a column per node per second for a number that
        moves hourly.
        """
        registry = ctx.deps.registry
        try:
            nodes = registry.list_nodes()
        except Exception:
            log.exception("node listing failed")
            nodes = []

        lookup = getattr(registry, "agent_url", None)
        targets: list[tuple[str, str]] = []
        no_agent: list[dict] = []
        for node in nodes:
            node_id = node.profile.node_id
            url = None
            if callable(lookup):
                try:
                    url = lookup(node_id)
                except Exception:
                    url = None
            if url:
                targets.append((node_id, url))
            else:
                no_agent.append(
                    {
                        "node_id": node_id,
                        "filesystems": [],
                        "estate": [],
                        "unreadable": [],
                        "available": False,
                        "reason": (
                            f"No agent URL for node '{node_id}'; its disk "
                            "usage cannot be read."
                        ),
                    }
                )

        measured = list(
            await asyncio.gather(*(_agent_storage(n, u) for n, u in targets))
        )
        # The downloaded weights, from the same fan-out. Folded into the node
        # row rather than served as a second endpoint: a screen that shows a
        # filesystem 71% full and the 894 GiB of weights filling it must not be
        # able to render one half a poll ahead of the other.
        caches = list(
            await asyncio.gather(*(_agent_model_cache(n, u) for n, u in targets))
        )
        for row, cache in zip(measured, caches):
            row["models"] = cache
        node_rows = sorted(measured + no_agent, key=lambda r: r["node_id"])

        # The retention horizons come from the module that enforces them, so
        # the UI never re-types a number that a deployment can move.
        cfg = tquery.config
        retention = {
            "samples_raw_s": cfg.SAMPLES_RAW_RETENTION_S,
            "requests_raw_s": cfg.REQUESTS_RAW_RETENTION_S,
            "events_s": cfg.EVENTS_RETENTION_S,
            "logs_s": cfg.LOGS_RETENTION_S,
            "rollup_1m_s": cfg.ROLLUP_1M_RETENTION_S,
            "rollup_1h_s": cfg.ROLLUP_1H_RETENTION_S,
            "archive_max_bytes": cfg.ARCHIVE_MAX_BYTES,
            "journal_max_bytes": cfg.JOURNAL_MAX_BYTES,
            "journal_retention_s": cfg.JOURNAL_RETENTION_S,
        }

        telemetry = getattr(ctx, "telemetry", None)
        if telemetry is None:
            status: dict = {"enabled": False, "reason": "not configured"}
        else:
            try:
                status = await asyncio.to_thread(telemetry.status)
            except Exception:
                log.exception("telemetry status failed")
                status = {"enabled": False, "reason": "status unavailable"}

        return JSONResponse(
            {
                "nodes": node_rows,
                "telemetry": status,
                "retention": retention,
                "measured_at": time.time(),
            }
        )

    @router.delete("/api/storage/nodes/{node_id}/models/{folder}")
    async def delete_cached_model(node_id: str, folder: str) -> Response:
        """Delete one downloaded repository from one node.

        The largest reclaim in the product by orders of magnitude -- a single
        120B repository is 182 GiB -- and the only one that can break a
        running deployment, so the in-use check is here rather than on the
        agent. The agent has no idea what a deployment is; this is the same
        split, and the same 409, that killing a GPU process already uses.
        """
        try:
            deployments = ctx.deps.deployments.list()
        except Exception:
            log.exception("deployment listing failed")
            deployments = []

        # Compared as encoded folder names. Decoding a folder back to a repo
        # id is ambiguous whenever a name contains a double hyphen, and this
        # is the comparison that decides whether a running model keeps its
        # weights.
        for dep in deployments:
            state = getattr(dep, "state", None)
            if state in _TERMINAL_STATES:
                continue
            model_id = getattr(getattr(dep, "shape", None), "model_id", "")
            if not model_id or modelcache.folder_for(model_id) != folder:
                continue
            return errors.error_response(
                409,
                f"{model_id} is being served by deployment "
                f"{getattr(dep, 'deployment_id', '?')} ({getattr(state, 'value', state)}). "
                "Stop the deployment before deleting its weights; it would "
                "otherwise keep running until it next needed a file that is "
                "no longer there.",
                "invalid_request_error",
                "model_in_use",
            )

        token = None
        token_fn = getattr(ctx.deps.registry, "cluster_token", None)
        if callable(token_fn):
            try:
                token = token_fn()
            except Exception:
                log.exception("cluster token unavailable")
        if not token:
            return errors.error_response(
                503,
                "No cluster token is available, so the node agent cannot be "
                "asked to delete anything.",
                "server_error",
                "cluster_token_unavailable",
            )

        lookup = getattr(ctx.deps.registry, "agent_url", None)
        agent_url = lookup(node_id) if callable(lookup) else None
        if not agent_url:
            return errors.error_response(
                404,
                f"No agent URL for node '{node_id}'; its model cache cannot "
                "be reached.",
                "invalid_request_error",
                "node_agent_unreachable",
            )

        import httpx

        try:
            async with httpx.AsyncClient(timeout=_DELETE_TIMEOUT_S) as client:
                res = await client.delete(
                    f"{agent_url.rstrip('/')}/agent/models/cache/{folder}",
                    headers={"X-Derate-Token": token},
                )
        except Exception as exc:
            log.exception("model delete failed")
            return errors.error_response(
                502,
                f"The node agent on '{node_id}' did not answer the delete: "
                f"{errors.detail(exc, _redactor())}",
                "server_error",
                "node_agent_unreachable",
            )
        if res.status_code >= 400:
            # The agent's own sentence, forwarded rather than paraphrased --
            # only it knows whether the folder was missing, outside the cache,
            # or refused by the filesystem.
            return errors.error_response(
                res.status_code if res.status_code != 403 else 502,
                f"The delete was refused on '{node_id}': {_agent_detail(res)}",
                "invalid_request_error",
                "delete_refused",
            )
        return JSONResponse(res.json())

    @router.delete("/api/storage/cache/resolver")
    async def clear_resolver_cache() -> Response:
        """Drop every cached model resolution.

        The only mutation on this surface. Safe by construction: a cache miss
        costs one hub round trip, and ShapeCache is keyed by model, revision
        and dtype with a schema version, so nothing here can outlive a format
        change anyway.
        """
        cache = getattr(ctx.deps.resolver, "cache", None)
        if cache is None or not callable(getattr(cache, "clear", None)):
            return errors.error_response(
                503,
                "This resolver has no shape cache, so there is nothing to "
                "clear.",
                "server_error",
                "resolver_cache_unavailable",
            )
        directory = getattr(cache, "directory", None)
        before = 0
        if directory is not None:
            before = registry_storage.path_bytes(Path(directory)) or 0
        try:
            await asyncio.to_thread(cache.clear)
        except Exception as exc:
            log.exception("resolver cache clear failed")
            return errors.error_response(
                500,
                f"The resolver cache could not be cleared: "
                f"{errors.detail(exc, _redactor())}",
                "server_error",
                "resolver_cache_clear_failed",
            )
        after = 0
        if directory is not None:
            after = registry_storage.path_bytes(Path(directory)) or 0
        return JSONResponse(
            {"cleared": True, "bytes_freed": max(0, before - after)}
        )

    return router
