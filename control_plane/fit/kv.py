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

from .constants import KV_ELEM_BYTES, KV_FALLBACK_DTYPE


def kv_elem_bytes(kv_dtype: str, shape: ModelShape | None = None) -> float:
    """Bytes per cached element.

    ``auto`` follows the model's own dtype when that dtype is something a cache
    can actually be stored in, and falls back to bf16 otherwise: a model whose
    *weights* are mxfp4 still caches in bf16 or fp8.
    """
    key = (kv_dtype or "auto").strip().lower()
    if key in ("auto", "", "none", "model"):
        if shape is not None and shape.dtype.lower() in KV_ELEM_BYTES:
            return KV_ELEM_BYTES[shape.dtype.lower()]
        return KV_ELEM_BYTES[KV_FALLBACK_DTYPE]
    return KV_ELEM_BYTES.get(key, KV_ELEM_BYTES[KV_FALLBACK_DTYPE])


def is_known_kv_dtype(kv_dtype: str) -> bool:
    key = (kv_dtype or "auto").strip().lower()
    return key in ("auto", "", "none", "model") or key in KV_ELEM_BYTES


def per_layer_per_token_bytes(shape: ModelShape, kv_dtype: str) -> float:
    """One layer, one token, one sequence, before sharding."""
    elem = kv_elem_bytes(kv_dtype, shape)
    if shape.mla_latent_dim:
        # One compressed latent, not per-head K and V.
        return shape.mla_latent_dim * elem
    return 2 * shape.num_kv_heads * shape.effective_head_dim * elem


def kv_bytes_per_token(shape: ModelShape, kv_dtype: str) -> float:
    """Bytes one token of one sequence adds to the cache, across all layers,
    before sharding.

    This is the full-attention rate. A sliding window caps the *total*, not the
    rate, so a windowed model's real cache is at or below this figure times the
    context. Use :func:`kv_cache_bytes` when you need the real total.

    Exported for Agent E.
    """
    return shape.num_layers * per_layer_per_token_bytes(shape, kv_dtype)


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
    """Total KV cache for the whole model, before sharding."""
    return (
        per_layer_per_token_bytes(shape, kv_dtype)
        * cached_layer_tokens(shape, context)
        * max(0, int(batch))
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
