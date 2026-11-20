"""The OpenAI-compatible surface.

A client points at one base URL, calls /v1/models, and sees every model in the
cluster whatever node it runs on and whatever runtime serves it. It sends a
request naming any of them and gets tokens. It never learns which node
answered.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from dataclasses import replace

from fastapi import APIRouter, Request, WebSocket
from starlette.responses import JSONResponse, Response, StreamingResponse

from control_plane.contracts import Modality, TargetKind
from control_plane.contracts.auth import (
    AuthError,
    AuthErrorCode,
    AuthUnavailable,
    LimitScope,
    Principal,
    UsageActuals,
)
from control_plane.contracts.telemetry import CostBasis, FallbackReason, FieldError
from control_plane.telemetry import RequestTrace
from control_plane.telemetry.records import spec_fields

from . import authz, errors, policies, proxy as proxy_mod, realtime
from .body import read_bounded_body
from .proxy import Attempt
from .deps import GatewayContext

log = logging.getLogger("gateway.openai")

#: Endpoints whose body takes a ``max_tokens``. Only these get one injected
#: when a key's ``max_output_tokens_per_request`` applies and none was sent.
_GENERATION_PATHS = frozenset({"/chat/completions", "/completions"})

#: Background attributions in flight; held so the loop does not drop them.
_BACKGROUND: set[asyncio.Task] = set()

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

#: Endpoints whose RESPONSE is audio bytes, which is the only thing that
#: decides whether reading the body for a `usage` block is worth anything.
#:
#: This used to be inferred from the request instead -- `count_tokens = body is
#: not None` -- and that is backwards for both audio endpoints, in opposite
#: directions. /audio/speech sends JSON and receives MP3, so it sniffed a
#: megabyte of audio to learn nothing; /audio/transcriptions sends multipart
#: and receives JSON that carries a real usage block, so it refused to read the
#: one response here worth reading. A claim about the response has to be made
#: about the response.
_RESPONSE_IS_AUDIO: frozenset[str] = frozenset({"/audio/speech"})

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


class _AmbiguousBoundary(ValueError):
    """The content-type names more than one ``boundary`` parameter.

    Parsers disagree on which one wins (first here, last in most upstreams),
    so the part the gateway authorises and the part the provider reads could
    differ. Refused rather than guessed.
    """


def _boundary_of(content_type: str) -> bytes | None:
    """The boundary token out of a multipart content-type header.

    :class:`_AmbiguousBoundary` when the header names more than one.
    """
    found: bytes | None = None
    seen = False
    for part in content_type.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.strip().lower() == "boundary":
            if seen:
                raise _AmbiguousBoundary(content_type)
            seen = True
            token = value.strip().strip('"')
            found = token.encode("latin-1") if token else None
    return found


class _DuplicateField(ValueError):
    """A form field the gateway reads appears in more than one part."""


def _part_name(headers: bytes) -> str | None:
    """The ``name`` parameter of a part's content-disposition, or None.

    Only that header and only that parameter: ``filename="model"`` or a
    ``name="model"`` in some other header is not the field. A part with two
    content-disposition headers, or two ``name=`` parameters, is refused: the
    upstream parser keeps the last where this one would read the first.
    """
    found: str | None = None
    seen = False
    for line in headers.split(b"\r\n"):
        key, sep, value = line.partition(b":")
        if not sep or key.strip().lower() != b"content-disposition":
            continue
        if seen:
            raise _DuplicateField("content-disposition")
        seen = True
        names = _DISPOSITION_NAME.findall(value.decode("latin-1"))
        if len(names) > 1:
            raise _DuplicateField("content-disposition")
        found = names[0].strip() if names else None
    return found


#: Quoted or bare, any case: upstream parsers accept ``name=model`` and
#: ``NAME="model"`` both, so a second spelling must count as a second part.
_DISPOSITION_NAME = re.compile(r';\s*name\s*=\s*"?([^";]*)"?', re.IGNORECASE)


def _field_span(raw: bytes, content_type: str, name: str) -> tuple[str, int, int] | None:
    """Find a simple form field: its value, and where the value sits in *raw*.

    Deliberately not a general multipart parser. It reads the one text field
    the gateway needs -- the model name -- and returns its byte span so the
    value can be swapped without re-encoding the file part beside it. Anything
    it does not understand it declines to find, and the caller answers with a
    missing-parameter 400 rather than guessing.

    A second part with the same name raises :class:`_DuplicateField`: the
    gateway would authorise and price one value while the upstream's parser
    takes the other.
    """
    boundary = _boundary_of(content_type)
    if not boundary:
        return None
    delimiter = b"--" + boundary
    cursor = 0
    found: tuple[str, int, int] | None = None
    while True:
        start = raw.find(delimiter, cursor)
        if start < 0:
            return found
        cursor = start + len(delimiter)
        head_end = raw.find(b"\r\n\r\n", cursor)
        if head_end < 0:
            return found
        headers = raw[cursor:head_end]
        body_start = head_end + 4
        body_end = raw.find(b"\r\n" + delimiter, body_start)
        if body_end < 0:
            return found
        part_name = _part_name(headers)
        if part_name is not None and part_name.lower() == name.lower():
            if found is not None:
                raise _DuplicateField(name)
            try:
                found = raw[body_start:body_end].decode("utf-8"), body_start, body_end
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


class _EngineSlot:
    """One request's stay on one local engine."""

    __slots__ = ("started_at", "shared")

    def __init__(self, started_at: float) -> None:
        self.started_at = started_at
        #: Set when another request was on the same engine at any point.
        self.shared = False


