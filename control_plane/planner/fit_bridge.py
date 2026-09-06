"""Capacity questions the planner asks, answered by Agent D when D exists.

The planner needs exactly two numbers from the fit calculator:

    min_nodes_required(shape, profile, context, max_seqs) -> int
    kv_bytes_per_token(shape, kv_dtype) -> float

Agent D owns both and exports them from ``control_plane.fit``. Until that lands,
this module answers them itself, using the memory budget from the fit
specification so the two implementations agree on method even before they agree
on the last byte.

The fallback is deliberately conservative: it may ask for one node more than
strictly necessary, never one fewer. Over-provisioning produces a slower plan;
under-provisioning produces an OOM five minutes into a model load.

This lives in the planner's own package rather than in ``control_plane/fit/``
because that path belongs to Agent D. When D's module appears it is imported and
wins; nothing here is edited.
"""

from __future__ import annotations

import logging
import math
from typing import Protocol

from control_plane.contracts import (
    COMM_BUFFER_BYTES,
    EP_EXTRA_BUFFER_BYTES,
    FRAMEWORK_OVERHEAD,
    ModelShape,
    NodeProfile,
)

from .constants import DEFAULT_KV_DTYPE, MAX_NODES_CONSIDERED

log = logging.getLogger(__name__)

# Bytes per KV element. Agent C owns the weight quantization table; KV cache
# dtype is a separate axis and a much shorter list.
_KV_ELEM_BYTES: dict[str, float] = {
    "fp32": 4.0,
    "fp16": 2.0,
    "bf16": 2.0,
    "fp8": 1.0,
    "int8": 1.0,
}

# Prefill chunk used to size compute scratch. Agent D owns the authoritative
# value; 2048 is the figure the fit specification names.
_ACTIVATION_CHUNK_TOKENS = 2048


class FitHelpers(Protocol):
    """The slice of Agent D the planner depends on."""

    def min_nodes_required(
        self,
        shape: ModelShape,
        profile: NodeProfile,
        context: int,
        max_seqs: int,
        kv_dtype: str = DEFAULT_KV_DTYPE,
    ) -> int: ...

    def kv_bytes_per_token(self, shape: ModelShape, kv_dtype: str) -> float: ...


def kv_elem_bytes(kv_dtype: str) -> float:
    return _KV_ELEM_BYTES.get(kv_dtype.lower(), 2.0)


def kv_bytes_per_token(shape: ModelShape, kv_dtype: str = DEFAULT_KV_DTYPE) -> float:
    """KV bytes for one token across all layers, before any sharding.

    Uses ``num_kv_heads``, not ``num_attention_heads``. Under GQA those differ by
    the grouping ratio -- 8x on Llama 3.3 70B -- and using the query head count
    is the most common over-estimate in this field.

    For a sliding-window model this returns the marginal cost of one more token
    of context, which is the full-attention layers only: windowed layers stop
    growing once the window is full. The windowed layers' fixed cost is charged
    by ``kv_total_bytes``, which knows the context length.
    """
    elem = kv_elem_bytes(kv_dtype)

    if shape.mla_latent_dim:
        # Multi-head latent attention caches one compressed vector per layer per
        # token, plus the decoupled RoPE component cached alongside it -- never
        # the latent alone. Dropping the RoPE half under-counts a DeepSeek-family
        # cache by roughly 11 percent, in the OOM direction.
        return (
            shape.num_layers
            * (shape.mla_latent_dim + shape.effective_mla_rope_dim)
            * elem
        )

    per_layer = 2 * shape.num_kv_heads * shape.effective_head_dim * elem
    if shape.sliding_window and shape.layers_with_full_attention is not None:
        return per_layer * shape.layers_with_full_attention
    return per_layer * shape.num_layers


def kv_total_bytes(
    shape: ModelShape, kv_dtype: str, context: int, batch: int
) -> float:
    """Total KV cache for ``batch`` sequences at ``context`` tokens, unsharded."""
    elem = kv_elem_bytes(kv_dtype)

    if shape.mla_latent_dim:
        # Same correction as kv_bytes_per_token: latent plus decoupled RoPE,
        # never the latent alone.
        per_token = (
            shape.num_layers
            * (shape.mla_latent_dim + shape.effective_mla_rope_dim)
            * elem
        )
        return per_token * context * batch

    per_layer_per_token = 2 * shape.num_kv_heads * shape.effective_head_dim * elem

    if shape.sliding_window and shape.layers_with_full_attention is not None:
        full = shape.layers_with_full_attention
        windowed = max(0, shape.num_layers - full)
        # Full layers cache the whole context; windowed layers cache the window.
        # Treating a windowed model as fully cached over-estimates by multiples.
        layer_tokens = full * context + windowed * min(shape.sliding_window, context)
        return per_layer_per_token * layer_tokens * batch

    return per_layer_per_token * shape.num_layers * context * batch


