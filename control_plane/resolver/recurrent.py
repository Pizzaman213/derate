"""The fixed-size state a recurrent layer keeps per sequence.

A hybrid checkpoint interleaves attention with layers that cache NO tokens:
Gated DeltaNet (Qwen3-Next, Qwen3.5, Olmo-Hybrid), KDA (Kimi Linear,
Bailing V3), Mamba-1 (Jamba, Mamba, Falcon-Mamba), Mamba-2 (Nemotron-H,
Granite-4.0-H, Falcon-H1, Zamba2, Bamba, Mamba-2) and short convolution
(LFM2). Each holds a conv window and, except the convolutions, a recurrent
matrix, and both are the same size at any context. Charging those layers a
KV cache -- even a "window" of one -- prices a constant as if it grew with
the context: 48 of Qwen3.8-27B's 64 layers at 4096 tokens came to 9 GiB for
twelve sequences that actually need under 4.

Every formula below is the one the pinned vLLM image computes in
``model_executor/layers/mamba/mamba_utils.py::MambaStateShapeCalculator``,
fed from the config keys that model's own ``get_mamba_state_shape_from_config``
reads. The figures are BEFORE tensor parallel (the image divides heads by
``tp``), with no speculative slots (charged by the fit gate per drafted token)
and without the Mamba-2 group padding a TP degree that does not divide
``n_groups`` adds -- a TP-dependent correction of a few KB per layer.

The element widths are vLLM's defaults: the conv state in the model dtype,
the recurrent matrix in the model dtype unless the checkpoint asks for more
(``mamba_ssm_dtype`` on Qwen3.5, ``mamba_ssm_cache_dtype`` on Nemotron-H,
which defaults to float32 there) -- and KDA's recurrent matrix is float32
always. The model dtype is charged as two bytes whatever the config says,
because the image serves a float32 checkpoint in 16-bit by default.

A GGUF is charged in float32 throughout, because that is what llama.cpp holds
recurrent state in, and the vllm GGUF plugin loads the base checkpoint's config
(which for Qwen3.5 asks for a float32 recurrent matrix anyway).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Bytes the image stores a "model dtype" state element in.
MODEL_STATE_BYTES = 2

#: Bytes llama.cpp stores every recurrent state element in.
GGUF_STATE_BYTES = 4

_DTYPE_BYTES = {
    "float32": 4, "fp32": 4, "torch.float32": 4,
    "bfloat16": 2, "bf16": 2, "float16": 2, "fp16": 2, "half": 2,
    "torch.bfloat16": 2, "torch.float16": 2,
}

#: GGUF architectures whose recurrent layers are Gated DeltaNet, stored by
#: llama.cpp under ``ssm.*`` with a different meaning per key than Mamba's.
_GGUF_GDN_ARCHS = ("qwen3next", "qwen35", "qwen35moe")


@dataclass(frozen=True)
class Recurrent:
    """What a hybrid's non-attention layers cost.

    ``attention_free_layers`` is how many layers hold no KV cache;
    ``state_bytes`` is one sequence's state over every layer that keeps one.
    """

    attention_free_layers: int
    state_bytes: int
    note: str


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = int(value)
    except (TypeError, ValueError):
        return None
    return out


def _first_int(cfg: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = _int(cfg.get(key))
        if value is not None:
            return value
    return None


def _dtype_bytes(value: Any, default: int) -> int:
    if value is None:
        return default
    return _DTYPE_BYTES.get(str(value).strip().lower(), default)


def _count(types: Any, *names: str) -> int | None:
    if not isinstance(types, list) or not types:
        return None
    return sum(1 for t in types if str(t).lower() in names)


def _mib(n: float) -> str:
    if n < 2**20:
        return f"{n / 2**10:.0f} KiB"
    return f"{n / 2**20:.1f} MiB"


def _note(kind: str, layers: int, per_layer: int, attention_free: int, total: int) -> str:
    note = (
        f"{layers} of {total} layers are {kind} and keep a fixed "
        f"{_mib(per_layer)} state per sequence"
    )
    if attention_free == layers:
        return note + " instead of a KV cache"
    if attention_free == 0:
        return note + ", beside the attention cache every layer also keeps"
    return note + f"; {attention_free} layers keep no KV cache"


# ---------------------------------------------------------------------------
# per-layer state, element counts times widths
# ---------------------------------------------------------------------------


def _gdn_layer_bytes(
    k_heads: int, v_heads: int, k_dim: int, v_dim: int, kernel: int,
    conv_bytes: int, ssm_bytes: int,
) -> int:
    """``gated_delta_net_state_shape``: conv (k-1, 2*Hk*Dk + Hv*Dv), temporal
    (Hv, Dv, Dk)."""
    conv = (kernel - 1) * (2 * k_heads * k_dim + v_heads * v_dim)
    temporal = v_heads * v_dim * k_dim
    return conv * conv_bytes + temporal * ssm_bytes


def _mamba1_layer_bytes(inner: int, state: int, kernel: int, conv_bytes: int, ssm_bytes: int) -> int:
    """``mamba1_state_shape``: conv (k-1, inner), temporal (inner, state)."""
    return (kernel - 1) * inner * conv_bytes + inner * state * ssm_bytes


def _mamba2_layer_bytes(
    inner: int, groups: int, heads: int, head_dim: int, state: int, kernel: int,
    conv_bytes: int, ssm_bytes: int,
) -> int:
    """``mamba2_state_shape``: conv (k-1, inner + 2*G*N), temporal (H, P, N)."""
    conv = (kernel - 1) * (inner + 2 * groups * state)
    temporal = heads * head_dim * state
    return conv * conv_bytes + temporal * ssm_bytes


def _kda_layer_bytes(heads: int, head_dim: int, kernel: int, conv_bytes: int) -> int:
    """``kda_state_shape`` with k heads == v heads: conv (k-1, 3*H*D), recurrent
    (H, D, D) in float32 (``kda_state_dtype``)."""
    conv = (kernel - 1) * 3 * heads * head_dim
    return conv * conv_bytes + heads * head_dim * head_dim * 4


def _short_conv_layer_bytes(dim: int, kernel: int, conv_bytes: int) -> int:
    """``short_conv_state_shape``: conv (k-1, dim), nothing else."""
    return (kernel - 1) * dim * conv_bytes


# ---------------------------------------------------------------------------
# config.json
# ---------------------------------------------------------------------------


def from_config(cfg: dict[str, Any], num_layers: int, hidden_size: int) -> Recurrent | None:
    """Read a hybrid's recurrent layers off its (text) config, or None.

    None means "no recurrent layer this build recognises", which is every
    ordinary transformer and is also the safe answer for a hybrid it does not:
    every layer is then charged a full-context cache.
    """
    model_type = str(cfg.get("model_type") or "").lower()
    for reader in (_gdn, _kda, _mamba2, _mamba1, _short_conv):
        found = reader(cfg, model_type, num_layers, hidden_size)
        if found is not None:
            return found
    return None


def _gdn(cfg, model_type, num_layers, hidden_size) -> Recurrent | None:
    k_heads = _int(cfg.get("linear_num_key_heads"))
    v_heads = _int(cfg.get("linear_num_value_heads"))
    k_dim = _int(cfg.get("linear_key_head_dim"))
    v_dim = _int(cfg.get("linear_value_head_dim"))
    kernel = _int(cfg.get("linear_conv_kernel_dim"))
    if not (k_heads and v_heads and k_dim and v_dim and kernel):
        return None
    linear = _count(cfg.get("layer_types"), "linear_attention")
    if linear is None:
        # Qwen3-Next states the interleave as a period: layer i is full
        # attention when (i + 1) % interval == 0.
        interval = _int(cfg.get("full_attention_interval"))
        if not interval or interval < 1:
            return None
        linear = num_layers - num_layers // interval
    if linear <= 0:
        return None
    per_layer = _gdn_layer_bytes(
        k_heads, v_heads, k_dim, v_dim, kernel,
        MODEL_STATE_BYTES,
        _dtype_bytes(cfg.get("mamba_ssm_dtype"), MODEL_STATE_BYTES),
    )
    return Recurrent(
        linear, per_layer * linear,
        _note("linear attention (Gated DeltaNet)", linear, per_layer, linear, num_layers),
    )


def _kda(cfg, model_type, num_layers, hidden_size) -> Recurrent | None:
    lac = cfg.get("linear_attn_config")
    if isinstance(lac, dict):
        # Kimi Linear: 1-based layer numbers.
        layers = lac.get("kda_layers")
        heads = _int(lac.get("num_heads"))
        head_dim = _int(lac.get("head_dim"))
        kernel = _int(lac.get("short_conv_kernel_size"))
        if not (isinstance(layers, list) and heads and head_dim and kernel):
            return None
        kda = len(layers)
    elif model_type == "bailing_moe_v3" or "BailingMoeV3ForCausalLM" in (cfg.get("architectures") or ()):
        group = _int(cfg.get("layer_group_size"))
        heads = _int(cfg.get("num_attention_heads"))
        head_dim = _int(cfg.get("head_dim"))
        kernel = _int(cfg.get("short_conv_kernel_size"))
        if not (group and heads and head_dim and kernel):
            return None
        # `_is_kda_layer`: every group's last layer and the ragged tail are
        # attention.
        whole = num_layers // group * group
        kda = sum(1 for i in range(whole) if (i + 1) % group != 0)
    else:
        return None
    if kda <= 0:
        return None
    per_layer = _kda_layer_bytes(heads, head_dim, kernel, MODEL_STATE_BYTES)
    return Recurrent(
        kda, per_layer * kda,
        _note("linear attention (KDA)", kda, per_layer, kda, num_layers),
    )


def _mamba2(cfg, model_type, num_layers, hidden_size) -> Recurrent | None:
    heads = _first_int(cfg, "mamba_n_heads", "mamba_num_heads", "n_mamba_heads")
    head_dim = _first_int(cfg, "mamba_d_head", "mamba_head_dim", "mamba_headdim")
    state = _first_int(cfg, "mamba_d_state", "ssm_state_size")
    kernel = _first_int(cfg, "mamba_d_conv", "conv_kernel")
    groups = _first_int(cfg, "mamba_n_groups", "mamba_ngroups", "n_groups")
    if model_type == "mamba2":
        heads = heads or _int(cfg.get("num_heads"))
        head_dim = head_dim or _int(cfg.get("head_dim"))
        state = state or _int(cfg.get("state_size"))
    if not (heads and head_dim and state and kernel and groups):
        return None

    if model_type == "nemotron_h" or "hybrid_override_pattern" in cfg:
        inner = heads * head_dim
    elif model_type == "falcon_h1":
        inner = _int(cfg.get("mamba_d_ssm")) or int(
            float(cfg.get("mamba_expand") or 2) * hidden_size
        )
    else:
        expand = cfg.get("mamba_expand", cfg.get("expand"))
        inner = int(float(expand) * hidden_size) if expand else heads * head_dim

    attention: int
    pattern = cfg.get("hybrid_override_pattern")
    if isinstance(pattern, str) and pattern:
        # Nemotron-H: M mamba, * attention, - MLP, E MoE -- one per block.
        mamba = pattern.count("M")
        attention = pattern.count("*")
    elif _count(cfg.get("layer_types"), "mamba") is not None:
        mamba = _count(cfg.get("layer_types"), "mamba") or 0
        attention = _count(cfg.get("layer_types"), "attention", "full_attention") or 0
    elif isinstance(cfg.get("layers_block_type"), list):
        # Zamba2: every block runs Mamba, and a "hybrid" one ALSO runs the
        # shared transformer block, which keeps its own cache per invocation.
        types = [str(t).lower() for t in cfg["layers_block_type"]]
        mamba = len(types)
        attention = sum(1 for t in types if t == "hybrid")
    elif isinstance(cfg.get("attn_layer_indices"), list):
        # Bamba: the listed layers are attention, the rest Mamba.
        attention = len(cfg["attn_layer_indices"])
        mamba = num_layers - attention
    elif model_type == "falcon_h1":
        # Parallel hybrid: attention and Mamba side by side in every layer.
        mamba = num_layers
        attention = num_layers
    elif model_type == "mamba2":
        mamba = num_layers
        attention = 0
    else:
        return None
    if mamba <= 0:
        return None

    ssm_default = MODEL_STATE_BYTES
    ssm_key = cfg.get("mamba_ssm_cache_dtype")
    if model_type == "nemotron_h" and ssm_key is None:
        ssm_default = 4  # NemotronHForCausalLMConfig.DEFAULT_MAMBA_SSM_CACHE_DTYPE
    per_layer = _mamba2_layer_bytes(
        inner, groups, heads, head_dim, state, kernel,
        MODEL_STATE_BYTES, _dtype_bytes(ssm_key, ssm_default),
    )
    attention_free = max(0, num_layers - attention)
    return Recurrent(
        attention_free, per_layer * mamba,
        _note("Mamba-2", mamba, per_layer, attention_free, num_layers),
    )


def _mamba1(cfg, model_type, num_layers, hidden_size) -> Recurrent | None:
    state = _first_int(cfg, "mamba_d_state", "state_size")
    kernel = _first_int(cfg, "mamba_d_conv", "conv_kernel")
    if not (state and kernel):
        return None
    if model_type == "jamba" or "attn_layer_period" in cfg:
        expand = _int(cfg.get("mamba_expand")) or 2
        inner = expand * hidden_size
        period = _int(cfg.get("attn_layer_period"))
        offset = _int(cfg.get("attn_layer_offset"))
        if period and offset is not None:
            attention = sum(1 for i in range(num_layers) if i % period == offset)
        elif isinstance(cfg.get("layers_block_type"), list):
            attention = sum(1 for t in cfg["layers_block_type"] if str(t).lower() == "attention")
        else:
            return None
        mamba = num_layers - attention
        kind = "Mamba"
    elif model_type in ("mamba", "falcon_mamba"):
        inner = _int(cfg.get("intermediate_size"))
        if not inner:
            return None
        mamba = num_layers
        attention = 0
        kind = "Mamba"
    else:
        return None
    if mamba <= 0:
        return None
    per_layer = _mamba1_layer_bytes(inner, state, kernel, MODEL_STATE_BYTES, MODEL_STATE_BYTES)
    attention_free = num_layers - attention
    return Recurrent(
        attention_free, per_layer * mamba,
        _note(kind, mamba, per_layer, attention_free, num_layers),
    )


def _short_conv(cfg, model_type, num_layers, hidden_size) -> Recurrent | None:
    kernel = _int(cfg.get("conv_L_cache"))
    if not kernel:
        return None
    conv = _count(cfg.get("layer_types"), "conv")
    if conv is None:
        full = cfg.get("full_attn_idxs")
        if not isinstance(full, list):
            return None
        conv = num_layers - len(full)
    if conv <= 0:
        return None
    # vLLM's Lfm2 reads conv_dim; Lfm2Moe and Lfm2-VL read hidden_size.
    dim = (_int(cfg.get("conv_dim")) if model_type == "lfm2" else None) or hidden_size
    per_layer = _short_conv_layer_bytes(dim, kernel, MODEL_STATE_BYTES)
    return Recurrent(
        conv, per_layer * conv,
        _note("short convolutions", conv, per_layer, conv, num_layers),
    )


# ---------------------------------------------------------------------------
# GGUF header
# ---------------------------------------------------------------------------


def per_layer_kv_heads(value: Any, layers: int) -> list[int] | None:
    """llama.cpp writes ``attention.head_count_kv`` as one number, or -- for a
    hybrid -- one per layer with 0 on every layer that has no attention."""
    if isinstance(value, (list, tuple)):
        out = [_int(v) or 0 for v in value]
        return out if len(out) == layers else None
    return None


def from_gguf(header: Any, layers: int, hidden: int) -> Recurrent | None:
    """The same answer from a GGUF header's ``ssm.*`` / ``shortconv.*`` keys."""
    arch = str(getattr(header, "architecture", "") or "").lower()
    kernel = _int(header.get("ssm.conv_kernel"))
    kv = per_layer_kv_heads(header.get("attention.head_count_kv"), layers)
    b = GGUF_STATE_BYTES
    # Newer llama.cpp converters write the per-layer answer outright.
    marked = header.get("attention.recurrent_layers")
    recurrent_count = (
        sum(1 for v in marked if v)
        if isinstance(marked, (list, tuple)) and len(marked) == layers
        else None
    )

    if arch in _GGUF_GDN_ARCHS:
        # llama.cpp's naming for a Gated DeltaNet layer: group_count is the K
        # heads, time_step_rank the V heads, state_size the K head width, and
        # inner_size the V heads times their width.
        k_heads = _int(header.get("ssm.group_count"))
        v_heads = _int(header.get("ssm.time_step_rank"))
        k_dim = _int(header.get("ssm.state_size"))
        inner = _int(header.get("ssm.inner_size"))
        # qwen35 writes the period; qwen3next does not, and llama.cpp then
        # uses 4 -- its own rule for that architecture, so it is this one's.
        interval = _int(header.get("full_attention_interval")) or 4
        if not (kernel and k_heads and v_heads and k_dim and inner):
            return None
        linear = (
            recurrent_count
            if recurrent_count is not None
            else layers - layers // interval
        )
        per_layer = _gdn_layer_bytes(k_heads, v_heads, k_dim, inner // v_heads, kernel, b, b)
        return Recurrent(
            linear, per_layer * linear,
            _note("linear attention (Gated DeltaNet)", linear, per_layer, linear, layers),
        )

    l_cache = _int(header.get("shortconv.l_cache"))
    if l_cache:
        if kv is None:
            return None
        conv = sum(1 for h in kv if h == 0)
        if conv <= 0:
            return None
        per_layer = _short_conv_layer_bytes(hidden, l_cache, b)
        return Recurrent(
            conv, per_layer * conv,
            _note("short convolutions", conv, per_layer, conv, layers),
        )

    inner = _int(header.get("ssm.inner_size"))
    state = _int(header.get("ssm.state_size"))
    if not (kernel and inner and state):
        return None
    groups = _int(header.get("ssm.group_count")) or 0
    heads = _int(header.get("ssm.time_step_rank"))

    if kv is not None:
        attention_free = sum(1 for h in kv if h == 0)
    elif arch in ("mamba", "mamba2"):
        attention_free = layers
    else:
        attention_free = 0
    if recurrent_count is not None:
        mamba = recurrent_count
    elif arch == "falcon-h1":
        mamba = layers
    elif arch.startswith("nemotron_h"):
        # A Nemotron-H block with no attention is Mamba unless it has an MLP.
        ff = header.get("feed_forward_length")
        ffs = [_int(v) or 0 for v in ff] if isinstance(ff, (list, tuple)) else None
        if kv is None or ffs is None or len(ffs) != layers:
            mamba = attention_free
        else:
            mamba = sum(1 for h, f in zip(kv, ffs) if h == 0 and f == 0)
    else:
        mamba = attention_free
    if mamba <= 0:
        return None

    if groups and heads:
        per_layer = _mamba2_layer_bytes(inner, groups, heads, inner // heads, state, kernel, b, b)
        kind = "Mamba-2"
    else:
        per_layer = _mamba1_layer_bytes(inner, state, kernel, b, b)
        kind = "Mamba"
    return Recurrent(
        attention_free, per_layer * mamba,
        _note(kind, mamba, per_layer, attention_free, layers),
    )
