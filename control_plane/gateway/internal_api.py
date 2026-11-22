"""The internal API. Exactly the surface in architecture section 4.8.

No additions without updating that file: the UI codes against it exactly.
Where a port does not yet expose an operation the HTTP surface
promises, the endpoint degrades with a clear 501 rather than disappearing,
so the UI can be built against the full shape from day 0.
"""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import html
import itertools
import json
import logging
import os
import re
import shlex
import threading
from functools import partial
import time
from pathlib import Path
from urllib.parse import quote, urlparse, urlsplit

from fastapi import APIRouter, Query, Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from control_plane.contracts.constants import JOIN_PER_ADDRESS_PER_MIN
from control_plane.contracts.deployment import (
    EngineOptions,
    Lifecycle,
    check_speculative_tokens,
)
from control_plane.contracts import (
    DeviceClass,
    DeploymentState,
    FitRequest,
    Modality,
    RoutingPolicy,
    SpeculativeMethod,
    SpeculativeSpec,
    Verdict,
)
from control_plane.fit.capacity import (
    MAX_DERIVED_CONCURRENCY,
    concurrency_for,
    context_for,
)
from control_plane.planner.constants import LATENCY_CONCURRENCY_CEILING
from control_plane.providers import (
    AdapterUnsupportedError,
    SignInError,
    UnknownProviderError,
    UpstreamError,
)
from control_plane.planner import (
    Candidate,
    DEFAULT_KV_DTYPE,
    IllegalDegrees,
    homogeneous_groups,
    pooling_note,
    valid_ep_degrees,
    valid_pp_degrees,
    valid_tp_degrees,
)
from control_plane.registry import JoinRejected, NodeNotFound
from control_plane.version import build_id
from control_plane.registry import joinproof, modelcache, reach, storage as registry_storage
from control_plane.registry import net as registry_net
from control_plane.registry.serde import profile_from_dict
from control_plane.registry.agent import AGENT_FEATURES

from control_plane.telemetry import query as tquery

from control_plane.humanize import binary_bytes

from control_plane.contracts.auth import AuditAction, AuthUnavailable

from . import errors, gpu_procs, livefit, serialize, states, ui_detail
from .authz import audit_request, client_ip, is_loopback, request_context, stores_of
from .body import BodyTooLarge, optional_json_object, payload_too_large, read_json_object
from .loopback import LoopbackCallback
from control_plane.providers.config import CHATGPT_REDIRECT_URI, SIGN_IN_ATTEMPT_TTL_S
from .deps import GatewayContext

log = logging.getLogger("gateway.api")


def _sign_in_page(title: str, message: str) -> str:
    """The page a ChatGPT redirect lands on. Static, no script, every value escaped."""
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        f"<title>{html.escape(title)} — derate</title>"
        "<meta name=viewport content='width=device-width,initial-scale=1'></head>"
        "<body style='font:16px/1.5 system-ui,sans-serif;max-width:36em;margin:4em auto;padding:0 1em'>"
        f"<h1 style='font-size:1.25em'>{html.escape(title)}</h1>"
        f"<p>{html.escape(message)}</p></body></html>"
    )


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

#: Speculative decoding, when the operator turned it on. Absent means one token
#: per step, which is what every request sent before this key existed meant.
_SPECULATIVE_PARAM = "speculative"

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

#: One compute measurement at a time on this host: two matmul probes sharing
#: the GPU would each time half of it.
_COMPUTE_LOCK = threading.Lock()


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
    #: The repo ships Python --trust-remote-code would run (R2), so the
    #: Serve panel can offer that opt-in beside the resolver's warning.
    remote_code: bool = False
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
    #: Every speculative method this checkpoint declares, whether or not one
    #: was asked for, so the screen can offer them without a second round trip.
    #: An entry with `launchable: false` is named and refused, not hidden --
    #: "derate found DSpark and cannot price it" is a more useful thing to read
    #: than silence.
    speculative_options: list = dataclasses.field(default_factory=list)
    #: What one of these machines actually decoded at, when anything has. None
    #: is the ordinary answer and means nobody has run this model on this
    #: hardware yet -- never a stand-in for the prediction, which is stated
    #: beside it and is still true.
    measured_decode: dict | None = None
    #: The KV cache element width the gate SIZED WITH, after the settings
    #: default has been applied. It rides here rather than being re-derived
    #: at the launch call site because the two must be one value: the gate
    #: halves `kv_bytes_per_token` for fp8 and approves a context on that
    #: basis, so a launch that does not pass the same width gets the halved
    #: byte budget filled with full-width entries -- half the context the
    #: gate promised, no error anywhere.
    kv_dtype: str = DEFAULT_KV_DTYPE
    #: The weight quantization the CALLER forced, or None to let the runtime
    #: read it off the checkpoint. Distinct from `shape.dtype`, which is the
    #: forced value once an override was applied and the checkpoint's own
    #: otherwise -- the launch needs to know which of the two it is looking
    #: at, because only a forced one may render a `--quantization` flag.
    quantization: str | None = None
    #: What was actually charged, echoed back. None when the request asked for
    #: none, which is the ordinary case.
    speculative: object | None = None
    #: The commit sha the resolver read the shape from, so the launch runs
    #: exactly that revision's remote code (M7). None when unknown.
    revision: str | None = None
    #: The typed engine options the request carried, parsed once here so the
    #: plan and the launch render the same flags.
    engine_options: EngineOptions = EngineOptions()
    #: The model's trained window (max_position_embeddings), None when the
    #: config states none. Rendering YaRN needs it.
    original_window: int | None = None
    #: The window the plan sized against: the trained one, times rope_factor.
    native_window: int | None = None
    #: Whether the runtime loads this model on the machines the plan landed
    #: on, judged by the image EACH of them runs (`_support_per_node`), and
    #: the runtime's own sentence when it does not. The launch and the
    #: restart read this rather than asking `supported_by` again: asked
    #: without the machines, the question is answered by the default image,
    #: which refuses an architecture only another machine's image loads.
    runtime_supported: bool = True
    runtime_unsupported_reason: str | None = None


def _kv_dtype_ratios(shape) -> dict[str, float]:
    """Every `--kv-cache-dtype` the gate prices for *shape*, as its cache
    bytes per token over the 16-bit rate. Absent: a name the gate does not
    price, cannot render, or refuses for this attention geometry."""
    from control_plane.deploy.flags import VLLM_KV_CACHE_DTYPES, render_kv_cache_dtype
    from control_plane.fit.kv import is_known_kv_dtype, kv_bytes_per_token, kv_dtype_refusal

    base = kv_bytes_per_token(shape, "bf16")
    if base <= 0:
        return {}
    out: dict[str, float] = {}
    for name in sorted(VLLM_KV_CACHE_DTYPES):
        if name == "auto" or not is_known_kv_dtype(name):
            continue
        try:
            render_kv_cache_dtype(name)
        except ValueError:
            continue
        if kv_dtype_refusal(shape, name) is not None:
            continue
        out[name] = kv_bytes_per_token(shape, name) / base
    return out


_ENGINE_ENV_VALUE = re.compile(r"^[A-Za-z0-9_.,:/=+-]*$")
_MM_KEYS = ("image", "video", "audio")


def _engine_number(name: str, value: object, *, integer: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"engine_options.{name} must be a number.")
    if integer and (not isinstance(value, int) and not float(value).is_integer()):
        raise ValueError(f"engine_options.{name} must be a whole number.")
    return value


def _parse_engine_options(payload: dict) -> EngineOptions:
    """The request's `engine_options`, typed and range-checked.

    Unknown keys are a 400 rather than ignored: a misspelled knob would
    otherwise launch and report as set. The three object-valued fields are
    turned into the contract's sorted tuples of pairs.
    """
    from control_plane.deploy.flags import (
        NCCL_TUNABLES,
        VLLM_REASONING_PARSERS,
        VLLM_TOOL_CALL_PARSERS,
    )

    raw = payload.get("engine_options") if isinstance(payload, dict) else None
    if raw is None:
        return EngineOptions()
    if not isinstance(raw, dict):
        raise ValueError("engine_options must be an object.")
    known = [f.name for f in dataclasses.fields(EngineOptions)]
    unknown = sorted(set(raw) - set(known))
    if unknown:
        raise ValueError(
            f"engine_options has no field {', '.join(unknown)}. "
            f"Valid fields: {', '.join(known)}."
        )
    out: dict = {}
    for name in ("reasoning_parser", "tool_call_parser"):
        value = raw.get(name)
        if value is not None:
            if not isinstance(value, str) or not value:
                raise ValueError(f"engine_options.{name} must be a parser name.")
            allowed = (
                VLLM_REASONING_PARSERS if name == "reasoning_parser" else VLLM_TOOL_CALL_PARSERS
            )
            if value not in allowed:
                kind = "reasoning" if name == "reasoning_parser" else "tool-call"
                raise ValueError(
                    f"{value} is not a {kind} parser in this vLLM build; it has: "
                    f"{', '.join(sorted(allowed))}."
                )
            out[name] = value
    for name in ("prefix_caching",):
        value = raw.get(name)
        if value is not None:
            if not isinstance(value, bool):
                raise ValueError(f"engine_options.{name} must be true, false or null.")
            out[name] = value
    for name in ("async_scheduling", "language_model_only"):
        value = raw.get(name, False)
        if not isinstance(value, bool):
            raise ValueError(f"engine_options.{name} must be true or false.")
        out[name] = value
    value = raw.get("max_num_batched_tokens")
    if value is not None:
        value = _engine_number("max_num_batched_tokens", value, integer=True)
        if not 1 <= value <= 1_048_576:
            raise ValueError("engine_options.max_num_batched_tokens must be from 1 to 1048576.")
        out["max_num_batched_tokens"] = int(value)
    mm = raw.get("limit_mm_per_prompt")
    if mm is not None:
        if not isinstance(mm, dict):
            raise ValueError("engine_options.limit_mm_per_prompt must be an object.")
        pairs = []
        for key, count in mm.items():
            if key not in _MM_KEYS:
                raise ValueError(
                    f"engine_options.limit_mm_per_prompt has no key {key}; "
                    f"use {', '.join(_MM_KEYS)}."
                )
            count = _engine_number(f"limit_mm_per_prompt.{key}", count, integer=True)
            if not 0 <= count <= 64:
                raise ValueError(f"engine_options.limit_mm_per_prompt.{key} must be from 0 to 64.")
            pairs.append((key, int(count)))
        out["limit_mm_per_prompt"] = tuple(sorted(pairs))
    value = raw.get("mm_processor_cache_gb")
    if value is not None:
        value = _engine_number("mm_processor_cache_gb", value)
        if not 0 <= value <= 64:
            raise ValueError("engine_options.mm_processor_cache_gb must be from 0 to 64.")
        out["mm_processor_cache_gb"] = float(value)
    value = raw.get("rope_factor")
    if value is not None:
        value = _engine_number("rope_factor", value)
        if not 1.0 < value <= 8.0:
            raise ValueError("engine_options.rope_factor must be above 1 and at most 8.")
        out["rope_factor"] = float(value)
    gen = raw.get("generation_overrides")
    if gen is not None:
        if not isinstance(gen, dict):
            raise ValueError("engine_options.generation_overrides must be an object.")
        pairs = []
        for key, number in gen.items():
            if key == "temperature":
                number = _engine_number("generation_overrides.temperature", number)
                ok = 0 <= number <= 2
            elif key == "top_p":
                number = _engine_number("generation_overrides.top_p", number)
                ok = 0 < number <= 1
            elif key == "top_k":
                number = _engine_number("generation_overrides.top_k", number, integer=True)
                ok = -1 <= number <= 1000
            else:
                raise ValueError(
                    f"engine_options.generation_overrides has no key {key}; "
                    "use temperature, top_p, top_k."
                )
            if not ok:
                raise ValueError(f"engine_options.generation_overrides.{key} is out of range.")
            pairs.append((key, float(number)))
        out["generation_overrides"] = tuple(sorted(pairs))
    env = raw.get("env")
    if env is not None:
        if not isinstance(env, dict):
            raise ValueError("engine_options.env must be an object of names to values.")
        pairs = []
        for name, value in env.items():
            if name not in NCCL_TUNABLES:
                raise ValueError(
                    f"{name} is not an NCCL variable this image's library reads; "
                    "derate only passes NCCL tuning through a launch."
                )
            if not isinstance(value, str) or not _ENGINE_ENV_VALUE.match(value):
                raise ValueError(
                    f"engine_options.env.{name} must be a string of letters, digits "
                    "and _.,:/=+-"
                )
            pairs.append((name, value))
        out["env"] = tuple(sorted(pairs))
    return EngineOptions(**out)


_LIFECYCLE_PRIORITIES = ("low", "normal", "high", "pinned")
_IDLE_UNLOAD_RANGE = (300, 604800)


def _parse_lifecycle(payload: dict, base: Lifecycle = Lifecycle()) -> Lifecycle:
    """The request's `lifecycle`, merged over `base` (PATCH sends a partial).

    Not a fit input. Unknown keys are a 400, like `engine_options`.
    """
    raw = payload.get("lifecycle") if isinstance(payload, dict) else None
    if raw is None:
        return base
    if not isinstance(raw, dict):
        raise ValueError("lifecycle must be an object.")
    known = {f.name for f in dataclasses.fields(Lifecycle)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ValueError(
            f"lifecycle has no field {', '.join(unknown)}; it takes "
            f"{', '.join(sorted(known))}."
        )
    changes: dict = {}
    if "idle_unload_s" in raw:
        v = raw["idle_unload_s"]
        lo, hi = _IDLE_UNLOAD_RANGE
        if v is not None and (
            isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi
        ):
            raise ValueError(
                f"lifecycle.idle_unload_s must be whole seconds from {lo} to "
                f"{hi}, or null to stay loaded."
            )
        changes["idle_unload_s"] = v
    if "priority" in raw:
        v = raw["priority"]
        if v not in _LIFECYCLE_PRIORITIES:
            raise ValueError(
                f"lifecycle.priority must be one of {', '.join(_LIFECYCLE_PRIORITIES)}."
            )
        changes["priority"] = v
    if "start_at_boot" in raw:
        v = raw["start_at_boot"]
        if not isinstance(v, bool):
            raise ValueError("lifecycle.start_at_boot must be true or false.")
        changes["start_at_boot"] = v
    return dataclasses.replace(base, **changes)


def _without_vision(shape, weight_bytes: int | None):
    """The shape and measured weights with the vision tower taken out, for
    `engine_options.language_model_only` (vLLM `--language-model-only`)."""
    if shape.vision_params == 0:
        raise ValueError("This model has no vision tower to skip.")
    vision_bytes = int(shape.vision_params * shape.bytes_per_param())
    shape = dataclasses.replace(
        shape,
        vision_params=0,
        total_params=shape.total_params - shape.vision_params,
        active_params=(
            shape.active_params - shape.vision_params
            if shape.active_params is not None
            else None
        ),
    )
    if weight_bytes is not None:
        weight_bytes = max(0, weight_bytes - vision_bytes)
    return shape, weight_bytes


def _with_codec_working_set(shape, architectures):
    """The shape carrying the tts server's measured codec working set
    (`resolver/support.py::TTS_WORKING_SETS`), which the fit gate charges in
    `activations`. Unchanged when the tts runtime loads none of
    *architectures*: that is the support check's refusal to make."""
    from control_plane.resolver.support import speech_working_set

    found = speech_working_set(architectures)
    if found is None:
        return shape
    return dataclasses.replace(
        shape,
        codec_resident_bytes=found.resident_bytes,
        codec_bytes_per_frame=found.bytes_per_frame,
    )


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


def _parse_speculative(payload: dict) -> tuple[str, int, str | None] | None:
    """The method and drafted-token count the operator asked for, or None.

    Shape only. Whether *this* checkpoint offers that method, and what the
    draft costs, are questions this function cannot answer -- the resolution
    holds both -- so it returns the request's own two values and
    ``_speculative_spec`` below turns them into a priced
    :class:`SpeculativeSpec` or refuses.
    """
    if _SPECULATIVE_PARAM not in payload:
        return None
    raw = payload.get(_SPECULATIVE_PARAM)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(
            f"{_SPECULATIVE_PARAM} must be an object with a method and a "
            f"num_speculative_tokens, or be omitted"
        )
    method = raw.get("method")
    if not isinstance(method, str) or not method:
        raise ValueError(f"{_SPECULATIVE_PARAM}.method is required")
    tokens = raw.get("num_speculative_tokens")
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 1:
        raise ValueError(
            f"{_SPECULATIVE_PARAM}.num_speculative_tokens must be an integer of "
            f"at least 1"
        )
    head = raw.get("model")
    if head is not None and (not isinstance(head, str) or not head):
        raise ValueError(
            f"{_SPECULATIVE_PARAM}.model must be the head's repository id, or be "
            f"omitted for a method the target's own checkpoint carries"
        )
    return method, tokens, head


def _speculative_spec(resolution, asked, *, resolve_head=None, image_speculators=None):
    """A priced ``SpeculativeSpec``, or a refusal naming what is on offer.

    Every number in the returned spec comes from the resolution, never from the
    request: a caller may choose the method and how many tokens to draft, and
    may not tell the fit gate what the draft weighs. That is the whole point of
    charging it -- a caller-supplied cost would be a budget the operator wrote
    for themselves.
    """
    if asked is None:
        return None, None
    method, tokens, head_id = asked

    # An externally-published head. The target's config says nothing about it
    # -- that is what makes it external -- so the option is built from the
    # head's OWN resolution, and the method comes from the class it declares
    # rather than from what the caller typed beside it.
    if head_id:
        spec_detect = _speculative_detect()
        if spec_detect is None:
            raise ValueError(
                "this build cannot describe a speculative head, so it will "
                "not budget one"
            )
        if resolve_head is None:
            raise ValueError(
                "a speculative head has to be resolved before it can be "
                "budgeted, and this gateway has no resolver that can do it"
            )
        if head_id == resolution.shape.model_id:
            raise ValueError(
                f"{head_id} is the model being served; a draft head is a "
                f"separate, smaller repository trained to predict for it"
            )
        try:
            head = resolve_head(head_id)
        except Exception as exc:
            raise ValueError(
                f"could not resolve the speculative head {head_id}: {exc}"
            ) from exc
        option = spec_detect.head_option(
            head, resolution.shape, image_speculators=image_speculators,
            # What the TARGET is, so a head that declares a different one is
            # refused. Absent on an older resolution, which degrades to the
            # geometry-only gates rather than refusing anything.
            target_model_type=getattr(resolution, "model_type", "") or "",
        )
        if not option.launchable:
            raise ValueError(option.note)
        if tokens > option.max_tokens:
            raise ValueError(
                f"{option.method.value} with {head_id} drafts at most "
                f"{option.max_tokens} token"
                f"{'s' if option.max_tokens != 1 else ''} per step, not {tokens}"
            )
        return (
            SpeculativeSpec(
                method=option.method,
                num_speculative_tokens=tokens,
                draft_bytes=int(option.draft_bytes or 0),
                draft_params=int(option.draft_params or 0),
                model=head_id,
                draft_kv_ratio=float(getattr(option, "draft_kv_ratio", 0.0) or 0.0),
            ),
            option,
        )

    options = list(getattr(resolution, "speculators", ()) or ())
    if not options:
        raise ValueError(
            f"{_SPECULATIVE_PARAM} was requested but this build has not "
            f"determined which speculative methods {resolution.shape.model_id} "
            f"supports; re-resolve the model and try again"
        )
    offered = {opt.method.value: opt for opt in options}
    option = offered.get(method)
    if option is None:
        raise ValueError(
            f"{resolution.shape.model_id} does not offer speculative method "
            f"{method!r}. It offers: {', '.join(sorted(offered))}."
        )
    if not option.launchable:
        # `note` is the resolver's own sentence and says why. Shown whole
        # rather than summarised, on the same terms as every other refusal
        # string in this project.
        raise ValueError(
            f"{method} cannot be launched on {resolution.shape.model_id}: "
            f"{option.note}"
        )
    if tokens > option.max_tokens:
        raise ValueError(
            f"{method} on {resolution.shape.model_id} drafts at most "
            f"{option.max_tokens} token"
            f"{'s' if option.max_tokens != 1 else ''} per step, not {tokens}"
        )
    # No second value: a built-in method's option is already on
    # `speculative_options`. A head's cannot be -- nothing knew it existed
    # until the request named it -- which is why the head branch returns one.
    return (
        SpeculativeSpec(
            method=SpeculativeMethod(method),
            num_speculative_tokens=tokens,
            draft_bytes=int(option.draft_bytes or 0),
            draft_params=int(option.draft_params or 0),
        ),
        None,
    )


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


def _default_vllm_image() -> str:
    """The image a tuning record is keyed by, from the one place that owns it.

    `deploy/flags.py` holds the runtime specs, so the default lives there and
    not in a second string here -- a copy would drift and a record keyed by the
    drifted one would miss for ever while looking calibrated.
    """
    from control_plane.deploy.flags import runtime_spec

    return runtime_spec("vllm").default_image


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
    speculative_window=1,
):
    """The ranked plans, best first, from whichever surface this planner has.

    `alternatives` is the planner's own extra API and is what `plan` calls
    internally, so asking for the whole list costs nothing beyond the one call
    that was already happening. A planner exposing only the frozen port still
    works; it just cannot say what it ranked second.
    """
    kwargs = {"context_length": context_length, "kv_dtype": kv_dtype}
    # Spread only when it would change an answer, on the same terms the launch
    # path spreads `speculative`: absence is the contract for "one token per
    # step", which is what every plan meant before the planner had heard of a
    # draft window. A port that predates the field therefore keeps working
    # untouched for every ordinary plan, and only a speculative request can
    # meet one that cannot take it -- where it degrades rather than 500s.
    if speculative_window > 1:
        kwargs["speculative_window"] = speculative_window
    alternatives = getattr(planner, "alternatives", None)
    if callable(alternatives):
        try:
            return alternatives(
                shape,
                nodes,
                link,
                target,
                concurrency,
                allow_mixed_hardware=allow_mixed_hardware,
                **kwargs,
            )
        except TypeError:
            kwargs.pop("speculative_window", None)
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


def _placement_refusal(registry, runtime: str, node_id: str, profile) -> str | None:
    """`flags.placement_refusal`, with the live reading fetched for it.

    Lazily imported for exactly the reason `_sharding_refusal` below is: this
    module is the gateway's and `control_plane.deploy` is not always installed
    behind it.

    The live budget is read through the registry rather than passed in, because
    only a host-pool runtime needs it and fetching it for every placement on
    every plan request would put a telemetry read on a path that answers on
    every keystroke in the Serve panel.
    """
    try:
        from control_plane.deploy.flags import RUNTIMES, placement_refusal
    except Exception:  # pragma: no cover - deploy not installed
        return (
            None
            if profile.addressable_memory > 0
            else f"The machine '{node_id}' reports no addressable GPU memory, "
            f"so nothing can be placed on it."
        )
    spec = RUNTIMES.get(runtime)
    live = None
    if spec is not None and spec.memory_pool == "host":
        # The HOST pool's reading, never the GPU's: on a discrete box the two
        # are different machines' worth of memory.
        probe = getattr(registry, "allocatable_or_none", None)
        if callable(probe) and livefit._accepts_keyword(probe, "pool"):
            try:
                live = probe(node_id, pool=spec.memory_pool)
            except Exception:
                log.exception(
                    "live memory lookup failed for %s while planning", node_id
                )
                live = None
    return placement_refusal(runtime, profile, live)


def _select_nodes(registry, nodes, requested, warnings: list[str], runtime: str = "vllm"):
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
        # Whether this RUNTIME can be placed on this MACHINE, which is two
        # questions where it used to be one. The fit gate already drops a node
        # with nothing to spend from the live budget so it cannot become the
        # argmin (`livefit.drop_unbudgetable`); naming one explicitly deserves
        # the same answer said out loud, rather than a plan built around a
        # machine that can hold nothing.
        #
        # `flags.placement_refusal` owns the rule -- see its docstring for why
        # a CPU runtime refuses on a missing live reading where every other
        # path in this project degrades to a static one.
        refusal = _placement_refusal(registry, runtime, node_id, profile)
        if refusal is not None:
            raise _PlacementRefused(
                400,
                refusal,
                "node_has_no_memory",
                param=_PLACEMENT_PARAM,
                extra={"unusable_node_ids": [node_id]},
            )
        # A machine this runtime has no image for. Its own code, because the
        # fix is a different one: not memory, but a runtime -- or an image --
        # built for this machine's architecture.
        from control_plane.deploy.flags import arch_refusal

        refusal = arch_refusal(runtime, node_id, getattr(profile, "arch", ""))
        if refusal is not None:
            raise _PlacementRefused(
                400,
                refusal,
                "no_image_for_architecture",
                param=_PLACEMENT_PARAM,
                extra={"unusable_node_ids": [node_id]},
            )
        if profile.device_class is DeviceClass.UNKNOWN:
            warnings.append(serialize.INELIGIBLE_DEVICE_CLASS)
        chosen.append(profile)
    # One launch runs one image, and an image is built for one architecture:
    # a set that mixes them cannot be one deployment, whatever each machine
    # could run on its own.
    arches = {p.arch for p in chosen if getattr(p, "arch", "")}
    if len(arches) > 1:
        by = ", ".join(f"{p.node_id} ({p.arch})" for p in chosen if p.arch)
        raise _PlacementRefused(
            400,
            f"These machines are different architectures -- {by} -- and one "
            f"deployment runs one container image, built for one of them. "
            f"Tick machines of one architecture.",
            "mixed_architecture",
            param=_PLACEMENT_PARAM,
            extra={"architectures": sorted(arches)},
        )
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


