"""config.json to model shape fields.

Key names vary by architecture and every one of them has been spelled at least
three ways. The rules here are deliberately explicit rather than clever: a
silently wrong ``num_kv_heads`` over-charges or under-charges the KV cache by
the grouped-query ratio, which is 4x or 8x on the models people actually run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Sub-config keys that hold the language model when the top level is a wrapper
# around a multimodal composite (Gemma 3, Llama 4, Llava, Qwen-Omni).
_TEXT_CONFIG_KEYS = ("text_config", "language_config", "llm_config", "decoder")
_VISION_CONFIG_KEYS = ("vision_config", "vision_tower_config", "visual", "vit_config")

_LAYER_KEYS = ("num_hidden_layers", "n_layer", "n_layers", "num_layers", "num_blocks")
_HIDDEN_KEYS = ("hidden_size", "d_model", "n_embd", "model_dim", "hidden_dim", "dim")
_HEAD_KEYS = ("num_attention_heads", "n_head", "num_heads", "n_heads", "attention_heads")
_KV_HEAD_KEYS = (
    "num_key_value_heads",
    "num_kv_heads",
    "n_head_kv",
    "n_kv_heads",
    "num_query_groups",  # Nemotron / Megatron lineage
    "multi_query_group_num",  # ChatGLM
    "attention_kv_heads",
)
_VOCAB_KEYS = ("vocab_size", "padded_vocab_size", "n_vocab")
_HEAD_DIM_KEYS = ("head_dim", "attention_head_dim", "kv_channels", "d_head")
_INTERMEDIATE_KEYS = (
    "intermediate_size",
    "ffn_hidden_size",
    "n_inner",
    "d_ff",
    "feed_forward_length",
)
_EXPERT_COUNT_KEYS = (
    "num_local_experts",
    "num_experts",
    "n_routed_experts",
    "moe_num_experts",
    "num_routed_experts",
    "expert_count",
)
_EXPERT_TOPK_KEYS = (
    "num_experts_per_tok",
    "experts_per_token",
    "moe_top_k",
    "moe_k",
    "n_experts_per_tok",
    "expert_used_count",
    "topk",
)
_MOE_INTERMEDIATE_KEYS = (
    "moe_intermediate_size",
    "moe_ffn_hidden_size",
    "expert_intermediate_size",
    "moe_intermediate_dim",
)
_GATED_ACTS = ("silu", "swiglu", "geglu", "swish", "glu")
#: Architectures whose MLP is two matrices, not three.
_NON_GATED_TYPES = (
    "gpt2", "gptj", "gpt_neox", "gpt_bigcode", "bloom", "opt", "falcon",
    "mpt", "codegen", "starcoder", "santacoder", "xglm",
)


def _first(config: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in config and config[key] is not None:
            return config[key]
    return None


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def text_config(config: dict[str, Any]) -> dict[str, Any]:
    """The language-model config, unwrapped from any multimodal wrapper.

    Top-level keys are kept as a fallback: some wrappers put ``vocab_size`` or
    ``torch_dtype`` outside ``text_config``.
    """
    for key in _TEXT_CONFIG_KEYS:
        sub = config.get(key)
        if isinstance(sub, dict) and _first(sub, _LAYER_KEYS) is not None:
            merged = {k: v for k, v in config.items() if not isinstance(v, dict)}
            merged.update(sub)
            return merged
    return config


def vision_config(config: dict[str, Any]) -> dict[str, Any] | None:
    for key in _VISION_CONFIG_KEYS:
        sub = config.get(key)
        if isinstance(sub, dict) and sub:
            return sub
    return None


@dataclass
class Mapped:
    """Everything the shape needs, read out of one config."""

    num_layers: int
    hidden_size: int
    num_attention_heads: int
    num_kv_heads: int
    vocab_size: int
    head_dim: int | None
    intermediate_size: int
    gated_mlp: bool
    tie_word_embeddings: bool
    attention_bias: bool

    # MoE
    num_experts: int = 0
    num_experts_per_token: int = 0
    moe_intermediate_size: int = 0
    num_shared_experts: int = 0
    shared_expert_intermediate_size: int = 0
    moe_layer_indices: tuple[int, ...] = ()

    # Sliding window
    sliding_window: int | None = None
    layers_with_full_attention: int | None = None

    # MLA
    mla_latent_dim: int | None = None
    kv_lora_rank: int | None = None
    q_lora_rank: int | None = None
    qk_nope_head_dim: int | None = None
    qk_rope_head_dim: int | None = None
    v_head_dim: int | None = None

    # Extras
    num_nextn_predict_layers: int = 0
    max_position_embeddings: int | None = None
    model_type: str = ""
    architectures: tuple[str, ...] = ()
    warnings: list[str] = field(default_factory=list)

    @property
    def is_moe(self) -> bool:
        return self.num_experts > 0


def map_config(config: dict[str, Any]) -> Mapped:
    """Map one config.json onto shape fields, warning wherever we had to guess."""
    warnings: list[str] = []
    root = config
    cfg = text_config(config)

    num_layers = _int(_first(cfg, _LAYER_KEYS))
    hidden_size = _int(_first(cfg, _HIDDEN_KEYS))
    num_heads = _int(_first(cfg, _HEAD_KEYS))
    if num_layers is None or hidden_size is None or num_heads is None:
        missing = [
            name
            for name, value in (
                ("num_hidden_layers", num_layers),
                ("hidden_size", hidden_size),
                ("num_attention_heads", num_heads),
            )
            if value is None
        ]
        raise KeyError(f"config is missing required field(s): {', '.join(missing)}")

    # Grouped-query attention. Absent means multi-head with no grouping, which
    # is num_kv_heads == num_attention_heads, not 1.
    kv_heads = _int(_first(cfg, _KV_HEAD_KEYS))
    if kv_heads is None:
        if cfg.get("multi_query") is True or cfg.get("multi_query_attention") is True:
            kv_heads = 1
            warnings.append(
                "config declares multi-query attention without a KV head count; "
                "charged 1 KV head per layer"
            )
        else:
            kv_heads = num_heads
            warnings.append(
                "no num_key_value_heads in config; assuming multi-head attention "
                f"with {num_heads} KV heads (no grouping)"
            )
    elif kv_heads <= 0:
        warnings.append(f"num_key_value_heads was {kv_heads}; treating as {num_heads}")
        kv_heads = num_heads

    vocab_size = _int(_first(cfg, _VOCAB_KEYS)) or _int(_first(root, _VOCAB_KEYS))
    if vocab_size is None:
        vocab_size = 32000
        warnings.append("no vocab_size in config; assumed 32000 for the embedding estimate")

    # head_dim only when the config says so. Several architectures break the
    # hidden_size / num_heads identity and silently trusting it mis-sizes KV.
    head_dim = _int(_first(cfg, _HEAD_DIM_KEYS))

    intermediate = _int(_first(cfg, _INTERMEDIATE_KEYS))
    if intermediate is None:
        intermediate = 4 * hidden_size
        warnings.append("no intermediate_size in config; assumed 4x hidden for the MLP estimate")

    act = str(
        cfg.get("hidden_act")
        or cfg.get("hidden_activation")
        or cfg.get("activation_function")
        or ""
    ).lower()
    arch_names = tuple(root.get("architectures") or cfg.get("architectures") or ())
    model_type = str(cfg.get("model_type") or root.get("model_type") or "")
    # Gated MLPs carry three matrices per layer, ungated two. Nearly everything
    # current is gated, including Gemma, whose activation name does not say so.
    gated = True
    if any(model_type.startswith(t) for t in _NON_GATED_TYPES):
        gated = False
    if any(tag in act for tag in _GATED_ACTS):
        gated = True

    mapped = Mapped(
        num_layers=num_layers,
        hidden_size=hidden_size,
        num_attention_heads=num_heads,
        num_kv_heads=kv_heads,
        vocab_size=vocab_size,
        head_dim=head_dim,
        intermediate_size=intermediate,
        gated_mlp=gated,
        tie_word_embeddings=_tie_word_embeddings(cfg, root, warnings),
        attention_bias=bool(
            cfg.get("attention_bias", cfg.get("bias", cfg.get("use_bias", False)))
        ),
        max_position_embeddings=_int(
            _first(cfg, ("max_position_embeddings", "n_positions", "max_seq_len", "seq_length"))
        ),
        model_type=model_type,
        architectures=arch_names,
        num_nextn_predict_layers=_int(cfg.get("num_nextn_predict_layers")) or 0,
        warnings=warnings,
    )

    _map_moe(cfg, mapped)
    _map_sliding_window(cfg, mapped)
    _map_mla(cfg, mapped)
    return mapped



def _tie_word_embeddings(
    cfg: dict[str, Any], root: dict[str, Any], warnings: list[str]
) -> bool:
    """Whether the output head shares the embedding matrix.

    HuggingFace's own default is True and the models that leave the key out
    (GPT-2, Gemma) do tie, so an absent key is not a licence to charge for a
    second vocab-sized matrix. Say so either way.
    """
    for source in (cfg, root):
        value = source.get("tie_word_embeddings")
        if value is not None:
            return bool(value)
    warnings.append(
        "config does not state tie_word_embeddings; assumed tied, as HuggingFace "
        "defaults it, which is one vocab-sized matrix rather than two"
    )
    return True

def _map_moe(cfg: dict[str, Any], m: Mapped) -> None:
    """Expert count, top-k, expert width, and which layers are sparse."""
    ffn_config = cfg.get("ffn_config") if isinstance(cfg.get("ffn_config"), dict) else {}
    experts = _int(_first(cfg, _EXPERT_COUNT_KEYS)) or _int(_first(ffn_config, _EXPERT_COUNT_KEYS))
    if not experts or experts <= 1:
        return
    top_k = _int(_first(cfg, _EXPERT_TOPK_KEYS)) or _int(_first(ffn_config, _EXPERT_TOPK_KEYS))
    if not top_k:
        top_k = 2
        m.warnings.append(
            f"MoE config declares {experts} experts but no experts-per-token; "
            "assumed 2, which under-states active parameters if wrong"
        )
    m.num_experts = experts
    m.num_experts_per_token = min(top_k, experts)

    moe_inter = _int(_first(cfg, _MOE_INTERMEDIATE_KEYS))
    if moe_inter is None:
        moe_inter = _int(_first(ffn_config, _INTERMEDIATE_KEYS)) or m.intermediate_size
    m.moe_intermediate_size = moe_inter

    m.num_shared_experts = _int(_first(cfg, ("n_shared_experts", "num_shared_experts"))) or 0
    m.shared_expert_intermediate_size = (
        _int(_first(cfg, ("shared_expert_intermediate_size", "moe_shared_expert_intermediate_size")))
        or 0
    )

    # Which layers are sparse. DeepSeek keeps the first k dense; Qwen names the
    # dense ones outright; some models sparsify every nth layer.
    dense_prefix = _int(cfg.get("first_k_dense_replace")) or 0
    mlp_only = cfg.get("mlp_only_layers")
    step = _int(cfg.get("decoder_sparse_step")) or 1
    indices = []
    for i in range(m.num_layers):
        if i < dense_prefix:
            continue
        if isinstance(mlp_only, list) and i in mlp_only:
            continue
        if step > 1 and (i - dense_prefix) % step != 0:
            continue
        indices.append(i)
    m.moe_layer_indices = tuple(indices)


def _map_sliding_window(cfg: dict[str, Any], m: Mapped) -> None:
    """Sliding window size and how many layers still cache the full context.

    Windowed layers cache the window, not the context. Getting the count wrong
    changes KV totals by multiples on Gemma, Llama 4 and GPT-OSS.
    """
    window = _int(cfg.get("sliding_window")) or _int(cfg.get("attention_chunk_size"))
    layer_types = cfg.get("layer_types")

    # An explicit per-layer list is the only unambiguous source; use it first.
    if isinstance(layer_types, list) and layer_types:
        full = sum(1 for t in layer_types if "full" in str(t).lower())
        if full != len(layer_types):
            m.sliding_window = window
            m.layers_with_full_attention = full
            if window is None:
                m.sliding_window = 4096
                m.warnings.append(
                    "config interleaves windowed attention but names no window size; "
                    "assumed 4096"
                )
            return
        return  # every layer is full attention

    if cfg.get("use_sliding_window") is False:
        return  # key present but disabled, e.g. Qwen2.5
    if not window:
        return

    pattern = _int(cfg.get("sliding_window_pattern"))
    model_type = m.model_type
    if pattern and pattern > 1:
        # Gemma 3: every pattern-th layer is full attention.
        m.sliding_window = window
        m.layers_with_full_attention = m.num_layers // pattern
        return
    if model_type.startswith("gemma2"):
        m.sliding_window = window
        m.layers_with_full_attention = m.num_layers // 2
        return

    max_window_layers = _int(cfg.get("max_window_layers"))
    if cfg.get("use_sliding_window") is True and max_window_layers is not None:
        # Qwen2 lineage: layers below max_window_layers keep full attention.
        m.sliding_window = window
        m.layers_with_full_attention = min(max_window_layers, m.num_layers)
        return

    no_rope = cfg.get("no_rope_layers")
    if isinstance(no_rope, list) and no_rope:
        # Llama 4: a 0 marks a NoPE layer, which attends over the full context.
        m.sliding_window = window
        m.layers_with_full_attention = sum(1 for v in no_rope if not v)
        return

    # A bare sliding_window with no pattern means every layer is windowed.
    m.sliding_window = window
    m.layers_with_full_attention = 0


def _map_mla(cfg: dict[str, Any], m: Mapped) -> None:
    """Multi-head latent attention: DeepSeek caches a compressed latent.

    ``mla_latent_dim`` is the config's KV compression dimension, ``kv_lora_rank``.
    The runtime also stores a decoupled RoPE component of ``qk_rope_head_dim``
    per layer per token on top of it, which is an eighth again on DeepSeek-V3, so
    that is called out in a warning rather than folded in silently.

    ``head_dim`` is the value head dimension. DeepSeek's query heads are wider
    than its value heads (192 against 128) and neither equals
    ``hidden_size / num_attention_heads``, which is why the config's own numbers
    are read rather than derived.
    """
    kv_lora = _int(_first(cfg, ("kv_lora_rank", "kv_compression_dim", "kv_lora_dim")))
    if not kv_lora:
        return
    qk_rope = _int(cfg.get("qk_rope_head_dim")) or 0
    m.kv_lora_rank = kv_lora
    m.q_lora_rank = _int(cfg.get("q_lora_rank"))
    m.qk_nope_head_dim = _int(cfg.get("qk_nope_head_dim"))
    m.qk_rope_head_dim = qk_rope or None
    m.v_head_dim = _int(cfg.get("v_head_dim"))
    m.mla_latent_dim = kv_lora
    if m.head_dim is None:
        m.head_dim = m.v_head_dim or ((m.qk_nope_head_dim or 0) + qk_rope) or None
    if qk_rope:
        m.warnings.append(
            f"MLA latent width is the {kv_lora}-wide KV LoRA rank; the runtime also "
            f"caches a {qk_rope}-wide decoupled RoPE component per layer per token, "
            f"so the true cached width is {kv_lora + qk_rope}"
        )
    else:
        m.warnings.append(
            "MLA config has kv_lora_rank but no qk_rope_head_dim; latent width may "
            "under-count the decoupled RoPE component"
        )
