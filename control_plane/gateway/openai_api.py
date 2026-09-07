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

from fastapi import APIRouter, Request, WebSocket
from starlette.responses import JSONResponse, Response

from control_plane.contracts import Modality, TargetKind
from control_plane.telemetry import RequestTrace

from . import errors, realtime
from .deps import GatewayContext

log = logging.getLogger("gateway.openai")

# Which kind of model each endpoint family needs. The gateway has always had
# one family, so naming a model was the only question a request had to answer;
# with /v1/audio/* there are two, and a chat request that lands on a TTS
# deployment gets a confusing 400 from the backend instead of a useful one from
# us. This table is what makes the refusal actionable.
_ENDPOINT_MODALITY: dict[str, Modality] = {
    "/chat/completions": Modality.TEXT,
    "/completions": Modality.TEXT,
    "/embeddings": Modality.EMBEDDING,
    "/audio/speech": Modality.SPEECH,
    "/audio/transcriptions": Modality.TRANSCRIPTION,
}

#: The modalities that are genuinely a different endpoint family. TEXT and
#: EMBEDDING are deliberately not in here: one vLLM server answers
#: /v1/chat/completions and /v1/embeddings from the same weights, and this
#: gateway has always let it, so treating them as exclusive would refuse
#: requests that work today. Audio is the axis that is actually new.
_AUDIO_MODALITIES = frozenset({Modality.SPEECH, Modality.TRANSCRIPTION})


def _modality_conflict(has: Modality, wants: Modality) -> bool:
    """True when this model cannot serve this endpoint.

    Only asked about audio, and asked in both directions: a speech model must
    not receive a chat request, and a chat model must not receive a speech one.
    """
    if has is wants:
        return False
    return has in _AUDIO_MODALITIES or wants in _AUDIO_MODALITIES

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


