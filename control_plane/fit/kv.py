"""KV cache sizing.

The one calculation everything else in this component leans on, and the one the
field most often gets wrong. Four rules, in order of how much damage getting
them wrong does:

1. ``num_kv_heads``, never ``num_attention_heads``. Llama 3.3 70B has 64 query
   heads over 8 KV heads; the mistake over-charges by exactly 8x.
2. A sliding-window layer caches the window, not the context. GPT-OSS-120B
   windows half its layers at 128 tokens; treating it as fully cached
   over-charges by multiples at long context.
3. Multi-head latent attention caches one compressed vector per layer per
   token, not per-head K and V. DeepSeek-V3 is an order of magnitude cheaper
   than its head count suggests.
4. A recurrent layer (linear attention, Mamba, short convolution) caches no
   tokens at all. It keeps a fixed-size state per sequence, so it is charged
   per SEQUENCE and never per token. Qwen3.8-27B is 48 such layers of 64.
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
    RECURRENT_STATES_PER_SEQUENCE,
)

#: Every spelling of "the model's own width, pass no flag". `fp16`/`bf16`/
#: `half` are derate's own words for it (`settings.default_kv_dtype`,
#: autoadopt); `float16`/`bfloat16` name a width (`gateway/kv_widths.py`).
MODEL_OWN_KV_DTYPES = ("auto", "", "none", "model", "fp16", "bf16", "half")

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


def kv_elem_bytes(kv_dtype: str) -> float:
    """Bytes per cached element.

    ``auto`` -- the model's own -- is the compute width, 16 bits, and never
    ``shape.dtype``: that is the WEIGHT scheme, and an fp8- or int8-weight
    checkpoint still caches at bfloat16 (pricing it at one byte approved twice
    the context the engine could hold). An fp32 checkpoint is downcast to 16
    bits by vLLM and SGLang, and llama.cpp's default cache is f16. A KV scheme
    the checkpoint itself declares is not ``auto`` here: `gateway/kv_widths.py`
    names it explicitly, so the gate prices it (vLLM applies it itself).
    """
    key = _key(kv_dtype)
    if key in MODEL_OWN_KV_DTYPES:
        return KV_ELEM_BYTES[KV_FALLBACK_DTYPE]
    return KV_ELEM_BYTES.get(key, KV_ELEM_BYTES[KV_FALLBACK_DTYPE])


def is_known_kv_dtype(kv_dtype: str) -> bool:
    key = _key(kv_dtype)
    return key in MODEL_OWN_KV_DTYPES or key in KV_ELEM_BYTES or key in PACKED_KV_DTYPES


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
    if key in KV_TURBOQUANT_BITS and shape.sliding_window and _turboquant_skips(shape):
        return (
            f"kv_dtype {key!r} keeps the first and last "
            f"{KV_TURBOQUANT_BOUNDARY_LAYERS} layers at the model's own width, "
            f"and next to {name}'s sliding-window layers the image pads those "
            f"pages to a shared size derate does not price; refused rather "
            f"than estimated"
        )
    return None


def _turboquant_skips(shape: ModelShape) -> bool:
    """Whether the image keeps boundary layers unquantized for this model: only
    when no layer is recurrent (`TurboQuantConfig.get_boundary_skip_layers`
    returns nothing for a hybrid)."""
    return shape.attention_layers >= shape.num_layers and shape.recurrent_state_bytes <= 0


def skipped_layers(shape: ModelShape, kv_dtype: str) -> int:
    """Attention layers the image stores at the model's own width despite
    *kv_dtype*: TurboQuant's first and last ``KV_TURBOQUANT_BOUNDARY_LAYERS``
    on a model with no recurrent layers, and none otherwise."""
    if _key(kv_dtype) not in KV_TURBOQUANT_BITS or not _turboquant_skips(shape):
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
    instead; :func:`kv_bytes_per_token` and :func:`attention_cache_bytes` add
    that difference.
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
    elem = kv_elem_bytes(kv_dtype)
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
    """Bytes one token of one sequence adds to the cache, across every
    attention layer, before sharding.

    This is the full-attention rate. A sliding window caps the *total*, not the
    rate, so a windowed model's real cache is at or below this figure times the
    context, and a recurrent layer adds nothing per token at all. Use
    :func:`kv_cache_bytes` when you need the real total.

    Exported for the planner.
    """
    return (
        shape.attention_layers * per_layer_per_token_bytes(shape, kv_dtype)
        + skipped_layers(shape, kv_dtype) * _skipped_layer_extra(shape, kv_dtype)
    )


def cached_layer_tokens(shape: ModelShape, context: int) -> int:
    """Sum over attention layers of the tokens each one actually caches.

    ``attention_layers * context`` for a normal model. For an interleaved
    sliding-window model, full layers cache the context and windowed layers
    cache the window. Recurrent layers are not in the sum: they cache no token.
    """
    context = max(0, int(context))
    layers = shape.attention_layers
    if shape.sliding_window and shape.layers_with_full_attention is not None:
        full = min(max(0, shape.layers_with_full_attention), layers)
        windowed = layers - full
        return full * context + windowed * min(shape.sliding_window, context)
    if shape.sliding_window:
        # Window set but no full-attention count: every layer is windowed.
        return layers * min(shape.sliding_window, context)
    return layers * context


def recurrent_state_bytes(
    shape: ModelShape, batch: int, copies: int = RECURRENT_STATES_PER_SEQUENCE
) -> float:
    """The fixed state ``batch`` sequences hold on the recurrent layers, before
    sharding. ``copies`` per sequence, see RECURRENT_STATES_PER_SEQUENCE."""
    return float(max(0, shape.recurrent_state_bytes) * max(0, int(batch)) * copies)


def attention_cache_bytes(
    shape: ModelShape, context: int, batch: int, kv_dtype: str
) -> float:
    """The per-token half of the cache: attention layers only, before sharding.

    A skipped layer is charged the whole context on top of its quantized rate,
    which is exact for a full-attention layer and an upper bound otherwise.
    """
    batch = max(0, int(batch))
    return (
        per_layer_per_token_bytes(shape, kv_dtype) * cached_layer_tokens(shape, context) * batch
        + skipped_layers(shape, kv_dtype)
        * _skipped_layer_extra(shape, kv_dtype)
        * max(0, int(context))
        * batch
    )


def kv_cache_bytes(
    shape: ModelShape, context: int, batch: int, kv_dtype: str
) -> float:
    """Everything the runtime's cache pool must hold for ``batch`` sequences at
    ``context``, before sharding: the attention cache plus the recurrent state.

    One pool, because that is how the image allocates it -- a hybrid's Mamba
    pages come out of the same ``--kv-cache-memory-bytes`` as its attention
    blocks.
    """
    return attention_cache_bytes(shape, context, batch, kv_dtype) + recurrent_state_bytes(
        shape, batch
    )


def kv_read_bytes(shape: ModelShape, context: int, kv_dtype: str) -> float:
    """What one decode step reads from ONE sequence's cache at ``context``:
    the attention cache and one copy of the recurrent state (the checkpointed
    copy is written at a block boundary, not read every step)."""
    return attention_cache_bytes(shape, context, 1, kv_dtype) + recurrent_state_bytes(
        shape, 1, copies=1
    )


def cache_bytes_per_rank(
    shape: ModelShape, plan: ParallelismPlan, context: int, batch: int, kv_dtype: str
) -> float:
    """:func:`kv_cache_bytes` on the busiest rank.

    The two halves shard differently: attention by KV head (capped at the
    head count, and not at all for MLA -- :func:`kv_divisor`), a recurrent
    state by its own heads, which the image divides by the full TP degree.
    """
    batch = max(0, int(batch))
    state = recurrent_state_bytes(shape, batch) / recurrent_divisor(shape, plan)
    return attention_cache_bytes_per_rank(shape, plan, context, batch, kv_dtype) + state


def attention_cache_bytes_per_rank(
    shape: ModelShape, plan: ParallelismPlan, context: int, batch: int, kv_dtype: str
) -> float:
    """:func:`attention_cache_bytes` on the busiest rank: the attention half of
    :func:`cache_bytes_per_rank`, for the terms that carry no recurrent state
    (a speculative head's own cache, its drafted positions)."""
    batch = max(0, int(batch))
    quantized = (
        per_layer_per_token_bytes(shape, kv_dtype) * cached_layer_tokens(shape, context) * batch
    ) / kv_divisor(shape, plan)
    # The skipped layers sit at both ends of the model, so a pipeline does not
    # spread them evenly: the busiest stage is charged as many of them as the
    # stage holding the most of them actually holds -- never its proportional
    # share, and never more than its own layer range contains.
    pp = max(1, plan.pipeline_parallel)
    skipped_count = skipped_layers(shape, kv_dtype)
    on_busiest = _skipped_on_busiest_stage(shape, pp) if skipped_count else 0
    skipped = (
        on_busiest * _skipped_layer_extra(shape, kv_dtype) * max(0, int(context)) * batch
    ) / kv_tp_divisor(shape, plan)
    return quantized + skipped


def _skipped_on_busiest_stage(shape: ModelShape, pp: int) -> int:
    """The most of :func:`skipped_layers` any one pipeline stage holds.

    Stages are contiguous runs of ``ceil(L / pp)`` layers (the last one
    shorter, as :func:`stage_fraction` counts them), and the wide layers sit
    at both ENDS -- so for pp > 1 the first stage holds the front ones and the
    last stage the back ones, and only a stage spanning both ends holds them
    all. ``min(skipped, ceil(L / pp))`` charged a two-stage split all four of
    a 4-boundary model's wide layers on one stage when each holds two.
    """
    layers = shape.num_layers
    n = min(KV_TURBOQUANT_BOUNDARY_LAYERS, layers // 2)
    wide = set(range(n)) | set(range(layers - n, layers))
    per_stage = max(1, math.ceil(layers / max(1, pp)))
    return max(
        (
            len(wide & set(range(start, min(start + per_stage, layers))))
            for start in range(0, layers, per_stage)
        ),
        default=0,
    )


def recurrent_divisor(shape: ModelShape, plan: ParallelismPlan) -> float:
    """How much of the recurrent state one rank holds, as a divisor: every
    recurrent head shards under TP, and a pipeline stage holds its layers."""
    tp = max(1, plan.tensor_parallel)
    return tp / stage_fraction(shape, max(1, plan.pipeline_parallel))


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
    pp = max(1, plan.pipeline_parallel)
    return kv_tp_divisor(shape, plan) / stage_fraction(shape, pp)


def kv_tp_divisor(shape: ModelShape, plan: ParallelismPlan | None) -> float:
    """The tensor-parallel half of ``kv_divisor``: how many ways TP splits the
    cache one sequence reads. 1.0 without a plan, and 1.0 for MLA, whose single
    latent per token has no head to split by. Capped at ``num_kv_heads``, past
    which the runtime replicates heads rather than splitting them.

    Decode reads its cache through this alone: pipeline stages run one after
    another on a single sequence, so PP divides what a rank HOLDS and not what
    a decoded token waits on.
    """
    if plan is None or shape.mla_latent_dim:
        return 1.0
    tp = max(1, plan.tensor_parallel)
    return float(min(tp, max(1, shape.num_kv_heads)))


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