def _requested_max_tokens(body: dict) -> int | None:
    for name in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        value = body.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _limit_refusal(decision) -> Response:
    """A limit service refusal as the response a client gets, verbatim.

    429 carries the limit that stopped it, how much is used, and when it frees,
    in the body and in ``X-RateLimit-*`` (Reset in epoch seconds); the
    per-request caps (400/413) and the allowlist (403) carry their sentence.
    """
    if decision.status != 429:
        return errors.error_response(
            decision.status, decision.message, "invalid_request_error", decision.code
        )
    reset = decision.resets_at if decision.resets_at is not None else time.time()
    headers = {"X-RateLimit-Reset": str(int(math.ceil(reset)))}
    if decision.limit is not None:
        headers["X-RateLimit-Limit"] = _num(decision.limit)
        headers["X-RateLimit-Remaining"] = _num(max(0.0, decision.limit - (decision.used or 0.0)))
    return errors.error_response(
        429,
        decision.message,
        "rate_limit_error",
        decision.code or "limit_exceeded",
        headers=headers,
        limit_source=decision.limit_source,
        used=decision.used,
        limit=decision.limit,
        resets_at=decision.resets_at,
    )


def _model_not_allowed(model: str) -> Response:
    """The allowlist's 403, answered before any catalogue lookup (K-3): a
    name the key may not use gets this whether it exists or not."""
    return errors.error_response(
        403,
        f"model {model!r} is not in this key's allowed models",
        "invalid_request_error",
        AuthErrorCode.MODEL_NOT_ALLOWED.value,
    )


async def _released_at_end(body_iterator, release, started=None):
    """A response body that runs ``release`` once it is over, however it ends.

    ``started`` runs once, when the first chunk of the answer exists: from
    then on the request was served, and a release that has no actuals to
    give counts the reservation's estimate instead of voiding it.
    """
    first = True
    try:
        async for chunk in body_iterator:
            if first and started is not None:
                first = False
                try:
                    started()
                except Exception:
                    log.exception("could not mark a reservation started")
            yield chunk
    finally:
        try:
            aclose = getattr(body_iterator, "aclose", None)
            if aclose is not None:
                await aclose()
        except BaseException:
            pass
        release(None)