def _launcher_version(deployments) -> str | None:
    """The installed sparkrun's version, or None when it cannot be asked.

    Through whatever the manager is actually holding rather than a fresh
    adapter, so a test double or a `DERATE_SPARKRUN_BIN` override is the thing
    reported. Every step degrades to None: a stub manager has no adapter, and
    `version()` shells out, which can time out.
    """
    adapter = getattr(deployments, "adapter", None)
    version = getattr(adapter, "version", None)
    if not callable(version):
        return None
    try:
        return version()
    except Exception:
        return None


def _data_parallel_refusal(
    tensor_parallel: int,
    pipeline_parallel: int,
    data_parallel: int = 1,
    launcher_version: str | None = None,
):
    """Why a pure data-parallel plan cannot be launched, or None.

    Its own wrapper rather than a branch of ``_sharding_refusal`` because the
    two answer different questions and so must carry different error codes:
    `runtime_cannot_shard` is about the runtime, and this one is about the
    launcher, on a runtime that shards perfectly well. Lazily imported for the
    reason the one below is.
    """
    try:
        from control_plane.deploy.flags import data_parallel_refusal
    except Exception:
        return None
    return data_parallel_refusal(
        tensor_parallel, pipeline_parallel, data_parallel, launcher_version
    )


def _sharding_refusal(
    runtime: str,
    tensor_parallel: int,
    pipeline_parallel: int,
    expert_parallel: int = 1,
    data_parallel: int = 1,
    launcher_version: str | None = None,
):
    """Why this runtime cannot run these degrees, or None.

    Imported lazily for the reason ``_TERMINAL_STATES`` is spelled out rather
    than imported: ``control_plane.deploy`` runs its package __init__, which
    pulls the manager, the sparkrun adapter and the event bus into a request
    module. An unknown runtime is not this check's to refuse -- ``supported_by``
    above already answered that question -- so it degrades to None.
    """
    try:
        from control_plane.deploy.flags import sharding_refusal
    except Exception:
        return None
    try:
        return sharding_refusal(
            runtime,
            tensor_parallel,
            pipeline_parallel,
            expert_parallel,
            data_parallel,
            launcher_version,
        )
    except ValueError:
        return None


def _image_speculators() -> frozenset[str] | None:
    """Which speculator classes the runtime image registers, or None.

    None means the image could not be asked -- no docker, no image, a probe
    that timed out -- and it must never be read as "this image supports none".
    The head check treats None as "no opinion" for exactly the reason
    `imageprobe.py` degrades rather than raising: the better answer is used
    when it is there, and its absence is not a refusal.
    """
    try:
        from control_plane.resolver import support
    except Exception:
        return None
    # The same recorded probe `architectures_for` reads, so the speculator
    # answer and the servable answer always come from one container start and
    # one image. `probed()` is None until the resolver's background probe
    # lands, and on a coordinator with no docker it stays None forever.
    found = support.probed("vllm")
    specs = getattr(found, "speculators", None) if found is not None else None
    return frozenset(specs) if specs else None


def _attach_measurements(options: list[dict], model_id: str, nodes: list) -> list[dict]:
    """Add any measured record that is about THIS model on THESE machines.

    A record is a fact about one workload on one GPU under one image
    (`control_plane/measurements.py` says why each of those is part of the key),
    so the criteria are read off the plan's own nodes rather than off the
    coordinator's. A sweep taken on a GB10 must not be cited for a launch the
    planner just placed on something else.

    Every matching workload is attached, not the best one. Which to believe is
    the reader's call -- acceptance on code that mostly copies its input is a
    different number from acceptance on prose, and picking one here would be
    picking the flattering one.

    Failure is silence. A missing record, an unreadable estate and a
    coordinator with no measurements at all are the same thing to the screen:
    the range is still stated, and it is still true.
    """
    if not options:
        return options
    try:
        from control_plane import measurements
    except Exception:
        return options

    gpu = bandwidth = None
    arch = ""
    for node in nodes:
        if getattr(node, "memory_bandwidth_gbps", 0):
            gpu = getattr(node, "gpu_name", None)
            bandwidth = node.memory_bandwidth_gbps
            arch = getattr(node, "arch", "") or ""
            break
    # The version of the image THAT machine runs, never the default image's:
    # a record taken under the 3090's vLLM 0.31 must not answer for a Spark on
    # 0.28.1. No probe of that image is a miss -- `matching` reads a falsy
    # version as "any", so the lookup is skipped rather than asked.
    version = measurements.image_version("vllm", arch) if gpu is not None else ""

    out: list[dict] = []
    for option in options:
        found = []
        if version:
            try:
                found = measurements.matching(
                    model_id, option.get("method", ""), gpu_name=gpu,
                    memory_bandwidth_gbps=bandwidth, runtime_version=version,
                )
            except Exception:
                log.exception("reading speculative measurements failed")
                found = []
        out.append({
            **option,
            "measured": [serialize.measurement(r) for r in found],
        })
    return out


def _measured_decode(
    model_id: str, nodes: list, context_length: int, runtime: str
) -> dict | None:
    """The measured decode rate for THIS model on THESE machines, or None.

    The same argument as :func:`_attach_measurements`, applied to the ordinary
    decode rate rather than the speculative one: a record is a fact about one
    model on one GPU under one image, so the criteria come off the plan's own
    nodes and a mismatch on any of them misses rather than approximating.

    Cited BESIDE the predicted range and never in place of it. The range is
    still what is true for a context nobody has run; this says what one machine
    actually did, and on the box this was written for the two disagreed by 2x --
    which is the whole reason the range now has two ends.

    Context matters here in a way it does not for acceptance: decode reads the
    cache for the tokens actually present, so a rate measured at 300 tokens is
    not the rate at 8192. The band is matched on, and `matching_decode` orders
    nearest-first rather than excluding, so a neighbouring band is offered
    rather than nothing at all.

    Failure is silence, on the same terms as the speculative path.
    """
    try:
        from control_plane import measurements
    except Exception:
        return None

    gpu = bandwidth = None
    arch = ""
    for node in nodes:
        if getattr(node, "memory_bandwidth_gbps", 0):
            gpu = getattr(node, "gpu_name", None)
            bandwidth = node.memory_bandwidth_gbps
            arch = getattr(node, "arch", "") or ""
            break
    if gpu is None:
        return None
    # The image THAT machine runs for THIS runtime; no probe of it is a miss.
    version = measurements.image_version(runtime, arch)
    if not version:
        return None
    try:
        found = measurements.matching_decode(
            model_id,
            gpu_name=gpu,
            memory_bandwidth_gbps=bandwidth,
            runtime_version=version,
            context_band=measurements.band(context_length),
        )
    except Exception:
        log.exception("reading decode measurements failed")
        return None
    if not found:
        return None
    best = found[0]
    return {
        "decode_tps": best.decode_tps,
        "context_band": best.context_band,
        "concurrency_band": best.concurrency_band,
        "requests": best.requests,
        "measured_at": best.measured_at,
        # What the gate said when this was taken, so a reader can see the
        # disagreement rather than having to recompute it against a prediction
        # that may since have moved.
        "predicted_tps": best.predicted_tps,
    }


def _speculative_detect():
    """`resolver.speculators`, imported lazily like every other heavy import here.

    Returns a module or None. None means external heads cannot be priced, which
    is a refusal on that path and nothing at all on every other one.
    """
    try:
        from control_plane.resolver import speculators

        return speculators
    except Exception:
        return None


def speculative_refusal(runtime: str, method: str) -> str | None:
    """Why this runtime cannot be told to speculate, or None.

    Lazily imported for exactly the reason ``_sharding_refusal`` above is, and
    it degrades the same way -- but NOT on the same terms in one respect worth
    stating: a missing ``control_plane.deploy`` here means no refusal, and a
    request that then launches decodes one token per step while the screen
    beside it says otherwise. That is only reachable on a gateway with no
    deployment package at all, which cannot launch anything, so there is no
    launch left to mislead.
    """
    try:
        from control_plane.deploy.flags import speculative_refusal as refusal
    except Exception:
        return None
    try:
        return refusal(runtime, method)
    except ValueError:
        return None


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


#: One process-wide logo cache, created on first use.
#:
#: Not on GatewayContext: it holds no cluster state, survives nothing, and a
#: provider's mark is the same file whichever coordinator asked for it. A
#: module global keeps it out of a frozen contract for something cosmetic.
_LOGOS = None


def _logo_cache():
    """The shared provider-logo cache.

    Imported here rather than at module scope so a build without ``httpx``
    reachable still imports the gateway: the logo path is the only thing that
    stops working, and it degrades to a 404, which already draws correctly.
    """
    global _LOGOS
    if _LOGOS is None:
        from control_plane.providers.logos import LogoCache

        _LOGOS = LogoCache()
    return _LOGOS


async def _warm_logo(cache, provider) -> None:
    """Fetch one provider's mark into the cache. Never raises, never awaited.

    Fire-and-forget behind an already-sent 404. The lock collapses a burst of
    renders into one request; the miss it records on failure is what stops a
    vendor without a favicon costing a fetch per repaint.
    """
    from control_plane.providers.logos import fetch_logo

    lock = cache.lock_for(provider.provider_id)
    if lock.locked():
        return
    async with lock:
        try:
            found = await fetch_logo(provider)
        except Exception:  # noqa: BLE001 - cosmetic path
            log.debug("logo warm failed for %s", provider.provider_id, exc_info=True)
            cache.remember_miss(provider.provider_id, permanent=False)
            return
        if found is None:
            cache.remember_miss(provider.provider_id, permanent=True)
            return
        body, content_type = found
        cache.put(provider.provider_id, body, content_type)


#: A node agent that cannot answer a read in this long is treated as down.
_AGENT_TIMEOUT_S = 5.0
#: SIGTERM grace (10s) plus SIGKILL wait (5s) on the agent side, plus slack.
_KILL_TIMEOUT_S = 25.0
#: Removing 182 GiB of blobs is a lot of unlink syscalls. Generous, because
#: the alternative is a timeout on a delete that is actually succeeding.
_DELETE_TIMEOUT_S = 120.0

#: A cache folder the delete route relays: exactly one `models--` segment (E2).
_CACHE_FOLDER = re.compile(r"models--[A-Za-z0-9._-]+")

#: The batch avatar route's per-session budget (R7).
AVATAR_OWNERS_PER_WINDOW = 240
AVATAR_WINDOW_S = 60.0

#: A deployment past these is not serving anything and its weights are fair
#: game. Taken from ``gateway.states``, which is the gateway's one spelling of
#: a judgement ``deploy.fsm`` owns -- importing the original would run the
#: deploy package __init__ and pull the manager, the sparkrun adapter and the
#: event bus into this request module to obtain one frozenset.
#: ``tests/unit/test_single_source.py`` holds the two equal.
_TERMINAL_STATES = states.TERMINAL

#: How many FAILED/STOPPED deployments GET /api/deployments carries. The live
#: ones are always all of them; see the route. Two hundred is more history
#: than a screen shows and small enough that the payload stays in the tens of
#: kilobytes where it belongs.
DEPLOYMENT_LIST_TERMINAL = 200

#: The states that mean "on its way but not yet serving" -- what /api/activity
#: reports. Deliberately an allowlist: every other state is a deployment that
#: has arrived, is leaving, or is over, and each of those already has a section
#: of the screen describing it.
_ARRIVING_STATES = frozenset({DeploymentState.PLANNED, DeploymentState.LAUNCHING})
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


#: How long to wait for a provider to report what it is about to download.
#: Long enough for a cold registry lookup on a slow link, short enough that a
#: silent upstream does not hold a browser request open.
PULL_GATE_TIMEOUT_S = 30.0

#: Strong references to in-flight pulls. asyncio holds only a weak reference to
#: a bare task, so without this the transfer can be collected mid-download and
#: the model simply never arrives.
_PULLS: set = set()


@dataclasses.dataclass
class _Download:
    """One transfer, while it is happening.

    The 202 this route returns says how big the download is and then the
    request ends, so until this existed the only trace of a running pull was an
    anonymous task in ``_PULLS`` -- nothing could be asked how far along it was,
    and the screen had a single number reported once followed by minutes of
    silence. ``ProviderService.pull``'s ``on_progress`` fills these in.

    Deliberately process-local and deliberately not persisted. The transfer
    itself does not survive a coordinator restart, so a record that did would
    outlive the thing it describes and report a download that is no longer
    running.
    """

    pull_id: str
    provider_id: str
    provider: str
    model: str
    #: None until the upstream reports one. Never 0: a download of unknown size
    #: and a download of no size are different answers, and only one of them
    #: can honestly draw a bar.
    total: int | None
    completed: int
    #: The upstream's own word for what it is doing, passed through unchanged.
    status: str
    started_at: float
    finished_at: float | None
    error: str | None
    #: Whether the upstream ever reported a `completed` figure. Without this a
    #: provider that streams only a total cannot be told apart from one whose
    #: transfer was cut off, and the two need opposite answers.
    saw_progress: bool = False


#: Live and just-finished transfers, keyed by pull id.
_DOWNLOADS: dict[str, _Download] = {}

#: How long a finished transfer stays listed. A download that vanished the
#: instant it completed would, on a fast local pull, never be seen at all --
#: the screen would flicker and show nothing, which reads exactly like the
#: silence this whole record exists to end. Long enough to notice, short enough
#: that the rail does not accumulate history it was never meant to hold.
_DONE_LINGER_S = 20.0

#: How much of the reported total must have arrived before a transfer that
#: ended without raising is believed to have finished. Not 1.0: reported sizes
#: jitter by a few bytes and an exact test would call a finished transfer
#: partial. The same fraction, for the same reason, as the UI's cache index.
_COMPLETE_FRACTION = 0.99

#: Monotonic source of pull ids. A counter rather than a random token because
#: the id is only ever used to address a record in this process's own dict, and
#: a predictable one is a readable test.
_PULL_SEQ = itertools.count(1)

#: When this coordinator first saw a deployment launching, by deployment id.
#: ``Deployment.started_at`` is None until the READY transition stamps it
#: (deploy/manager.py), so a launching deployment carries no timestamp at all
#: and there is nothing else to subtract. Adding a field to the record would be
#: a change to a frozen contract, which goes through the architecture doc
#: first. After a restart this is when the coordinator came back rather than
#: when the launch began, which is why the wire field is called `since` and the
#: screen does not call it "launched".
_LAUNCH_SEEN: dict[str, float] = {}


def _sweep_downloads(now: float) -> list[_Download]:
    """Live transfers plus those that finished within the linger window."""
    for pull_id, rec in list(_DOWNLOADS.items()):
        if rec.finished_at is not None and now - rec.finished_at > _DONE_LINGER_S:
            del _DOWNLOADS[pull_id]
    return sorted(_DOWNLOADS.values(), key=lambda r: r.started_at)


def _gib(value: int | float | None) -> str:
    """A byte count with its unit, from the one formatter that has the ladder.

    This used to be a bare divide, which meant a download that failed in its
    first few megabytes reported "stopped after 0.0 GiB of 4.2 GiB" -- the
    exact rendering ``fit/calculator``'s version was written to prevent, and
    the exact case where somebody is reading the sentence closely."""
    return binary_bytes(value or 0)


def _node_for_address(nodes, base_url: str):
    """The roster node a provider's URL points at, or None.

    An EXACT match on address or hostname, never a guess: no DNS resolution, no
    subnet inference, no reverse lookup. Not in the roster means somebody
    else's machine, which is a different thing from a machine we have simply
    failed to recognise, and only an exact match can tell them apart.

    This exists because a provider can be a machine that also joined the
    cluster -- the Pi enrols as a GPU-less node AND registers as an Ollama
    provider -- and in that case a model served "remotely" is running on a box
    already drawn on the cluster screen.
    """
    host = urlparse(base_url).hostname or ""
    if not host:
        return None
    for state in nodes:
        if state.profile.address == host or state.profile.hostname == host:
            return state
    return None


def _provider_host_memory(ctx, base_url: str) -> tuple[int, str, bool]:
    """(free bytes, node name, measured) for the machine a provider addresses.

    Matched by address against the roster, because a provider is just a URL and
    a machine only becomes measurable by having joined. (0, its host, False)
    when nothing matches -- that is "unknown", not "full", and the caller must
    not turn it into a refusal.

    ``measured`` is the third element because the first two cannot tell the two
    zeroes apart: a node that never joined and a node with nothing free both
    report 0, and a caller that infers "no gate ran" from a zero would say the
    pull went unjudged when in fact it was judged against a full disk. The
    common case is not exotic -- the kind's default base_url is
    ``http://localhost:11434/v1`` and loopback can never match a roster entry,
    so the honest answer has to be carried rather than reconstructed.

    ``memory_total`` and ``memory_used`` are the node's live figures. On a
    machine with no GPU they are host RAM, which is exactly the pool a model
    served from that box would occupy.
    """
    host = urlparse(base_url).hostname or ""
    if not host:
        return 0, "that machine", False
    try:
        nodes = ctx.deps.registry.list_nodes()
    except Exception:
        log.exception("registry unavailable while sizing a pull")
        return 0, host, False
    state = _node_for_address(nodes, base_url)
    if state is not None:
        free = max(0, (state.memory_total or 0) - (state.memory_used or 0))
        return free, state.profile.node_id, True
    return 0, host, False


def _not_implemented(operation: str, owner: str) -> JSONResponse:
    return errors.error_response(
        501,
        f"{operation} is not wired up yet: the {owner} does not expose it.",
        "server_error",
        "not_implemented",
    )