async def _read_bounded_body(request: Request, limit: int) -> bytes | None:
    """Read the whole body, or None if it goes past *limit*.

    Streamed rather than `await request.body()` so an oversized upload is
    dropped at the limit instead of being buffered in full and then rejected.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _boundary_of(content_type: str) -> bytes | None:
    """The boundary token out of a multipart content-type header."""
    for part in content_type.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.strip().lower() == "boundary":
            token = value.strip().strip('"')
            return token.encode("latin-1") if token else None
    return None


def _field_span(raw: bytes, content_type: str, name: str) -> tuple[str, int, int] | None:
    """Find a simple form field: its value, and where the value sits in *raw*.

    Deliberately not a general multipart parser. It reads the one text field
    the gateway needs -- the model name -- and returns its byte span so the
    value can be swapped without re-encoding the file part beside it. Anything
    it does not understand it declines to find, and the caller answers with a
    missing-parameter 400 rather than guessing.
    """
    boundary = _boundary_of(content_type)
    if not boundary:
        return None
    needle = b'name="%s"' % name.encode("latin-1")
    delimiter = b"--" + boundary
    cursor = 0
    while True:
        start = raw.find(delimiter, cursor)
        if start < 0:
            return None
        cursor = start + len(delimiter)
        head_end = raw.find(b"\r\n\r\n", cursor)
        if head_end < 0:
            return None
        headers = raw[cursor:head_end]
        body_start = head_end + 4
        body_end = raw.find(b"\r\n" + delimiter, body_start)
        if body_end < 0:
            return None
        if needle in headers:
            try:
                return raw[body_start:body_end].decode("utf-8"), body_start, body_end
            except UnicodeDecodeError:
                return None
        cursor = body_end


def _multipart_field(raw: bytes, content_type: str, name: str) -> str | None:
    found = _field_span(raw, content_type, name)
    return found[0].strip() if found else None


def _rewrite_multipart_field(
    raw: bytes, content_type: str, name: str, value: str
) -> bytes | None:
    """Swap one field's value, leaving every other byte alone.

    Needed because a remote provider calls the model something else, and on
    this path the name is inside the body rather than in a dict we can copy.
    """
    found = _field_span(raw, content_type, name)
    if found is None:
        return None
    _, start, end = found
    return raw[:start] + value.encode("utf-8") + raw[end:]


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
                    "owned_by": "derate",
                    # Extras. Clients ignore what they do not know; the UI uses them.
                    "context_length": index.context_length.get(served_name),
                    "target_count": len(targets),
                    "target_kinds": kinds,
                    # Which endpoint family accepts this name. Without it a
                    # client cannot tell a TTS model from a chat model.
                    "modality": index.modality.get(
                        served_name, Modality.TEXT
                    ).value,
                }
            )
        return JSONResponse({"object": "list", "data": data})

    async def _proxy(
        request: Request, path: str, streaming_allowed: bool
    ) -> Response:
        """The JSON entry point: chat, completions, embeddings, audio/speech."""
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

        return await _serve(
            request,
            path,
            model=model,
            body=body,
            raw_len=len(raw),
            streaming=bool(body.get("stream")) and streaming_allowed,
        )

    async def _proxy_multipart(request: Request, path: str) -> Response:
        """The multipart entry point: audio/transcriptions.

        The one request shape that cannot go through _proxy. The body is a
        form carrying an audio file, so there is no JSON to parse and the model
        name arrives as a field rather than a key.

        The bytes are read once and forwarded verbatim, boundary and all.
        Parsing the form properly would mean pulling in python-multipart and
        rebuilding a 25 MiB upload in order to change nothing about it -- and
        the rebuilt body would have to be held anyway, because a target that
        fails still has to be retryable.
        """
        content_type = request.headers.get("content-type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            return errors.error_response(
                400,
                "This endpoint expects a multipart/form-data upload carrying "
                "'file' and 'model'.",
                "invalid_request_error",
                "invalid_content_type",
            )

        limit = ctx.settings.max_audio_upload_bytes
        raw = await _read_bounded_body(request, limit)
        if raw is None:
            return errors.error_response(
                413,
                f"The upload is larger than the {limit // (1024 * 1024)} MiB limit.",
                "invalid_request_error",
                "payload_too_large",
            )

        model = _multipart_field(raw, content_type, "model")
        if not model:
            return errors.error_response(
                400, "You must provide a 'model' parameter.",
                "invalid_request_error", "missing_model",
            )

        return await _serve(
            request,
            path,
            model=model,
            body=None,
            raw_len=len(raw),
            streaming=False,
            content=raw,
            content_type=content_type,
        )

    async def _serve(
        request: Request,
        path: str,
        *,
        model: str,
        body: dict | None,
        raw_len: int,
        streaming: bool,
        content: bytes | None = None,
        content_type: str | None = None,
    ) -> Response:
        """Everything both entry points share: model lookup, the modality
        guard, failover, the breaker, parking and the trace id.

        `body` is None exactly when `content` is set, which is what tells the
        rest of this function it is looking at bytes it cannot inspect.
        """
        settings = ctx.settings
        # An audio body has no tokens to count and nothing to read a `usage`
        # block out of.
        count_tokens = body is not None

        # Minted before the first thing that can refuse, so a 404 is recorded
        # as readily as a 200. Every attempt this request makes shares the id,
        # and it goes back as X-Request-Id so a client that saw a bad answer
        # can name the row that explains it.
        trace = RequestTrace(
            request_id=f"r-{uuid4().hex[:12]}",
            served_name=model,
            node_id=settings.coordinator_node_id or "",
            streaming=streaming,
            body_bytes=raw_len,
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

        # The model exists (or is parkable). Before anything is sent, check it
        # answers on this endpoint at all -- a refusal here is cheap and says
        # something the backend's own 400 would not.
        wants = _ENDPOINT_MODALITY.get(path)
        has = index.modality.get(model)
        if wants is not None and has is not None and _modality_conflict(has, wants):
            return _refused(
                errors.wrong_modality(model, has, wants, f"/v1{path}"),
                "wrong_modality",
            )

        # Nothing to hash in an opaque upload, and cache affinity means
        # nothing for a transcription anyway -- there is no shared prefix.
        prefix_key = _prefix_key(body) if body is not None else None
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
                upstream_content = content
                api_key = None
                on_finish = None
                use_provider_service = False
                provider_id = None
                provider_upstream_id = None
                open_upstream = None

                if target.kind is TargetKind.LOCAL:
                    deployment = selection.deployment
                    # Admission is KV-cache arithmetic end to end, and a KV
                    # cache is not what a speech or transcription server has.
                    # Charging one a budget that does not describe it would
                    # refuse requests for a reason that does not exist.
                    if (
                        deployment is not None
                        and deployment.modality is Modality.TEXT
                        and body is not None
                    ):
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
                        if body is not None:
                            upstream_body = dict(body)
                            upstream_body["model"] = selection.model.upstream_id
                        elif content is not None and content_type is not None:
                            # The name is inside the body here rather than in a
                            # dict we can copy, so it is spliced in place. Only
                            # the field's bytes move; the audio part beside it
                            # is untouched.
                            rewritten = _rewrite_multipart_field(
                                content,
                                content_type,
                                "model",
                                selection.model.upstream_id,
                            )
                            if rewritten is None:
                                # We read the field on the way in, so failing to
                                # rewrite it means the body changed shape under
                                # us. Sending it unrewritten would name a model
                                # this provider does not have.
                                ctx.breaker.abandon(target.target_id)
                                continue
                            upstream_content = rewritten
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
                        # The managed path builds its payload with dict(body),
                        # which an opaque upload has none of. A multipart
                        # request therefore takes the raw forward below: it
                        # still carries the provider's key and still fails over,
                        # but the provider service's own spend accounting and
                        # budget enforcement do not run on it. Acceptable for
                        # now because transcription is priced per audio-minute
                        # and nothing on this path can read that figure anyway.
                        and content is None
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
                        content=upstream_content,
                        content_type=content_type,
                        count_tokens=count_tokens,
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
        if ctx.parking.accepts(model, raw_len) and ctx.router.parkable(model):
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

    @router.post("/v1/audio/speech")
    async def audio_speech(request: Request) -> Response:
        """Text in, audio bytes out.

        The body is JSON with a top-level "model", which is exactly what _proxy
        already parses, and the response path has always been byte-transparent
        -- it streams the upstream's own bytes and headers -- so an MP3 comes
        back through the same machinery a token stream does. There is no
        `stream` field on this endpoint, which is why providers/service.py stops
        injecting one.
        """
        return await _proxy(request, "/audio/speech", False)

    @router.post("/v1/audio/transcriptions")
    async def audio_transcriptions(request: Request) -> Response:
        """Audio file in, text out.

        The only endpoint here that is not JSON: it is a multipart upload, so
        it takes _proxy_multipart. Everything after the body is read -- model
        lookup, the modality guard, failover, the breaker, parking, the trace
        id -- is the same code path as every other route.
        """
        return await _proxy_multipart(request, "/audio/transcriptions")

    @router.websocket("/v1/realtime")
    async def realtime_session(websocket: WebSocket) -> None:
        """A realtime voice session, relayed to a provider that implements it.

        The only route here that is not a request/response pair, and the only
        one that does not go through _serve: a session cannot be re-offered to
        another target halfway through, because the upstream holds conversation
        state we never saw. See realtime.py for what this does and does not do.
        """
        await realtime.relay(ctx, websocket)

    return router
