"""KV cache sizing.

The one calculation everything else in this component leans on, and the one the
field most often gets wrong. Three rules, in order of how much damage getting
them wrong does:

1. ``num_kv_heads``, never ``num_attention_heads``. Llama 3.3 70B has 64 query
   heads over 8 KV heads; the mistake over-charges by exactly 8x.
2. A sliding-window layer caches the window, not the context. GPT-OSS-120B
   windows half its layers at 128 tokens; treating it as fully cached
   over-charges by multiples at long context.
3. Multi-head latent attention caches one compressed vector per layer per
   token, not per-head K and V. DeepSeek-V3 is an order of magnitude cheaper
   than its head count suggests.
"""

from __future__ import annotations

import math

from control_plane.contracts import ModelShape, ParallelismPlan

from .constants import (
    KV_DS_MLA_BYTES,
    KV_ELEM_BYTES,
    KV_FALLBACK_DTYPE,
    KV_NVFP4_BLOCK,
    KV_NVFP4_DTYPES,
    KV_PER_TOKEN_HEAD_BITS,
    KV_PER_TOKEN_HEAD_SCALE_BYTES,
    KV_TURBOQUANT_BITS,
    KV_TURBOQUANT_BOUNDARY_LAYERS,
    KV_TURBOQUANT_KEY_NORM_BYTES,
    KV_TURBOQUANT_VALUE_META_BYTES,
)

_MODEL_OWN = ("auto", "", "none", "model")

#: Every packed layout `per_layer_per_token_bytes` prices.
PACKED_KV_DTYPES: frozenset[str] = frozenset(
    set(KV_PER_TOKEN_HEAD_BITS) | KV_NVFP4_DTYPES | set(KV_TURBOQUANT_BITS)
    | set(KV_DS_MLA_BYTES)
)


def is_packed_kv_dtype(kv_dtype: str | None) -> bool:
    """Whether *kv_dtype* is one of the packed layouts (per-token-head
    scales, nvfp4, TurboQuant, DeepSeek MLA) rather than a plain width."""
    return _key(kv_dtype) in PACKED_KV_DTYPES


def _key(kv_dtype: str | None) -> str:
    return (kv_dtype or "auto").strip().lower()


def kv_elem_bytes(kv_dtype: str, shape: ModelShape | None = None) -> float:
    """Bytes per cached element.

    ``auto`` follows the model's own dtype when that dtype is something a cache
    can actually be stored in, and falls back to bf16 otherwise: a model whose
    *weights* are mxfp4 still caches in bf16 or fp8.
    """
    key = _key(kv_dtype)
    if key in _MODEL_OWN:
        if shape is not None and shape.dtype.lower() in KV_ELEM_BYTES:
            return KV_ELEM_BYTES[shape.dtype.lower()]
        return KV_ELEM_BYTES[KV_FALLBACK_DTYPE]
    return KV_ELEM_BYTES.get(key, KV_ELEM_BYTES[KV_FALLBACK_DTYPE])


def is_known_kv_dtype(kv_dtype: str) -> bool:
    key = _key(kv_dtype)
    return key in _MODEL_OWN or key in KV_ELEM_BYTES or key in PACKED_KV_DTYPES


def kv_dtype_refusal(shape: ModelShape, kv_dtype: str) -> str | None:
    """Why *kv_dtype* cannot be priced for this model, or None.

    A packed layout is a kernel for one attention geometry. Asked of the other
    one, the gate would have to guess at a page the image either never builds
    or builds in a way nothing here derived -- and the gate does not approve
    what it cannot budget.
    """
    key = _key(kv_dtype)
    name = shape.model_id
    if key in KV_DS_MLA_BYTES and not shape.mla_latent_dim:
        return (
            f"kv_dtype {key!r} is a cache layout for multi-head latent attention, "
            f"and {name} has none; use fp8, int8_per_token_head or nvfp4 for "
            f"per-head K and V"
        )
    if shape.mla_latent_dim and (key in KV_NVFP4_DTYPES or key in KV_TURBOQUANT_BITS):
        return (
            f"kv_dtype {key!r} packs per-head K and V, and {name} caches one "
            f"latent per token (multi-head latent attention); the latent forms "
            f"this image has are fp8_ds_mla, nvfp4_ds_mla and the "
            f"*_per_token_head widths"
        )
    if key in KV_TURBOQUANT_BITS and shape.sliding_window:
        return (
            f"kv_dtype {key!r} keeps the first and last "
            f"{KV_TURBOQUANT_BOUNDARY_LAYERS} layers at the model's own width, "
            f"and next to {name}'s sliding-window layers the image pads those "
            f"pages to a shared size derate does not price; refused rather "
            f"than estimated"
        )
    return None