def _capacity_block(
    ctx: GatewayContext,
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


def _launch_options(payload: dict) -> dict:
    """The launch-only keys of a deployment request, read as
    `create_deployment` reads them, as keyword arguments for
    `preview_launch`. ValueError on a malformed one."""
    out: dict = {}
    served_name = payload.get("served_name")
    if served_name is not None:
        if not isinstance(served_name, str) or not served_name.strip():
            raise ValueError(
                "served_name must be the name clients will pass as \"model\", "
                "or be omitted to derive one from the model id."
            )
        out["served_name"] = served_name.strip()
    for field in ("extra_args", "custom_command"):
        raw = payload.get(field)
        if not raw:
            out[field] = ()
            continue
        if not isinstance(raw, str):
            raise ValueError("%s must be a string of space-separated CLI tokens." % field)
        try:
            out[field] = tuple(shlex.split(raw))
        except ValueError as exc:
            raise ValueError(
                "%s could not be parsed as a CLI argument string: %s" % (field, exc)
            ) from None
    for field in ("trust_remote_code", "enforce_eager"):
        value = payload.get(field, False)
        if value is None:
            value = False
        if not isinstance(value, bool):
            raise ValueError("%s must be true or false." % field)
        out[field] = value
    sizes = payload.get("cudagraph_capture_sizes")
    if sizes is not None:
        if not isinstance(sizes, list) or not sizes or not all(
            isinstance(n, int) and not isinstance(n, bool) for n in sizes
        ):
            raise ValueError("cudagraph_capture_sizes must be a non-empty list of integers.")
        out["cudagraph_capture_sizes"] = tuple(sizes)
    return out


def _runtime_image(runtime: str, arch: str) -> str | None:
    """The image *runtime* runs on a machine of *arch* (`flags.image_for_arch`),
    or None for a runtime the launcher has no spec for."""
    from control_plane.deploy.flags import RUNTIMES, image_for_arch

    spec = RUNTIMES.get(runtime)
    return image_for_arch(spec, arch) if spec is not None else None


async def _support_per_node(resolver, shape, runtime: str, nodes) -> dict[str, tuple[bool, str]]:
    """`supported_by` for each machine, asked of the image that machine runs.

    One call per distinct image, not per machine: every Spark runs the same
    one. Empty when the resolver cannot answer at all.
    """
    supported_by = getattr(resolver, "supported_by", None)
    if not callable(supported_by):
        return {}
    by_image: dict[str | None, tuple[bool, str]] = {}
    out: dict[str, tuple[bool, str]] = {}
    for node in nodes:
        image = _runtime_image(runtime, getattr(node, "arch", ""))
        if image not in by_image:
            by_image[image] = await asyncio.to_thread(
                supported_by, shape, runtime, image=image
            )
        out[node.node_id] = by_image[image]
    return out


def _silicon_per_node(
    runtime: str, nodes, *, dtype: str, quant_method: str,
    kv_cache_dtype: str | None, kv_label: str,
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """`support.silicon_check` for each machine against the image it runs:
    the refusals by node id, and every machine's warnings by node id."""
    from control_plane.resolver.support import silicon_check

    refused: dict[str, str] = {}
    notes: dict[str, list[str]] = {}
    for node in nodes:
        refusal, warnings = silicon_check(
            runtime,
            node,
            _runtime_image(runtime, getattr(node, "arch", "")),
            dtype=dtype,
            quant_method=quant_method,
            kv_cache_dtype=kv_cache_dtype,
            kv_label=kv_label,
        )
        if refusal is not None:
            refused[node.node_id] = refusal
        notes[node.node_id] = warnings
    return refused, notes


async def _plan_and_fit(ctx: GatewayContext, payload: dict):
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
    # Absent is not 8192. Absent means "choose one", and the choice is
    # made below, once the plan and the machines it lands on are known --
    # a context picked before the placement is a number, not a fit.
    raw_context = payload.get("context")
    context_length = int(raw_context) if raw_context else None
    # Absent is not 1, for the same reason absent is not 8192 above. Until
    # 2026-09-11 this line read `int(payload.get("concurrency") or 1)`, three
    # lines under a comment explaining why that is the wrong shape of answer --
    # so every deployment on this cluster launched `--max-num-seqs 1` and each
    # decode step's weight read produced one token. Resolved below, once the
    # plan and the machines it lands on are known.
    raw_concurrency = payload.get("concurrency")
    concurrency = int(raw_concurrency) if raw_concurrency else None
    target = payload.get("target") or "throughput"
    kv_dtype = payload.get("kv_dtype") or ctx.settings.default_kv_dtype
    dtype = payload.get("dtype")
    runtime = payload.get("runtime") or "vllm"
    requested_nodes = _parse_node_ids(payload)
    requested_degrees = _parse_degrees(payload)
    # Parsed here (shape only) and priced below, once the resolution that knows
    # what the draft weighs exists.
    requested_speculative = _parse_speculative(payload)
    speculative_tokens = check_speculative_tokens(payload.get("speculative_tokens", 0))
    if speculative_tokens > 0 and requested_speculative is not None:
        # Two spellings of one request: `speculative_tokens` is the checkpoint's
        # own MTP module (the older, narrower form), `speculative` is the
        # general one, which names `mtp` as a method among others.
        raise ValueError(
            "send either speculative_tokens or speculative, not both: "
            "speculative={method: 'mtp', ...} is the general form"
        )
    engine_options = _parse_engine_options(payload)

    # Prefer resolve_full when the port exposes it: it carries warnings
    # worth showing and, on mixed-precision repos, real measured weight
    # bytes that beat total_params * bytes_per_param (H-10).
    resolve_full = getattr(ctx.deps.resolver, "resolve_full", None)
    resolver_warnings: list[str] = []
    weight_bytes: int | None = None
    modality = Modality.TEXT
    remote_code = False
    if callable(resolve_full):
        resolution = await asyncio.to_thread(resolve_full, model_id, dtype)
        shape = resolution.shape
        resolver_warnings = list(resolution.warnings)
        remote_code = bool(getattr(resolution, "remote_code", False))
        weight_bytes = resolution.effective_weight_bytes()
        modality = _modality_of(resolution.architectures)
        if speculative_tokens > 0:
            mtp = int((resolution.param_breakdown or {}).get("mtp") or 0)
            if mtp <= 0:
                raise ValueError(
                    f"{model_id} carries no multi-token-prediction module; "
                    "speculative decoding has nothing to draft with."
                )
            # The resolver excluded the module from the charge because no
            # runtime loads it by default; speculative decoding does load it.
            total = shape.total_params
            if weight_bytes and total:
                weight_bytes = int(weight_bytes * (total + mtp) / total)
            resolver_warnings.append(
                f"speculative decoding loads the {mtp/1e9:.1f}B-parameter "
                "multi-token-prediction module; the fit above includes it"
            )
    else:
        shape = await asyncio.to_thread(ctx.deps.resolver.resolve, model_id, dtype)

    # Skipping the vision tower is sized BEFORE planning, so the planner, the
    # fit and the decode prediction all see the smaller shape.
    if engine_options.language_model_only:
        shape, weight_bytes = _without_vision(shape, weight_bytes)

    # The tts server's codec is its biggest allocation and is sized BEFORE
    # planning for the same reason. Only a resolution names the architecture;
    # one the table has no row for is one the tts runtime does not load, and
    # the support check below refuses it rather than this pricing a guess.
    if runtime == "tts" and callable(resolve_full):
        shape = _with_codec_working_set(shape, resolution.architectures)

    # A packed KV layout is a kernel for one attention geometry; asked of the
    # other one there is no page the gate could size honestly.
    from control_plane.fit.kv import kv_dtype_refusal

    kv_refusal = kv_dtype_refusal(shape, kv_dtype)
    if kv_refusal is not None:
        raise ValueError(kv_refusal)

    native_window = (
        getattr(resolution, "max_position_embeddings", None)
        if callable(resolve_full)
        else None
    )
    # The trained window, always: the UI offers YaRN against it.
    original_window: int | None = int(native_window) if native_window else None
    if engine_options.rope_factor is not None:
        if native_window is None:
            raise ValueError(
                "YaRN needs the model's trained window, and this config does "
                "not state max_position_embeddings."
            )
        native_window = int(native_window * engine_options.rope_factor)

    # What this checkpoint could be served with, and what the operator asked
    # for out of that set. Both need the resolution: the options are read off
    # it, and the price of the one chosen comes from it rather than from the
    # request. A port with no `resolve_full` offers nothing, which is honest --
    # it never told us what the model declares.
    speculative_options = [
        opt.as_dict() for opt in getattr(resolution, "speculators", ()) or ()
    ] if callable(resolve_full) else []
    speculative, head_option = (
        await asyncio.to_thread(
            _speculative_spec,
            resolution,
            requested_speculative,
            # A head is a repository, so pricing one costs a hub resolution --
            # hence the worker thread, and hence passing the resolver in rather
            # than reaching for it: this function is handed ports, not globals.
            resolve_head=resolve_full,
            image_speculators=_image_speculators(),
        )
        if callable(resolve_full)
        else (None, None)
    )
    # A named head joins the offered list, so the screen can name what it found
    # -- the class, the measured size, the target width it matches -- instead of
    # showing generic copy beside a head it has already priced.
    if head_option is not None:
        speculative_options = speculative_options + [head_option.as_dict()]
    if requested_speculative is not None and not callable(resolve_full):
        raise ValueError(
            "speculative decoding needs a resolver that reports what a "
            "checkpoint declares, and this one does not"
        )
    if speculative is not None:
        refusal = speculative_refusal(runtime, speculative.method.value)
        if refusal is not None:
            raise ValueError(refusal)

    try:
        nodes = [n.profile for n in ctx.deps.registry.healthy_nodes()]
    except Exception:
        log.exception("registry unavailable while planning")
        nodes = []
    healthy = list(nodes)

    # Narrow here, before the link lookup, so `worst_all_reduce` measures
    # only the path this deployment will actually cross. Filtering later
    # would leave `plan.measured_link_gbps` describing a link the plan
    # never uses.
    placement_warnings: list[str] = []
    if requested_nodes is not None:
        nodes = _select_nodes(
            ctx.deps.registry, nodes, requested_nodes, placement_warnings, runtime
        )
    else:
        # The planner's own pick: leave out every machine this runtime has no
        # image for, said once, rather than hand it a node the launch would
        # die on at `exec format error`.
        from control_plane.deploy.flags import arch_refusal

        runnable = [n for n in nodes if arch_refusal(runtime, n.node_id, getattr(n, "arch", "")) is None]
        if len(runnable) < len(nodes):
            skipped = sorted({n.node_id for n in nodes} - {n.node_id for n in runnable})
            placement_warnings.append(
                f"{runtime} has no image for the architecture of {', '.join(skipped)}, "
                f"so the planner left {'it' if len(skipped) == 1 else 'them'} out."
            )
        nodes = runnable
        # A host-pool runtime leaves out every machine `placement_refusal`
        # refuses -- a unified-memory machine, or one whose host memory nothing
        # has measured -- for the reason the arch filter above gives: handing
        # the planner a node the named-machine path would refuse with 400
        # plans a launch onto it. GPU runtimes keep their existing path, where
        # `drop_unbudgetable` already removes a machine with nothing to spend.
        from control_plane.deploy.flags import RUNTIMES as _RUNTIMES

        if getattr(_RUNTIMES.get(runtime), "memory_pool", "gpu") == "host":
            refused_here = {
                n.node_id: r
                for n in nodes
                if (r := _placement_refusal(ctx.deps.registry, runtime, n.node_id, n))
                is not None
            }
            if refused_here:
                placement_warnings.append(
                    f"{runtime} cannot be placed on "
                    f"{', '.join(sorted(refused_here))}, so the planner left "
                    f"{'it' if len(refused_here) == 1 else 'them'} out: "
                    f"{next(iter(refused_here.values()))}"
                )
                nodes = [n for n in nodes if n.node_id not in refused_here]

    # Whether the runtime loads this architecture, asked of the image EACH
    # machine runs rather than of the default one: the 3090's vLLM 0.31.0
    # registers architectures the Spark's 0.28.1 does not, and the reverse.
    # The same verdict `create_deployment` launches on (it rides the outcome),
    # so a dry run cannot say "fits, click Serve" about an architecture the
    # placed machines cannot load -- and cannot refuse one they can.
    runtime_ok, runtime_unsupported_reason = True, None
    runtime_supported_by = getattr(ctx.deps.resolver, "supported_by", None)
    if callable(runtime_supported_by) and not nodes:
        runtime_ok, runtime_unsupported_reason = await asyncio.to_thread(
            runtime_supported_by, shape, runtime
        )
    elif callable(runtime_supported_by):
        verdicts = await _support_per_node(ctx.deps.resolver, shape, runtime, nodes)
        unsupported = {nid: why for nid, (ok, why) in verdicts.items() if not ok}
        if unsupported and requested_nodes is None and len(unsupported) < len(nodes):
            placement_warnings.append(
                f"{runtime} cannot load this model on "
                f"{', '.join(sorted(unsupported))}, so the planner left "
                f"{'it' if len(unsupported) == 1 else 'them'} out: "
                f"{next(iter(unsupported.values()))}"
            )
            nodes = [n for n in nodes if n.node_id not in unsupported]
        elif unsupported:
            elsewhere: list[str] = []
            if requested_nodes is not None:
                from control_plane.deploy.flags import arch_refusal

                others = [
                    n for n in healthy
                    if n.node_id not in verdicts
                    and arch_refusal(runtime, n.node_id, getattr(n, "arch", "")) is None
                ]
                elsewhere = sorted(
                    nid
                    for nid, (ok, _) in (
                        await _support_per_node(ctx.deps.resolver, shape, runtime, others)
                    ).items()
                    if ok
                )
            if requested_nodes is not None and (elsewhere or len(unsupported) < len(nodes)):
                # A named machine whose image cannot load it while another
                # machine's can: the machine is what to change, so it is
                # named, with its own code -- not the "nothing here loads
                # this" verdict below, which no choice of machine unblocks.
                first = next(iter(unsupported))
                usable = elsewhere or sorted(set(verdicts) - set(unsupported))
                raise _PlacementRefused(
                    400,
                    f"On '{first}': {unsupported[first]}. "
                    f"{', '.join(usable)} run{'s' if len(usable) == 1 else ''} an "
                    f"image that loads it.",
                    "runtime_unsupported_on_machine",
                    param=_PLACEMENT_PARAM,
                    extra={"unusable_node_ids": sorted(unsupported)},
                )
            runtime_ok, runtime_unsupported_reason = False, next(iter(unsupported.values()))

    # The GPU in each machine, asked of the image it runs: vLLM's own floor
    # for the checkpoint's quantization method and the KV widths its attention
    # selector has a kernel for (`support.silicon_check`). A named machine
    # that cannot is a 400 naming it; the planner's own pick leaves it out --
    # unless no machine can, which is a 400 too, because nothing is left to
    # plan on and every machine got the same answer.
    if nodes:
        from control_plane.deploy.flags import render_kv_cache_dtype, render_quantization

        quant_method = (
            str(getattr(resolution, "quant_method", "") or "")
            if callable(resolve_full)
            else ""
        )
        kv_cache_dtype = None
        if runtime == "vllm":
            # A forced scheme is what the loader will be told; `quantization_refusal`
            # answers for one this build cannot render.
            try:
                quant_method = (render_quantization(dtype) if dtype else None) or quant_method
            except ValueError:
                pass
            # The probe asked the selector about ordinary attention only, so an
            # MLA model's cache is not judged: no evidence is no refusal.
            if not getattr(shape, "mla_latent_dim", 0):
                try:
                    kv_cache_dtype = render_kv_cache_dtype(kv_dtype)
                except ValueError:
                    kv_cache_dtype = None
        silicon_refused, silicon_notes = _silicon_per_node(
            runtime,
            nodes,
            dtype=shape.dtype,
            quant_method=quant_method,
            kv_cache_dtype=kv_cache_dtype,
            kv_label=kv_dtype,
        )
        if silicon_refused and (
            requested_nodes is not None or len(silicon_refused) == len(nodes)
        ):
            raise _PlacementRefused(
                400,
                next(iter(silicon_refused.values())),
                "unsupported_on_this_gpu",
                param=_PLACEMENT_PARAM if requested_nodes is not None else None,
                extra={"unusable_node_ids": sorted(silicon_refused)},
            )
        if silicon_refused:
            placement_warnings.append(
                f"{', '.join(sorted(silicon_refused))} cannot run this on "
                f"{'its' if len(silicon_refused) == 1 else 'their'} GPU, so the "
                f"planner left {'it' if len(silicon_refused) == 1 else 'them'} "
                f"out: {next(iter(silicon_refused.values()))}"
            )
            nodes = [n for n in nodes if n.node_id not in silicon_refused]
        for node in nodes:
            placement_warnings.extend(silicon_notes.get(node.node_id, ()))

    # The machines as this runtime's budget sees them. For a host-pool runtime
    # on a discrete box that is the box without its card (`flags.budget_profile`),
    # so the planner and the fit gate below never size a host-RAM launch
    # against VRAM. Applied after every placement check above, which judge the
    # real hardware.
    from control_plane.deploy.flags import budget_profile

    nodes = [budget_profile(runtime, n) for n in nodes]

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
            # Provisional when the caller named none: the real figure is
            # derived below, against the plan this call produces. The order
            # cannot be the other way round -- `concurrency_for` needs a plan
            # and a set of machines to ask its question of.
            concurrency or 1,
            context_length=context_length,
            kv_dtype=kv_dtype,
            allow_mixed_hardware=pool,
            # k+1 positions go through the model per step, not one, and every
            # byte of them crosses the wire. The fit gate has charged them
            # since speculation landed; the planner costed every plan as if a
            # single token crossed until 2026-09-11.
            speculative_window=(
                getattr(speculative, "num_speculative_tokens", 0) + 1
                if speculative is not None
                else 1
            ),
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
                    concurrency or 1,
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
    # The recipe clears /dev/infiniband for every host when any one of them
    # lacks it (`recipes.synthesize`), so a multi-node plan across such a
    # machine runs NCCL over TCP. Same predicate as the recipe's, said here.
    from control_plane.deploy.recipes import tcp_fabric_note

    if len(plan.node_ids) > 1 and (
        fabric_note := tcp_fabric_note(
            [n for n in nodes if n.node_id in set(plan.node_ids)]
        )
    ):
        placement_warnings.append(fabric_note)

    # The live budget. Two checks, not one widened verdict: the breakdown
    # is identical under both budgets and only the budget-dependent terms
    # differ, so two internally consistent FitResults beat one object whose
    # `reason` would have to describe two budgets in one sentence.
    #
    # Computed before the FitRequest, not after, because a context nobody
    # named is derived against this budget: choosing it off the static
    # ceiling would offer a window the machine cannot actually hold right
    # now, which is the whole reason the live budget exists.
    #
    # In the runtime's own pool: host RAM for llamacpp, so a discrete box is
    # budgeted from MemAvailable rather than from its card.
    from control_plane.deploy.flags import RUNTIMES as _RUNTIMES

    budgets, excluded, live_reason = livefit.allocatable_map(
        ctx.deps.registry,
        [n.node_id for n in plan_nodes],
        pool=getattr(_RUNTIMES.get(runtime), "memory_pool", "gpu"),
    )
    budgets, zero_excluded = livefit.drop_unbudgetable(plan_nodes, budgets)
    excluded = excluded + zero_excluded
    if not budgets and live_reason is None:
        live_reason = "every node was excluded from the live memory budget"

    # Derived here, and in this order, for the reason `context_length` is:
    # the question is "how many sequences will this plan hold on these
    # machines right now", and neither the plan nor the live budget existed
    # further up. Derived AFTER the plan rather than before it on purpose --
    # asking the planner for a node count at a concurrency nobody has agreed
    # to yet would demand machines to serve a batch that was never requested,
    # and could refuse a launch that fits. This way the batch is whatever the
    # chosen plan has room for.
    if concurrency is None:
        concurrency = await asyncio.to_thread(
            partial(
                concurrency_for,
                ctx.deps.fit,
                shape,
                plan,
                plan_nodes,
                kv_dtype=kv_dtype,
                # A latency-targeted request is asking for the single-stream
                # regime, and the planner already has a constant that says
                # where that stops. Deriving 16 for it would hand back a plan
                # chosen for batch 1.
                ceiling=(
                    LATENCY_CONCURRENCY_CEILING
                    if target == "latency"
                    else MAX_DERIVED_CONCURRENCY
                ),
                weight_bytes=weight_bytes,
                allocatable=budgets or None,
            )
        )

    if context_length is None:
        context_length = await asyncio.to_thread(
            partial(
                context_for,
                ctx.deps.fit,
                shape,
                plan,
                plan_nodes,
                max_seqs=concurrency,
                kv_dtype=kv_dtype,
                weight_bytes=weight_bytes,
                allocatable=budgets or None,
                native_window=native_window,
                speculative=speculative,
            )
        )

    req = FitRequest(
        shape=shape,
        context_length=context_length,
        max_concurrent_seqs=concurrency,
        kv_dtype=kv_dtype,
        plan=plan,
        weight_bytes=weight_bytes,
        native_window=native_window,
        speculative=speculative,
        chunk_tokens=engine_options.max_num_batched_tokens,
        extra_replicated_bytes=(
            int(engine_options.mm_processor_cache_gb * 2**30)
            if engine_options.mm_processor_cache_gb
            else 0
        ),
    )

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
    # An unsupported architecture is a refusal no tick can unblock -- it
    # overrides whatever the fit gate decided, never merges with it. `fit`
    # can still pass and the model still never load.
    if not runtime_ok:
        serve = {
            **serve,
            "allowed": False,
            "reason": runtime_unsupported_reason,
            "override_required": False,
            "override_param": None,
            "overrides": [],
        }
    capacity = _capacity_block(ctx, plan_nodes, budgets, excluded, fit, fit_live)

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

    # Decorated here and not where the options were built: a measurement is a
    # fact about particular hardware, and which machines this plan lands on is
    # not known until the planner has placed it. `head_option` is already in
    # the list by now, so a named external head is cited on the same terms.
    speculative_options = _attach_measurements(
        speculative_options, shape.model_id, plan_nodes
    )

    return _PlanOutcome(
        shape=shape,
        plan=plan,
        measured_decode=_measured_decode(
            shape.model_id, plan_nodes, context_length, runtime
        ),
        fit=fit,
        fit_live=fit_live,
        capacity=capacity,
        serve=serve,
        context_length=context_length,
        concurrency=concurrency,
        resolver_warnings=resolver_warnings,
        modality=modality,
        remote_code=remote_code,
        placement=placement,
        degrees=degrees,
        recommended=recommended,
        alternatives=[serialize.plan_degrees_payload(p) for p in ranked],
        speculative_options=speculative_options,
        speculative=speculative,
        kv_dtype=kv_dtype,
        quantization=dtype,
        revision=(getattr(resolution, "revision", None) if callable(resolve_full) else None),
        engine_options=engine_options,
        original_window=original_window,
        native_window=native_window,
        runtime_supported=runtime_ok,
        runtime_unsupported_reason=runtime_unsupported_reason,
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

    def _coordinator_build() -> str:
        """The build this coordinator is running.

        Read from the process rather than from the registry's own member row,
        so it is right even before the first health round has recorded
        anything, and right on a registry that tracks no builds at all.
        """
        return build_id()

    def _links_for(node_ids: list[str]):
        """Every pair, measured or not. Never emit a bandwidth figure that was
        not measured: an unmeasured pair carries measured=false and no numbers.

        Also carries the NCCL tuning for the pair, and whether a probe is in
        flight. The second is what `LinkService.measuring()` has existed for
        since it was written -- `links/README.md`: "so the screen can say a
        measurement is running" -- and nothing has ever read it. That was
        survivable while a probe took thirty seconds. A calibration is one
        two-rank collective per candidate setting, so it is minutes, and an
        action that long with no sign of life reads as a button that did
        nothing.
        """
        try:
            inflight = {tuple(sorted(p)) for p in ctx.deps.links.measuring()}
        except Exception:
            # A port without the method is a port that cannot say, which is
            # not the same as "nothing is running" -- but it is the only
            # answer available, and refusing to serve the link list over it
            # would be worse.
            inflight = set()
        image = os.environ.get("DERATE_VLLM_IMAGE") or _default_vllm_image()
        out = []
        for i, a in enumerate(node_ids):
            for b in node_ids[i + 1 :]:
                try:
                    link = ctx.deps.links.get(a, b)
                except Exception:
                    log.exception("link lookup failed for %s/%s", a, b)
                    link = None
                # Same for every pair, measured or not: a link nobody has
                # probed can still have been calibrated, and one that is being
                # worked on right now has to say so.
                extra = {
                    "measuring": tuple(sorted((a, b))) in inflight,
                    "tuning": serialize.link_tuning(a, b, image=image),
                }
                if link is None:
                    out.append(
                        {"src": a, "dst": b, "measured": False, "stale": False, **extra}
                    )
                    continue
                payload = serialize.link_payload(link)
                payload["measured"] = True
                payload["stale"] = (time.time() - link.measured_at) > STALE_LINK_AGE_S
                payload["medium"] = _medium(link.method)
                payload.update(extra)
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
                # The build every node row's `build_skew` was compared with,
                # so a screen can say what the node differs FROM. Null when
                # this process cannot establish its own build.
                "build": _coordinator_build() or None,
                # Every feature an agent of this build reports when healthy;
                # a node row whose `features` lacks one is missing it.
                "agent_features": list(AGENT_FEATURES),
                "nodes": [
                    serialize.node_payload(
                        n, labels.get(n.profile.node_id), _coordinator_build()
                    )
                    for n in nodes
                ],
                "links": _links_for(node_ids),
                "summary": {
                    "node_count": len(nodes),
                    "healthy_nodes": sum(1 for n in nodes if n.healthy),
                    "total_memory": sum(n.profile.total_memory for n in nodes),
                    "total_power_w": round(
                        sum(serialize.power_reading(n) or 0.0 for n in nodes), 1
                    ),
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
                    # Carried so a plate can name its own utilisation line
                    # without a second fetch: 0 means the figure below came
                    # from /proc/stat and is CPU, not GPU. The layout is built
                    # from this payload, so a card can exist before
                    # /api/cluster has answered.
                    "gpu_count": node.profile.gpu_count,
                    "state": "healthy" if node.healthy else "unhealthy",
                    "role": "coordinator"
                    if node_id == settings.coordinator_node_id
                    else "worker",
                    # From serialize, not recomputed: this row used
                    # profile.total_memory alone and dropped the host-total
                    # fallback, so a GPU-less node read as unmeasured here and
                    # as a real percentage on /api/nodes, at the same instant.
                    "memory_used_pct": serialize.memory_used_pct(node),
                    # Same rule as /api/nodes, from the same place: a machine
                    # with no GPU has no GPU power draw, and 0 W here would
                    # read as an idle one.
                    "power_w": serialize.power_reading(node),
                    "temp_c": serialize.temp_reading(node),
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

        # Models served by a provider rather than by a deployment.
        #
        # `index.remotes` has been populated since the router was written -- one
        # entry per model of every enabled provider, created on add and needing
        # no traffic -- and until now nothing read it. So a model routed to a
        # provider reached the cluster screen as a substring inside the provider
        # rail's sublabel and as nothing else: no band, no tap, no mention on
        # any machine.
        #
        # `node_id` is the fact that makes this worth reporting. A provider can
        # BE a machine on the roster, and when it is, a model running there is
        # running on a box already on screen. Null is the ordinary case -- a
        # provider nobody here hosts -- and must stay distinguishable from a
        # machine we failed to match, which is why the match is exact.
        remote_payloads = []
        for target_id, (provider, model) in index.remotes.items():
            host_node = _node_for_address(nodes, provider.base_url)
            st = ctx.stats.peek(target_id)
            target = next(
                (
                    t
                    for targets in index.targets.values()
                    for t in targets
                    if t.target_id == target_id
                ),
                None,
            )
            remote_payloads.append(
                {
                    "target_id": target_id,
                    "provider_id": provider.provider_id,
                    "served_name": model.served_name,
                    "upstream_id": model.upstream_id,
                    "node_id": host_node.profile.node_id if host_node else None,
                    # Which endpoint family this row answers on. A local band
                    # has carried it since audio landed; without it here, a
                    # provider's tts-1 was drawn hanging off the chat endpoint
                    # -- a picture of routing that does not exist.
                    "modality": model.modality.value,
                    # From the RouteTarget, not from Provider.healthy, so this
                    # and /api/routing cannot disagree about one fact.
                    "state": "healthy"
                    if (target.healthy if target is not None else provider.healthy)
                    else "unhealthy",
                    "admitting": target.admitting if target is not None else None,
                    # Same call and same window as a deployment two blocks up,
                    # so a remote row and a local row cannot report throughput
                    # two different ways.
                    "tokens_per_sec": round(st.tokens_per_sec(window), 1) if st else 0.0,
                }
            )
        remote_payloads.sort(key=lambda r: (r["served_name"], r["target_id"]))

        return JSONResponse(
            {
                "cluster_id": settings.cluster_id,
                "coordinator": settings.coordinator_node_id,
                "nodes": node_payloads,
                "edges": _links_for([n.profile.node_id for n in nodes]),
                "deployments": deployment_payloads,
                "remotes": remote_payloads,
            }
        )

    # -- nodes -------------------------------------------------------------

    @router.get("/api/nodes")
    async def list_nodes() -> JSONResponse:
        labels = _labels()
        return JSONResponse(
            [
                serialize.node_payload(
                    n, labels.get(n.profile.node_id), _coordinator_build()
                )
                for n in _nodes()
            ]
        )

    @router.get("/api/nodes/candidates")
    async def node_candidates() -> JSONResponse:
        # Discovery proposes, a human accepts. The registry owns the candidate set.
        candidates = getattr(ctx.deps.registry, "candidates", None)
        if not callable(candidates):
            return JSONResponse([])
        try:
            return JSONResponse([serialize.candidate_payload(c) for c in candidates()])
        except Exception:
            log.exception("candidate listing failed")
            return JSONResponse([])

    def _agent_token(node_id: str) -> str | None:
        """What *node_id*'s agent expects on its gated routes, or None.

        The per-node agent token (``Registry.agent_token``), never the cluster
        token: each agent is shown only its own credential.
        """
        token_fn = getattr(ctx.deps.registry, "agent_token", None)
        if not callable(token_fn):
            return None
        try:
            return token_fn(node_id) or None
        except Exception:
            log.exception("agent token unavailable")
            return None

    def _agent_headers(node_id: str) -> dict[str, str] | None:
        token = _agent_token(node_id)
        return {"X-Derate-Token": token} if token else None

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
        token = _agent_token(node_id)
        if not token:
            # The agent gates its process list on its agent token (the
            # command lines are the whole GPU's), derived from the cluster
            # token, so without one there is nothing to ask.
            return None, errors.error_response(
                503,
                "No cluster token is available, so the node agent cannot be "
                "asked for its resident processes.",
                "server_error",
                "cluster_token_unavailable",
            )
        import httpx

        try:
            async with httpx.AsyncClient(timeout=_AGENT_TIMEOUT_S) as client:
                res = await client.get(
                    f"{agent_url.rstrip('/')}/agent/processes",
                    headers={"X-Derate-Token": token},
                )
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

    @router.get("/api/nodes/{node_id}/logs")
    async def node_logs(node_id: str, which: str = "node", tail: int = 500) -> Response:
        """This node's own ``node.log``/``proxy.log`` -- the control plane's
        process log, written by ``logfiles.py``.

        Distinct from ``/api/deployments/{id}/logs`` (a served model's own
        stdout) and ``/api/history/logs`` (the structured, queryable
        archive): this is the plain-text file an operator would otherwise
        need a shell on the machine to read.
        """
        if which not in ("node", "proxy"):
            return errors.error_response(
                400,
                "which must be 'node' or 'proxy'.",
                "invalid_request_error",
                "invalid_which",
            )
        agent_url = None
        lookup = getattr(ctx.deps.registry, "agent_url", None)
        if callable(lookup):
            try:
                agent_url = lookup(node_id)
            except Exception:
                log.exception("agent url lookup failed")
        if not agent_url:
            return errors.error_response(
                404,
                f"No agent URL for node '{node_id}'; its log files cannot be read.",
                "invalid_request_error",
                "node_agent_unreachable",
            )
        import httpx

        limit = max(1, min(int(tail), 5000))
        try:
            async with httpx.AsyncClient(timeout=_AGENT_TIMEOUT_S) as client:
                res = await client.get(
                    f"{agent_url.rstrip('/')}/agent/logs",
                    params={"which": which, "tail": limit},
                )
                res.raise_for_status()
                payload = res.json()
        except Exception as exc:
            log.warning("log read failed for %s: %s", node_id, exc)
            return errors.error_response(
                502,
                f"The node agent on '{node_id}' did not answer: "
                f"{errors.detail(exc, _redactor())}",
                "server_error",
                "node_agent_unreachable",
            )
        return JSONResponse(payload)

    @router.delete("/api/nodes/{node_id}/processes/{pid}")
    async def kill_node_process(node_id: str, pid: int, request: Request) -> Response:
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

        token = _agent_token(node_id)
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
        audit_request(
            request, AuditAction.NODE_PROCESS_KILLED, "node", node_id,
            after={
                "node_id": node_id,
                "pid": pid,
                "command": target.get("command") or target.get("name") or "",
            },
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
        return JSONResponse(
            serialize.node_payload(node, _labels().get(node_id), _coordinator_build())
        )

    # client address -> monotonic times of its joins in the last minute (N-8).
    join_window: dict[str, collections.deque[float]] = {}

    def _join_over_limit(ip: str) -> bool:
        """Record one join from *ip*; True when it is over the window."""
        now = time.monotonic()
        cutoff = now - 60.0
        for key in [k for k, q in join_window.items() if not q or q[-1] < cutoff]:
            del join_window[key]
        recent = join_window.setdefault(ip, collections.deque())
        while recent and recent[0] < cutoff:
            recent.popleft()
        if len(recent) >= JOIN_PER_ADDRESS_PER_MIN:
            return True
        recent.append(now)
        return False

    @router.post("/api/nodes/join")
    async def join_node(request: Request) -> Response:
        handle_join = getattr(ctx.deps.registry, "handle_join", None)
        if not callable(handle_join):
            return _not_implemented("Node join", "registry")
        ip = client_ip(request) or "-"
        if _join_over_limit(ip):
            ctx.secevents.note(
                AuditAction.JOIN_REJECTED,
                actor="-",
                ip=ip,
                user_agent=request.headers.get("user-agent", "")[:512],
                request_id=getattr(request.state, "request_id", None),
                target_kind="node",
                target_id="",
                detail={"reason": "rate"},
            )
            return errors.error_response(
                429, "Too many join attempts from this address.",
                "rate_limit_error", "rate_limited",
                headers={"Retry-After": "60"},
            )
        try:
            payload = await read_json_object(request)
        except BodyTooLarge:
            return payload_too_large()
        except Exception:
            return errors.error_response(
                400, "Join body must be JSON.", "invalid_request_error", "invalid_json"
            )
        try:
            raw_proof = payload.get("proof")
            proof = None if raw_proof is None else joinproof.from_dict(raw_proof)
            profile = profile_from_dict(payload["profile"])
            # Validated before the registry dials it: this route is open, and
            # an unchecked address makes the coordinator GET whatever the
            # caller names (UnusableTarget is a ValueError).
            agent_url = reach.validate_target(str(payload["agent_url"]))
        except (KeyError, TypeError, ValueError) as exc:
            return errors.error_response(
                400, f"Join body malformed: {str(exc).rstrip('.')}.",
                "invalid_request_error", "invalid_join_body",
            )
        facts: dict = {}
        try:
            requester = _requesting_host(request, agent_url)
            result = await handle_join(
                proof, profile, agent_url, peer=requester, facts=facts
            )
        except JoinRejected as exc:
            ctx.secevents.note(
                AuditAction.JOIN_REJECTED,
                actor=profile.node_id or "-",
                ip=client_ip(request),
                user_agent=request.headers.get("user-agent", "")[:512],
                request_id=getattr(request.state, "request_id", None),
                target_kind="node",
                target_id=profile.node_id,
                detail={"agent_url": agent_url, "reason": str(exc)},
            )
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
        if not facts.get("unchanged"):
            await _audit_join(request, profile.node_id, agent_url, proof, result, facts)
        return JSONResponse(serialize.plain(result))

    def _requesting_host(request: Request, agent_url: str) -> str:
        """The host a join may register: the address that sent it.

        Raises JoinRejected otherwise. ``agent_url`` is where the coordinator
        will send prompts, kills and the agent token from now on, so it must
        name the machine asking -- never a third host, and never the
        coordinator's own loopback or a metadata address. A loopback peer is
        a worker on this very machine (single-box dev): it dials 127.0.0.1 yet
        names its LAN address, so any address this machine holds is accepted.
        """
        peer = client_ip(request) or ""
        host = urlparse(agent_url).hostname or ""
        if is_loopback(peer):
            if not registry_net.is_local_address(host):
                raise JoinRejected("agent_url must name the requesting address")
            return host
        if not registry_net.is_peer_address(host):
            raise JoinRejected("agent_url is not a peer address")
        if not registry_net.same_host(host, peer):
            raise JoinRejected("agent_url must name the requesting address")
        return host

    async def _audit_join(
        request: Request,
        node_id: str,
        agent_url: str,
        proof,
        result: dict,
        facts: dict,
    ) -> None:
        """``node_joined``: who joined, from where, on which credential.

        The route is open, so the gate writes no ``api_write`` row for it;
        without this a membership change -- or a member's traffic being
        re-pointed -- left nothing in the record. Routine re-polls that change
        nothing (``facts["unchanged"]``) are not written.
        """
        previous_url = facts.get("previous_agent_url")
        try:
            auth = stores_of(request).auth
            await asyncio.to_thread(
                auth.audit,
                node_id,
                AuditAction.NODE_JOINED,
                "node",
                node_id,
                before={"agent_url": previous_url} if previous_url else None,
                after={
                    "status": result.get("status") if isinstance(result, dict) else None,
                    "agent_url": agent_url,
                    "address": facts.get("address"),
                    "source": facts.get(
                        "source", "cluster_token" if proof is not None else "none"
                    ),
                    "enrollment_token_id": facts.get("enrollment_token_id"),
                },
                **request_context(request),
            )
        except Exception:
            log.exception("could not write the node_joined audit row for %s", node_id)

    @router.post("/api/nodes/{node_id}/admit")
    async def admit_node(node_id: str, request: Request) -> Response:
        admit = getattr(ctx.deps.registry, "admit", None)
        if not callable(admit):
            return _not_implemented("Node admission", "registry")
        # What the click admits, read before the candidate record is popped
        # (N-15): the name alone does not say which machine got in.
        record: dict = {}
        candidates = getattr(ctx.deps.registry, "candidates", None)
        if callable(candidates):
            try:
                record = next(
                    (c for c in candidates() if isinstance(c, dict) and c.get("node_id") == node_id),
                    {},
                )
            except Exception:
                log.debug("could not read the candidate record for %s", node_id, exc_info=True)
        try:
            admitted = serialize.plain(admit(node_id))
            audit_request(
                request, AuditAction.NODE_ADMITTED, "node", node_id,
                after={
                    "node_id": node_id,
                    **{
                        k: record.get(k)
                        for k in ("agent_url", "address", "source", "first_seen")
                        if record
                    },
                },
            )
            return JSONResponse(admitted)
        except NodeNotFound:
            return errors.error_response(
                404, f"No candidate '{node_id}'.",
                "invalid_request_error", "node_not_found",
            )

    @router.post("/api/cluster/token/rotate")
    async def rotate_cluster_token(request: Request) -> Response:
        """Replace the cluster token (CT-1). Asks for ``current_password``.

        Refused with 409 while any member would still present the old token
        on its next join. The new token is never in a response or in the
        audit row: members do not need it, and new nodes join on enrollment
        tokens minted in the UI.
        """
        from control_plane.registry import MembersNotMigrated

        from .auth_api import json_object, reauthenticated

        rotate = getattr(ctx.deps.registry, "rotate_cluster_token", None)
        if not callable(rotate):
            return _not_implemented("Rotating the cluster token", "registry")
        body = await json_object(request)
        if isinstance(body, Response):
            return body
        refused = await reauthenticated(request, stores_of(request), body)
        if refused is not None:
            return refused
        try:
            members = rotate()
        except MembersNotMigrated as exc:
            return errors.error_response(
                409,
                "These members have not re-joined since the upgrade: "
                f"{', '.join(exc.node_ids)}. Wait for them or remove them first.",
                "invalid_request_error", "members_not_migrated",
            )
        audit_request(
            request, AuditAction.CLUSTER_TOKEN_ROTATED, "cluster", "cluster",
            after={"members": members},
        )
        return JSONResponse({"rotated": True})

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
            payload = await read_json_object(request)
        except BodyTooLarge:
            return payload_too_large()
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
    async def remove_node(node_id: str, request: Request) -> Response:
        remove = getattr(ctx.deps.registry, "remove_node", None)
        if not callable(remove):
            return _not_implemented("Node removal", "registry")
        try:
            remove(node_id)
        except NodeNotFound:
            return errors.error_response(
                404, f"No node '{node_id}'.", "invalid_request_error", "node_not_found"
            )
        audit_request(
            request, AuditAction.NODE_REMOVED, "node", node_id,
            before={"node_id": node_id},
        )
        return JSONResponse({"removed": node_id})

    # -- links -------------------------------------------------------------

    @router.get("/api/links")
    async def list_links() -> JSONResponse:
        return JSONResponse(_links_for([n.profile.node_id for n in _nodes()]))

    @router.post("/api/links/tune")
    async def tune_link(request: Request) -> Response:
        """Start a calibration for a pair. Returns before it finishes.

        Unlike `/api/links/measure`, which blocks for the thirty seconds a
        probe takes, this cannot: a calibration runs one two-rank collective
        per candidate setting and takes minutes. Holding a request open that
        long would tie up a worker, time out in any proxy in front of this, and
        tell the caller nothing while it waited.

        So it returns `202` with `measuring: true` and the work continues on a
        thread. The caller watches the `measuring` field on `/api/links`, which
        the cluster screen already polls -- which also means the state survives
        a page reload and is the same for two people looking at once, neither
        of which a local spinner manages.
        """
        try:
            payload = await request.json()
            a, b = payload["a"], payload["b"]
        except Exception:
            return errors.error_response(
                400, "Body must be {\"a\": node_id, \"b\": node_id}.",
                "invalid_request_error", "invalid_request",
            )
        if a == b:
            return errors.error_response(
                400, "A link needs two distinct nodes.",
                "invalid_request_error", "invalid_request",
            )
        calibrate = getattr(ctx.deps.links, "calibrate", None)
        if not callable(calibrate):
            # A link port without calibration is a legal port -- the day-0 stub
            # is one. Say so rather than 500ing on a missing attribute.
            return errors.error_response(
                501, "This link service cannot calibrate.",
                "invalid_request_error", "not_supported",
            )

        image = os.environ.get("DERATE_VLLM_IMAGE") or _default_vllm_image()

        def _run() -> None:
            try:
                calibrate(a, b, image=image)
            except Exception:
                log.exception("calibration of %s/%s failed", a, b)

        threading.Thread(
            target=_run, name=f"derate-calibrate-{a}-{b}", daemon=True
        ).start()
        return JSONResponse({"a": a, "b": b, "measuring": True}, status_code=202)

    @router.post("/api/links/measure")
    async def measure_link(request: Request) -> Response:
        try:
            payload = await read_json_object(request)
            a, b = payload["a"], payload["b"]
        except BodyTooLarge:
            return payload_too_large()
        except Exception:
            return errors.error_response(
                400, "Body must be {\"a\": node_id, \"b\": node_id}.",
                "invalid_request_error", "invalid_request",
            )
        # Only a member is measured: an id the registry does not know would
        # otherwise be dialled verbatim as an ssh host, and "-oProxyCommand=..."
        # is an ssh option.
        try:
            known = all(
                isinstance(n, str) and ctx.deps.registry.get_node(n) is not None for n in (a, b)
            )
        except Exception:
            log.exception("registry unavailable")
            known = False
        if not known:
            return errors.error_response(
                404, f"Unknown node: {a!r} / {b!r}.", "invalid_request_error", "unknown_node",
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
            payload = await read_json_object(request)
            a, b = payload["a"], payload["b"]
        except BodyTooLarge:
            return payload_too_large()
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

    @router.get("/api/links/health")
    async def link_health(a: str | None = None, b: str | None = None) -> Response:
        """What to change on each fabric port, per pair of healthy nodes.

        Asks every healthy node's agent for its RDMA ports
        (`GET /agent/links/health`) and compares the two ends of each pair
        (`links/nichealth.py`). `?a=&b=` narrows it to one pair, which is what
        the cluster rail asks for. Read-only and seconds: a handful of sysfs
        reads and `ethtool` calls per node, then one end of each pair dials the
        other's copy receiver once from each rail toward each fabric address
        (`nichealth.probe_plan`, bounded and timed out) -- a TCP connect that is
        closed as soon as it opens -- so a rail that is up and carries nothing
        is a warning. The coordinator dials from itself; any other end is asked
        through its agent (`POST /agent/links/probe`).
        """
        from control_plane.links import nichealth

        try:
            healthy = [n.profile.node_id for n in ctx.deps.registry.healthy_nodes()]
        except Exception:
            log.exception("registry unavailable")
            healthy = []
        if (a is None) != (b is None) or (a is not None and a == b):
            return errors.error_response(
                400, "Name both ends of one link: ?a=<node_id>&b=<node_id>.",
                "invalid_request_error", "invalid_request",
            )
        wanted = sorted(healthy) if a is None else [n for n in (a, b) if n in healthy]
        pairs = (
            [(x, y) for i, x in enumerate(wanted) for y in wanted[i + 1 :]]
            if a is None
            else ([(a, b)] if len(wanted) == 2 else [])
        )
        involved = sorted({n for p in pairs for n in p})
        health: dict[str, dict] = {}
        unreadable: dict[str, str] = {}
        if a is not None:
            for n in (a, b):
                if n not in healthy:
                    unreadable[n] = f"'{n}' is not a healthy member of this cluster."

        import httpx

        async def _read(client, node_id: str) -> None:
            try:
                agent_url = ctx.deps.registry.agent_url(node_id)
            except Exception:
                agent_url = None
            headers = _agent_headers(node_id)
            if not agent_url:
                unreadable[node_id] = f"No agent address is known for '{node_id}'."
                return
            if headers is None:
                unreadable[node_id] = f"No agent credential is held for '{node_id}'."
                return
            try:
                res = await client.get(
                    f"{agent_url.rstrip('/')}/agent/links/health", headers=headers
                )
            except Exception as exc:
                unreadable[node_id] = (
                    f"The node agent on '{node_id}' did not answer: "
                    f"{errors.detail(exc, _redactor())}"
                )
                return
            if res.status_code == 404:
                unreadable[node_id] = (
                    f"The node agent on '{node_id}' has no fabric settings route, so it "
                    "runs a build from before they were read. Upgrade that node."
                )
                return
            try:
                res.raise_for_status()
                payload = res.json()
            except Exception as exc:
                unreadable[node_id] = (
                    f"The node agent on '{node_id}' did not answer: "
                    f"{errors.detail(exc, _redactor())}"
                )
                return
            if isinstance(payload, dict):
                health[node_id] = payload

        async with httpx.AsyncClient(timeout=_AGENT_TIMEOUT_S) as client:
            await asyncio.gather(*(_read(client, n) for n in involved))

            coordinator = getattr(ctx.settings, "coordinator_node_id", None)
            reach: dict[tuple[str, str], nichealth.RailReach] = {}

            async def _probe(client, a_id: str, b_id: str) -> None:
                # The coordinator's own end probes from this process; it is
                # on the host network and its agent would only be a detour.
                prober, other = (b_id, a_id) if b_id == coordinator else (a_id, b_id)
                local = nichealth.rails_from(health[prober])
                targets = nichealth.probe_plan(local, nichealth.rails_from(health[other]))
                if not targets:
                    return
                if prober == coordinator:
                    results = await asyncio.to_thread(nichealth.probe_rails, targets, local)
                    reach[(a_id, b_id)] = nichealth.RailReach(prober, other, tuple(results))
                    return
                agent_url = ctx.deps.registry.agent_url(prober)
                headers = _agent_headers(prober)
                try:
                    res = await client.post(
                        f"{agent_url.rstrip('/')}/agent/links/probe",
                        headers=headers,
                        json={"targets": [t.to_json() for t in targets]},
                    )
                    if res.status_code == 404:
                        reach[(a_id, b_id)] = nichealth.RailReach(
                            prober, other,
                            unavailable=(
                                f"The node agent on '{prober}' has no rail probe route, so "
                                f"whether each rail carries traffic to '{other}' was not "
                                "checked. Upgrade that node."
                            ),
                        )
                        return
                    res.raise_for_status()
                    rows = res.json().get("results") or []
                except Exception as exc:
                    reach[(a_id, b_id)] = nichealth.RailReach(
                        prober, other,
                        unavailable=(
                            f"The node agent on '{prober}' could not probe its rails: "
                            f"{errors.detail(exc, _redactor())}"
                        ),
                    )
                    return
                reach[(a_id, b_id)] = nichealth.RailReach(
                    prober, other,
                    tuple(nichealth.ProbeResult.from_json(r) for r in rows if isinstance(r, dict)),
                )

            await asyncio.gather(
                *(_probe(client, x, y) for x, y in pairs if x in health and y in health)
            )
        return JSONResponse(
            nichealth.link_health_report(pairs, health, unreadable, time.time(), reach)
        )

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
            payload = await read_json_object(request)
            policy = RoutingPolicy(payload["policy"])
        except BodyTooLarge:
            return payload_too_large()
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
        limits = payload.get("limits") if isinstance(payload, dict) else None
        if limits is not None and not isinstance(limits, dict):
            return errors.error_response(
                400, "limits must be an object with rpm and/or tpm.",
                "invalid_request_error", "invalid_request",
            )
        shares = payload.get("shares") if isinstance(payload, dict) else None
        if shares is not None and not isinstance(shares, dict):
            return errors.error_response(
                400, "shares must be an object of target id to a whole number.",
                "invalid_request_error", "invalid_request",
            )
        previous = ctx.router.config_for(served_name)
        try:
            config = ctx.router.set_policy(served_name, policy, limits=limits, shares=shares)
        except KeyError:
            return errors.error_response(
                404, f"No model '{served_name}' is being served.",
                "invalid_request_error", "model_not_found",
            )
        except ValueError as exc:
            return errors.error_response(
                400, str(exc), "invalid_request_error", "invalid_request",
            )
        before = {"policy": _policy_name(previous)}
        after = {"policy": _policy_name(config), "override": True}
        if limits is not None:
            before["limits"] = dict(getattr(previous, "limits", {}) or {})
            after["limits"] = dict(config.limits)
        if shares is not None:
            before["shares"] = dict(getattr(previous, "shares", {}) or {})
            after["shares"] = dict(config.shares)
        audit_request(
            request, AuditAction.ROUTING_CHANGED, "routing", served_name,
            before=before, after=after,
        )
        index = ctx.router.index()
        return JSONResponse(_routing_payload(index, config))

    @router.delete("/api/routing/{served_name}")
    async def clear_routing(served_name: str, request: Request) -> Response:
        """Drop the explicit override and go back to what auto_policy picks.

        The PUT is not its own inverse. Re-PUTting the policy a model currently
        resolves to registers an override where there was none, which flips
        ``auto_selected`` false and freezes the model on a policy the auto
        ladder would otherwise have moved it off -- so anything that pins a
        policy for the duration of a run and then puts it back needs this. It
        is idempotent: a model that is already auto is answered, not mutated.
        """
        config = ctx.router.config_for(served_name)
        if config is None:
            return errors.error_response(
                404, f"No model '{served_name}' is being served.",
                "invalid_request_error", "model_not_found",
            )
        if not ctx.router.auto_selected(served_name):
            previous = config
            ctx.router.clear_policy(served_name)
            # Re-read: clearing the override changes the resolved policy, and
            # the reply has to be the state the caller is left with.
            config = ctx.router.config_for(served_name) or config
            audit_request(
                request, AuditAction.ROUTING_CHANGED, "routing", served_name,
                before={"policy": _policy_name(previous), "override": True},
                after={"policy": _policy_name(config), "override": False},
            )
        index = ctx.router.index()
        return JSONResponse(_routing_payload(index, config))

    def _policy_name(config) -> str | None:
        policy = getattr(config, "policy", None)
        return getattr(policy, "value", policy)

    # -- providers ---------------------------------------------------------

    def _servable(port) -> list:
        """Providers carrying only the models they are allowed to serve.

        Duck-typed on ``servable`` the way this module reaches every optional
        port operation. A port without it -- the day-0 stub, a test double --
        answers with its whole catalogue, which is the behaviour that predates
        the allowlist and the only honest thing such a port can say.
        """
        return getattr(port, "servable", port.list)()

    def _served_view(port, provider):
        """One provider as the rest of the product sees it.

        The single-provider replies (add, patch, refresh) are the same payload
        as a row of the listing and have to agree with it. Serializing the
        record the service handed back instead would put the entire catalogue
        in the body beside a `model_count` of 0 -- a reply contradicting itself,
        and 312 models on the wire the moment a provider is added.
        """
        try:
            for candidate in _servable(port):
                if candidate.provider_id == provider.provider_id:
                    return candidate
        except Exception:
            log.exception("provider listing failed")
        return provider

    def _key_state(provider_id: str) -> dict | None:
        """Whether this provider's credential resolves, from where, and what
        the last key check found.

        Optional like every other port method reached through ``getattr`` in
        this module: a store that never grew ``key_status`` or ``key_health``
        leaves those fields null, and the listing they ride on still answers.
        Null is not "no key" -- the UI says so in as many words, because a
        Settings screen that reports a missing key on a working provider is
        worse than one that admits it does not know.

        Nothing here is key material and nothing here could be: the result is
        a state word, the name of a place, a check verdict and an expiry time,
        never a value.
        """
        status = getattr(ctx.deps.providers, "key_status", None)
        health = getattr(ctx.deps.providers, "key_health", None)
        if not callable(status) and not callable(health):
            return None
        result: dict = {}
        if callable(status):
            try:
                result.update(status(provider_id))
            except Exception:
                log.exception("provider key status failed")
        if callable(health):
            try:
                result.update(health(provider_id))
            except Exception:
                log.exception("provider key health failed")
        return result

    def _sign_in_account(provider_id: str) -> str | None:
        """The account a sign-in provider is signed in as. Optional like ``_key_state``."""
        account = getattr(ctx.deps.providers, "sign_in_account", None)
        if not callable(account):
            return None
        try:
            return account(provider_id)
        except Exception:
            log.exception("provider sign-in account lookup failed")
            return None

    def _backend_pins(provider_id: str) -> dict:
        """``{upstream_id: (tag, fallback)}``. Optional like ``_key_state``: a
        port without ``backend_pin_state`` reports no pins."""
        state = getattr(ctx.deps.providers, "backend_pin_state", None)
        if not callable(state):
            return {}
        try:
            return state(provider_id)
        except Exception:
            log.exception("provider backend pin lookup failed")
            return {}

    @router.get("/api/providers/kinds")
    async def provider_kinds() -> JSONResponse:
        """What each kind needs before anyone configures one.

        ``kinds_public()`` has said it carries "what the UI needs to render an
        add-provider form with sane defaults" since it was written, and nothing
        called it -- so the form hardcoded its own list and offered neither the
        defaults nor the constraints. Two consequences worth naming, because
        they are why this route exists:

        Ollama's default base_url is ``http://localhost:11434/v1``. Left blank
        in the form, that resolves on the *coordinator*, not on the machine the
        operator had in mind, and fails as a connection timeout with nothing to
        explain it. Showing the default is what makes it editable.

        And a kind can be unsupported with a reason (Anthropic's Messages API
        is not OpenAI-compatible here). Offering it in a select that cannot
        explain itself turns a documented limitation into a failed POST.

        Static: no key material, nothing per-cluster, so it is cacheable.
        """
        from control_plane.providers.serialization import kinds_public

        return JSONResponse(
            kinds_public(), headers={"cache-control": "public, max-age=300"}
        )

    @router.get("/api/providers/secret-refs")
    async def provider_secret_refs() -> JSONResponse:
        """The reference *names* already present in secrets.json.

        Names, never values -- ``SecretStore.refs()`` reads the keys of the
        file and nothing else, and a name is the one part of a secret that is
        safe to display (it is already rendered in every provider listing).

        The file only. Enumerating the process environment would list the
        names of every credential the coordinator was started with, which is
        not this route's business even though names are not values.

        This is what makes naming a reference a choice from what exists rather
        than typing into a blind box and finding out on POST.
        """
        secrets = getattr(ctx.deps.providers, "secrets", None)
        refs = getattr(secrets, "refs", None)
        if not callable(refs):
            return JSONResponse({"refs": []})
        try:
            names = [str(r) for r in refs()]
        except Exception:
            # An unreadable secrets file is already handled -- and logged --
            # inside the store, which treats it as empty rather than failing.
            # A form that cannot autocomplete is not a reason to 500.
            log.exception("secret reference listing failed")
            names = []
        return JSONResponse({"refs": names})

    @router.get("/api/providers")
    async def list_providers() -> JSONResponse:
        try:
            # The servable view, not the catalogue: every screen that reads
            # this one endpoint -- the provider table, the models list, the
            # inspector's "served elsewhere", the pull picker, spend -- shows
            # only what the operator switched on, without any of them filtering.
            # `/api/providers/{id}/models` is the one place the rest is offered.
            providers = _servable(ctx.deps.providers)
        except Exception:
            log.exception("provider listing failed")
            providers = []
        # Computed once per request, then sliced per provider below -- never
        # once per provider, which would repeat the underlying public_list()
        # call for no benefit.
        spend = ui_detail.provider_spend(ctx.deps.providers)
        return JSONResponse(
            [
                serialize.provider_payload(
                    p, spend.get(p.provider_id), _key_state(p.provider_id),
                    sign_in_account=_sign_in_account(p.provider_id),
                    pins=_backend_pins(p.provider_id),
                )
                for p in providers
            ]
        )

    @router.post("/api/providers")
    async def add_provider(request: Request) -> Response:
        try:
            spec = await read_json_object(request)
        except BodyTooLarge:
            return payload_too_large()
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
        audit_request(
            request, AuditAction.PROVIDER_CREATED, "provider", provider.provider_id,
            after=_provider_audit(provider),
        )
        spend = ui_detail.provider_spend(ctx.deps.providers)
        return JSONResponse(
            serialize.provider_payload(
                _served_view(ctx.deps.providers, provider),
                spend.get(provider.provider_id),
                _key_state(provider.provider_id),
                sign_in_account=_sign_in_account(provider.provider_id),
                pins=_backend_pins(provider.provider_id),
            ),
            status_code=201,
        )

    @router.patch("/api/providers/{provider_id}")
    async def patch_provider(provider_id: str, request: Request) -> Response:
        update = getattr(ctx.deps.providers, "update", None)
        if not callable(update):
            return _not_implemented("Provider update", "provider store")
        try:
            patch = await read_json_object(request)
        except BodyTooLarge:
            return payload_too_large()
        except Exception:
            return errors.error_response(
                400, "Body must be JSON.", "invalid_request_error", "invalid_json"
            )
        before = _provider_audit_by_id(provider_id)
        try:
            provider = update(provider_id, patch)
        except UnknownProviderError:
            return errors.error_response(
                404, f"No provider '{provider_id}'.",
                "invalid_request_error", "provider_not_found",
            )
        except Exception as exc:
            # POST has caught this since it was written; PATCH did not, so the
            # same rejected key came back as a framework 500 with the sentence
            # buried in a traceback instead of a 400 carrying it.
            log.exception("provider update failed")
            return errors.error_response(
                400, f"Could not update provider. {errors.detail(exc, _redactor())}",
                "invalid_request_error", "provider_update_failed",
            )
        if "api_key" in patch or "api_key_ref" in patch:
            # Say whether the new key works in the same answer, rather than
            # leaving the row on "not checked yet" until somebody presses.
            check_key_async = getattr(ctx.deps.providers, "check_key_async", None)
            if callable(check_key_async):
                try:
                    provider = await check_key_async(provider_id)
                except Exception:
                    log.exception("key check after key change failed")
        ctx.router.rebuild(force_scores=True)
        audit_request(
            request, AuditAction.PROVIDER_UPDATED, "provider", provider_id,
            before=before, after=_provider_audit(provider),
        )
        spend = ui_detail.provider_spend(ctx.deps.providers)
        return JSONResponse(
            serialize.provider_payload(
                _served_view(ctx.deps.providers, provider),
                spend.get(provider.provider_id),
                _key_state(provider.provider_id),
                sign_in_account=_sign_in_account(provider.provider_id),
                pins=_backend_pins(provider.provider_id),
            )
        )

    @router.post("/api/providers/{provider_id}/pull")
    async def pull_provider_model(provider_id: str, request: Request) -> Response:
        """Fetch weights onto a provider that hosts its own.

        The gate is the point. A pull is minutes of transfer onto a machine
        that may be a Raspberry Pi with a gigabyte free, and the only moment
        anything can judge it is the download total in the response's first
        frames -- before that there is no size, and after it the SD card is
        already filling. ``ProviderService.pull`` surfaces exactly that moment
        through ``on_size``, and raising from the callback aborts the transfer.

        Refusing needs a number to refuse against, and derate does not own this
        machine. When the provider's address matches a node in the roster, its
        measured free memory is that number. When it does not -- a box that
        never joined -- there is nothing to measure and the pull proceeds
        unjudged, which is the same rule the fit gate follows for a node with
        no live reading: absence degrades, it never refuses.

        Returns 202 once the size is known and accepted. The transfer continues
        in the background and the model appears when the catalogue refresh
        picks it up; holding the request open for the whole download would time
        out in every proxy between here and the browser.

        That background task lives in this process, so restarting the
        coordinator cancels an in-flight pull and leaves the provider holding a
        partial blob. It is a property of where the task runs rather than of
        this route, it is not currently resumable, and the card says so on the
        screen rather than letting a long download disappear unexplained.
        """
        from control_plane.providers.config import PULL_HEADROOM
        from control_plane.providers.errors import (
            PullRefusedError,
            PullUnsupportedError,
        )

        try:
            body = await read_json_object(request)
        except BodyTooLarge:
            return payload_too_large()
        except Exception:
            return errors.error_response(
                400, "Body must be JSON.", "invalid_request_error", "invalid_json"
            )
        model = str(body.get("model") or "").strip()
        if not model:
            return errors.error_response(
                400,
                "Name the model to pull, as the provider names it.",
                "invalid_request_error",
                "model_required",
            )
        override = bool(body.get("allow_over_memory"))

        pull = getattr(ctx.deps.providers, "pull", None)
        if not callable(pull):
            return _not_implemented("Pulling weights", "provider store")
        try:
            provider = ctx.deps.providers.get(provider_id)
        except UnknownProviderError:
            return errors.error_response(
                404, f"No provider '{provider_id}'.",
                "invalid_request_error", "provider_not_found",
            )

        free, node_id, measured = _provider_host_memory(ctx, provider.base_url)
        budget = int(free * PULL_HEADROOM) if free else 0

        loop = asyncio.get_running_loop()
        gated: asyncio.Future = loop.create_future()

        pull_id = f"pull-{next(_PULL_SEQ)}"
        record = _Download(
            pull_id=pull_id,
            provider_id=provider_id,
            provider=provider.display_name,
            model=model,
            total=None,
            completed=0,
            status="",
            started_at=time.time(),
            finished_at=None,
            error=None,
        )

        def on_progress(frame: dict) -> None:
            """Fold one upstream frame into the record.

            Every field is guarded because these are somebody else's JSON: a
            frame that reports `completed` as a string, or a total that shrinks,
            must leave the last good numbers alone rather than putting a bar
            that jumps backwards on the screen.
            """
            status = frame.get("status")
            if isinstance(status, str) and status:
                record.status = status[:200]
            total = frame.get("total")
            if isinstance(total, (int, float)) and total > 0:
                record.total = int(total)
            completed = frame.get("completed")
            if isinstance(completed, (int, float)) and completed >= 0:
                record.completed = int(completed)
                record.saw_progress = True

        def on_size(total: int) -> None:
            if budget and total > budget and not override:
                exc = PullRefusedError(
                    provider_id, model, total, free,
                    f"{model} is {_gib(total)} to download and {node_id} has "
                    f"{_gib(free)} free, of which a model may use "
                    f"{_gib(budget)} -- the server process, its KV cache and "
                    f"the operating system need the rest. Pick a smaller model, "
                    f"free memory on {node_id}, or send allow_over_memory to "
                    f"pull it anyway.",
                )
                if not gated.done():
                    gated.set_exception(exc)
                raise exc
            record.total = total
            # Listed from the moment the gate passes, which is the moment a
            # transfer actually begins. Earlier would list a pull that is about
            # to be refused; later would leave the first seconds of a long
            # download -- the ones somebody is watching for -- unaccounted for.
            # A pull whose weights are already upstream never reaches here, and
            # correctly never appears: nothing is being downloaded.
            _DOWNLOADS[pull_id] = record
            if not gated.done():
                gated.set_result(total)

        async def runner():
            try:
                result = await pull(
                    provider_id, model, on_size=on_size, on_progress=on_progress,
                )
            except Exception as exc:
                if not gated.done():
                    gated.set_exception(exc)
                raise
            # Not an `else:` clause. It used to be one, and a `return` in the
            # `try` above meant it never ran: a pull of a model the provider
            # already had reported no size, nothing ever resolved the gate, and
            # the request sat until PULL_GATE_TIMEOUT_S and answered 504 "did
            # not report a download size" about a model that was already there.
            # The `state: "present"` reply this line exists to produce was
            # unreachable from the day it was written.
            if not gated.done():
                # Completed without ever reporting a total: already present
                # upstream, so there was nothing to download.
                gated.set_result(0)
            return result

        def _finished(t: asyncio.Task) -> None:
            """Retire the task, and always retrieve its exception.

            Not optional bookkeeping: a task whose exception is never read has
            asyncio log "Task exception was never retrieved" at ERROR when it
            is collected, at an unpredictable later moment. The refusal has
            already been reported to the caller through `gated` by then, so
            that record is a duplicate arriving with no request attached to it
            -- and it lands in the journal, which is how it first showed up.
            """
            _PULLS.discard(t)
            record.finished_at = time.time()
            if t.cancelled():
                # The coordinator is going down, or the gate refused. Either
                # way the transfer stopped partway and saying so is the whole
                # point -- this is the case that cost somebody four minutes of
                # debugging their own code before they found the SIGTERM.
                record.error = "cancelled before it finished"
                return
            failure = t.exception()
            if failure is not None:
                record.error = errors.detail(failure, _redactor())
                log.warning(
                    "pull of %r onto %s did not complete: %s",
                    model, provider_id, failure,
                )
                return
            # A truncated stream ends CLEANLY. When the upstream dies partway
            # the frame loop simply runs out of lines and this task returns
            # normally, with no exception to report -- so "it did not raise"
            # is not evidence that the weights arrived. Taking it as evidence
            # filled the bar to 4.00/4.00 GiB for a transfer observed stopping
            # at 0.30 GiB, which is precisely the invented figure this surface
            # exists to avoid printing.
            if (
                record.saw_progress
                and record.total
                and record.completed < record.total * _COMPLETE_FRACTION
            ):
                record.error = (
                    f"stopped after {_gib(record.completed)} of "
                    f"{_gib(record.total)} -- the provider closed the "
                    f"connection before the transfer finished. Nothing "
                    f"resumes it; pull it again."
                )
                return
            # Only now: the last frame can be a few bytes short of the total
            # without the transfer being short, and a bar that stops at 99.98%
            # forever reads as a hang.
            if record.total is not None:
                record.completed = record.total
            # Only a completed pull changes what is routable.
            ctx.router.rebuild(force_scores=True)

        task = loop.create_task(runner())
        _PULLS.add(task)
        task.add_done_callback(_finished)

        try:
            total = await asyncio.wait_for(asyncio.shield(gated), timeout=PULL_GATE_TIMEOUT_S)
        except PullRefusedError as exc:
            return errors.error_response(
                409, str(exc), "invalid_request_error", "pull_over_memory",
            )
        except PullUnsupportedError as exc:
            return errors.error_response(
                400, str(exc), "invalid_request_error", "pull_unsupported",
            )
        except asyncio.TimeoutError:
            task.cancel()
            return errors.error_response(
                504,
                f"{provider.display_name} did not report a download size within "
                f"{PULL_GATE_TIMEOUT_S:.0f}s. Nothing was pulled.",
                "server_error", "pull_no_size",
            )
        except Exception as exc:
            log.exception("provider pull failed")
            return errors.error_response(
                502, f"Could not pull. {errors.detail(exc, _redactor())}",
                "server_error", "pull_failed",
            )

        return JSONResponse(
            {
                "provider_id": provider_id,
                "model": model,
                # Addresses this transfer's record in /api/activity for as long
                # as it runs. Absent when nothing is being downloaded, because
                # there is no transfer to name.
                "pull_id": pull_id if total else None,
                "download_bytes": total,
                "checked_against": node_id,
                "free_bytes": free,
                "budget_bytes": budget,
                # Whether a size was actually weighed against a measurement,
                # which `budget_bytes: 0` alone cannot say -- an unjoined box
                # and a full one produce the same zero. Without this the UI
                # can only report a number, and reporting "0 GiB free" for a
                # machine nobody measured is the kind of invented headroom
                # this project refuses to print.
                "gated": bool(measured and budget),
                "state": "pulling" if total else "present",
            },
            status_code=202,
        )

    @router.delete("/api/providers/{provider_id}")
    async def delete_provider(provider_id: str, request: Request) -> Response:
        remove = getattr(ctx.deps.providers, "remove", None)
        if not callable(remove):
            return _not_implemented("Provider removal", "provider store")
        # Try remote revocation first where the provider supports it. Local
        # removal still succeeds if the grant may outlive this connection.
        revoked = None
        before = _provider_audit_by_id(provider_id)
        sign_out = getattr(ctx.deps.providers, "sign_out_async", None)
        if callable(sign_out):
            revoked = await sign_out(provider_id)
        try:
            remove(provider_id)
        except UnknownProviderError:
            return errors.error_response(
                404, f"No provider '{provider_id}'.",
                "invalid_request_error", "provider_not_found",
            )
        ctx.router.rebuild(force_scores=True)
        audit_request(
            request, AuditAction.PROVIDER_DELETED, "provider", provider_id,
            before=before, after={"revoked": revoked},
        )
        return JSONResponse({"removed": provider_id, "revoked": revoked})

    # attempt_id -> (admin who started it, when): the redirect that finishes
    # an attempt carries no session, so its audit row names this admin.
    sign_in_started_by: dict[str, tuple[str, float]] = {}

    def _note_sign_in_start(attempt_id: str, request: Request) -> None:
        now = time.time()
        for stale in [a for a, (_, t) in sign_in_started_by.items() if now - t > SIGN_IN_ATTEMPT_TTL_S]:
            del sign_in_started_by[stale]
        session = request.scope.get("state", {}).get("session")
        sign_in_started_by[attempt_id] = (session.username if session is not None else "-", now)

    async def _audit_signed_in(
        attempt_id: str, provider_id: str, rebound: bool, request: Request | None
    ) -> None:
        """The ``provider_signed_in`` row: the redirect created or rebound a
        provider credential, so it is recorded like any other provider write."""
        started = sign_in_started_by.pop(attempt_id, None)
        actor = started[0] if started is not None else "-"
        context = (
            request_context(request) if request is not None
            else {"ip": None, "request_id": None, "user_agent": None, "session_id": None}
        )
        if request is not None:
            request.scope.setdefault("state", {})["audited"] = True
        provider = _provider_audit_by_id(provider_id)
        kind = provider.get("kind") if provider is not None else None
        try:
            await asyncio.to_thread(
                ctx.auth_stores.auth.audit,
                actor, AuditAction.PROVIDER_SIGNED_IN, "provider", provider_id,
                after={"kind": kind, "rebound": rebound, "attempt_id": attempt_id},
                **context,
            )
        except AuthUnavailable as exc:
            log.error("Provider sign-in of %s not audited: %s", provider_id, exc.message)

    async def _finish_redirect(query: str, request: Request | None = None) -> tuple[int, str]:
        """Finish the attempt a redirect's query belongs to: (status, page).

        Shared by ``GET /auth/callback`` and the loopback listener, so the
        browser sees the same page whichever one the redirect reached."""
        complete = ctx.deps.providers.complete_chatgpt_callback
        before = {p.provider_id for p in ctx.deps.providers.list()}
        try:
            attempt_id, provider = await complete(query)
        except (SignInError, UpstreamError) as exc:
            log.warning("ChatGPT sign-in redirect refused: %s", getattr(exc, "code", exc))
            redactor = _redactor()
            message = getattr(exc, "message", str(exc))
            message = redactor.scrub(message) if redactor is not None else message
            return 400, _sign_in_page("Sign-in did not finish", message)
        await _audit_signed_in(attempt_id, provider.provider_id, provider.provider_id in before, request)
        ctx.router.rebuild(force_scores=True)
        account = _sign_in_account(provider.provider_id) or "your ChatGPT account"
        return 200, _sign_in_page(
            "Signed in to ChatGPT",
            f"derate is using the ChatGPT plan of {account}. "
            "Close this tab and go back to derate.",
        )

    redirect = urlsplit(CHATGPT_REDIRECT_URI)
    loopback = LoopbackCallback(
        redirect.hostname or "127.0.0.1", redirect.port or 1455, redirect.path, _finish_redirect
    )

    @router.post("/api/sign-in/chatgpt")
    async def start_chatgpt_sign_in(request: Request) -> Response:
        """Begin Sign in with ChatGPT: the address to open in a browser.

        ``{"provider_id": ...}`` signs an existing provider in again; an empty
        body starts a new one. The coordinator then listens on the redirect
        address itself (``loopback.py``) until the attempt expires, so a
        browser whose port 1455 reaches this machine -- on it, tunnelled, or
        port-forwarded by an editor -- finishes by itself. Anywhere else the
        operator pastes where the browser lands into the ``complete`` route.
        """
        start = getattr(ctx.deps.providers, "start_chatgpt_sign_in", None)
        if not callable(start):
            return _not_implemented("Sign in with ChatGPT", "provider store")
        try:
            body = await optional_json_object(request)
        except BodyTooLarge:
            return payload_too_large()
        except Exception:
            body = None
        if not isinstance(body, dict) or not isinstance(body.get("provider_id", ""), (str, type(None))):
            return errors.error_response(
                400, "Body must be a JSON object, with an optional string provider_id.",
                "invalid_request_error", "invalid_json",
            )
        provider_id = body.get("provider_id") or None
        try:
            started = start(provider_id)
        except UnknownProviderError:
            return errors.error_response(
                404, f"No provider '{provider_id}'.",
                "invalid_request_error", "provider_not_found",
            )
        except ValueError as exc:
            return errors.error_response(
                400, f"Could not start sign-in. {errors.detail(exc, _redactor())}",
                "invalid_request_error", "sign_in_failed",
            )
        if callable(getattr(ctx.deps.providers, "complete_chatgpt_callback", None)):
            # Best effort: a port somebody else holds is logged, and the paste
            # path still finishes the sign-in.
            await loopback.hold(SIGN_IN_ATTEMPT_TTL_S)
        if isinstance(started, dict) and isinstance(started.get("attempt_id"), str):
            _note_sign_in_start(started["attempt_id"], request)
        return JSONResponse(started)

    @router.get("/api/sign-in/chatgpt/{attempt_id}")
    async def chatgpt_sign_in_status(attempt_id: str) -> Response:
        """Where a sign-in stands: ``pending``, ``done`` (with ``provider_id``) or
        ``failed`` (with ``code`` and ``message``). The UI polls this so a
        redirect that reached ``GET /auth/callback`` finishes its screen too."""
        status = getattr(ctx.deps.providers, "chatgpt_sign_in_status", None)
        if not callable(status):
            return _not_implemented("Sign in with ChatGPT", "provider store")
        try:
            return JSONResponse(status(attempt_id))
        except SignInError as exc:
            return errors.error_response(404, exc.message, "invalid_request_error", exc.code)

    @router.get("/auth/callback")
    async def chatgpt_redirect_landing(request: Request) -> Response:
        """Where OpenAI's redirect lands when it can reach the coordinator.

        The redirect is ``http://127.0.0.1:1455/auth/callback`` and OpenAI
        allows nothing but a loopback address, so this is reached from a
        browser on the coordinator or one tunnelled to this port
        (``ssh -L 1455:127.0.0.1:<port>``); the coordinator's own listener on
        1455 (``loopback.py``) answers the same way. Open by path -- it is not under /api -- because the browser
        following a redirect carries no session for this origin; the ``state``
        in the query, which only the attempt's own authorize URL ever carried,
        is what authorises it. Everybody else pastes the address into the UI,
        which ends at the same code.
        """
        if not callable(getattr(ctx.deps.providers, "complete_chatgpt_callback", None)):
            return _not_implemented("Sign in with ChatGPT", "provider store")
        status, page = await _finish_redirect(request.url.query, request)
        return HTMLResponse(page, status_code=status)

    @router.post("/api/sign-in/chatgpt/{attempt_id}/complete")
    async def complete_chatgpt_sign_in(attempt_id: str, request: Request) -> Response:
        """Finish a sign-in from the pasted callback address.

        201 with the new provider, or 200 with the provider signed in again.
        A refusal is 400 with a code per cause and a sentence saying what to do.
        """
        complete = getattr(ctx.deps.providers, "complete_chatgpt_sign_in", None)
        if not callable(complete):
            return _not_implemented("Sign in with ChatGPT", "provider store")
        try:
            body = await read_json_object(request)
        except BodyTooLarge:
            return payload_too_large()
        except Exception:
            body = None
        callback_url = body.get("callback_url") if isinstance(body, dict) else None
        if not isinstance(callback_url, str):
            return errors.error_response(
                400, "Body must be JSON with a string callback_url.",
                "invalid_request_error", "invalid_json",
            )
        before = {p.provider_id for p in ctx.deps.providers.list()}
        try:
            provider = await complete(attempt_id, callback_url)
        except SignInError as exc:
            redactor = _redactor()
            message = redactor.scrub(exc.message) if redactor is not None else exc.message
            return errors.error_response(400, message, "invalid_request_error", exc.code)
        except UpstreamError as exc:
            log.warning("ChatGPT sign-in failed: %s", exc)
            return errors.error_response(
                502, f"Could not finish sign-in. {errors.detail(exc, _redactor())}",
                "server_error", "sign_in_failed",
            )
        await _audit_signed_in(attempt_id, provider.provider_id, provider.provider_id in before, request)
        ctx.router.rebuild(force_scores=True)
        spend = ui_detail.provider_spend(ctx.deps.providers)
        return JSONResponse(
            serialize.provider_payload(
                _served_view(ctx.deps.providers, provider),
                spend.get(provider.provider_id),
                _key_state(provider.provider_id),
                sign_in_account=_sign_in_account(provider.provider_id),
                pins=_backend_pins(provider.provider_id),
            ),
            status_code=200 if provider.provider_id in before else 201,
        )

    @router.post("/api/sign-in/claude")
    async def start_claude_sign_in(request: Request) -> Response:
        """Begin an unofficial Claude subscription connection or reconnect."""
        start = getattr(ctx.deps.providers, "start_claude_sign_in", None)
        if not callable(start):
            return _not_implemented("Sign in with Claude", "provider store")
        try:
            body = await optional_json_object(request)
        except BodyTooLarge:
            return payload_too_large()
        except Exception:
            body = None
        if not isinstance(body, dict) or not isinstance(body.get("provider_id", ""), (str, type(None))):
            return errors.error_response(
                400, "Body must be a JSON object, with an optional string provider_id.",
                "invalid_request_error", "invalid_json",
            )
        provider_id = body.get("provider_id") or None
        try:
            started = start(provider_id)
        except UnknownProviderError:
            return errors.error_response(
                404, f"No provider '{provider_id}'.",
                "invalid_request_error", "provider_not_found",
            )
        except ValueError as exc:
            return errors.error_response(
                400, f"Could not start sign-in. {errors.detail(exc, _redactor())}",
                "invalid_request_error", "sign_in_failed",
            )
        if isinstance(started, dict) and isinstance(started.get("attempt_id"), str):
            _note_sign_in_start(started["attempt_id"], request)
        return JSONResponse(started)

    @router.get("/api/sign-in/claude/{attempt_id}")
    async def claude_sign_in_status(attempt_id: str) -> Response:
        """Return pending/done/failed without exposing authorization material."""
        status = getattr(ctx.deps.providers, "claude_sign_in_status", None)
        if not callable(status):
            return _not_implemented("Sign in with Claude", "provider store")
        try:
            return JSONResponse(status(attempt_id))
        except SignInError as exc:
            redactor = _redactor()
            message = redactor.scrub(exc.message) if redactor is not None else exc.message
            return errors.error_response(404, message, "invalid_request_error", exc.code)

    @router.post("/api/sign-in/claude/{attempt_id}/complete")
    async def complete_claude_sign_in(attempt_id: str, request: Request) -> Response:
        """Finish pasted Claude authorization: 201 for new, 200 for reconnect."""
        complete = getattr(ctx.deps.providers, "complete_claude_sign_in", None)
        if not callable(complete):
            return _not_implemented("Sign in with Claude", "provider store")
        try:
            body = await read_json_object(request)
        except BodyTooLarge:
            return payload_too_large()
        except Exception:
            body = None
        callback_url = body.get("callback_url") if isinstance(body, dict) else None
        if not isinstance(callback_url, str):
            return errors.error_response(
                400, "Body must be JSON with a string callback_url.",
                "invalid_request_error", "invalid_json",
            )
        before = {p.provider_id for p in ctx.deps.providers.list()}
        try:
            provider = await complete(attempt_id, callback_url)
        except SignInError as exc:
            redactor = _redactor()
            message = redactor.scrub(exc.message) if redactor is not None else exc.message
            return errors.error_response(400, message, "invalid_request_error", exc.code)
        except UpstreamError as exc:
            log.warning("Claude sign-in failed: %s", exc)
            return errors.error_response(
                502, f"Could not finish sign-in. {errors.detail(exc, _redactor())}",
                "server_error", "sign_in_failed",
            )
        await _audit_signed_in(attempt_id, provider.provider_id, provider.provider_id in before, request)
        ctx.router.rebuild(force_scores=True)
        spend = ui_detail.provider_spend(ctx.deps.providers)
        return JSONResponse(
            serialize.provider_payload(
                _served_view(ctx.deps.providers, provider),
                spend.get(provider.provider_id),
                _key_state(provider.provider_id),
                sign_in_account=_sign_in_account(provider.provider_id),
                pins=_backend_pins(provider.provider_id),
            ),
            status_code=200 if provider.provider_id in before else 201,
        )

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
            serialize.provider_payload(
                _served_view(ctx.deps.providers, provider),
                spend.get(provider.provider_id),
                _key_state(provider.provider_id),
                sign_in_account=_sign_in_account(provider.provider_id),
                pins=_backend_pins(provider.provider_id),
            )
        )

    @router.post("/api/providers/{provider_id}/key-check")
    async def check_provider_key(provider_id: str) -> Response:
        """Ask the upstream whether this provider's key works right now."""
        check = getattr(ctx.deps.providers, "check_key_async", None)
        if not callable(check):
            return _not_implemented("Provider key check", "provider store")
        try:
            provider = await check(provider_id)
        except UnknownProviderError:
            return errors.error_response(
                404, f"No provider '{provider_id}'.",
                "invalid_request_error", "provider_not_found",
            )
        except Exception as exc:
            log.exception("provider key check failed")
            return errors.error_response(
                502, f"Key check failed. {errors.detail(exc, _redactor())}",
                "server_error", "key_check_failed",
            )
        ctx.router.rebuild(force_scores=True)
        spend = ui_detail.provider_spend(ctx.deps.providers)
        return JSONResponse(
            serialize.provider_payload(
                _served_view(ctx.deps.providers, provider),
                spend.get(provider.provider_id),
                _key_state(provider.provider_id),
                sign_in_account=_sign_in_account(provider.provider_id),
                pins=_backend_pins(provider.provider_id),
            )
        )

    @router.get("/api/providers/{provider_id}/models")
    async def provider_models(provider_id: str) -> Response:
        """The whole catalogue, each model saying whether it is switched on.

        The one provider surface that is not filtered by the allowlist, because
        it is the one the allowlist is chosen from. A port too old to answer
        `catalogue()` falls back to its plain model list, where every model
        reads as enabled -- which is exactly what such a port serves.
        """
        catalogue = getattr(ctx.deps.providers, "catalogue", None)
        if callable(catalogue):
            try:
                return JSONResponse(catalogue(provider_id))
            except UnknownProviderError:
                return errors.error_response(
                    404, f"No provider '{provider_id}'.",
                    "invalid_request_error", "provider_not_found",
                )
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
            [
                {**serialize.provider_model_payload(m), "enabled": True}
                for m in provider.models
            ]
        )

    @router.get("/api/providers/{provider_id}/backends")
    async def provider_backends(provider_id: str, upstream_id: str) -> Response:
        """One model's backend hosts, for a kind that aggregates several per model.

        Live, not the cached catalogue: the whole point is to see what is
        available right now to pin against. 400 for a kind that does not
        aggregate backends at all (`AdapterUnsupportedError`) rather than an
        empty list, which would read as "this model has no backends" instead
        of "this provider does not have the concept".
        """
        list_backends_async = getattr(ctx.deps.providers, "list_backends_async", None)
        try:
            if callable(list_backends_async):
                rows = await list_backends_async(provider_id, upstream_id)
            else:
                rows = await asyncio.to_thread(
                    ctx.deps.providers.list_backends, provider_id, upstream_id
                )
        except UnknownProviderError:
            return errors.error_response(
                404, f"No provider '{provider_id}'.",
                "invalid_request_error", "provider_not_found",
            )
        except AdapterUnsupportedError as exc:
            return errors.error_response(
                400, str(exc), "invalid_request_error", "backend_routing_unsupported",
            )
        except UpstreamError as exc:
            return errors.error_response(
                exc.status_code, exc.message, "upstream_error", exc.error_code or "upstream_error",
            )
        except Exception as exc:
            log.exception("backend listing failed")
            return errors.error_response(
                502, f"Could not list backends. {errors.detail(exc, _redactor())}",
                "upstream_error", "backend_list_failed",
            )
        return JSONResponse(rows)

    @router.get("/api/providers/{provider_id}/logo")
    async def provider_logo(provider_id: str) -> Response:
        """This provider's own mark, fetched once by the coordinator and cached.

        Proxied rather than fetched by the browser so it still resolves on a
        console with no egress of its own -- frequently the case, a laptop on
        the lab LAN pointed at a coordinator that does have it -- and so no
        provider domain is handed to a third party.

        **Two different answers, and they are different status codes.** A
        provider this coordinator has never heard of is a 404 -- that is a
        request about something that does not exist. A provider that exists
        and simply has no mark to paint is a **204**: the question was
        answered, and the answer is "nothing to draw".

        It used to be 404 for both, on the reasoning that "a 404 is an
        ordinary answer here, not a fault". That reasoning is right about the
        product and wrong about the code: the Cluster tab does draw a monogram
        underneath and never paints over it, but a same-origin request the UI
        makes and the coordinator refuses is indistinguishable, to anything
        watching, from a route that is broken. `screens.check.mjs` gates on
        exactly that -- "the coordinator answered everything it was asked" --
        and it had been failing on an adopted ollama box, which has no vendor
        mark and never will.

        The batch route below (`/api/publishers/avatars`) had already reached
        the better shape for the same problem, and says so: `null` means "no
        mark, stop asking". This is that answer in one status code.

        A cold cache still answers immediately and starts the fetch behind it
        rather than holding the response open; the next render gets the mark.
        Nothing on this path is ever allowed to make a screen wait -- which is
        why "no mark yet" and "no mark ever" share the 204. The route could
        not tell them apart when both were 404 either, and the UI's retry is
        the next page load in both cases.
        """
        try:
            providers = ctx.deps.providers.list()
        except Exception:
            log.exception("provider listing failed")
            providers = []
        provider = next((p for p in providers if p.provider_id == provider_id), None)
        if provider is None:
            return errors.error_response(
                404, f"No provider '{provider_id}'.",
                "invalid_request_error", "provider_not_found",
            )

        cache = _logo_cache()
        hit = cache.get(provider_id)
        if hit is not None:
            body, content_type = hit
            return Response(
                body,
                media_type=content_type,
                headers={"cache-control": "private, max-age=86400"},
            )

        if not cache.is_fresh_miss(provider_id):
            asyncio.create_task(_warm_logo(cache, provider))
        # 204, not 404: the provider is real and the question was answerable.
        # An empty body fails an <image> decode exactly as a 404 did, so the
        # monogram underneath still shows and the UI path is unchanged.
        return Response(status_code=204)

    #: Per-session budget for the batch route (R7): 240 publishers a minute
    #: that the batch would go to the hub for. Answers out of the cache cost
    #: nothing -- every screen asks for the same grid, and charging those
    #: 429ed ordinary navigation -- so the bound is on the outbound fan-out,
    #: which is the thing a looping session could abuse.
    #: {session id: (window start, lookups charged)}.
    avatar_windows: dict[str, tuple[float, int]] = {}

    @router.get("/api/publishers/avatars")
    async def publisher_avatars(request: Request) -> Response:
        """Which of these model publishers have a mark, resolved once, cached.

        The browser used to ask huggingface.co directly, once per publisher,
        on every page load. A Models grid is ~45 distinct publishers and the
        hub's unauthenticated limit is below that, so the grid 429ed itself and
        every card fell back to two letters for the length of the backoff --
        which is the bug this endpoint exists to remove. Here it is forty-five
        lookups TOTAL, kept on disk, shared by every browser and surviving a
        restart.

        Batched rather than one route per publisher because the whole grid is
        one question, and asking it as forty-five requests over a browser's six
        connections is how the image route would starve `/api/*` behind it.

        Three answers, and the third is the important one:

            {"avatars": {"Qwen": "/api/publishers/Qwen/avatar", "acme": null}}

        a string is ready to draw, null is "this publisher has no mark, stop
        asking", and a name that is ABSENT is still resolving -- ask again in a
        moment. Collapsing that third case into null is what would put a
        publisher on letters for a day because the hub was slow once.
        """
        from control_plane.resolver import avatars

        raw = request.query_params.get("owners", "")
        owners = [part for part in raw.split(",") if part.strip()]
        if not owners:
            return JSONResponse({"avatars": {}})
        # A cap, not a pagination scheme: the client chunks, and an unbounded
        # list here would be an unbounded fan-out at somebody else's hub.
        owners = owners[:64]

        session = getattr(request.state, "session", None)
        sid = str(getattr(session, "session_id", "") or "-")
        cost = len(avatars.uncached(owners))
        now = time.monotonic()
        start, used = avatar_windows.get(sid, (now, 0))
        if now - start >= AVATAR_WINDOW_S:
            start, used = now, 0
        if cost and used + cost > AVATAR_OWNERS_PER_WINDOW:
            retry = max(1, int(AVATAR_WINDOW_S - (now - start)) + 1)
            return errors.error_response(
                429, "Too many publisher lookups; try again shortly.",
                "rate_limit_error", "rate_limited",
                headers={"retry-after": str(retry)},
            )
        # Drop expired windows so the map is bounded by live sessions.
        for stale in [k for k, (s, _) in avatar_windows.items() if now - s >= AVATAR_WINDOW_S]:
            avatar_windows.pop(stale, None)
        avatar_windows[sid] = (start, used + cost)

        try:
            found = await avatars.resolve_many(owners)
        except Exception:  # noqa: BLE001 - cosmetic path, never a 500
            log.debug("avatar batch failed", exc_info=True)
            return JSONResponse({"avatars": {}})

        return JSONResponse(
            {
                "avatars": {
                    owner: (f"/api/publishers/{quote(owner, safe='')}/avatar" if ready else None)
                    for owner, ready in found.items()
                }
            },
            headers={"cache-control": "no-store"},
        )

    @router.get("/api/publishers/{owner}/avatar")
    async def publisher_avatar(owner: str) -> Response:
        """One publisher's mark, from the cache. This route never fetches.

        Deliberately not the place the network is touched: on HTTP/1.1 a
        browser opens about six connections per origin, and a grid of ninety
        images that each waited on huggingface.co would hold all six for
        seconds with `/api/*` queued behind them. `/api/publishers/avatars`
        does the fetching, on one connection, and only names it reported as
        ready are ever requested here -- so in practice this is always a hit.
        """
        from control_plane.resolver import avatars

        name = avatars.normalize(owner)
        hit = avatars.cache().get(name) if name else None
        if hit is None:
            return errors.error_response(
                404, f"No avatar for '{owner}'.",
                "invalid_request_error", "avatar_not_found",
            )
        body, content_type = hit
        return Response(
            body,
            media_type=content_type,
            headers={"cache-control": "private, max-age=86400"},
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

    def _provider_audit(provider) -> dict | None:
        return serialize.provider_audit(provider, _redactor())

    def _provider_audit_by_id(provider_id: str) -> dict | None:
        get = getattr(ctx.deps.providers, "get", None)
        if not callable(get):
            return None
        try:
            return _provider_audit(get(provider_id))
        except Exception:
            return None

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
            # Not "the architecture is not supported": that asserts a cause,
            # and this code covers two of them. A model can arrive here
            # because no runtime loads its architecture, or because its
            # config.json could not be sized at all -- `Qwen3-TTS` keeps its
            # stack under `talker_config` and was reported as an unsupported
            # architecture when the architecture was never the problem. The
            # detail appended below is the resolver's own sentence and names
            # which of the two it was.
            "unsupported_architecture": (
                f"Could not plan{named}: derate could not size this model."
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


    @router.post("/api/plan")
    async def plan_endpoint(request: Request) -> Response:
        try:
            payload = await read_json_object(request)
        except BodyTooLarge:
            return payload_too_large()
        except Exception:
            return errors.error_response(
                400, "Body must be JSON.", "invalid_request_error", "invalid_json"
            )
        model_id = payload.get("model_id") if isinstance(payload, dict) else None
        try:
            out = await _plan_and_fit(ctx, payload)
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
                # Beside the range, never instead of it. See _measured_decode.
                "measured_decode": out.measured_decode,
                # The live verdict, budgeted against what the nodes can
                # actually hand out right now. None when nothing could read
                # them -- never a fabricated stand-in for the static answer.
                "fit_live": (
                    serialize.fit_payload(out.fit_live) if out.fit_live else None
                ),
                "capacity": out.capacity,
                # The numbers this verdict was actually taken at. Echoed
                # because they are no longer necessarily the ones that were
                # sent: a body with no `context` asks the coordinator to choose
                # one from what fits, and a verdict whose question is not on
                # screen beside it is not checkable.
                "context": out.context_length,
                "concurrency": out.concurrency,
                # The one field the UI reads for the button. Which verdict
                # governs is decided here, not in the client.
                "serve": out.serve,
                "resolver_warnings": out.resolver_warnings,
                "remote_code": out.remote_code,
                # The window the plan sized against (stretched when YaRN is
                # on) and the trained one, so the UI can offer YaRN.
                "native_window": out.native_window,
                "original_window": out.original_window,
                # Cache bytes per token at each KV width this image takes,
                # over this model's 16-bit rate. Per model, because a packed
                # layout's overhead depends on head_dim and its boundary
                # layers on the layer count. A width refused for this
                # geometry is absent.
                "kv_dtype_ratios": _kv_dtype_ratios(out.shape),
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
                # What this checkpoint could speculate with, and what this
                # verdict was taken with. Both, for the same reason `context`
                # and `concurrency` are both echoed: the options say what the
                # control may offer, and the echo says what the numbers beside
                # it were actually computed under.
                "speculative_options": out.speculative_options,
                "speculative": serialize.speculative_payload(out.speculative),
            }
        )

    async def _read_plan_body(request: Request) -> dict | Response:
        try:
            payload = await read_json_object(request)
        except BodyTooLarge:
            return payload_too_large()
        except Exception:
            return errors.error_response(
                400, "Body must be JSON.", "invalid_request_error", "invalid_json"
            )
        if not isinstance(payload, dict):
            return errors.error_response(
                400, "Body must be a JSON object.", "invalid_request_error", "invalid_request"
            )
        return payload

    async def _plan_or_error(payload: dict):
        """`_plan_and_fit`, with its failures answered the way /api/plan
        answers them. Returns the outcome or a Response."""
        model_id = payload.get("model_id")
        try:
            return await _plan_and_fit(ctx, payload)
        except _PlacementRefused as exc:
            return exc.response()
        except ValueError as exc:
            return errors.error_response(
                400, str(exc), "invalid_request_error", "invalid_request"
            )
        except Exception as exc:
            log.exception("planning %s failed", model_id)
            return _plan_error_response(exc, model_id)

    @router.post("/api/plan/command")
    async def plan_command(request: Request) -> Response:
        """The command and recipe a launch of this body would run, refused
        exactly as the launch would refuse it. Starts nothing."""
        payload = await _read_plan_body(request)
        if isinstance(payload, Response):
            return payload
        preview = getattr(ctx.deps.deployments, "preview_launch", None)
        if not callable(preview):
            return errors.error_response(
                501, "This deployment backend cannot preview a launch command.",
                "server_error", "preview_unavailable",
            )
        try:
            options = _launch_options(payload)
        except ValueError as exc:
            return errors.error_response(
                400, str(exc), "invalid_request_error", "invalid_request"
            )
        out = await _plan_or_error(payload)
        if isinstance(out, Response):
            return out
        fit = out.fit_live if out.fit_live is not None else out.fit
        if fit is None:
            return errors.error_response(
                503, "the fit calculator is not wired; there is no budget to render",
                "server_error", "fit_unavailable",
            )
        from control_plane.deploy.flags import render_kv_cache_dtype, render_quantization

        try:
            result = await asyncio.to_thread(
                partial(
                    preview,
                    out.shape,
                    out.plan,
                    fit,
                    payload.get("runtime") or "vllm",
                    out.context_length,
                    out.concurrency,
                    modality=out.modality,
                    speculative=out.speculative,
                    # The same spreads create_deployment applies.
                    kv_dtype=(
                        out.kv_dtype if render_kv_cache_dtype(out.kv_dtype) is not None else None
                    ),
                    quantization=(
                        out.quantization
                        if out.quantization and render_quantization(out.quantization) is not None
                        else None
                    ),
                    revision=out.revision,
                    speculative_tokens=payload.get("speculative_tokens", 0),
                    engine_options=out.engine_options,
                    original_window=(
                        out.original_window
                        if out.engine_options.rope_factor is not None
                        else None
                    ),
                    **options,
                )
            )
        except ValueError as exc:
            return errors.error_response(
                400, str(exc), "invalid_request_error", "invalid_request"
            )
        return JSONResponse(result)

    @router.post("/api/plan/concurrency")
    async def plan_concurrency(request: Request) -> Response:
        """How many users a plan holds before its KV cache is full, and the
        per-user decode ceiling memory bandwidth allows at each count."""
        payload = await _read_plan_body(request)
        if isinstance(payload, Response):
            return payload

        def bad(message: str) -> Response:
            return errors.error_response(
                400, message, "invalid_request_error", "invalid_request"
            )

        contexts = payload.get("contexts")
        if (
            not isinstance(contexts, list)
            or not 1 <= len(contexts) <= 4
            or not all(isinstance(c, int) and not isinstance(c, bool) for c in contexts)
            or any(c < 512 for c in contexts)
        ):
            return bad("contexts must be a list of 1 to 4 whole numbers, each at least 512.")
        max_users = payload.get("max_users", 64)
        if isinstance(max_users, bool) or not isinstance(max_users, int) or not 1 <= max_users <= 256:
            return bad("max_users must be a whole number from 1 to 256.")
        base = {
            k: v for k, v in payload.items() if k not in ("contexts", "max_users")
        }

        from control_plane import computeprobe, measurements
        from control_plane.deploy.flags import budget_profile
        from control_plane.fit.calculator import bandwidth_ceiling_tps, prefill_floor_seconds
        from control_plane.fit.kv import kv_cache_bytes, kv_cache_bytes_per_rank

        # The machines as the runtime sees them, like the plan: a llamacpp
        # launch decodes from host RAM, never at the card's bandwidth.
        runtime = base.get("runtime") or "vllm"
        try:
            profiles = [
                budget_profile(runtime, n.profile)
                for n in ctx.deps.registry.healthy_nodes()
            ]
        except Exception:
            log.exception("registry unavailable while sizing concurrency")
            profiles = []

        series = []
        bandwidth = None
        basis = "static"
        compute = None
        compute_looked = False
        for context in contexts:
            out = await _plan_or_error({**base, "context": context, "concurrency": 1})
            if isinstance(out, Response):
                return out
            if out.native_window is not None and context > out.native_window:
                return bad(
                    f"{context} is past this model's {out.native_window}-token window."
                )
            fit = out.fit_live if out.fit_live is not None else out.fit
            if fit is None:
                return errors.error_response(
                    503, "the fit calculator is not wired; there is no budget to size",
                    "server_error", "fit_unavailable",
                )
            if out.fit_live is not None:
                basis = "live"
            shape, plan = out.shape, out.plan
            used = set(plan.node_ids)
            rates = [
                p.memory_bandwidth_gbps for p in profiles if p.node_id in used
            ]
            bw = min(rates) if rates else 0.0
            bandwidth = bw if bandwidth is None else min(bandwidth, bw)
            kv_read = kv_cache_bytes(shape, context, 1, out.kv_dtype)
            # One sequence's cache on the busiest rank, boundary layers
            # included -- the same figure the fit gate charged.
            kv_seq = kv_cache_bytes_per_rank(shape, plan, context, 1, out.kv_dtype)
            fixed = fit.breakdown.total - fit.breakdown.kv_cache
            if kv_seq > 0:
                wall = max(0, int((fit.usable_per_node - fixed) // kv_seq))
            else:
                wall = max_users
            users = set()
            n = 1
            while n <= max_users:
                users.add(n)
                n *= 2
            if wall >= 1:
                users.add(wall)
            if not compute_looked:
                # Once, against the first plan's machines: the floor is only
                # stated for a GPU this image was measured on, and a plan over
                # unlike GPUs has no single rate to divide by.
                compute_looked = True
                gpus = {p.gpu_name for p in profiles if p.node_id in used}
                if len(gpus) == 1:
                    try:
                        compute = await asyncio.to_thread(
                            measurements.matching_compute,
                            next(iter(gpus)),
                            image=computeprobe.resolved_image(),
                        )
                    except Exception:
                        log.exception("reading compute records failed")
                        compute = None
            prefill = (
                prefill_floor_seconds(shape, context, compute.tflops, plan.tensor_parallel)
                if compute is not None
                else None
            )
            points = []
            for u in sorted(users):
                each = (
                    bandwidth_ceiling_tps(shape, bw, kv_read, u) if bw > 0 else None
                )
                points.append(
                    {
                        "users": u,
                        "ceiling_tps_each": each,
                        "ceiling_tps_total": (
                            each * min(u, max(wall, 1)) if each is not None else None
                        ),
                        "queued": u > wall,
                        # Past the wall a request waits for a slot, and how
                        # long is not something derate can say.
                        "ttft_floor_s": (
                            u * prefill if prefill is not None and u <= wall else None
                        ),
                    }
                )
            series.append(
                {
                    "context": context,
                    "wall_users": wall,
                    "prompt_tokens": context,
                    "points": points,
                }
            )
        return JSONResponse(
            {
                "series": series,
                "bandwidth_gbps": bandwidth if bandwidth else None,
                "basis": basis,
                "caveat": (
                    "A ceiling from memory bandwidth alone. Past a few users "
                    "decode becomes compute-bound and runs slower than this; "
                    "derate has not measured where that starts on this "
                    "hardware. Run a benchmark for real numbers."
                ),
                "compute": (
                    {
                        "tflops": compute.tflops,
                        "gpu_name": compute.gpu_name,
                        "measured_at": compute.measured_at,
                    }
                    if compute is not None
                    else None
                ),
                "ttft_caveat": (
                    "No sooner than this: every prompt is the full context, all "
                    "users send at once, and only the weights' matrix work is "
                    "counted on compute measured on this machine. Attention, "
                    "scheduling and queueing make it slower; a benchmark shows "
                    "the real figure."
                ),
            }
        )

    @router.post("/api/plan/candidates")
    async def plan_candidates(request: Request) -> Response:
        """The best-ranked plan of each kind on this node set, each re-judged
        by the fit gate at the planner pick's context and concurrency."""
        payload = await _read_plan_body(request)
        if isinstance(payload, Response):
            return payload
        base = {k: v for k, v in payload.items() if k != _DEGREES_PARAM}
        out = await _plan_or_error(base)
        if isinstance(out, Response):
            return out
        kept: list[dict] = []
        seen: set[str] = set()
        for alt in out.alternatives:
            if alt["kind"] in seen:
                continue
            seen.add(alt["kind"])
            kept.append(alt)
            if len(kept) == 5:
                break
        candidates = []
        for alt in kept:
            degrees = {axis: alt[axis] for axis in _DEGREE_AXES}
            row = {
                "kind": alt["kind"],
                "degrees": degrees,
                "node_ids": list(alt["node_ids"]),
                "verdict": None,
                "per_node_bytes": None,
                "reason": None,
                "error": None,
            }
            try:
                one = await _plan_and_fit(
                    ctx,
                    {
                        **base,
                        _PLACEMENT_PARAM: list(alt["node_ids"]),
                        _DEGREES_PARAM: degrees,
                        "context": out.context_length,
                        "concurrency": out.concurrency,
                    },
                )
            except _PlacementRefused as exc:
                row["error"] = exc.message
                candidates.append(row)
                continue
            except Exception:
                log.exception("re-judging the %s candidate failed", alt["kind"])
                continue
            fit = one.fit_live if one.fit_live is not None else one.fit
            if fit is not None:
                row["verdict"] = fit.verdict.value
                row["per_node_bytes"] = fit.breakdown.total
            row["reason"] = one.plan.reason
            candidates.append(row)
        recommended = out.recommended if out.recommended is not None else out.plan
        return JSONResponse(
            {"candidates": candidates, "recommended_kind": recommended.kind.value}
        )

    @router.get("/api/compute")
    async def list_compute() -> JSONResponse:
        from control_plane import measurements

        records = await asyncio.to_thread(measurements.load_compute_all)
        return JSONResponse({"records": [dataclasses.asdict(r) for r in records]})

    @router.post("/api/compute/measure")
    async def measure_compute(request: Request) -> Response:
        """Time a bf16 matmul on THIS host's GPU, in the serving image."""
        from control_plane import computeprobe, measurements

        registry = ctx.deps.registry
        local = getattr(registry, "local_profile", None)
        if local is None:
            try:
                local = next(
                    (n.profile for n in registry.healthy_nodes() if getattr(n, "is_local", False)),
                    None,
                )
            except Exception:
                local = None
        if local is None:
            return errors.error_response(
                503,
                "This coordinator has no GPU node of its own registered, so there "
                "is nothing here to measure.",
                "server_error", "measurement_failed",
            )
        if not _COMPUTE_LOCK.acquire(blocking=False):
            return errors.error_response(
                409, "A compute measurement is already running on this machine.",
                "invalid_request_error", "measurement_running",
            )
        try:
            image = computeprobe.resolved_image()
            result = await asyncio.to_thread(computeprobe.run_compute, image=image)
        finally:
            _COMPUTE_LOCK.release()
        if result.error is not None or result.tflops is None:
            return errors.error_response(
                503, result.error or "the matmul did not complete",
                "server_error", "measurement_failed",
            )
        record = measurements.ComputeRecord(
            gpu_name=local.gpu_name,
            image=image,
            dtype="bf16",
            tflops=result.tflops,
            node_id=local.node_id,
            measured_at=time.time(),
        )
        await asyncio.to_thread(measurements.save_compute, record)
        audit_request(
            request, AuditAction.API_WRITE, "compute", local.node_id,
            after={"tflops": round(result.tflops, 2), "image": image},
        )
        return JSONResponse(dataclasses.asdict(record))

    @router.get("/api/deployments")
    async def list_deployments(limit: int = DEPLOYMENT_LIST_TERMINAL) -> JSONResponse:
        """Everything still alive, plus the newest *limit* that are not.

        Unbounded, this returned every record the manager held. On this
        cluster that reached 1,628 records and 7.7 MB -- 5.3 MB of it
        tracebacks in `last_error` -- which the browser downloaded and parsed
        on every refresh of the models screen, and which is what took the tab
        down after a launch. A launch that fails is retried, every retry is a
        new deployment id, and the list grew by one every twenty seconds.

        Live deployments are never dropped, however many there are: they are
        the ones the screen is actually about, and a cap that could hide a
        running model would be a worse bug than the one this fixes. The cut is
        only ever applied to FAILED and STOPPED, newest kept.

        Original order is preserved rather than re-sorted -- the callers do
        their own sorting and this route has never promised one.
        """
        try:
            deployments = ctx.deps.deployments.list()
        except Exception:
            log.exception("deployment listing failed")
            deployments = []
        keep = max(0, min(int(limit), 2000))
        terminal = [d for d in deployments if d.state in _TERMINAL_STATES]
        kept = {d.deployment_id for d in terminal[len(terminal) - keep:]} if keep else set()
        return JSONResponse([
            serialize.deployment_payload(d, error_chars=serialize.LIST_ERROR_CHARS)
            for d in deployments
            if d.state not in _TERMINAL_STATES or d.deployment_id in kept
        ])

    @router.get("/api/deployments/{deployment_id}")
    async def get_deployment(deployment_id: str) -> Response:
        """One deployment, with `last_error` whole.

        The list bounds that field; this is where the rest of it lives, and
        the sheet that renders a refusal through `Verbatim` -- planner and fit
        strings, which are the product and are never truncated -- reads it
        from here.
        """
        try:
            deployment = ctx.deps.deployments.get(deployment_id)
        except Exception:
            log.exception("deployment lookup failed")
            deployment = None
        if deployment is None:
            return errors.error_response(
                404, f"No deployment '{deployment_id}'.",
                "invalid_request_error", "deployment_not_found",
            )
        return JSONResponse(serialize.deployment_payload(deployment))

    @router.post("/api/deployments")
    async def create_deployment(request: Request) -> Response:
        try:
            payload = await read_json_object(request)
        except BodyTooLarge:
            return payload_too_large()
        except Exception:
            return errors.error_response(
                400, "Body must be JSON.", "invalid_request_error", "invalid_json"
            )
        return await _launch_response(payload, request=request)

    async def _create_deployment(
        payload: dict, *, actor: str, request: Request | None = None
    ) -> tuple[int, dict]:
        """Launch exactly as POST /api/deployments does, for callers inside
        the coordinator (boot relaunch, benchmarks). Returns the status and
        body the route would have answered; audit is written only when there
        is a `request` to attribute it to. `actor` names the caller in logs."""
        response = await _launch_response(payload, request=request)
        try:
            body = json.loads(bytes(response.body or b"{}"))
        except ValueError:
            body = {}
        if response.status_code != 201:
            log.info("%s launch refused (%s)", actor, response.status_code)
        return response.status_code, body

    ctx.launcher = _create_deployment

    async def _launch_response(payload, *, request: Request | None) -> Response:
        model_id = payload.get("model_id") if isinstance(payload, dict) else None

        # A dtype used to be refused here outright, and the reason given was
        # that neither serve command template carried --quantization -- so
        # honouring one would budget for 4-bit weights and then start the
        # repository's real 16-bit ones, the precise out-of-memory kill this
        # gate exists to refuse.
        #
        # That premise stopped being true on 2026-09-11: both templates carry
        # `quantization_arg` now, `flags.render_quantization` maps a derate
        # scheme onto a name read off the pinned image's OWN registry, and
        # `flags.quantization_refusal` refuses a runtime that cannot be told
        # -- rather than dropping the flag, which is what made the blanket
        # refusal the right answer before.
        #
        # What survives is the half that was always load-bearing: a scheme
        # this build cannot actually request is still refused, and refused
        # BEFORE a resolve and a planner search are spent on it. That check is
        # `render_quantization` below, which raises by name for every GGUF
        # scheme (llama.cpp's format, not launchable here at all), for nf4
        # (needs a bitsandbytes loader this image does not carry) and for
        # int8. Its message says to pass the variant repository's own id
        # instead, which is still the right move for those.

        # Free-text CLI tokens, for a model the standard recipe doesn't cover.
        # Read here, not inside _plan_and_fit: unlike dtype these never touch
        # sizing or planning, so a dry run has no reason to see them.
        # shlex.split so a caller can paste a flag string with a quoted value
        # ("--foo 'bar baz'") without doing any parsing itself; each resulting
        # token is checked against the M-22 allowlist inside
        # DeploymentManager.launch, not here. extra_args appends to the
        # generated command; custom_command replaces it -- sending both is
        # refused as a ValueError from launch() itself, turned into a 400 in
        # the except block below, so the two parses below do not need to
        # agree with each other about which one wins.
        # One parser for the launch-only keys, shared with /api/plan/command.
        try:
            launch_opts = _launch_options(payload if isinstance(payload, dict) else {})
            lifecycle = _parse_lifecycle(payload)
        except ValueError as exc:
            return errors.error_response(
                400, str(exc), "invalid_request_error", "invalid_request"
            )
        served_name = launch_opts.get("served_name")
        extra_args = launch_opts["extra_args"]
        custom_command = launch_opts["custom_command"]
        # Opt-in (R2): the repo's own Python runs in the container only when
        # the operator asked for it, and the audit row says they did.
        trust_remote_code = launch_opts["trust_remote_code"]
        # Launch-only: changes startup time, not memory sizing.
        enforce_eager = launch_opts["enforce_eager"]
        cudagraph_capture_sizes = launch_opts.get("cudagraph_capture_sizes")

        # Local, like `sharding_refusal` and `speculative_refusal` above:
        # the gateway does not import deploy internals at module scope.
        from control_plane.deploy.flags import (
            render_kv_cache_dtype,
            render_quantization,
        )

        # Unlike the two above, this one IS a fit-gate input -- `_plan_and_fit`
        # reads it and sizes the cache with it. Validated here anyway, before
        # a resolve and a planner search are spent on a width the launch will
        # have to refuse. Only a dtype the caller NAMED is checked: the
        # settings default is resolved in `_plan_and_fit` and nowhere else, so
        # there is no second copy of it here to drift.
        raw_kv_dtype = payload.get("kv_dtype") if isinstance(payload, dict) else None
        if raw_kv_dtype is not None:
            if not isinstance(raw_kv_dtype, str):
                return errors.error_response(
                    400,
                    "kv_dtype must be a string naming a KV cache element width.",
                    "invalid_request_error",
                    "invalid_request",
                )
            try:
                render_kv_cache_dtype(raw_kv_dtype)
            except ValueError as exc:
                return errors.error_response(
                    400, str(exc), "invalid_request_error", "invalid_request"
                )

        # A FIT-GATE input, like kv_dtype above and heavier: `_plan_and_fit`
        # passes it to the resolver as a dtype override, so every weight
        # figure in the verdict the caller is about to be shown is computed at
        # this scheme. Validated here so a scheme this build cannot request
        # fails before a resolve and a planner search are spent on it.
        raw_quantization = payload.get("dtype") if isinstance(payload, dict) else None
        if raw_quantization is not None:
            if not isinstance(raw_quantization, str):
                return errors.error_response(
                    400,
                    "dtype must be a string naming a weight quantization scheme.",
                    "invalid_request_error",
                    "invalid_request",
                )
            try:
                render_quantization(raw_quantization)
            except ValueError as exc:
                return errors.error_response(
                    400, str(exc), "invalid_request_error", "invalid_request"
                )

        try:
            out = await _plan_and_fit(ctx, payload)
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
        # The verdict `_plan_and_fit` reached on the machines it placed this
        # on, each judged by the image it runs -- along with the GPU check,
        # which refused there or left a machine out. Not asked again here:
        # `supported_by` without the machines answers for the default image,
        # and would refuse an architecture only the placed machine's image
        # loads.
        if not out.runtime_supported:
            return errors.error_response(
                400,
                out.runtime_unsupported_reason or f"{runtime} cannot load this model",
                "invalid_request_error",
                "runtime_unsupported",
            )

        # A shape the launcher cannot start, ahead of the runtime question
        # because it is the more specific one: vLLM shards fine, and this is
        # sparkrun's cluster path refusing to bring up a pure data-parallel
        # job. Its own code, because "the runtime cannot shard" would send a
        # caller looking at the wrong thing.
        dp_problem = _data_parallel_refusal(
            plan.tensor_parallel,
            plan.pipeline_parallel,
            plan.data_parallel,
            _launcher_version(ctx.deps.deployments),
        )
        if dp_problem:
            return errors.error_response(
                400,
                dp_problem,
                "invalid_request_error",
                "launcher_cannot_data_parallel",
            )

        # A runtime that cannot shard, handed a plan that shards. Checked here
        # and not inside the launcher because this is the last place a refusal
        # is still a 400 the caller can act on: past it, the machines are
        # committed and the same fact becomes a FAILED record.
        sharding_problem = _sharding_refusal(
            runtime,
            plan.tensor_parallel,
            plan.pipeline_parallel,
            plan.expert_parallel,
            plan.data_parallel,
            _launcher_version(ctx.deps.deployments),
        )
        if sharding_problem:
            return errors.error_response(
                400, sharding_problem, "invalid_request_error", "runtime_cannot_shard"
            )

        # If the verdict is WONT_FIT the launch is refused with the reason.
        # Nothing is started. For a host-pool runtime (llamacpp) the LIVE
        # verdict is that verdict: its static budget is 0 by design -- there
        # is no host-RAM nameplate, see `flags.budget_profile` -- so judging it
        # statically refused every llamacpp launch everywhere, a Pi and the
        # RTX box alike, while /api/plan (which reads the live one) said fits.
        from control_plane.deploy.flags import runtime_spec

        host_pool = runtime_spec(runtime).memory_pool == "host"
        if host_pool and out.fit_live is None:
            return errors.error_response(
                400,
                f"{runtime} serves from host memory, and there is no live reading "
                f"of host memory on {', '.join(plan.node_ids) or 'these machines'}. "
                f"derate never invents a host budget: wait for the next sample, "
                f"and make sure the machine's agent is current (an agent older "
                f"than host-pool budgeting does not report host memory).",
                "invalid_request_error",
                "host_memory_unmeasured",
            )
        if host_pool:
            fit = out.fit_live
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
        if (
            live is not None and live.verdict is Verdict.WONT_FIT and not allow_over
            and not host_pool
        ):
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
                    # The name clients pass as "model", when the caller wants
                    # one of their own. Absent, the manager derives it from the
                    # model id, which is what every launch did before this.
                    #
                    # It exists because the served-name rule is cluster-wide --
                    # `manager._find_conflict` refuses a second deployment
                    # answering to a name already in use, anywhere -- so two
                    # copies of one model, however they are placed, need
                    # distinct names or one of them is refused. That docstring
                    # already said replication "has to be asked for under its
                    # own name"; there was no way to ask.
                    #
                    # Not validated here: `manager.launch` runs
                    # `check_recipe_identifiers` on it before any record
                    # exists, and a second copy of that grammar is a second
                    # thing to keep in step with `deploy/recipes.py`.
                    **({"served_name": served_name} if served_name else {}),
                    extra_args=extra_args,
                    custom_command=custom_command,
                    enforce_eager=enforce_eager,
                    cudagraph_capture_sizes=cudagraph_capture_sizes,
                    # Spread rather than sent as None, on the same terms as
                    # `node_ids` on the plan request: absence is the contract
                    # for "one token per step", which is what every launch did
                    # before this existed. `deployments` is a port the gateway
                    # composes and does not own -- including the stub whose
                    # whole job is to have nothing on it -- so a port that
                    # predates this field keeps working untouched, and one that
                    # cannot take it fails loudly on the only requests where
                    # that matters: the ones where somebody asked to speculate
                    # and the fit gate already charged the draft's memory.
                    **({"speculative": out.speculative} if out.speculative else {}),
                    # Spread on exactly the terms `speculative` is, and for
                    # the same reason: sent only when it would change the
                    # command, so a port that predates the field keeps working
                    # for every launch that asks for the model's own width --
                    # and fails loudly on the only ones where dropping it
                    # matters, the ones the fit gate has already sized narrow.
                    **(
                        {"kv_dtype": out.kv_dtype}
                        if render_kv_cache_dtype(out.kv_dtype) is not None
                        else {}
                    ),
                    # Spread on exactly the same terms, and this is the one
                    # that closes the gap: the `dtype` override has always
                    # re-priced the fit gate and has never reached the launch
                    # command, so a plan approved at nvfp4 launched a
                    # checkpoint in its own bf16 with a budget sized for
                    # something 3.5x smaller.
                    **(
                        {"quantization": out.quantization}
                        if out.quantization
                        and render_quantization(out.quantization) is not None
                        else {}
                    ),
                    revision=out.revision,
                    speculative_tokens=payload.get("speculative_tokens", 0),
                    # Only sent when set, so a DeploymentPort that predates
                    # the opt-in still launches everything it always did.
                    **({"trust_remote_code": True} if trust_remote_code else {}),
                    # Spread on `speculative`'s terms: only a launch that
                    # asked for an engine option passes them.
                    **(
                        {
                            "engine_options": out.engine_options,
                            # Stored only when YaRN stretched the window.
                            "original_window": (
                                out.original_window
                                if out.engine_options.rope_factor is not None
                                else None
                            ),
                        }
                        if out.engine_options != EngineOptions()
                        else {}
                    ),
                    # Same spread: a port that predates lifecycle keeps working.
                    **({"lifecycle": lifecycle} if lifecycle != Lifecycle() else {}),
                )
            )
        except ValueError as exc:
            # An input validation error -- e.g. a command-unsafe model id, an
            # extra_args/custom_command token check_extra_args_safe rejected,
            # enforce_eager/cudagraph_capture_sizes sent together or alongside
            # custom_command, or a runtime that cannot take either -- not a
            # launch that genuinely failed.
            return errors.error_response(
                400, str(exc), "invalid_request_error", "invalid_request"
            )
        except Exception as exc:
            # A node in the plan already runs this model, or already answers to
            # this served name. Duck-typed on `existing` rather than caught by
            # class, for the reason given at _TERMINAL_STATES above: importing
            # DuplicateDeployment means importing control_plane.deploy, which
            # pulls the manager, the sparkrun adapter and the event bus into
            # this request module.
            #
            # 409, and the same reasoning as the live-memory 409 above: the
            # request is well formed, the conflict is with what is running now,
            # and an unchanged retry succeeds once that deployment is stopped.
            # Reaching the 502 instead reported an operator's own placement
            # back to them as a server fault.
            existing = getattr(exc, "existing", None)
            if getattr(existing, "deployment_id", None):
                return JSONResponse(
                    status_code=409,
                    content={
                        "error": {
                            # The manager's sentence, unedited: it names the
                            # deployment in the way and what to do about it.
                            "message": str(exc),
                            "type": "invalid_request_error",
                            "param": None,
                            "code": "already_deployed",
                        },
                        # So the screen can link to the thing it must stop
                        # rather than making the reader go and find it.
                        "conflict": serialize.deployment_payload(existing),
                        "clash": getattr(exc, "clash", "served_name"),
                        "plan": serialize.plan_payload(plan),
                        "fit": serialize.fit_payload(fit),
                    },
                )
            log.exception("launch failed")
            return errors.error_response(
                502, f"Launch failed. {errors.detail(exc, _redactor())}",
                "server_error", "launch_failed",
            )
        ctx.router.rebuild(force_scores=True)
        if request is not None:
            audit_request(
                request, AuditAction.DEPLOYMENT_LAUNCHED, "deployment",
                str(getattr(deployment, "deployment_id", "")),
                after=_deployment_audit(deployment),
            )
            # An operator launch is remembered as a run profile. Boot relaunch
            # and benchmark copies pass no request and are not. A profile
            # write must never fail the launch it describes.
            if ctx.profiles is not None:
                try:
                    from control_plane.deploy import relaunch

                    session = request.scope.get("state", {}).get("session")
                    ctx.profiles.record_run(
                        model_id=deployment.shape.model_id,
                        request=relaunch.launch_request_from(deployment),
                        deployment_id=deployment.deployment_id,
                        served_name=deployment.served_name,
                        actor=session.username if session is not None else "-",
                    )
                except Exception:
                    log.warning("could not record the run profile", exc_info=True)
        return JSONResponse(
            serialize.deployment_payload(deployment), status_code=201
        )

    # -- Benchmarks (gateway/bench.py): a throwaway copy, measured, stopped. --

    def _bench_describe(
        node_ids: list[str], runtime: str
    ) -> tuple[str | None, str | None]:
        """The GPU and image version of the machine the benchmark ran on --
        the version of the image THAT machine runs for *runtime*, never the
        default image's. None when that image has no probe."""
        from control_plane import measurements

        for node_id in node_ids:
            node = ctx.deps.registry.get_node(node_id)
            profile = getattr(node, "profile", None)
            gpu = getattr(profile, "gpu_name", None)
            if gpu:
                version = measurements.image_version(
                    runtime, getattr(profile, "arch", "") or ""
                )
                return gpu, version or None
        return None, None

    def _bench_done(record) -> None:
        """A finished benchmark is the result of every profile with its setup."""
        if ctx.profiles is None:
            return
        one = next((p for p in record.points if p.get("users") == 1), None) or {}
        ctx.profiles.attach_result(record.request, {
            "bench_id": record.bench_id,
            "finished_at": record.finished_at,
            "spec_mean_acceptance": record.spec_mean_acceptance,
            "tps_each_1": one.get("tps_each_mean"),
            "ttft_ms_1": one.get("ttft_ms_p50"),
        })

    if ctx.bench is None:
        from .bench import BenchRunner

        ctx.bench = BenchRunner(
            launcher=lambda: ctx.launcher,
            deployments=ctx.deps.deployments,
            describe=_bench_describe,
            on_done=_bench_done,
        )

    def _bench_not_found() -> JSONResponse:
        return errors.error_response(
            404, "No benchmark has that id.", "invalid_request_error",
            "benchmark_not_found",
        )

    @router.get("/api/benchmarks")
    async def list_benchmarks(model_id: str | None = None) -> JSONResponse:
        records = await asyncio.to_thread(ctx.bench.list, model_id)
        return JSONResponse({"benchmarks": [dataclasses.asdict(r) for r in records]})

    @router.get("/api/benchmarks/{bench_id}")
    async def get_benchmark(bench_id: str) -> JSONResponse:
        record = ctx.bench.get(bench_id)
        if record is None:
            return _bench_not_found()
        return JSONResponse(dataclasses.asdict(record))

    @router.post("/api/benchmarks")
    async def run_benchmark(request: Request) -> Response:
        from .bench import WORKLOADS, BenchBusy

        try:
            payload = await read_json_object(request)
        except BodyTooLarge:
            return payload_too_large()
        except Exception:
            return errors.error_response(
                400, "Body must be JSON.", "invalid_request_error", "invalid_json"
            )

        def bad(message: str) -> JSONResponse:
            return errors.error_response(
                400, message, "invalid_request_error", "invalid_request"
            )

        if not isinstance(payload, dict):
            return bad("Body must be a JSON object.")
        launch = payload.get("request")
        if not isinstance(launch, dict) or not isinstance(launch.get("model_id"), str):
            return bad("request must be a launch request with a model_id.")
        workload = payload.get("workload", "prose")
        if workload not in WORKLOADS:
            return bad(f"workload must be one of {', '.join(WORKLOADS)}.")
        users = payload.get("users", [1, 4, 8, 16])
        if (
            not isinstance(users, list)
            or not 1 <= len(users) <= 4
            or len(set(users)) != len(users)
            or not all(isinstance(u, int) and not isinstance(u, bool) and 1 <= u <= 64
                       for u in users)
        ):
            return bad("users must be 1 to 4 distinct whole numbers from 1 to 64.")
        try:
            record = ctx.bench.start(launch, workload=workload, users=users)
        except BenchBusy as exc:
            return errors.error_response(
                409, str(exc), "invalid_request_error", "benchmark_running"
            )
        after = dataclasses.asdict(record)
        audit_request(
            request, AuditAction.BENCHMARK_CHANGED, "benchmark", record.bench_id,
            after=after,
        )
        return JSONResponse(after, status_code=202)

    @router.delete("/api/benchmarks/{bench_id}")
    async def delete_benchmark(bench_id: str, request: Request) -> Response:
        record = ctx.bench.get(bench_id)
        if record is None:
            return _bench_not_found()
        ctx.bench.delete(bench_id)
        audit_request(
            request, AuditAction.BENCHMARK_CHANGED, "benchmark", bench_id,
            before=dataclasses.asdict(record),
        )
        return Response(status_code=204)

    @router.patch("/api/deployments/{deployment_id}")
    async def patch_deployment(deployment_id: str, request: Request) -> Response:
        """Offer a deployment on the API, or stop offering it.

        `{"serving": false}` takes the model off `/v1/models`, out of routing,
        off the chat picker and off the topology graph together -- one seam,
        `targets.build_index`, exactly as a provider's `enabled_models`
        allowlist works. **The container keeps running and keeps holding its
        GPU memory**; DELETE is what stops it.

        Mirrors `patch_provider` above, including the `rebuild(force_scores=
        True)` that makes the edit take effect with no restart and nothing
        else to press.
        """
        try:
            patch = await request.json()
        except Exception:
            return errors.error_response(
                400, "Body must be JSON.", "invalid_request_error", "invalid_json"
            )
        if not isinstance(patch, dict) or not ({"serving", "lifecycle"} & set(patch)):
            return errors.error_response(
                400,
                "Body must be an object with a 'serving' boolean and/or a "
                "'lifecycle' object. They are the only fields this route edits.",
                "invalid_request_error",
                "invalid_request",
            )
        set_serving = getattr(ctx.deps.deployments, "set_serving", None)
        set_lifecycle = getattr(ctx.deps.deployments, "set_lifecycle", None)
        if ("serving" in patch and not callable(set_serving)) or (
            "lifecycle" in patch and not callable(set_lifecycle)
        ):
            return _not_implemented("Deployment update", "deployment manager")
        serving = patch.get("serving")
        if "serving" in patch and not isinstance(serving, bool):
            # Not coerced. "false" and 0 are both truthy-adjacent in ways that
            # would silently do the opposite of what was asked, and this route
            # decides whether a model is reachable.
            return errors.error_response(
                400,
                f"'serving' must be true or false, not {type(serving).__name__}.",
                "invalid_request_error",
                "invalid_request",
            )
        lifecycle = None
        if "lifecycle" in patch:
            try:
                current = ctx.deps.deployments.get(deployment_id)
            except Exception:
                log.exception("deployment lookup failed")
                current = None
            if current is None:
                return errors.error_response(
                    404, f"No deployment '{deployment_id}'.",
                    "invalid_request_error", "deployment_not_found",
                )
            try:
                lifecycle = _parse_lifecycle(
                    patch, getattr(current, "lifecycle", None) or Lifecycle()
                )
            except ValueError as exc:
                return errors.error_response(
                    400, str(exc), "invalid_request_error", "invalid_request"
                )
        try:
            deployment = None
            if lifecycle is not None:
                deployment = await asyncio.to_thread(
                    set_lifecycle, deployment_id, lifecycle
                )
            if "serving" in patch and (lifecycle is None or deployment is not None):
                deployment = await asyncio.to_thread(set_serving, deployment_id, serving)
        except Exception as exc:
            log.exception("deployment update failed")
            return errors.error_response(
                400,
                f"Could not update deployment. {errors.detail(exc, _redactor())}",
                "invalid_request_error",
                "deployment_update_failed",
            )
        if deployment is None:
            return errors.error_response(
                404, f"No deployment '{deployment_id}'.",
                "invalid_request_error", "deployment_not_found",
            )
        ctx.router.rebuild(force_scores=True)
        return JSONResponse(serialize.deployment_payload(deployment))

    @router.delete("/api/deployments/{deployment_id}")
    async def delete_deployment(deployment_id: str, request: Request) -> Response:
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
        # An operator's stop is not undone at the next boot.
        set_lifecycle = getattr(ctx.deps.deployments, "set_lifecycle", None)
        current_lc = getattr(existing, "lifecycle", None)
        if callable(set_lifecycle) and current_lc is not None and current_lc.start_at_boot:
            try:
                await asyncio.to_thread(
                    set_lifecycle,
                    deployment_id,
                    dataclasses.replace(current_lc, start_at_boot=False),
                )
            except Exception:
                log.exception("clearing start_at_boot failed")
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
        audit_request(
            request, AuditAction.DEPLOYMENT_STOPPED, "deployment", deployment_id,
            before=_deployment_audit(existing),
        )
        return JSONResponse({"stopping": deployment_id})

    def _deployment_audit(dep) -> dict:
        """What an investigator needs of a deployment: what ran, where, how."""
        runtime = getattr(dep, "runtime", None)
        row = {
            "served_name": getattr(dep, "served_name", None),
            "model_id": getattr(getattr(dep, "shape", None), "model_id", None),
            "node_ids": list(getattr(getattr(dep, "plan", None), "node_ids", None) or []),
            "runtime": getattr(runtime, "value", runtime),
        }
        revision = getattr(dep, "revision", None)
        if revision:
            row["revision"] = revision
        # How the command was shaped, and what it trusted and ran (R6, SC-05):
        # an investigator reconstructing a launch needs the operator's own
        # tokens and the image as much as the model id.
        row["extra_args"] = list(getattr(dep, "extra_args", ()) or ())
        row["custom_command"] = list(getattr(dep, "custom_command", ()) or ())
        row["speculative_tokens"] = int(getattr(dep, "speculative_tokens", 0) or 0)
        row["trust_remote_code"] = bool(getattr(dep, "trust_remote_code", False))
        row["image"] = str(getattr(dep, "image", "") or "")
        return row

    @router.get("/api/deployments/{deployment_id}/logs")
    async def deployment_logs(deployment_id: str, tail: int = 500) -> Response:
        """What the launcher and the backend said, for the sheet showing it.

        The deployment inspector had every measurement a serving model
        produces and nothing at all about one that is still arriving -- and
        arriving is when somebody actually wants the log. The manager keeps
        the lines it already streams, so while a launch is in flight this is a
        read from memory and a screen may poll it; once it is over the same
        route falls back to one bounded `sparkrun logs`, which is why the
        answer says which of the two it is.

        Off the event loop: the fallback shells out and blocks by design.
        """
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
        reader = getattr(ctx.deps.deployments, "log_tail", None)
        if not callable(reader):
            # A port that cannot show a log says so, rather than an empty log
            # that reads as a backend which printed nothing.
            return JSONResponse({"lines": [], "source": "unavailable"})
        limit = max(1, min(int(tail), 2000))
        try:
            answer = await asyncio.to_thread(reader, deployment_id, limit=limit)
        except Exception as exc:
            log.exception("reading the deployment log failed")
            return errors.error_response(
                502, f"Could not read the log. {errors.detail(exc, _redactor())}",
                "server_error", "log_read_failed",
            )
        return JSONResponse(answer)

    # -- activity ----------------------------------------------------------

    # -- Hub weight prefetch: download without launching ----------------
    # deploy/prefetch.py says why it runs on the coordinator. Process-local,
    # so /api/activity reads it without fan-out.

    _PREFETCH_DEFAULT_PATTERNS = ("*.safetensors", "*.json", "*.model", "*.txt", "tokenizer*")
    _PREFETCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")

    def _prefetch_sizes(model_id: str, revision: str | None) -> dict[str, int]:
        client = getattr(ctx.deps.resolver, "client", None)
        if client is None or not hasattr(client, "model_info"):
            from control_plane.resolver.hf import HubClient

            client = HubClient()
        info = client.model_info(model_id, revision or "main")
        return dict(info.file_sizes)

    def _download_not_found(download_id: str) -> JSONResponse:
        return errors.error_response(
            404, f"No download '{download_id}' is running here.",
            "invalid_request_error", "download_not_found",
        )

    @router.post("/api/downloads")
    async def start_download(request: Request) -> JSONResponse:
        import fnmatch

        from control_plane.deploy import prefetch

        try:
            body = await read_json_object(request)
        except BodyTooLarge:
            return payload_too_large()
        except ValueError as exc:
            return errors.error_response(400, str(exc), "invalid_request_error", "invalid_request")
        model_id = body.get("model_id")
        gguf_file = body.get("gguf_file")
        revision = body.get("revision")
        problem = None
        if not isinstance(model_id, str) or not _PREFETCH_ID_RE.match(model_id.strip()):
            problem = "model_id must be a Hugging Face repository id, owner/name."
        elif gguf_file is not None and (
            not isinstance(gguf_file, str) or not gguf_file.endswith(".gguf")
            or ".." in gguf_file or gguf_file.startswith("/")
        ):
            problem = "gguf_file must be a .gguf file path inside the repository."
        elif revision is not None and (
            not isinstance(revision, str) or not re.match(r"^[A-Za-z0-9._/-]{1,96}$", revision)
        ):
            problem = "revision must be a branch, tag or commit."
        if problem:
            return errors.error_response(400, problem, "invalid_request_error", "invalid_request")
        model_id = model_id.strip()
        patterns = (gguf_file,) if gguf_file else _PREFETCH_DEFAULT_PATTERNS
        try:
            sizes = await asyncio.to_thread(_prefetch_sizes, model_id, revision)
        except Exception as exc:  # noqa: BLE001 -- the hub's sentence is the answer
            return errors.error_response(
                502, f"Could not read the file sizes for {model_id}: {exc}",
                "upstream_error", "metadata_unavailable",
            )
        total = sum(
            size for name, size in sizes.items()
            if any(fnmatch.fnmatch(name, p) for p in patterns)
        )
        try:
            job = await asyncio.to_thread(
                prefetch.default().start, model_id, revision=revision,
                patterns=patterns, total_bytes=total or None,
            )
        except ValueError as exc:
            return errors.error_response(400, str(exc), "invalid_request_error", "invalid_request")
        audit_request(
            request, AuditAction.DOWNLOAD_CHANGED, "download", job["download_id"],
            after={"op": "start", "repo_id": model_id, "patterns": list(patterns)},
        )
        return JSONResponse(job, status_code=202)

    async def _download_op(request: Request, download_id: str, op: str) -> JSONResponse:
        from control_plane.deploy import prefetch

        reg = prefetch.default()
        job = await asyncio.to_thread(getattr(reg, op), download_id)
        if job is None:
            return _download_not_found(download_id)
        audit_request(
            request, AuditAction.DOWNLOAD_CHANGED, "download", download_id,
            after={"op": op, "repo_id": job["repo_id"], "status": job["status"]},
        )
        return JSONResponse(job)

    @router.post("/api/downloads/{download_id}/pause")
    async def pause_download(download_id: str, request: Request) -> JSONResponse:
        return await _download_op(request, download_id, "pause")

    @router.post("/api/downloads/{download_id}/resume")
    async def resume_download(download_id: str, request: Request) -> JSONResponse:
        return await _download_op(request, download_id, "resume")

    @router.delete("/api/downloads/{download_id}")
    async def cancel_download(download_id: str, request: Request) -> Response:
        from control_plane.deploy import prefetch

        if not await asyncio.to_thread(prefetch.default().cancel, download_id):
            return _download_not_found(download_id)
        audit_request(
            request, AuditAction.DOWNLOAD_CHANGED, "download", download_id,
            after={"op": "cancel"},
        )
        return Response(status_code=204)

    # Copies of a cached model from the coordinator to another node, over the
    # fabric when there is one (control_plane/transfer). Process-local like the
    # prefetches above, so /api/activity lists them without fan-out. The audit
    # action is DOWNLOAD_CHANGED on purpose: a new AuditAction member would be a
    # frozen-contract edit, and "op" already says which kind of change it was.

    def _transfers_unavailable() -> JSONResponse:
        return errors.error_response(
            503, "Copies between nodes are not available on this gateway.",
            "server_error", "transfers_unavailable",
        )

    @router.post("/api/transfers")
    async def start_transfer(request: Request) -> JSONResponse:
        from control_plane.transfer.jobs import ReplicateRefused
        from control_plane.transfer.protocol import DEFAULT_STREAMS

        try:
            body = await read_json_object(request)
        except BodyTooLarge:
            return payload_too_large()
        except ValueError as exc:
            return errors.error_response(400, str(exc), "invalid_request_error", "invalid_request")
        folder = body.get("folder")
        node_id = body.get("node_id")
        streams = body.get("streams")
        if not isinstance(node_id, str) or not node_id.strip():
            return errors.error_response(
                400, "node_id is required.", "invalid_request_error", "invalid_request"
            )
        if ctx.deps.transfers is None:
            return _transfers_unavailable()
        try:
            job = await asyncio.to_thread(
                ctx.deps.transfers.start, folder, node_id.strip(),
                streams=streams or DEFAULT_STREAMS,
            )
        except ReplicateRefused as exc:
            return errors.error_response(exc.status, exc.sentence, "invalid_request_error", exc.code)
        audit_request(
            request, AuditAction.DOWNLOAD_CHANGED, "transfer", job["transfer_id"],
            after={"op": "transfer_start", "folder": folder, "node_id": node_id.strip(),
                   "streams": job["streams"]},
        )
        return JSONResponse(job, status_code=202)

    @router.get("/api/transfers")
    async def list_transfers() -> JSONResponse:
        if ctx.deps.transfers is None:
            return JSONResponse({"transfers": []})
        return JSONResponse({"transfers": await asyncio.to_thread(ctx.deps.transfers.list)})

    @router.delete("/api/transfers/{transfer_id}")
    async def cancel_transfer(transfer_id: str, request: Request) -> Response:
        if ctx.deps.transfers is None:
            return _transfers_unavailable()
        from control_plane.transfer.jobs import CancelRefused

        try:
            cancelled = await asyncio.to_thread(ctx.deps.transfers.cancel, transfer_id)
        except CancelRefused as exc:
            return errors.error_response(
                409, exc.sentence, "invalid_request_error", "transfer_verifying"
            )
        if not cancelled:
            return errors.error_response(
                404, f"No copy '{transfer_id}' is running here.",
                "invalid_request_error", "transfer_not_found",
            )
        audit_request(
            request, AuditAction.DOWNLOAD_CHANGED, "transfer", transfer_id,
            after={"op": "transfer_cancel"},
        )
        return Response(status_code=204)

    # Background copies (control_plane/transfer/replication.py). The monitor
    # PROPOSES; nothing is copied or deleted until one of these approvals,
    # which must name the exact deletions the operator was shown.

    def _replication_unavailable() -> JSONResponse:
        return errors.error_response(
            503, "Background copies are not available on this gateway.",
            "server_error", "replication_unavailable",
        )

    def _replication_pair(body: dict) -> tuple[str, str] | JSONResponse:
        folder, node_id = body.get("folder"), body.get("node_id")
        if not isinstance(folder, str) or not folder or not isinstance(node_id, str) or not node_id:
            return errors.error_response(
                400, "folder and node_id are required.", "invalid_request_error", "invalid_request"
            )
        return folder, node_id

    def _kick_replication() -> None:
        async def run() -> None:
            try:
                await ctx.replication.tick()
            except Exception:
                log.exception("replication tick after an approval failed")

        asyncio.get_running_loop().create_task(run())

    @router.get("/api/replication")
    async def replication_state() -> JSONResponse:
        if ctx.replication is None:
            return _replication_unavailable()
        return JSONResponse(await asyncio.to_thread(ctx.replication.state))

    @router.post("/api/replication/approve")
    async def approve_replication(request: Request) -> JSONResponse:
        from control_plane.transfer.replication import PlanChanged, ProposalNotFound

        if ctx.replication is None:
            return _replication_unavailable()
        try:
            body = await read_json_object(request)
        except BodyTooLarge:
            return payload_too_large()
        except ValueError as exc:
            return errors.error_response(400, str(exc), "invalid_request_error", "invalid_request")
        pair = _replication_pair(body)
        if isinstance(pair, JSONResponse):
            return pair
        evict = body.get("evict", [])
        if not isinstance(evict, list) or not all(isinstance(e, str) for e in evict):
            return errors.error_response(
                400, "evict must be the list of folders you were shown, [] for none.",
                "invalid_request_error", "invalid_request",
            )
        try:
            row = await asyncio.to_thread(ctx.replication.approve, pair[0], pair[1], evict)
        except ProposalNotFound as exc:
            return errors.error_response(
                404, str(exc), "invalid_request_error", "replication_proposal_not_found"
            )
        except PlanChanged as exc:
            return errors.error_response(
                409, str(exc), "invalid_request_error", "replication_plan_changed"
            )
        audit_request(
            request, AuditAction.DOWNLOAD_CHANGED, "replica", f"{pair[1]}/{pair[0]}",
            after={"op": "replica_approve", "folder": pair[0], "node_id": pair[1],
                   "evict": sorted(evict), "bytes": row["bytes"]},
        )
        _kick_replication()
        return JSONResponse(row)

    @router.post("/api/replication/dismiss")
    async def dismiss_replication(request: Request) -> Response:
        if ctx.replication is None:
            return _replication_unavailable()
        try:
            body = await read_json_object(request)
        except BodyTooLarge:
            return payload_too_large()
        except ValueError as exc:
            return errors.error_response(400, str(exc), "invalid_request_error", "invalid_request")
        pair = _replication_pair(body)
        if isinstance(pair, JSONResponse):
            return pair
        if not await asyncio.to_thread(ctx.replication.dismiss, pair[0], pair[1]):
            return errors.error_response(
                404, f"No proposal to copy {pair[0]} to {pair[1]}.",
                "invalid_request_error", "replication_proposal_not_found",
            )
        audit_request(
            request, AuditAction.DOWNLOAD_CHANGED, "replica", f"{pair[1]}/{pair[0]}",
            after={"op": "replica_dismiss", "folder": pair[0], "node_id": pair[1]},
        )
        return Response(status_code=204)

    @router.get("/api/activity")
    async def activity() -> JSONResponse:
        """What is arriving: transfers in flight, and models still starting.

        Two things the product could previously only report as silence. A pull
        was one number in a 202 followed by minutes of nothing, and a launch is
        up to READY_TIMEOUT_S of `launching` with no number attached -- while
        the rail beside it said "Nothing is being served" three times over.

        A separate endpoint rather than a field on the metrics frame: section
        4.8 fixes that payload and the UI codes against it, so this follows the
        rule the history surface already established a few lines below.

        Cheap on purpose, because the UI polls it every couple of seconds: a
        process-local dict and the in-memory deployment list. No fan-out to the
        node agents and no syscalls -- which is exactly why /api/storage, which
        does both, is polled at thirty seconds instead.
        """
        now = time.time()

        downloads = [
            {
                "pull_id": rec.pull_id,
                "provider_id": rec.provider_id,
                "provider": rec.provider,
                "model": rec.model,
                "completed": rec.completed,
                "total": rec.total,
                "status": rec.status,
                "error": rec.error,
                "done": rec.finished_at is not None,
                "kind": "provider",
                "rate_bps": None,
                "eta_s": None,
            }
            for rec in _sweep_downloads(now)
        ]
        # Hub prefetches, in the same row shape plus their own keys. Imported
        # lazily: the deploy package is the coordinator's, and a gateway built
        # without it must still answer this route.
        try:
            from control_plane.deploy import prefetch as _prefetch

            hub_jobs = _prefetch.default().list()
        except Exception:
            log.exception("reading hub prefetches failed while reading activity")
            hub_jobs = []
        downloads.extend(
            {
                "pull_id": job["download_id"],
                "provider_id": None,
                "provider": "Hugging Face",
                "model": job["repo_id"],
                "completed": job["completed"],
                "total": job["total"],
                "status": job["status"],
                "error": job["error"],
                "done": job["status"] == "done",
                "kind": "hub",
                "download_id": job["download_id"],
                "paused": job["status"] == "paused",
                "rate_bps": job.get("rate_bps"),
                "eta_s": job.get("eta_s"),
            }
            for job in hub_jobs
        )
        # Copies to other nodes, in the same row shape.
        if ctx.deps.transfers is not None:
            try:
                copies = ctx.deps.transfers.list()
            except Exception:
                log.exception("reading copies between nodes failed while reading activity")
                copies = []
            # The link the copy chose, as it describes itself ("RoCE, 2 × 200 Gb/s").
            downloads.extend(
                {
                    "pull_id": t["transfer_id"],
                    "provider_id": None,
                    "provider": f"copy to {t['node_id']}" + (f" over {t['link']}" if t.get("link") else ""),
                    "model": t["repo_id"],
                    "completed": t["completed"],
                    "total": t["total"],
                    "status": t["status"],
                    "error": t["error"],
                    "done": t["status"] == "done",
                    "kind": "transfer",
                    "transfer_id": t["transfer_id"],
                    "rate_bps": t["rate_bps"],
                    "eta_s": t["eta_s"],
                }
                for t in copies
            )

        try:
            deployments = ctx.deps.deployments.list()
        except Exception:
            log.exception("deployment listing failed while reading activity")
            deployments = []

        # What each launch is actually doing, read off sparkrun's output and
        # the backend's own log by the deployment manager. Through a getattr
        # like `handles` below it, because DeploymentPort is frozen at
        # launch/stop/list/get: a port without this reports no phase, which is
        # exactly what the screen drew before there was one.
        reader = getattr(ctx.deps.deployments, "progress", None)
        phases: dict[str, dict] = {}
        if callable(reader):
            try:
                phases = reader() or {}
            except Exception:
                log.exception("reading launch progress failed")

        launches = []
        live_ids = set()
        for d in deployments:
            # An allowlist, not "everything that is not terminal". Written the
            # other way round it let DEGRADED through, which is a model that is
            # up and serving badly -- the plan and routing sections beside this
            # one already describe it, and calling it activity would report a
            # live deployment as one that had not arrived yet. STOPPING is the
            # mirror of the same mistake: it is leaving, not arriving.
            if d.state not in _ARRIVING_STATES:
                continue
            live_ids.add(d.deployment_id)
            since = _LAUNCH_SEEN.setdefault(d.deployment_id, now)
            phase = phases.get(d.deployment_id) or {}
            launches.append(
                {
                    "deployment_id": d.deployment_id,
                    "served_name": d.served_name,
                    "model_id": d.shape.model_id if d.shape else None,
                    "runtime": d.runtime,
                    "state": d.state.value,
                    "node_ids": list(d.plan.node_ids) if d.plan else [],
                    "since": since,
                    # Which of the four slow things it is on, and the sentence
                    # whoever is doing it printed about it. Null when nothing
                    # has been read yet -- an unstarted launch, or a runtime
                    # whose log says nothing this build recognises.
                    "phase": phase.get("phase"),
                    "status": phase.get("status") or "",
                    # A real fraction or nothing. The checkpoint-shard loader
                    # counts its own shards; no other part of a launch reports
                    # a denominator, and none is invented for them.
                    "fraction": phase.get("fraction"),
                    # Seconds left, as the downloader or the checkpoint loader
                    # estimated it about itself. Null for every step that
                    # counts nothing, and never filled in from a rate computed
                    # here -- an estimate a person can plan around has to come
                    # from the thing being estimated.
                    "eta_s": phase.get("eta_s"),
                    # The runtime said it was dying. The manager fails the
                    # launch on this too, so a row carrying it is one caught
                    # between the reading and the transition -- worth drawing
                    # as a fault for that moment rather than as progress.
                    "fatal": bool(phase.get("fatal")),
                    # Whatever the manager last recorded. Usually None while a
                    # launch is healthy; the health probe's per-poll reason is
                    # local to _wait_for_ready and never reaches the record.
                    "last_error": d.last_error,
                }
            )
        for stale in set(_LAUNCH_SEEN) - live_ids:
            del _LAUNCH_SEEN[stale]

        launches.sort(key=lambda item: item["since"])
        body: dict = {"downloads": downloads, "launches": launches}
        if ctx.replication is not None:
            try:
                body["replication"] = {"awaiting": await asyncio.to_thread(ctx.replication.awaiting)}
            except Exception:
                log.exception("reading background copy proposals failed while reading activity")
        return JSONResponse(body)

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
    # and the UI codes against it; a history surface belongs beside it, not
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
        exclude: str | None = None,
        from_: str = Query("", alias="from"),
        to: str = "",
        limit: int = 500,
    ) -> Response:
        # `exclude` defaults to whatever the log handler declines to record, so
        # a caller gets the same answer either side of the change that started
        # dropping them -- a window from last week reads like a window from
        # today, without every client having to carry the list. `?exclude=`
        # (present, empty) asks for everything, including the access lines
        # still sitting in the archive from before.
        prefixes = (
            tquery.config.quiet_loggers()
            if exclude is None
            else tuple(p.strip() for p in exclude.split(",") if p.strip())
        )
        return await _history(
            tquery.logs,
            level=level,
            logger=logger,
            q=q,
            node_id=node_id,
            exclude=prefixes,
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
                res = await client.get(
                    f"{agent_url.rstrip('/')}/agent/storage",
                    headers=_agent_headers(node_id),
                )
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
                res = await client.get(
                    f"{agent_url.rstrip('/')}/agent/models/cache",
                    headers=_agent_headers(node_id),
                )
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

    async def _agent_copy_receiver(node_id: str, agent_url: str) -> dict:
        """Whether a node can take a copy, read before anybody clicks one.

        `available` False carries the sentence the copy would have failed with:
        an agent with no capacity route predates copies (404), a 503 is the
        agent's own refusal (no receiver on 47101, no cache folder), and a
        transport error is that error. `free` is the cache filesystem's free
        bytes when the agent could say. Never raises.
        """
        import httpx

        def _no(reason: str) -> dict:
            return {"available": False, "reason": reason, "free": None}

        try:
            async with httpx.AsyncClient(timeout=_AGENT_TIMEOUT_S) as client:
                res = await client.get(
                    f"{agent_url.rstrip('/')}/agent/transfers/capacity",
                    headers=_agent_headers(node_id),
                )
        except Exception as exc:
            log.warning("copy capacity read failed for %s: %s", node_id, exc)
            return _no(f"{node_id} did not answer: {errors.detail(exc, _redactor())}")
        if res.status_code == 404:
            return _no(
                f"{node_id} runs an older build without the copy receiver; upgrade it"
            )
        if res.status_code != 200:
            try:
                detail = res.json().get("detail")
            except Exception:
                detail = None
            if not isinstance(detail, str) or not detail:
                detail = f"its agent answered HTTP {res.status_code}"
            return _no(f"{node_id}: {detail}")
        try:
            free = res.json().get("free")
        except Exception as exc:
            return _no(f"{node_id} did not answer: {errors.detail(exc, _redactor())}")
        return {
            "available": True,
            "reason": None,
            "free": free if isinstance(free, int) else None,
        }

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
        receivers = list(
            await asyncio.gather(*(_agent_copy_receiver(n, u) for n, u in targets))
        )
        for row, cache, receiver in zip(measured, caches, receivers):
            row["models"] = cache
            row["copy_receiver"] = receiver
        for row in no_agent:
            row["copy_receiver"] = {
                "available": False,
                "reason": f"No agent URL for node '{row['node_id']}'.",
                "free": None,
            }
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
            # The effective value, not the compiled-in default: this screen is
            # where an operator checks whether DERATE_TELEMETRY_MAX_BYTES took.
            "journal_max_bytes": cfg.journal_max_bytes(),
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

        body = {
            "nodes": node_rows,
            "telemetry": status,
            "retention": retention,
            "measured_at": time.time(),
        }

        # Hand the weights half to the model registry, which has no fan-out of
        # its own on purpose: walking every node's cache is the expensive part
        # of this endpoint, and scheduling a second cluster-wide walk to
        # populate /api/models would double it for the same answer. The
        # direction is one-way -- storage feeds the registry, never the
        # reverse -- or a registry refreshed on a timer would start answering
        # this screen with a reading older than the one it just took.
        #
        # Guarded, and deliberately after `body` is built: a registry fault
        # must never turn the Storage tab into a 500 over a view that can be
        # rebuilt from stores that are all still readable.
        inventory = getattr(ctx, "inventory", None)
        if inventory is not None:
            try:
                await asyncio.to_thread(inventory.refresh_cache, body)
                keep = {n["node_id"] for n in node_rows}
                if keep:
                    # Only with a roster we actually read. If list_nodes()
                    # raised above, `nodes` is empty and every node would look
                    # departed -- wiping the disk picture for the cluster over
                    # one registry hiccup.
                    await asyncio.to_thread(inventory.drop_nodes, keep)
            except Exception:
                log.exception("model registry could not take the storage reading")

        return JSONResponse(body)

    @router.delete("/api/storage/nodes/{node_id}/models/{folder}")
    async def delete_cached_model(node_id: str, folder: str, request: Request) -> Response:
        """Delete one downloaded repository from one node.

        The largest reclaim in the product by orders of magnitude -- a single
        120B repository is 182 GiB -- and the only one that can break a
        running deployment, so the in-use check is here rather than on the
        agent. The agent has no idea what a deployment is; this is the same
        split, and the same 409, that killing a GPU process already uses.
        """
        # One cache folder, by name, before anything is relayed (E2): a
        # decoded `?`, `#` or `/` here would make the agent act on a different
        # path than the one the audit row records.
        if not _CACHE_FOLDER.fullmatch(folder):
            return errors.error_response(
                400,
                "folder must be a single models--<org>--<name> cache folder.",
                "invalid_request_error",
                "invalid_folder",
            )
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

        token = _agent_token(node_id)
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
                    f"{agent_url.rstrip('/')}/agent/models/cache/{quote(folder, safe='')}",
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
        answer = res.json()
        audit_request(
            request, AuditAction.STORAGE_DELETED, "model_cache", f"{node_id}/{folder}",
            before={"node_id": node_id, "folder": folder},
            after=answer if isinstance(answer, dict) else None,
        )
        return JSONResponse(answer)

    @router.delete("/api/storage/cache/resolver")
    async def clear_resolver_cache(request: Request) -> Response:
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
        freed = max(0, before - after)
        audit_request(
            request, AuditAction.STORAGE_DELETED, "resolver_cache",
            str(directory) if directory is not None else "-",
            after={"bytes_freed": freed},
        )
        return JSONResponse({"cleared": True, "bytes_freed": freed})

    return router