def create_router(ctx: GatewayContext) -> APIRouter:
    router = APIRouter()
    # deployment_id -> the requests on that engine right now (see _EngineSlot).
    engine_busy: dict[str, set[_EngineSlot]] = {}

    def _attribute_spec(record, slot: _EngineSlot) -> None:
        """Fill a local row's spec-decode columns once the engine's next
        scrape brackets the request, then re-emit it under the same
        ``(request_id, attempt_no)``, which replaces the first row. Scheduled,
        never awaited: the response does not wait for a scrape."""
        poller = ctx.engine_metrics
        if poller is None:
            return
        start, end, alone = slot.started_at, time.time(), not slot.shared

        async def attribute() -> None:
            try:
                spec = await poller.attribute_when_ready(record.deployment_id, start, end, alone)
                if spec == FieldError.NOT_REPORTED:
                    return  # what the first row already says
                ctx.sink.request(replace(record, **spec_fields(spec)))
            except Exception:
                log.debug("spec attribution failed for %s", record.request_id, exc_info=True)

        try:
            task = asyncio.get_running_loop().create_task(attribute())
        except RuntimeError:
            return
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)

    def _visible_names(request: Request, names) -> list[str]:
        """*names* as the caller may see them: a key with an allowlist (its
        own or its org's) sees only that; a session, or an unrestricted key,
        sees everything."""
        names = list(names)
        principal = getattr(request.state, "principal", None)
        if principal is None:
            return names
        key, org = principal.key, principal.org
        if key.allowed_models is None and org.allowed_models is None:
            return names
        allowed = set(ctx.auth_stores.keys.effective_allowlist(key, org, names))
        return [n for n in names if n in allowed]

    @router.get("/v1/models")
    @router.get("/api/chat/models")
    async def list_models(request: Request) -> JSONResponse:
        """Every served model. ``/api/chat/models`` is the same list behind the
        session gate, for the UI, which holds a session and never a key.

        A key with an allowlist sees only its allowlist: the full catalogue
        is the admin's business, not every key holder's."""
        index = ctx.router.index()
        created = int(ctx.started_at or time.time())
        data = []
        for served_name in _visible_names(request, sorted(index.targets)):
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

    @router.get("/v1/key")
    async def current_key(request: Request) -> JSONResponse:
        """The calling key: what it is, what it may use, and how much is left.

        Never the secret or its hash -- ``ApiKey`` has neither -- and never
        the admin's notes about it.
        """
        try:
            from .keys_api import usage_totals
        except ImportError:  # the auth-keys branch is not merged: no cycle usage
            usage_totals = None

        principal: Principal = request.state.principal
        stores = ctx.auth_stores
        key, org = principal.key, principal.org
        now = time.time()
        exposed = list(ctx.router.index().served_names())
        cycle_start, resets_at = stores.limits.cycle_bounds(key.created_at, now)
        archive = getattr(getattr(ctx, "telemetry", None), "archive", None)
        usage = None
        if archive is not None and usage_totals is not None:
            try:
                totals = await asyncio.to_thread(
                    usage_totals, archive, "key_id", [key.key_id], cycle_start, now
                )
                usage = totals.get(key.key_id)
            except Exception:
                log.exception("cycle usage for %s failed", key.key_id)
        body = authz.key_dict(key)
        # Admin bookkeeping, not the key holder's: who minted it, from where
        # it was last used. The notes never left; these three no longer do.
        for private in ("notes", "created_by", "admin_username", "last_used_ip"):
            body.pop(private, None)
        return JSONResponse(
            {
                "key": body,
                "org": {"org_id": org.org_id, "name": org.name},
                "expires_at": key.expires_at,
                "allowed_models": stores.keys.effective_allowlist(key, org, exposed),
                "limit_states": {
                    "key": authz.limit_states(stores, LimitScope.KEY, key.key_id),
                    "org": authz.limit_states(stores, LimitScope.ORG, org.org_id),
                },
                "cycle": {"start": cycle_start, "resets_at": resets_at, "usage": usage},
            }
        )

    async def _proxy(
        request: Request, path: str, streaming_allowed: bool
    ) -> Response:
        """The JSON entry point: chat, completions, embeddings, audio/speech."""
        limit = ctx.settings.max_json_body_bytes
        raw = await read_bounded_body(request, limit)
        if raw is None:
            return errors.error_response(
                413,
                f"The request body is larger than the {limit // (1024 * 1024)} MiB limit.",
                "invalid_request_error",
                "payload_too_large",
            )
        try:
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
            raw_body=raw,
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
        try:
            _boundary_of(content_type)
        except _AmbiguousBoundary:
            return errors.error_response(
                400, "ambiguous multipart boundary",
                "invalid_request_error", "invalid_request",
            )

        limit = ctx.settings.max_audio_upload_bytes
        raw = await read_bounded_body(request, limit)
        if raw is None:
            return errors.error_response(
                413,
                f"The upload is larger than the {limit // (1024 * 1024)} MiB limit.",
                "invalid_request_error",
                "payload_too_large",
            )

        try:
            model = _multipart_field(raw, content_type, "model")
        except _DuplicateField:
            return errors.error_response(
                400, "duplicate multipart field: model -- send exactly one 'model' part.",
                "invalid_request_error", "invalid_request",
            )
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
        raw_body: bytes | None = None,
    ) -> Response:
        """Everything both entry points share: model lookup, the modality
        guard, failover, the breaker, parking and the trace id.

        `body` is None exactly when `content` is set, which is what tells the
        rest of this function it is looking at bytes it cannot inspect.

        `raw_body` is the JSON path's original bytes, carried alongside `body`
        so an attempt that needs no rewrite can forward them verbatim instead
        of paying to re-serialize `body` back to JSON. `content` already means
        "opaque upload" elsewhere in this function, so this is a second,
        separate channel rather than overloading that one.
        """
        settings = ctx.settings
        # Audio bytes carry no usage block; everything else may. See
        # _RESPONSE_IS_AUDIO for why this is not asked of the request.
        count_tokens = path not in _RESPONSE_IS_AUDIO

        # Minted at the gate (authz.AuthGuard) before the first thing that can
        # refuse, so a 404 is recorded as readily as a 200 and the id joins
        # this row to the audit trail and the access line. Every attempt this
        # request makes shares the id, and it goes back as X-Request-Id so a
        # client that saw a bad answer can name the row that explains it.
        trace = RequestTrace(
            request_id=getattr(request.state, "request_id", None) or authz.new_request_id(),
            served_name=model,
            node_id=settings.coordinator_node_id or "",
            streaming=streaming,
            body_bytes=raw_len,
        )
        principal: Principal | None = getattr(request.state, "principal", None)
        trace.model_requested = model
        if principal is not None:
            trace.key_id = principal.key.key_id
            trace.key_name = principal.key.name
            trace.org_id = principal.org.org_id
            trace.client_ip = principal.ip
            trace.user_agent = principal.user_agent
        trace.client_port = getattr(request.state, "client_port", None)
        if body is not None:
            trace.max_tokens_requested = _requested_max_tokens(body)
            messages = body.get("messages")
            trace.message_count = len(messages) if isinstance(messages, list) else None

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

        if principal is not None:
            try:
                allowed = authz.model_allowed(ctx.auth_stores, principal, model)
            except AuthUnavailable as exc:
                return _refused(authz.unavailable(exc), exc.code.value)
            if not allowed:
                return _refused(_model_not_allowed(model), AuthErrorCode.MODEL_NOT_ALLOWED.value)

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
                    errors.unknown_model(model, _visible_names(request, index.served_names())),
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

        # Limits, before anything is sent. The allowlist, the per-request
        # caps, the rate windows and the cycle budgets all answer here, and a
        # refusal goes through _refused so its row carries the limit_source.
        # An admitted request holds a reservation until _settle_limits runs.
        reservation = None
        route_policy = None
        if principal is not None:
            route_policy = principal.key.routing_policy or principal.org.routing_policy
            kinds = {t.kind for t in index.targets.get(model, ())}
            # A name with both kinds is admitted as REMOTE, the stricter
            # assumption: `cloud_*` limits apply up front, and `local_*` ones
            # are re-checked in _attempt_on if a LOCAL target is picked
            # (finding L9 -- `None` here used to apply neither).
            mixed_kinds = len(kinds) > 1
            target_kind = (
                TargetKind.REMOTE.value if mixed_kinds
                else next(iter(kinds)).value if kinds
                else None
            )
            try:
                limits = ctx.auth_stores.limits
                decision, reservation = limits.check_and_reserve(
                    principal, model=model, target_kind=target_kind, body=body or {},
                    route_limits=ctx.router.limits_for(model),
                )
                if (
                    not decision.allowed
                    and decision.fallback_model
                    and target_kind == TargetKind.REMOTE.value
                ):
                    # The cloud pool is spent and the key names a local model
                    # to fall back to: re-check as that model, on a LOCAL target.
                    fallback = decision.fallback_model
                    fb_body = dict(body, model=fallback) if body is not None else {}
                    fb_decision, fb_reservation = limits.check_and_reserve(
                        principal,
                        model=fallback,
                        target_kind=TargetKind.LOCAL.value,
                        body=fb_body,
                        route_limits=ctx.router.limits_for(fallback),
                    )
                    if fb_decision.allowed and fallback in index.targets:
                        model = fallback
                        trace.served_name = fallback
                        trace.fallback_used = True
                        trace.fallback_reason = FallbackReason.CLOUD_POOL_EXHAUSTED.value
                        if body is not None:
                            body, raw_body = fb_body, None
                        elif content is not None and content_type is not None:
                            content = (
                                _rewrite_multipart_field(content, content_type, "model", fallback)
                                or content
                            )
                        decision, reservation = fb_decision, fb_reservation
                    elif fb_reservation is not None:
                        limits.settle(fb_reservation, None)
            except AuthUnavailable as exc:
                return _refused(authz.unavailable(exc), exc.code.value)
            if not decision.allowed:
                trace.limit_source = decision.limit_source
                return _refused(_limit_refusal(decision), decision.code)
            if (
                decision.inject_max_tokens is not None
                and body is not None
                and path in _GENERATION_PATHS
            ):
                body = dict(body)
                body["max_tokens"] = decision.inject_max_tokens
                # The engine reads `max_completion_tokens or max_tokens`, so
                # leaving a client's other spelling beside the cap is a bypass.
                body.pop("max_completion_tokens", None)
                body.pop("max_output_tokens", None)
                raw_body = None
            if reservation is not None:
                # The estimate the limits were checked on, so a stream whose
                # upstream never sends a usage frame records it rather than a
                # prompt of zero (finding H5). Admission overwrites it with its
                # own on a LOCAL text target.
                trace.prompt_tokens = reservation.estimated_prompt

        # One entry per attempt, carried on every row this request writes.
        attempt_chain: list[dict] = []
        # deployment_id -> this request's slot on that engine, for the
        # spec-decode attribution's "was it alone" question.
        engine_slots: dict[str, _EngineSlot] = {}

        def _settle_limits(actuals: UsageActuals | None) -> None:
            """Count what the request used, or give the reservation back.
            The limit service ignores a second settle, so the release in the
            response's own cleanup is a no-op after an attempt counted."""
            if reservation is None:
                return
            try:
                ctx.auth_stores.limits.settle(reservation, actuals)
            except Exception:
                log.exception("could not settle reservation for %s", trace.request_id)

        def _on_record(record, *, failed: bool, selection=None):
            """Every attempt's row, on its way to the sink: attempt chain,
            cost and its basis, and -- for the attempt that answered -- the
            usage counted against the key's and org's limits."""
            # _trace_record carries upstream error metadata on a complete
            # singleton entry. Keep it when adding this request's other turns.
            entry = dict(record.attempt_chain[0]) if record.attempt_chain else {}
            entry.update(
                target_id=record.target_id,
                status=record.status,
                error_class=record.error_class or None,
                retry_reason=record.retry_reason or None,
                ms=record.duration_ms,
            )
            attempt_chain.append(entry)
            cost, basis = record.cost_usd, None
            if not failed and record.status is not None and ctx.auth_stores is not None:
                provider_model = getattr(selection, "model", None)
                try:
                    cost, cost_basis = ctx.auth_stores.pricing.cost(
                        model=record.served_name,
                        target_kind=record.target_kind,
                        prompt_tokens=record.prompt_tokens or 0,
                        completion_tokens=record.completion_tokens or 0,
                        metered_usd=record.cost_usd,
                        provider_in_per_mtok=getattr(provider_model, "input_cost_per_mtok", None),
                        provider_out_per_mtok=getattr(provider_model, "output_cost_per_mtok", None),
                    )
                    basis = cost_basis.value
                except Exception:
                    log.debug("could not price %s", trace.request_id, exc_info=True)
                    cost, cost_basis = None, CostBasis.UNPRICED
                _settle_limits(
                    UsageActuals(
                        prompt_tokens=record.prompt_tokens or 0,
                        completion_tokens=record.completion_tokens or 0,
                        cost_usd=cost,
                        cost_basis=cost_basis,
                        target_kind=record.target_kind,
                        model_served=record.served_name,
                    )
                )
            record = replace(
                record, attempt_chain=list(attempt_chain), cost_usd=cost, cost_basis=basis
            )
            slot = engine_slots.pop(record.deployment_id, None) if record.deployment_id else None
            if slot is not None:
                engine_busy.get(record.deployment_id, set()).discard(slot)
                if record.status is not None:
                    _attribute_spec(record, slot)
            return record

        def _occupy_engine(selection) -> None:
            """Note that this request is on that engine now, and whether it
            shares it -- spec-decode counters are per engine, so a request
            that was not alone cannot be told its own share of them."""
            if ctx.engine_metrics is None or selection.deployment is None:
                return
            dep_id = selection.deployment.deployment_id
            slot = _EngineSlot(time.time())
            peers = engine_busy.setdefault(dep_id, set())
            if peers:
                slot.shared = True
                for peer in peers:
                    peer.shared = True
            peers.add(slot)
            engine_slots[dep_id] = slot

        policy_kw = {"policy": route_policy} if route_policy is not None else {}

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
            if last.reason == proxy_mod.RETRY_POOL:
                # We never reached a backend, but that is our pool's doing and
                # not the backend's. Saying "unreachable" here would blame a
                # machine that is answering perfectly well.
                return errors.upstream_pool_exhausted(model, len(tried))
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
            _SKIP = object()

            def _claim(attempt_no):
                """Pick the next untried target and take the right to send.

                Three outcomes, and the difference between the last two is
                load-bearing: a selection; `_SKIP`, meaning this target refused
                but another may not; or None, meaning stop offering the request
                at all.

                There is deliberately no await between the select and the
                breaker claim, which is what lets a single half-open probe be
                safe without a lock. Keep it that way.
                """
                selection = ctx.router.select(
                    model,
                    # Only the first attempt uses the cache-affinity hash. A
                    # retry is by definition going somewhere the prefix is not
                    # cached, and re-hashing over the smaller ring left by the
                    # exclusion would only repin the prefix somewhere new.
                    prefix_key=prefix_key if attempt_no == 0 else None,
                    exclude=tried,
                    # A key's or org's routing override; omitted when there is
                    # none, so a router double without the kwarg still works.
                    **policy_kw,
                )
                if selection is None:
                    return None
                target = selection.target
                tried.add(target.target_id)
                # Which evidence set this target's strength -- measured decode
                # rate, the fit calculator's prediction, raw bandwidth, or a
                # default. It lives on the index, not on RouteTarget, and it is
                # what makes a recorded strength interpretable later.
                score = ctx.router.index().raw_strength.get(target.target_id)
                trace.strength_source = getattr(score, "source", "") or ""
                if not ctx.breaker.begin(target.target_id):
                    return _SKIP  # its one probe is already out; another may do
                if attempt_no > 0 and not ctx.retry_budget.take():
                    ctx.breaker.abandon(target.target_id)
                    return None  # out of budget: stop, do not try another
                return selection

            def _make_reopen(committed_at):
                """Finish a committed response from another target.

                Handed to `forward`, which calls it only when an upstream dies
                with the client still on zero body bytes. Returns the
                replacement's own body iterator -- so its accounting, breaker
                verdict, KV commitment and cleanup all stay inside its own
                generator -- or None to let the original failure stand.

                Deliberately NOT bounded by `deadline`. That budget exists to
                stop stacking retries on a request nobody is waiting for, and a
                committed response is the opposite case: the client has been
                holding for the whole prefill, which on a 30B is two minutes,
                and would far rather wait again than be handed an error. The
                bound here is attempts, and only ever one more.
                """

                async def reopen(sent_content_type):
                    n = committed_at + 1
                    if n >= settings.failover_max_attempts:
                        return None
                    selection = _claim(n)
                    if selection is None or selection is _SKIP:
                        return None
                    outcome = await _attempt_on(selection, n, None)
                    if not isinstance(outcome, Attempt) or outcome.response is None:
                        # A refusal or a skip. Neither can be shown to a client
                        # that already has a 200 on the wire, so the original
                        # failure stands.
                        if isinstance(outcome, Attempt):
                            last_error = outcome.error or outcome.reason
                            log.info("reopen found no usable target: %s", last_error)
                        return None
                    replacement = outcome.response
                    got = replacement.headers.get("content-type")
                    if sent_content_type and got and got != sent_content_type:
                        # We have already sent the first target's headers. A
                        # body of a different type underneath them would be
                        # corruption, not recovery.
                        log.warning(
                            "not finishing a %s response from a %s one",
                            sent_content_type, got,
                        )
                        return None
                    body_iterator = getattr(replacement, "body_iterator", None)
                    return body_iterator

                return reopen

            async def _attempt_on(selection, attempt_no, reopen):
                """One target's turn, from body rewrite to bytes on the wire.

                Extracted so that `reopen` below can run a second target
                through exactly the same path -- key resolution, body rewrite,
                admission, provider routing -- rather than a simplified copy of
                it that would drift. Returns an `Attempt`, or a `Response` when
                the request itself is refused, or None to try another target.
                """
                target = selection.target
                # Everything below is rebuilt per attempt. Hoisting any of it
                # out of the loop would carry one target's body rewrite -- or
                # worse, one provider's key -- onto the next target.
                upstream_body = body
                upstream_content = content
                upstream_raw = raw_body
                api_key = None
                on_finish = None
                use_provider_service = False
                provider_id = None
                provider_upstream_id = None
                open_upstream = None

                if reservation is not None:
                    # The name may have both kinds and was admitted as REMOTE;
                    # this target's own kind-specific limits answer here.
                    try:
                        kind_decision = ctx.auth_stores.limits.check_target_kind(
                            reservation, target.kind.value
                        )
                    except AuthUnavailable as exc:
                        ctx.breaker.abandon(target.target_id)
                        return _refused(authz.unavailable(exc), exc.code.value)
                    if not kind_decision.allowed:
                        ctx.breaker.abandon(target.target_id)
                        trace.limit_source = kind_decision.limit_source
                        return _refused(_limit_refusal(kind_decision), kind_decision.code)

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

                        if streaming and path in _GENERATION_PATHS:
                            # vLLM and SGLang only send a usage frame when
                            # asked, and a stream without one is metered on
                            # an estimate (finding H5). Asked on every local
                            # stream, whatever the client sent.
                            options = body.get("stream_options")
                            upstream_body = dict(
                                body,
                                stream_options={
                                    **(options if isinstance(options, dict) else {}),
                                    "include_usage": True,
                                },
                            )
                            upstream_raw = None
                else:
                    # Remote upstreams call the model something else. This is
                    # the only edit made to a request body, and it is required
                    # for it to route.
                    if selection.model is not None:
                        if body is not None:
                            upstream_body = dict(body)
                            upstream_body["model"] = selection.model.upstream_id
                            upstream_raw = None
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
                                return None  # nothing was sent; try another target
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
                    open_upstream_raw = getattr(
                        ctx.deps.providers, "open_upstream_raw", None
                    )
                    use_provider_service = (
                        open_upstream is not None
                        and provider_id is not None
                        and provider_upstream_id is not None
                        # An upload goes through the same service as JSON: the
                        # providers port's `open_upstream` takes the bytes as
                        # `content=` (or `open_upstream_raw`, the same path
                        # under its own name, when the port has it), so the
                        # provider's own gates and accounting run for a
                        # transcription too. Only a body with no content type
                        # cannot be forwarded as an upload.
                        and (content is None or content_type is not None)
                    )
                    if not use_provider_service and selection.provider is not None:
                        try:
                            api_key = await asyncio.to_thread(
                                ctx.deps.providers.resolve_key, provider_id
                            )
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
                        open_upstream_raw=open_upstream_raw,
                        count_tokens=count_tokens,
                        on_finish=on_finish,
                        selection=selection,
                        trace=trace,
                        attempt_no=attempt_no,
                        reopen=reopen,
                        on_record=_on_record,
                        # An upload goes through the same gate as JSON --
                        # enabled, serves(), rate limit, budget, spend
                        # (finding K5) -- byte for byte, with the provider's
                        # model name already spliced in above.
                        content=upstream_content if body is None else None,
                        content_type=content_type if body is None else None,
                    )
                    if result is None:
                        # The provider refused before anything was sent --
                        # rate limited, over budget, disabled. Not this
                        # target's fault to answer for; try another one.
                        ctx.breaker.abandon(target.target_id)
                        return None  # nothing was sent; try another target
                else:
                    if target.kind is TargetKind.LOCAL:
                        _occupy_engine(selection)
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
                        content=upstream_content
                        if upstream_content is not None
                        else upstream_raw,
                        content_type=content_type,
                        count_tokens=count_tokens,
                        reopen=reopen,
                        on_record=_on_record,
                    )
                return result

            async def _discard(outcome) -> None:
                """Throw away an attempt nobody will read.

                The loser of a hedge holds a live upstream whose body generator
                will never be started, so nothing would ever settle its
                accounting, release its KV commitment or return its connection.
                `Attempt.discard` does all three; without this a hedge leaks
                exactly what the 2026-09-08 outage leaked, one request at a
                time.
                """
                if not isinstance(outcome, Attempt):
                    return
                discard = outcome.discard
                if discard is None:
                    return
                try:
                    await discard()
                except Exception:
                    log.warning("could not discard a hedged attempt", exc_info=True)

            async def _hedged(selection, attempt_no):
                """Run one attempt, racing a second target if it stalls.

                The leader keeps its head start: the hedge is only sent if the
                leader has produced nothing by `hedge_after_s`, and whichever
                returns a usable response first is the one the client gets.
                """
                primary = asyncio.ensure_future(
                    _attempt_on(selection, attempt_no, _make_reopen(attempt_no))
                )
                done, _ = await asyncio.wait(
                    {primary}, timeout=settings.hedge_after_s
                )
                if primary in done:
                    return primary.result()

                config = ctx.router.config_for(model)
                mate = (
                    policies.hedge_candidate(
                        config, selection.target, settings, exclude=tried
                    )
                    if config is not None
                    else None
                )
                if mate is None:
                    return await primary

                hedge_selection = _claim(attempt_no + 1)
                if hedge_selection is None or hedge_selection is _SKIP:
                    return await primary
                # `_claim` runs the policy and the breaker, so what it hands
                # back need not be the target `hedge_candidate` picked. Check
                # the one actually claimed against the same rule, or a target
                # below the floor slips past the gate that exists to stop it.
                if not policies.may_hedge(
                    config, selection.target, hedge_selection.target, settings
                ):
                    ctx.breaker.abandon(hedge_selection.target.target_id)
                    return await primary
                secondary = asyncio.ensure_future(
                    _attempt_on(hedge_selection, attempt_no + 1, None)
                )
                log.info(
                    "%s has produced nothing in %.1fs; racing %s",
                    selection.target.target_id,
                    settings.hedge_after_s,
                    hedge_selection.target.target_id,
                )
                try:
                    done, _ = await asyncio.wait(
                        {primary, secondary},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    winner = next(iter(done))
                    loser = secondary if winner is primary else primary
                    result = winner.result()
                    # A winner that is not actually usable is no winner: fall
                    # back to whatever the other one has to say rather than
                    # discarding a live response to report a refusal.
                    if not isinstance(result, Attempt) or result.response is None:
                        return await loser
                    loser.cancel()
                    try:
                        await _discard(await loser)
                    except asyncio.CancelledError:
                        # Expected: the loser was cancelled mid-flight, and
                        # `forward` gives its upstream back on the way out.
                        pass
                    except Exception:
                        log.warning("hedged loser did not unwind", exc_info=True)
                    return result
                finally:
                    for task in (primary, secondary):
                        if not task.done():
                            task.cancel()

            for attempt_no in range(settings.failover_max_attempts):
                selection = _claim(attempt_no)
                if selection is None:
                    break
                if selection is _SKIP:
                    continue
                if settings.hedge_after_s is not None:
                    outcome = await _hedged(selection, attempt_no)
                else:
                    outcome = await _attempt_on(
                        selection, attempt_no, _make_reopen(attempt_no)
                    )
                if outcome is None:
                    continue
                if not isinstance(outcome, Attempt):
                    return outcome  # a refusal, already recorded
                result = outcome
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
            return errors.unknown_model(model, _visible_names(request, fresh.served_names()))

        async def _deliver() -> Response:
            answer = await _dispatch()
            if answer is not None:
                return answer

            # Nothing was reachable. If this model was serving a moment ago and
            # no target is deliberately shedding load, hold the request briefly
            # for one to come back rather than refusing outright.
            if ctx.parking.accepts(model, raw_len) and ctx.router.parkable(model):
                parked_at = time.monotonic()
                recovered = await ctx.parking.park(
                    model,
                    select=lambda: ctx.router.select(model, prefix_key=prefix_key, **policy_kw),
                    is_disconnected=request.is_disconnected,
                )
                # Recorded whether or not it recovered: how long requests wait
                # in the lot is the gauge this queue has never had.
                trace.parked_ms = (time.monotonic() - parked_at) * 1000.0
                if recovered is not None:
                    answer = await _dispatch()
                    if answer is not None:
                        return answer
            refusal = _nowhere_to_send()
            return _refused(refusal, "no_target")

        if reservation is None:
            return await _deliver()
        # Every reservation is settled exactly once, whatever happens: the
        # attempt that answered counts its usage in _on_record, and this
        # releases whatever is still held -- at once for a response that is
        # already complete, at the end of the body for a stream. The limit
        # service ignores the second settle, so the release after a counted
        # attempt is a no-op.
        try:
            answer = await _deliver()
        except BaseException:
            _settle_limits(None)
            raise
        if isinstance(answer, StreamingResponse):
            answer.body_iterator = _released_at_end(
                answer.body_iterator,
                _settle_limits,
                lambda: ctx.auth_stores.limits.mark_started(reservation),
            )
        else:
            _settle_limits(None)
        return answer

    async def _chat_as_session(request: Request, run) -> Response:
        """A UI chat request: the session's admin, running as their admin key.

        The session gate has already passed; this attaches the principal the
        /v1 key gate would have, so the request takes exactly the same path.
        """
        try:
            request.state.principal = authz.session_principal(
                ctx.auth_stores, request, request.state.session
            )
        except AuthUnavailable as exc:
            return authz.unavailable(exc)
        except AuthError as exc:
            return errors.error_response(401, exc.message, "invalid_request_error", exc.code.value)
        return await run()

    @router.post("/api/chat/completions")
    async def session_chat_completions(request: Request) -> Response:
        return await _chat_as_session(
            request, lambda: _proxy(request, "/chat/completions", True)
        )

    @router.post("/api/chat/speech")
    async def session_speech(request: Request) -> Response:
        return await _chat_as_session(request, lambda: _proxy(request, "/audio/speech", False))

    @router.post("/api/chat/transcriptions")
    async def session_transcriptions(request: Request) -> Response:
        return await _chat_as_session(
            request, lambda: _proxy_multipart(request, "/audio/transcriptions")
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

    @router.get("/v1/audio/voices")
    @router.get("/api/chat/voices")
    async def audio_voices(request: Request) -> Response:
        """Which reference voices this deployment has installed.

        Not an OpenAI route. It exists because `runtimes/tts.py` serves one and
        nothing outside the container could ask it: a voice is a `<name>.wav`
        beside a `<name>.txt` transcript, an unknown name is refused by design,
        and a caller that cannot enumerate them has to guess at the one thing
        the server will not let them guess at.

        Three ways it differs from every route above, each for a reason:

        *   The model is a **query parameter**. This is a GET with no body, and
            the name still has to be here -- voices are a property of one
            deployment's filesystem, not of the cluster.
        *   It does not go through `_serve`. Streaming, admission, the parking
            lot, cache affinity, spend and the retry budget are all about a
            generation request; this is a directory listing. What it does keep
            is the model lookup and the modality guard, so a wrong name fails
            here exactly as it would on /v1/audio/speech.
        *   It answers from a **local** target only. No provider implements
            this route, and a speech model that exists only behind one is
            refused with the reason rather than handed a 404 from somebody
            else's API.

        A key still answers to its allowlist and limits here (finding K2):
        the listing is one request against `rpm`, `requests_cycle` and
        `max_concurrent`, unpriced, and it leaves a `requests` row like any
        other /v1 call. The session route (`/api/chat/voices`) carries no key.
        """
        model = request.query_params.get("model")
        if not model:
            return errors.error_response(
                400,
                "You must provide a 'model' query parameter, naming the "
                "deployment whose voices you want: "
                "GET /v1/audio/voices?model=<served name>.",
                "invalid_request_error",
                "missing_model",
            )

        principal: Principal | None = getattr(request.state, "principal", None)
        trace = RequestTrace(
            request_id=getattr(request.state, "request_id", None) or authz.new_request_id(),
            served_name=model,
            node_id=ctx.settings.coordinator_node_id or "",
            streaming=False,
        )
        trace.model_requested = model
        if principal is not None:
            trace.key_id = principal.key.key_id
            trace.key_name = principal.key.name
            trace.org_id = principal.org.org_id
            trace.client_ip = principal.ip
            trace.user_agent = principal.user_agent
        started = time.monotonic()
        reservation = None
        reached = False

        def _done(response: Response, code: str | None = None) -> Response:
            """Settle what was reserved and write the row, then answer."""
            if reservation is not None:
                try:
                    ctx.auth_stores.limits.settle(
                        reservation,
                        UsageActuals(
                            prompt_tokens=0,
                            completion_tokens=0,
                            cost_usd=None,
                            cost_basis=CostBasis.UNPRICED,
                            target_kind=TargetKind.LOCAL.value,
                            model_served=model,
                        )
                        if reached
                        else None,
                    )
                except Exception:
                    log.exception("could not settle voices reservation %s", trace.request_id)
            if code:
                trace.error_code = code
            try:
                ctx.sink.request(
                    trace.record(
                        status=response.status_code,
                        duration_s=time.monotonic() - started if reached else None,
                    )
                )
            except Exception:
                log.debug("could not record voices telemetry", exc_info=True)
            response.headers["X-Request-Id"] = trace.request_id
            return response

        if principal is not None:
            try:
                allowed = authz.model_allowed(ctx.auth_stores, principal, model)
            except AuthUnavailable as exc:
                return _done(authz.unavailable(exc), exc.code.value)
            if not allowed:
                code = AuthErrorCode.MODEL_NOT_ALLOWED.value
                return _done(_model_not_allowed(model), code)

        index = ctx.router.index()
        if model not in index.targets:
            pending = index.pending.get(model)
            if pending:
                return _done(
                    errors.model_not_ready(model, sorted({d.state.value for d in pending})),
                    "model_not_ready",
                )
            return _done(
                errors.unknown_model(model, _visible_names(request, index.served_names())),
                "model_not_found",
            )

        has = index.modality.get(model)
        if has is not None and _modality_conflict(has, Modality.SPEECH):
            return _done(
                errors.wrong_modality(model, has, Modality.SPEECH, "/v1/audio/voices"),
                "wrong_modality",
            )

        local = next(
            (
                t
                for t in index.targets[model]
                if t.kind is TargetKind.LOCAL and t.backend_url
            ),
            None,
        )
        if local is None:
            return _done(
                errors.error_response(
                    400,
                    f"'{model}' is served by a provider, and a voice library is a "
                    f"property of a deployment this cluster runs: the reference "
                    f"clips live on the node, in the directory the tts runtime was "
                    f"pointed at. Deploy it here to choose a voice, or send "
                    f"/v1/audio/speech without one and the model speaks in its own.",
                    "invalid_request_error",
                    "no_local_target",
                ),
                "no_local_target",
            )

        if principal is not None:
            try:
                decision, reservation = ctx.auth_stores.limits.check_and_reserve(
                    principal, model=model, target_kind=TargetKind.LOCAL.value, body={},
                    route_limits=ctx.router.limits_for(model),
                )
            except AuthUnavailable as exc:
                return _done(authz.unavailable(exc), exc.code.value)
            if not decision.allowed:
                trace.limit_source = decision.limit_source
                return _done(_limit_refusal(decision), decision.code)

        url = local.backend_url.rstrip("/") + "/audio/voices"
        try:
            # The engine requires its per-deployment key on /v1 (H3).
            headers = (
                {"authorization": f"Bearer {local.runtime_api_key}"}
                if local.runtime_api_key
                else {}
            )
            headers["x-request-id"] = trace.request_id
            upstream = await ctx.proxy.client_for(url).get(url, headers=headers)
        except Exception as exc:  # httpx raises a family, not one class
            return _done(
                errors.error_response(
                    502,
                    f"Could not reach '{model}' to list its voices: "
                    # The shared provider redactor, reached the way internal_api's
                    # _redactor does: a fresh one knows no secrets to scrub, and a
                    # local backend_url is not a secret but the exception chain
                    # under it may have come from anywhere.
                    f"{errors.detail(exc, getattr(ctx.deps.providers, 'redactor', None))}.",
                    "server_error",
                    "upstream_unreachable",
                ),
                "upstream_unreachable",
            )
        reached = True
        # The runtime's own envelope, unaltered -- including `skipped`, which
        # is it explaining which clips it declined and why. Re-shaping that
        # would throw away the only sentence a person can act on.
        return _done(
            Response(
                content=upstream.content,
                status_code=upstream.status_code,
                media_type=upstream.headers.get("content-type", "application/json"),
            )
        )

    @router.websocket("/v1/realtime")
    async def realtime_session(websocket: WebSocket) -> None:
        """A realtime voice session, relayed to a provider that implements it.

        The only route here that is not a request/response pair, and the only
        one that does not go through _serve: a session cannot be re-offered to
        another target halfway through, because the upstream holds conversation
        state we never saw. See realtime.py for what this does and does not do.
        """
        stores = ctx.auth_stores
        principal = await authz.require_key(websocket, stores)
        if principal is None:
            return
        model = websocket.query_params.get("model") or ""
        trace = RequestTrace(
            request_id=getattr(websocket.state, "request_id", None) or authz.new_request_id(),
            served_name=model,
            node_id=ctx.settings.coordinator_node_id or "",
            streaming=True,
        )
        trace.key_id = principal.key.key_id
        trace.key_name = principal.key.name
        trace.org_id = principal.org.org_id
        trace.client_ip = principal.ip
        trace.user_agent = principal.user_agent
        trace.client_port = getattr(websocket.state, "client_port", None)
        trace.model_requested = model or None
        # The same limits every other /v1 request answers to (finding M4):
        # the allowlist, max_concurrent, the rate windows and the cloud cycle
        # budgets. A session is one request that holds its slot until it
        # closes; its audio minutes are not priced, so it settles unpriced.
        reservation = None
        if model:
            try:
                decision, reservation = stores.limits.check_and_reserve(
                    principal, model=model, target_kind=TargetKind.REMOTE.value, body={},
                    route_limits=ctx.router.limits_for(model),
                )
            except AuthUnavailable as exc:
                await websocket.close(code=realtime.CLOSE_UPSTREAM, reason=exc.code.value)
                return
            if not decision.allowed:
                trace.limit_source = decision.limit_source
                trace.error_code = decision.code
                try:
                    ctx.sink.request(trace.record(status=decision.status, duration_s=None))
                except Exception:
                    log.debug("could not record realtime telemetry", exc_info=True)
                await websocket.close(code=authz.CLOSE_POLICY, reason=decision.code)
                return
        # Whether the relay accepted the socket is the session's status: a
        # refusal before accept is the handshake's 403, an accepted one 101.
        accepted = False
        accept = websocket.accept

        async def _accept(*args, **kwargs):
            nonlocal accepted
            accepted = True
            return await accept(*args, **kwargs)

        websocket.accept = _accept
        started = time.monotonic()
        key_id = principal.key.key_id

        async def key_still_active() -> bool:
            return await asyncio.to_thread(realtime.key_active, stores, key_id)

        ended_by = None
        try:
            ended_by = await realtime.relay(ctx, websocket, still_valid=key_still_active)
        finally:
            if reservation is not None:
                try:
                    stores.limits.settle(
                        reservation,
                        UsageActuals(
                            prompt_tokens=0,
                            completion_tokens=0,
                            cost_usd=None,
                            cost_basis=CostBasis.UNPRICED,
                            target_kind=TargetKind.REMOTE.value,
                            model_served=model,
                        )
                        if accepted
                        else None,
                    )
                except Exception:
                    log.exception("could not settle realtime reservation %s", trace.request_id)
            if not accepted:
                trace.error_code = "realtime_refused"
            elif ended_by == realtime.ENDED_KEY_DISABLED:
                # The session's close record says it was cut, not hung up.
                trace.error_code = realtime.ENDED_KEY_DISABLED
            try:
                ctx.sink.request(
                    trace.record(
                        status=101 if accepted else 403,
                        duration_s=time.monotonic() - started,
                    )
                )
            except Exception:
                log.debug("could not record realtime telemetry", exc_info=True)

    return router