def skipped_layers(shape: ModelShape, kv_dtype: str) -> int:
    """Layers the image stores at the model's own width despite *kv_dtype*:
    TurboQuant's first and last ``KV_TURBOQUANT_BOUNDARY_LAYERS``, and none
    for every other width."""
    if _key(kv_dtype) not in KV_TURBOQUANT_BITS:
        return 0
    layers = shape.num_layers
    n = min(KV_TURBOQUANT_BOUNDARY_LAYERS, layers // 2)
    return len(set(range(n)) | set(range(layers - n, layers)))


def _turboquant_slot_bytes(key_bits: int, value_bits: int, head_dim: int) -> int:
    """K+V bytes for one head of one token (`TurboQuantConfig.slot_size_aligned`)."""
    if key_bits == 8:
        key = head_dim
    else:
        key = math.ceil(head_dim * key_bits / 8) + KV_TURBOQUANT_KEY_NORM_BYTES
    value = math.ceil(head_dim * value_bits / 8) + KV_TURBOQUANT_VALUE_META_BYTES
    slot = key + value
    return slot + slot % 2


def per_layer_per_token_bytes(shape: ModelShape, kv_dtype: str) -> float:
    """One quantized layer, one token, one sequence, before sharding.

    A layer :func:`skipped_layers` counts is charged at the model's own width
    instead; :func:`kv_bytes_per_token` and :func:`kv_cache_bytes` add that
    difference.
    """
    key = _key(kv_dtype)
    mla = (shape.mla_latent_dim + shape.effective_mla_rope_dim) if shape.mla_latent_dim else 0
    if key in KV_DS_MLA_BYTES and mla:
        return float(KV_DS_MLA_BYTES[key])
    if key in KV_PER_TOKEN_HEAD_BITS:
        bits = KV_PER_TOKEN_HEAD_BITS[key]
        if mla:
            # One slot holding the latent, then the scale pair.
            data = mla // 2 if bits == 4 else mla
            return float(data + KV_PER_TOKEN_HEAD_SCALE_BYTES)
        hd = shape.effective_head_dim
        data = 2 * (hd // 2 if bits == 4 else hd)
        return float(shape.num_kv_heads * (data + KV_PER_TOKEN_HEAD_SCALE_BYTES))
    if key in KV_NVFP4_DTYPES and not mla:
        hd = shape.effective_head_dim
        return float(2 * shape.num_kv_heads * (hd // 2 + hd // KV_NVFP4_BLOCK))
    if key in KV_TURBOQUANT_BITS and not mla:
        key_bits, value_bits = KV_TURBOQUANT_BITS[key]
        slot = _turboquant_slot_bytes(key_bits, value_bits, shape.effective_head_dim)
        return float(shape.num_kv_heads * slot)
    elem = kv_elem_bytes(kv_dtype, shape)
    if mla:
        # One compressed latent plus the decoupled RoPE component cached
        # alongside it, not per-head K and V. The RoPE half is not optional:
        # dropping it under-counts DeepSeek-family caches by about 11 percent,
        # in the OOM direction.
        return mla * elem
    return 2 * shape.num_kv_heads * shape.effective_head_dim * elem


def _skipped_layer_extra(shape: ModelShape, kv_dtype: str) -> float:
    """What one skipped layer costs per token above a quantized one."""
    return max(
        0.0,
        per_layer_per_token_bytes(shape, "auto") - per_layer_per_token_bytes(shape, kv_dtype),
    )


def kv_bytes_per_token(shape: ModelShape, kv_dtype: str) -> float:
    """Bytes one token of one sequence adds to the cache, across all layers,
    before sharding.

    This is the full-attention rate. A sliding window caps the *total*, not the
    rate, so a windowed model's real cache is at or below this figure times the
    context. Use :func:`kv_cache_bytes` when you need the real total.

    Exported for the planner.
    """
    return (
        shape.num_layers * per_layer_per_token_bytes(shape, kv_dtype)
        + skipped_layers(shape, kv_dtype) * _skipped_layer_extra(shape, kv_dtype)
    )


def cached_layer_tokens(shape: ModelShape, context: int) -> int:
    """Sum over layers of the tokens each layer actually caches.

    ``num_layers * context`` for a normal model. For an interleaved
    sliding-window model, full layers cache the context and windowed layers
    cache the window.
    """
    context = max(0, int(context))
    if shape.sliding_window and shape.layers_with_full_attention is not None:
        full = min(max(0, shape.layers_with_full_attention), shape.num_layers)
        windowed = shape.num_layers - full
        return full * context + windowed * min(shape.sliding_window, context)
    if shape.sliding_window:
        # Window set but no full-attention count: every layer is windowed.
        return shape.num_layers * min(shape.sliding_window, context)
    return shape.num_layers * context


def kv_cache_bytes(
    shape: ModelShape, context: int, batch: int, kv_dtype: str
) -> float:
    """Total KV cache for the whole model, before sharding.

    A skipped layer is charged the whole context on top of its quantized rate,
    which is exact for a full-attention layer (TurboQuant is refused beside
    sliding-window layers, see :func:`kv_dtype_refusal`).
    """
    batch = max(0, int(batch))
    return (
        per_layer_per_token_bytes(shape, kv_dtype) * cached_layer_tokens(shape, context) * batch
        + skipped_layers(shape, kv_dtype)
        * _skipped_layer_extra(shape, kv_dtype)
        * max(0, int(context))
        * batch
    )


def kv_divisor(shape: ModelShape, plan: ParallelismPlan) -> float:
    """How much of the cache one rank holds, as a divisor.

    Tensor parallel shards by KV head; pipeline parallel shards by layer. When
    both are active the divisor is their product.

    Two corrections that keep this honest rather than optimistic:

    - TP cannot shard past ``num_kv_heads``. A runtime asked for TP=8 on a
      model with 4 KV heads replicates the heads instead of splitting them.
    - MLA caches a single latent per token, so there is nothing to split by
      head; every TP rank holds the whole thing.

    The pipeline term charges the busiest stage, matching the weights term.
    Stages are not evenly sized when the layer count does not divide.
    """
    tp = max(1, plan.tensor_parallel)
    pp = max(1, plan.pipeline_parallel)

    if shape.mla_latent_dim:
        tp_div = 1.0
    else:
        tp_div = float(min(tp, max(1, shape.num_kv_heads)))

    return tp_div / stage_fraction(shape, pp)


def kv_cache_bytes_per_rank(
    shape: ModelShape, plan: ParallelismPlan, context: int, batch: int, kv_dtype: str
) -> float:
    """:func:`kv_cache_bytes` on the busiest rank.

    ``kv_cache_bytes / kv_divisor`` for every layer that is uniform. The
    layers :func:`skipped_layers` counts are not: TurboQuant keeps them at BOTH
    ENDS of the model, so a pipeline does not spread them -- the first and
    last stages each hold two. Dividing them proportionally charged the
    busiest stage of a four-way split one wide layer instead of two, in the
    out-of-memory direction; counting every one on the busiest stage
    over-charged it. They are charged here as many as the stage holding the
    most of them actually holds (:func:`_skipped_on_busiest_stage`).
    """
    batch = max(0, int(batch))
    divisor = kv_divisor(shape, plan)
    skipped = skipped_layers(shape, kv_dtype)
    extra = _skipped_layer_extra(shape, kv_dtype) * max(0, int(context)) * batch
    uniform = kv_cache_bytes(shape, context, batch, kv_dtype) - skipped * extra
    pp = max(1, plan.pipeline_parallel)
    on_busiest = _skipped_on_busiest_stage(shape, pp) if skipped else 0
    tp_div = divisor * stage_fraction(shape, pp)
    return uniform / divisor + on_busiest * extra / tp_div


def _skipped_on_busiest_stage(shape: ModelShape, pp: int) -> int:
    """The most of :func:`skipped_layers` any one pipeline stage holds.

    Stages are contiguous runs of ``ceil(L / pp)`` layers (the last one
    shorter), and the wide layers sit at both ENDS -- so for pp > 1 the first
    stage holds the front ones and the last stage the back ones, and only a
    stage spanning both ends holds them all.
    """
    layers = shape.num_layers
    n = min(KV_TURBOQUANT_BOUNDARY_LAYERS, layers // 2)
    wide = set(range(n)) | set(range(layers - n, layers))
    per_stage = max(1, math.ceil(layers / max(1, pp)))
    return max(
        len(wide & set(range(start, min(start + per_stage, layers))))
        for start in range(0, layers, per_stage)
    )


def stage_fraction(shape: ModelShape, pipeline_parallel: int) -> float:
    """Fraction of the model's layers carried by the busiest pipeline stage.

    Pipeline splits can be uneven. 80 layers over 3 stages is 27/27/26, and the
    node that OOMs is the one holding 27.
    """
    pp = max(1, int(pipeline_parallel))
    if pp <= 1:
        return 1.0
    layers_on_busiest = math.ceil(shape.num_layers / pp)
    return layers_on_busiest / shape.num_layers
