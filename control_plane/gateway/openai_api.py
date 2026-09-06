"""The OpenAI-compatible surface.

A client points at one base URL, calls /v1/models, and sees every model in the
cluster whatever node it runs on and whatever runtime serves it. It sends a
request naming any of them and gets tokens. It never learns which node
answered.
"""

from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, Response

from control_plane.contracts import TargetKind

from . import errors
from .deps import GatewayContext

log = logging.getLogger("gateway.openai")

# How much of the prompt seeds the cache-affinity hash. Long enough that a
# shared system prompt collides, short enough to stay cheap.
_PREFIX_CHARS = 512


def _prefix_key(body: dict) -> str | None:
    messages = body.get("messages")
    if isinstance(messages, list):
        parts: list[str] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and isinstance(item.get("text"), str):
                        parts.append(item["text"])
            if sum(len(p) for p in parts) >= _PREFIX_CHARS:
                break
        joined = "".join(parts)
        return joined[:_PREFIX_CHARS] if joined else None

    prompt = body.get("prompt")
    if isinstance(prompt, str):
        return prompt[:_PREFIX_CHARS] or None
    if isinstance(prompt, list) and prompt and isinstance(prompt[0], str):
        return prompt[0][:_PREFIX_CHARS] or None
    return None


def create_router(ctx: GatewayContext) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/models")
    async def list_models() -> JSONResponse:
        index = ctx.router.index()
        created = int(ctx.started_at or time.time())
        data = []
        for served_name in sorted(index.targets):
            targets = index.targets[served_name]
            kinds = sorted({t.kind.value for t in targets})
            data.append(
                {
                    # Standard OpenAI fields, so existing clients work unmodified.
                    "id": served_name,
                    "object": "model",
                    "created": created,
                    "owned_by": "sparkplane",
                    # Extras. Clients ignore what they do not know; the UI uses them.
                    "context_length": index.context_length.get(served_name),
                    "target_count": len(targets),
                    "target_kinds": kinds,
                }
            )
        return JSONResponse({"object": "list", "data": data})

    async def _proxy(request: Request, path: str, streaming_allowed: bool) -> Response:
        try:
            raw = await request.body()
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            return errors.error_response(
                400, f"Request body is not valid JSON: {exc.msg}.",
                "invalid_request_error", "invalid_json",
            )
        if not isinstance(body, dict):
            return errors.error_response(
                400, "Request body must be a JSON object.",
                "invalid_request_error", "invalid_json",
            )

        model = body.get("model")
        if not isinstance(model, str) or not model:
            return errors.error_response(
                400, "You must provide a 'model' parameter.",
                "invalid_request_error", "missing_model",
            )

        index = ctx.router.index()
        if model not in index.targets:
            pending = index.pending.get(model)
            if pending:
                # It exists, it just cannot answer yet. Say which state it is
                # in so the client can retry sensibly instead of hanging.
                return errors.model_not_ready(
                    model, sorted({d.state.value for d in pending})
                )
            return errors.unknown_model(model, index.served_names())

        selection = ctx.router.select(model, prefix_key=_prefix_key(body))
        if selection is None:
            return errors.no_target_admitting(model)

        target = selection.target
        upstream_body = body
        api_key = None
        on_finish = None

        if target.kind is TargetKind.LOCAL:
            deployment = selection.deployment
            if deployment is not None:
                decision = ctx.admission.check(deployment, body)
                if not decision.ok:
                    headers = (
                        {"Retry-After": str(decision.retry_after_s)}
                        if decision.retry_after_s
                        else None
                    )
                    return errors.error_response(
                        decision.status,
                        decision.message,
                        "invalid_request_error"
                        if decision.status == 400
                        else "rate_limit_error",
                        decision.code,
                        headers=headers,
                    )
                ctx.admission.commit(deployment.deployment_id, decision.kv_bytes)
                dep_id = deployment.deployment_id
                kv_bytes = decision.kv_bytes

                def on_finish() -> None:  # noqa: F811
                    ctx.admission.release(dep_id, kv_bytes)
        else:
            # Remote upstreams call the model something else. This is the only
            # edit made to a request body, and it is required for it to route.
            if selection.model is not None:
                upstream_body = dict(body)
                upstream_body["model"] = selection.model.upstream_id
            if selection.provider is not None:
                try:
                    api_key = ctx.deps.providers.resolve_key(
                        selection.provider.provider_id
                    )
                except Exception:
                    # Never echo anything about key material into a response.
                    log.warning(
                        "could not resolve api key for provider %s",
                        selection.provider.provider_id,
                    )
                    return errors.error_response(
                        502,
                        f"Provider '{selection.provider.provider_id}' has no usable "
                        "credential configured.",
                        "server_error",
                        "provider_key_unavailable",
                    )

        streaming = bool(body.get("stream")) and streaming_allowed
        return await ctx.proxy.forward(
            selection=selection,
            path=path,
            body=upstream_body,
            client_headers=dict(request.headers),
            api_key=api_key,
            stats=ctx.stats,
            streaming=streaming,
            on_finish=on_finish,
        )

    @router.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        return await _proxy(request, "/chat/completions", True)

    @router.post("/v1/completions")
    async def completions(request: Request) -> Response:
        return await _proxy(request, "/completions", True)

    @router.post("/v1/embeddings")
    async def embeddings(request: Request) -> Response:
        return await _proxy(request, "/embeddings", False)

    return router
