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
from uuid import uuid4

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, Response

from control_plane.contracts import TargetKind
from control_plane.telemetry import RequestTrace

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

        settings = ctx.settings
        streaming = bool(body.get("stream")) and streaming_allowed

        # Minted before the first thing that can refuse, so a 404 is recorded
        # as readily as a 200. Every attempt this request makes shares the id,
        # and it goes back as X-Request-Id so a client that saw a bad answer
        # can name the row that explains it.
        trace = RequestTrace(
            request_id=f"r-{uuid4().hex[:12]}",
            served_name=model,
            node_id=settings.coordinator_node_id or "",
            streaming=streaming,
            body_bytes=len(raw),
        )

        def _tagged(response: Response) -> Response:
            response.headers["X-Request-Id"] = trace.request_id
            return response

        def _refused(response: Response, code: str) -> Response:
            """Record a request that never reached an upstream.

            A refusal is exactly the kind of thing worth having a month of,
            and no attempt row would otherwise exist for it.
            """
            trace.error_code = code
            try:
                ctx.sink.request(
                    trace.record(status=response.status_code, duration_s=None)
                )
            except Exception:
                log.debug("could not record refusal telemetry", exc_info=True)
            return _tagged(response)

        index = ctx.router.index()
        if model not in index.targets:
            pending = index.pending.get(model)
            if pending:
                # It exists, it just cannot answer yet. Say which state it is
                # in so the client can retry sensibly instead of hanging.
                return _refused(
                    errors.model_not_ready(
                        model, sorted({d.state.value for d in pending})
                    ),
                    "model_not_ready",
                )
            if not ctx.router.parkable(model):
                return _refused(
                    errors.unknown_model(model, index.served_names()),
                    "model_not_found",
                )
            # Otherwise it was serving moments ago and its target has gone.
            # Fall through: the parking lot below is exactly for that.

        prefix_key = _prefix_key(body)
        client_headers = dict(request.headers)
        ctx.retry_budget.note_request()

        def _terminal(last, tried: set[str]) -> Response:
            """The answer when no target could serve the request."""
            if last.status is not None:
                # The backend's own words, byte for byte. Re-framing a
                # completed error body is not rewriting it -- status, headers
                # and bytes are unchanged -- and nothing of ours, no
                # Retry-After included, is added on top of it.
                return Response(
                    content=last.body or b"",
                    status_code=last.status,
                    headers=last.headers or {},
                )
            return errors.upstream_unreachable(
                model, last.error or "unreachable", len(tried)
            )

        async def _dispatch() -> Response | None:
            """Offer the request to targets until one answers.

            Returns None only when nothing was ever sent, which means there was
            nowhere to send it -- a different situation from every target
            having tried and failed, and one with a different answer.
            """
            tried: set[str] = set()
            last = None
            deadline = time.monotonic() + settings.failover_deadline_s

            for attempt_no in range(settings.failover_max_attempts):
                selection = ctx.router.select(
                    model,
                    # Only the first attempt uses the cache-affinity hash. A
                    # retry is by definition going somewhere the prefix is not
                    # cached, and re-hashing over the smaller ring left by the
                    # exclusion would only repin the prefix somewhere new.
                    prefix_key=prefix_key if attempt_no == 0 else None,
                    exclude=tried,
                )
                if selection is None:
                    break
                target = selection.target
                tried.add(target.target_id)
                # Which evidence set this target's strength -- measured decode
                # rate, the fit calculator's prediction, raw bandwidth, or a
                # default. It lives on the index, not on RouteTarget, and it is
                # what makes a recorded strength interpretable later.
                score = ctx.router.index().raw_strength.get(target.target_id)
                trace.strength_source = getattr(score, "source", "") or ""
                # No await between the selection above and this claim, which is
                # what lets a single-token half-open probe be safe without a
                # lock. Keep it that way.
                if not ctx.breaker.begin(target.target_id):
                    continue  # its one probe is already out; try another
                if attempt_no > 0 and not ctx.retry_budget.take():
                    ctx.breaker.abandon(target.target_id)
                    break

                # Everything below is rebuilt per attempt. Hoisting any of it
                # out of the loop would carry one target's body rewrite -- or
                # worse, one provider's key -- onto the next target.
                upstream_body = body
                api_key = None
                on_finish = None
                use_provider_service = False
                provider_id = None
                provider_upstream_id = None
                open_upstream = None

                if target.kind is TargetKind.LOCAL:
                    deployment = selection.deployment
                    if deployment is not None:
                        decision = ctx.admission.check(deployment, body)
                        trace.prompt_tokens = decision.prompt_tokens
                        if not decision.ok:
                            ctx.breaker.abandon(target.target_id)
                            headers = (
                                {"Retry-After": str(decision.retry_after_s)}
                                if decision.retry_after_s
                                else None
                            )
                            trace.admission_code = decision.code
                            return _refused(
                                errors.error_response(
                                    decision.status,
                                    decision.message,
                                    "invalid_request_error"
                                    if decision.status == 400
                                    else "rate_limit_error",
                                    decision.code,
                                    headers=headers,
                                ),
                                decision.code,
                            )
                        trace.kv_bytes = decision.kv_bytes
                        ctx.admission.commit(deployment.deployment_id, decision.kv_bytes)

                        # Bound as defaults rather than closed over, so a later
                        # attempt cannot rebind what this one has to release.
                        def on_finish(  # noqa: F811
                            _id=deployment.deployment_id, _kv=decision.kv_bytes
                        ) -> None:
                            ctx.admission.release(_id, _kv)
                else:
                    # Remote upstreams call the model something else. This is
                    # the only edit made to a request body, and it is required
                    # for it to route.
                    if selection.model is not None:
                        upstream_body = dict(body)
                        upstream_body["model"] = selection.model.upstream_id
                    provider_id = (
                        selection.provider.provider_id
                        if selection.provider is not None
                        else None
                    )
                    provider_upstream_id = (
                        selection.model.upstream_id
                        if selection.model is not None
                        else None
                    )
                    # Prefer routing through the provider's own open_upstream:
                    # its backoff, spend accounting, auth handling and budget
                    # enforcement then actually run on live traffic (audit
                    # H-9), instead of the gateway resolving a raw key and
                    # forwarding it blind. A providers port that predates
                    # open_upstream (the day-0 stub) falls back to that raw
                    # path so it keeps working.
                    open_upstream = getattr(
                        ctx.deps.providers, "open_upstream", None
                    )
                    use_provider_service = (
                        open_upstream is not None
                        and provider_id is not None
                        and provider_upstream_id is not None
                    )
                    if not use_provider_service and selection.provider is not None:
                        try:
                            api_key = ctx.deps.providers.resolve_key(provider_id)
                        except Exception:
                            # Never echo anything about key material.
                            log.warning(
                                "could not resolve api key for provider %s",
                                provider_id,
                            )
                            ctx.breaker.abandon(target.target_id)
                            return _refused(
                                errors.error_response(
                                    502,
                                    f"Provider '{provider_id}' has "
                                    "no usable credential configured.",
                                    "server_error",
                                    "provider_key_unavailable",
                                ),
                                "provider_key_unavailable",
                            )

                trace.attempts = attempt_no + 1
                if use_provider_service:
                    result = await ctx.proxy.forward_provider(
                        target=target,
                        provider_id=provider_id,
                        upstream_id=provider_upstream_id,
                        path=path,
                        body=body,
                        stats=ctx.stats,
                        streaming=streaming,
                        open_upstream=open_upstream,
                        on_finish=on_finish,
                        selection=selection,
                        trace=trace,
                        attempt_no=attempt_no,
                    )
                    if result is None:
                        # The provider refused before anything was sent --
                        # rate limited, over budget, disabled. Not this
                        # target's fault to answer for; try another one.
                        ctx.breaker.abandon(target.target_id)
                        continue
                else:
                    result = await ctx.proxy.forward(
                        selection=selection,
                        path=path,
                        body=upstream_body,
                        client_headers=client_headers,
                        api_key=api_key,
                        stats=ctx.stats,
                        streaming=streaming,
                        on_finish=on_finish,
                        trace=trace,
                        attempt_no=attempt_no,
                    )
                if not result.retryable:
                    return _tagged(result.response)
                last = result
                if time.monotonic() >= deadline:
                    break

            if last is None:
                return None
            # The attempts themselves are already recorded from settle(); this
            # only tags the answer the client actually sees.
            return _tagged(_terminal(last, tried))

        def _nowhere_to_send() -> Response:
            """Re-read the world before refusing: it may have moved on."""
            fresh = ctx.router.index()
            if model in fresh.targets:
                return errors.no_target_admitting(model)
            pending = fresh.pending.get(model)
            if pending:
                return errors.model_not_ready(
                    model, sorted({d.state.value for d in pending})
                )
            if ctx.router.recently_eligible(model):
                # Its deployment has left the index altogether, but it was
                # serving a moment ago. "No such model" would be a lie.
                return errors.no_target_admitting(model)
            return errors.unknown_model(model, fresh.served_names())

        answer = await _dispatch()
        if answer is not None:
            return answer

        # Nothing was reachable. If this model was serving a moment ago and no
        # target is deliberately shedding load, hold the request briefly for
        # one to come back rather than refusing outright.
        if ctx.parking.accepts(model, len(raw)) and ctx.router.parkable(model):
            parked_at = time.monotonic()
            recovered = await ctx.parking.park(
                model,
                select=lambda: ctx.router.select(model, prefix_key=prefix_key),
                is_disconnected=request.is_disconnected,
            )
            # Recorded whether or not it recovered: how long requests wait in
            # the lot is the gauge this queue has never had.
            trace.parked_ms = (time.monotonic() - parked_at) * 1000.0
            if recovered is not None:
                answer = await _dispatch()
                if answer is not None:
                    return answer
        refusal = _nowhere_to_send()
        return _refused(refusal, "no_target")

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
