"""The internal API. Exactly the surface in architecture section 4.8.

No additions without updating that file: Agent H is coding against it in
parallel. Where a port does not yet expose an operation the HTTP surface
promises, the endpoint degrades with a clear 501 rather than disappearing,
so the UI can be built against the full shape from day 0.
"""

from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from control_plane.contracts import (
    DeploymentState,
    FitRequest,
    RoutingPolicy,
    Verdict,
)

from . import errors, serialize
from .deps import GatewayContext

log = logging.getLogger("gateway.api")

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
        index = ctx.router.index()
        window = settings.metrics_rate_window_s
        return JSONResponse(
            {
                "cluster_id": settings.cluster_id,
                "coordinator": settings.coordinator_node_id,
                "nodes": [serialize.node_payload(n) for n in nodes],
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
            addressable = node.profile.addressable_memory or 0
            strength = strength_by_node.get(node_id)
            if strength is None:
                strength = raw_hw.get(node_id, 0.0) / top_hw if top_hw else 0.0
            node_payloads.append(
                {
                    "node_id": node_id,
                    "hostname": node.profile.hostname,
                    "device_class": node.profile.device_class.value,
                    "gpu_name": node.profile.gpu_name,
                    "state": "healthy" if node.healthy else "unhealthy",
                    "role": "coordinator"
                    if node_id == settings.coordinator_node_id
                    else "worker",
                    "memory_used_pct": round(node.memory_used / addressable * 100.0, 1)
                    if addressable
                    else None,
                    "power_w": node.power_watts,
                    "temp_c": node.temperature_c,
                    "util_pct": node.utilization_pct,
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
        return JSONResponse([serialize.node_payload(n) for n in _nodes()])

    @router.get("/api/nodes/candidates")
    async def node_candidates() -> JSONResponse:
        # Discovery proposes, a human accepts. Agent A owns the candidate set.
        candidates = getattr(ctx.deps.registry, "candidates", None)
        if not callable(candidates):
            return JSONResponse([])
        try:
            return JSONResponse([serialize.plain(c) for c in candidates()])
        except Exception:
            log.exception("candidate listing failed")
            return JSONResponse([])

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
        return JSONResponse(serialize.node_payload(node))

    @router.post("/api/nodes/join")
    async def join_node(request: Request) -> Response:
        join = getattr(ctx.deps.registry, "join", None)
        if not callable(join):
            return _not_implemented("Node join", "registry")
        try:
            payload = await request.json()
        except Exception:
            return errors.error_response(
                400, "Join body must be JSON.", "invalid_request_error", "invalid_json"
            )
        try:
            return JSONResponse(serialize.plain(join(payload)))
        except PermissionError:
            # A join with a wrong or missing token is rejected.
            return errors.error_response(
                403, "Cluster token rejected.", "invalid_request_error", "bad_token"
            )
        except Exception as exc:
            log.exception("join failed")
            return errors.error_response(
                400, f"Join failed: {type(exc).__name__}.",
                "invalid_request_error", "join_failed",
            )

    @router.post("/api/nodes/{node_id}/admit")
    async def admit_node(node_id: str) -> Response:
        admit = getattr(ctx.deps.registry, "admit", None)
        if not callable(admit):
            return _not_implemented("Node admission", "registry")
        try:
            return JSONResponse(serialize.plain(admit(node_id)))
        except KeyError:
            return errors.error_response(
                404, f"No candidate '{node_id}'.",
                "invalid_request_error", "node_not_found",
            )

    @router.delete("/api/nodes/{node_id}")
    async def remove_node(node_id: str) -> Response:
        remove = getattr(ctx.deps.registry, "remove", None)
        if not callable(remove):
            return _not_implemented("Node removal", "registry")
        try:
            remove(node_id)
        except KeyError:
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
            measurement = ctx.deps.links.measure(a, b)
        except Exception as exc:
            log.exception("link measurement failed")
            return errors.error_response(
                502, f"Measurement failed: {type(exc).__name__}.",
                "server_error", "measure_failed",
            )
        return JSONResponse(serialize.link_payload(measurement))

    # -- routing -----------------------------------------------------------

    @router.get("/api/routing")
    async def list_routing() -> JSONResponse:
        index = ctx.router.index()
        sources = {k: v.source for k, v in index.raw_strength.items()}
        return JSONResponse(
            [serialize.routing_payload(c, sources) for c in ctx.router.configs()]
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
        sources = {k: v.source for k, v in index.raw_strength.items()}
        return JSONResponse(serialize.routing_payload(config, sources))

    # -- providers ---------------------------------------------------------

    @router.get("/api/providers")
    async def list_providers() -> JSONResponse:
        try:
            providers = ctx.deps.providers.list()
        except Exception:
            log.exception("provider listing failed")
            providers = []
        return JSONResponse([serialize.provider_payload(p) for p in providers])

    @router.post("/api/providers")
    async def add_provider(request: Request) -> Response:
        try:
            spec = await request.json()
        except Exception:
            return errors.error_response(
                400, "Body must be JSON.", "invalid_request_error", "invalid_json"
            )
        try:
            provider = ctx.deps.providers.add(spec)
        except Exception as exc:
            log.exception("provider add failed")
            return errors.error_response(
                400, f"Could not add provider: {type(exc).__name__}.",
                "invalid_request_error", "provider_add_failed",
            )
        ctx.router.rebuild(force_scores=True)
        return JSONResponse(serialize.provider_payload(provider), status_code=201)

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
        except KeyError:
            return errors.error_response(
                404, f"No provider '{provider_id}'.",
                "invalid_request_error", "provider_not_found",
            )
        ctx.router.rebuild(force_scores=True)
        return JSONResponse(serialize.provider_payload(provider))

    @router.delete("/api/providers/{provider_id}")
    async def delete_provider(provider_id: str) -> Response:
        remove = getattr(ctx.deps.providers, "remove", None)
        if not callable(remove):
            return _not_implemented("Provider removal", "provider store")
        try:
            remove(provider_id)
        except KeyError:
            return errors.error_response(
                404, f"No provider '{provider_id}'.",
                "invalid_request_error", "provider_not_found",
            )
        ctx.router.rebuild(force_scores=True)
        return JSONResponse({"removed": provider_id})

    @router.post("/api/providers/{provider_id}/refresh")
    async def refresh_provider(provider_id: str) -> Response:
        try:
            provider = ctx.deps.providers.refresh(provider_id)
        except KeyError:
            return errors.error_response(
                404, f"No provider '{provider_id}'.",
                "invalid_request_error", "provider_not_found",
            )
        except Exception as exc:
            log.exception("provider refresh failed")
            return errors.error_response(
                502, f"Refresh failed: {type(exc).__name__}.",
                "server_error", "refresh_failed",
            )
        ctx.router.rebuild(force_scores=True)
        return JSONResponse(serialize.provider_payload(provider))

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

    def _plan_and_fit(payload: dict):
        """Resolve, plan, check fit. Launches nothing."""
        model_id = payload.get("model_id")
        if not isinstance(model_id, str) or not model_id:
            raise ValueError("model_id is required")
        context_length = int(payload.get("context") or 8192)
        concurrency = int(payload.get("concurrency") or 1)
        target = payload.get("target") or "throughput"
        kv_dtype = payload.get("kv_dtype") or settings.default_kv_dtype

        shape = ctx.deps.resolver.resolve(model_id, payload.get("dtype"))

        try:
            nodes = [n.profile for n in ctx.deps.registry.healthy_nodes()]
        except Exception:
            log.exception("registry unavailable while planning")
            nodes = []

        link = None
        if len(nodes) > 1:
            try:
                link = ctx.deps.links.worst_all_reduce([n.node_id for n in nodes])
            except Exception:
                log.exception("link lookup failed while planning")

        plan = ctx.deps.planner.plan(shape, nodes, link, target, concurrency)

        plan_nodes = [n for n in nodes if n.node_id in set(plan.node_ids)] or nodes
        fit = ctx.deps.fit.check(
            FitRequest(
                shape=shape,
                context_length=context_length,
                max_concurrent_seqs=concurrency,
                kv_dtype=kv_dtype,
                plan=plan,
            ),
            plan_nodes,
        )
        return shape, plan, fit, context_length, concurrency

    @router.post("/api/plan")
    async def plan_endpoint(request: Request) -> Response:
        try:
            payload = await request.json()
        except Exception:
            return errors.error_response(
                400, "Body must be JSON.", "invalid_request_error", "invalid_json"
            )
        try:
            shape, plan, fit, _, _ = _plan_and_fit(payload)
        except ValueError as exc:
            return errors.error_response(
                400, str(exc), "invalid_request_error", "invalid_request"
            )
        except Exception as exc:
            log.exception("planning failed")
            return errors.error_response(
                502, f"Could not plan: {type(exc).__name__}.",
                "server_error", "plan_failed",
            )
        return JSONResponse(
            {
                "shape": serialize.shape_payload(shape),
                "plan": serialize.plan_payload(plan),
                "fit": serialize.fit_payload(fit) if fit else None,
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
        try:
            shape, plan, fit, context_length, concurrency = _plan_and_fit(payload)
        except ValueError as exc:
            return errors.error_response(
                400, str(exc), "invalid_request_error", "invalid_request"
            )
        except Exception as exc:
            log.exception("planning failed")
            return errors.error_response(
                502, f"Could not plan: {type(exc).__name__}.",
                "server_error", "plan_failed",
            )

        # If the verdict is WONT_FIT the launch is refused with the reason.
        # Nothing is started.
        if fit is not None and fit.verdict is Verdict.WONT_FIT:
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

        try:
            deployment = ctx.deps.deployments.launch(
                shape,
                plan,
                fit,
                payload.get("runtime") or "vllm",
                context_length,
                concurrency,
            )
        except Exception as exc:
            log.exception("launch failed")
            return errors.error_response(
                502, f"Launch failed: {type(exc).__name__}.",
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
            ctx.deps.deployments.stop(deployment_id)
        except Exception as exc:
            log.exception("stop failed")
            return errors.error_response(
                502, f"Stop failed: {type(exc).__name__}.",
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

    return router