def activation_bytes(shape: ModelShape, batch: int) -> float:
    """Logits buffer plus compute scratch. Replicated on every node."""
    logits = shape.vocab_size * batch * 4
    scratch = _ACTIVATION_CHUNK_TOKENS * shape.hidden_size * 4 * 6
    return logits + scratch


def min_nodes_required(
    shape: ModelShape,
    profile: NodeProfile,
    context: int,
    max_seqs: int,
    kv_dtype: str = DEFAULT_KV_DTYPE,
) -> int:
    """Fewest nodes of this profile that can hold the model at this workload.

    Walks the node count upward and charges the same budget the fit calculator
    charges, with one deliberate simplification: rather than searching TP and PP
    shards separately the way the real calculator does, every candidate ``n`` is
    charged as the real calculator charges its *busiest pipeline stage* --
    ``ceil(num_layers / n) / num_layers`` of the shardable weights and KV,
    never a flat ``1/n``. An even divide is optimistic whenever the layer count
    does not divide evenly by ``n`` (80 layers over 3 nodes is 27/27/26, and the
    node holding 27 is the one that OOMs); charging the ceil'd fraction is what
    keeps this fallback's own docstring promise -- it may ask for one node more
    than strictly necessary, never one fewer. Activations and framework
    overhead are replicated, and communication buffers only appear once
    anything is sharded at all. Returns ``MAX_NODES_CONSIDERED`` when nothing in
    range works, which the caller surfaces rather than silently truncating.
    """
    usable = profile.usable_memory()
    weights = shape.total_params * shape.bytes_per_param()
    replicated = shape.vision_params * shape.bytes_per_param()
    kv = kv_total_bytes(shape, kv_dtype, context, max_seqs)
    activations = activation_bytes(shape, max_seqs)
    layers = max(1, shape.num_layers)

    for n in range(1, MAX_NODES_CONSIDERED + 1):
        if n == 1:
            comm = 0.0
            stage_fraction = 1.0
        else:
            comm = float(COMM_BUFFER_BYTES)
            # Expert-parallel staging is the term other planners forget, and
            # forgetting it is what produces a config that passes a fit check
            # and then OOMs on the first batch. Charge it whenever a MoE model
            # is sharded at all.
            if shape.is_moe:
                comm += float(EP_EXTRA_BUFFER_BYTES)
            stage_fraction = math.ceil(layers / n) / layers

        needed = (
            (weights + kv) * stage_fraction
            + activations
            + replicated
            + comm
            + FRAMEWORK_OVERHEAD
        )
        if needed <= usable:
            return n

    return MAX_NODES_CONSIDERED


class _FallbackFit:
    """Object form of the functions above, matching the ``FitHelpers`` protocol."""

    source = "planner-fallback"

    min_nodes_required = staticmethod(min_nodes_required)
    kv_bytes_per_token = staticmethod(kv_bytes_per_token)


def default_fit_helpers() -> FitHelpers:
    """Agent D's helpers if they exist, the conservative fallback otherwise.

    Imported lazily and by name so that the planner does not hard-depend on a
    module that may land after it, and so that a partially written fit package
    does not take the planner down with it. Either way, swapping in the
    fallback changes the capacity arithmetic a plan's reason and warnings rest
    on, so it is never silent: falling back logs a warning naming why, at
    import time, once per process -- not buried behind a passed exception a
    reader would have to go looking for.
    """
    try:
        from control_plane import fit as _fit  # noqa: PLC0415

        if hasattr(_fit, "min_nodes_required") and hasattr(_fit, "kv_bytes_per_token"):
            return _fit  # type: ignore[return-value]
        log.warning(
            "control_plane.fit is importable but does not export "
            "min_nodes_required/kv_bytes_per_token; planner falling back to "
            "its own conservative capacity arithmetic (source=%r)",
            _FallbackFit.source,
        )
    except Exception:
        log.warning(
            "control_plane.fit is not importable; planner falling back to its "
            "own conservative capacity arithmetic (source=%r) instead of "
            "Agent D's calculator",
            _FallbackFit.source,
            exc_info=True,
        )
    return _FallbackFit()
