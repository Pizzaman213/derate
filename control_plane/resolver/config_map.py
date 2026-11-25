"""config.json to model shape fields.

Key names vary by architecture and every one of them has been spelled at least
three ways. The rules here are deliberately explicit rather than clever: a
silently wrong ``num_kv_heads`` over-charges or under-charges the KV cache by
the grouped-query ratio, which is 4x or 8x on the models people actually run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import recurrent

# Sub-config keys that hold the language model when the top level is a wrapper
# around a multimodal composite (Gemma 3, Llama 4, Llava, Qwen-Omni).
#
# `thinker_config` and `talker_config` are in that order on purpose. A
# Qwen-Omni checkpoint has both: the thinker is the stack that answers
# /v1/chat/completions and the talker is the one that emits speech tokens, so
# the thinker is the shape a served model should be sized by.
#
# This list is not the whole answer and is not meant to be. It is a hand-kept
# list of somebody else's key names, and it failed the way such lists always
# fail -- `Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice` keeps its stack under
# `talker_config` and was refused outright. `_discover_stacks` below is the
# part that does not need a name.
_TEXT_CONFIG_KEYS = (
    "text_config", "language_config", "llm_config", "decoder",
    "thinker_config", "talker_config",
)

#: How deep `_discover_stacks` looks. Qwen-Omni buries the stack that matters
#: two levels down (`thinker_config.text_config`), so 1 is not enough; 3 is
#: past every composite anyone has published and stops the walk from wandering
#: into `id2label` and `rope_parameters`.
_MAX_STACK_DEPTH = 3
_VISION_CONFIG_KEYS = ("vision_config", "vision_tower_config", "visual", "vit_config")

_LAYER_KEYS = ("num_hidden_layers", "n_layer", "n_layers", "num_layers", "num_blocks")
_HIDDEN_KEYS = ("hidden_size", "d_model", "n_embd", "model_dim", "hidden_dim", "dim")
_HEAD_KEYS = ("num_attention_heads", "n_head", "num_heads", "n_heads", "attention_heads")

# Encoder-decoder speech models (Whisper and its descendants) publish their
# widths per half rather than once. The decoder is the autoregressive half --
# it is what generates tokens and what holds a KV cache -- so it is the half a
# shape must describe. Consulted only when the ordinary keys are absent, so no
# decoder-only config can reach them.
_DECODER_LAYER_KEYS = ("decoder_layers", "n_decoder_layers")
_DECODER_HEAD_KEYS = ("decoder_attention_heads",)
_KV_HEAD_KEYS = (
    "num_key_value_heads",
    "num_kv_heads",
    "n_head_kv",
    "n_kv_heads",
    "num_query_groups",  # Nemotron / Megatron lineage
    "multi_query_group_num",  # ChatGLM
    "attention_kv_heads",
    # gpt-fast's ModelArgs, which the DualAR speech checkpoints inherited from
    # Fish Speech: `n_head` queries against `n_local_heads` key/value heads,
    # sized in the projection as (n_head + 2 * n_local_heads) * head_dim. The
    # name reads like a tensor-parallel shard count and is not one -- no
    # published config states a sharded head count, because the shard is
    # chosen at launch. Without it a 14-head/2-KV-head model was charged as
    # multi-head and its KV cache came out seven times too large.
    "n_local_heads",
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
    # Gemma 4's spelling, DiffusionGemma included. Absent from this list, a
    # config that states `top_k_experts: 8` fell through to the assumed 2 --
    # a four-fold understatement of active parameters, on a model whose own
    # config said the number plainly.
    "top_k_experts",
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


def _int_tuple(value: Any) -> tuple[int, ...]:
    """A config list of integers, with every unreadable entry dropped.

    A string is not treated as a sequence here even though it is iterable:
    ``"40"`` would come back as ``(4, 0)``, which is a layer list nobody wrote.
    """
    if not isinstance(value, (list, tuple)):
        return ()
    out = [_int(item) for item in value]
    return tuple(item for item in out if item is not None)


def _is_stack(config: dict[str, Any]) -> bool:
    """Does this dict describe a transformer stack we can size?

    All three fields, under any spelling the mapper already understands, so a
    sub-config written with ``n_layer``/``n_embd``/``n_head`` is found on the
    same terms as one written the modern way.
    """
    return all(
        _first(config, keys) is not None
        for keys in (_LAYER_KEYS, _HIDDEN_KEYS, _HEAD_KEYS)
    )


def _merged(root: dict[str, Any], sub: dict[str, Any]) -> dict[str, Any]:
    """*sub*, over the root's scalars.

    Some wrappers keep ``vocab_size`` or ``torch_dtype`` outside the stack, so
    the top level stays a fallback -- but only its scalars, or a sibling
    sub-config's fields would leak in and be read as this stack's.
    """
    merged = {k: v for k, v in root.items() if not isinstance(v, dict)}
    merged.update(sub)
    return merged


def _named_stack(config: dict[str, Any]) -> dict[str, Any] | None:
    """The stack under a key we know by name, descending when it wraps again.

    `thinker_config` is why the descent exists: it holds no shape fields
    itself, its own `text_config` does. One level of recursion covers that and
    every other wrapper-around-a-wrapper published so far.
    """
    for key in _TEXT_CONFIG_KEYS:
        sub = config.get(key)
        if not isinstance(sub, dict):
            continue
        if _is_stack(sub):
            return sub
        inner = _named_stack(sub)
        if inner is not None:
            return _merged(sub, inner)
    return None


def _discover_stacks(
    config: dict[str, Any], path: str = "", depth: int = 0
) -> list[tuple[str, dict[str, Any]]]:
    """Every transformer stack in *config*, by dotted path, deepest included.

    The fallback for a wrapper nobody has taught this module about. It is
    reached only where `map_config` used to raise, so it cannot change a model
    that already resolves -- it can only turn a refusal into a shape.
    """
    found: list[tuple[str, dict[str, Any]]] = []
    if depth > _MAX_STACK_DEPTH:
        return found
    for key, sub in config.items():
        if not isinstance(sub, dict) or not sub:
            continue
        here = f"{path}.{key}" if path else key
        if _is_stack(sub):
            found.append((here, sub))
        found.extend(_discover_stacks(sub, here, depth + 1))
    return found


def _stack_size(sub: dict[str, Any]) -> int:
    """Layers times width, the ranking used to pick between stacks.

    Crude on purpose, and it is the number that matters: it is monotonic in
    both of the dimensions that drive KV cache per token, and it is what
    separates a model's own stack from the auxiliary heads bolted beside it.
    """
    layers = _int(_first(sub, _LAYER_KEYS)) or 0
    hidden = _int(_first(sub, _HIDDEN_KEYS)) or 0
    return layers * hidden


def _describe(path: str, sub: dict[str, Any]) -> str:
    layers = _int(_first(sub, _LAYER_KEYS)) or 0
    hidden = _int(_first(sub, _HIDDEN_KEYS)) or 0
    return f"{path!r} ({layers} x {hidden})"


def text_config(config: dict[str, Any]) -> dict[str, Any]:
    """The language-model config, unwrapped from any multimodal wrapper.

    Top-level keys are kept as a fallback: some wrappers put ``vocab_size`` or
    ``torch_dtype`` outside ``text_config``.
    """
    named = _named_stack(config)
    if named is not None:
        return _merged(config, named)
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
    #: KV cache layers when a looped core is unrolled, or None for
    #: ``num_layers``. A looped transformer (Inkling 2's `loop_config`) runs its
    #: core several times per token and keeps a cache per VISIT -- llama.cpp
    #: expands 24 blocks to 48 cache slots -- while the weights stay physical,
    #: which is why this is not `num_layers`: the expert accounting reads that.
    #: ``layers_with_full_attention`` counts slots too when this is set.
    cache_layers: int | None = None

    # Recurrent layers (resolver/recurrent.py): how many cache no tokens, and
    # one sequence's fixed state over every layer that keeps one.
    attention_free_layers: int = 0
    recurrent_state_bytes: int = 0

    # MLA
    mla_latent_dim: int | None = None
    kv_lora_rank: int | None = None
    q_lora_rank: int | None = None
    qk_nope_head_dim: int | None = None
    qk_rope_head_dim: int | None = None
    v_head_dim: int | None = None

    # Extras
    num_nextn_predict_layers: int = 0
    # DeepSeek-V4's own speculator, which is not the MTP module above and does
    # not replace it -- deepseek-v4-flash declares both. Read here rather than
    # inferred from the architecture name for the same reason every other field
    # in this table is: the config is the thing the runtime reads too.
    #
    # `dspark_target_layer_ids` names layers the model already has, so unlike
    # `num_nextn_predict_layers` it does NOT imply a decoder layer's worth of
    # extra parameters, and nothing here converts it into one.
    #: A speculator head's OWN output vocabulary, which is smaller than the
    #: target's and is the field that makes it cheap. `AngelSlim/Qwen3-4B_eagle3`
    #: declares vocab_size 151936 and draft_vocab_size 32000, and measures
    #: 218,429,056 parameters -- which is one layer plus a 32000-row head and
    #: NO embedding at all. Charging it the target's vocab twice would price it
    #: at 778M, three and a half times over.
    draft_vocab_size: int = 0
    dspark_block_size: int = 0
    dspark_target_layer_ids: tuple[int, ...] = ()
    dspark_markov_rank: int = 0
    max_position_embeddings: int | None = None
    model_type: str = ""
    #: What a DRAFT HEAD says it was trained against, from its own
    #: ``target_model_type``. Empty for an ordinary model and for most heads --
    #: only some declare it -- so absence means "did not say", never "matches".
    #:
    #: It is the only signal that separates a vision head from a text one. The
    #: geometry cannot: ``AngelSlim/Qwen3-VL-30B-A3B-Instruct_eagle3``,
    #: ``nvidia/Qwen3-30B-A3B-Thinking-2507-Eagle3`` and
    #: ``Qwen/Qwen3-30B-A3B`` all report hidden_size 2048, vocab_size 151936
    #: and vision_params 0. The VL head declares ``target_model_type:
    #: "qwen3_vl"`` and the text head declares nothing at all.
    target_model_type: str = ""
    architectures: tuple[str, ...] = ()
    warnings: list[str] = field(default_factory=list)

    # Encoder-decoder (Whisper family). KV-cache math downstream only charges
    # the decoder's self-attention -- see kv.py -- so this flags shapes where
    # that figure is a floor, not the real number: cross-attention over the
    # encoder's own output is real cache memory this shape does not count.
    is_encoder_decoder: bool = False

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

    # Whisper large-v3 carries num_hidden_layers but no num_attention_heads,
    # only encoder_/decoder_attention_heads. Fall back to the decoder's
    # figures, and say so: an encoder-decoder shape charged as if it were
    # decoder-only understates the weights by the size of the encoder, which
    # the parameter count from the weight index later corrects.
    is_encoder_decoder = cfg.get("is_encoder_decoder") is True
    if is_encoder_decoder:
        if num_layers is None:
            num_layers = _int(_first(cfg, _DECODER_LAYER_KEYS))
        if num_heads is None:
            num_heads = _int(_first(cfg, _DECODER_HEAD_KEYS))
        if num_layers is not None and num_heads is not None:
            warnings.append(
                "encoder-decoder config; shape describes the decoder stack "
                "only, and the encoder is counted through the measured "
                "parameter total rather than this shape"
            )

    if num_layers is None or hidden_size is None or num_heads is None:
        # No key we know by name held a stack. Go and look for one.
        #
        # Reached only here, which is the safety property worth stating: this
        # branch is the one that used to raise, so discovery cannot change the
        # shape of a model that already resolves -- it can only turn a refusal
        # into an answer.
        #
        # The ranking is what makes it safe to guess. Qwen3-Omni is the case
        # that decides the design: the stack it serves from is
        # `thinker_config.text_config`, 48 layers of 2048 with 128 experts,
        # while the only candidate one level down is `code2wav_config`, an
        # 8-layer vocoder. Taking the first or the shallowest match would
        # charge a 30B mixture-of-experts as a small vocoder, and the fit gate
        # -- the thing this project exists for -- would wave through a launch
        # that runs out of memory. Largest wins, and the largest is right.
        candidates = _discover_stacks(config)
        if candidates:
            path, chosen = max(candidates, key=lambda item: _stack_size(item[1]))
            cfg = _merged(config, chosen)
            num_layers = _int(_first(cfg, _LAYER_KEYS))
            hidden_size = _int(_first(cfg, _HIDDEN_KEYS))
            num_heads = _int(_first(cfg, _HEAD_KEYS))
            note = (
                f"shape read from {_describe(path, chosen)}: no top-level "
                f"field named a language model, so this config was searched "
                f"for one"
            )
            rejected = [
                _describe(other, sub)
                for other, sub in sorted(
                    candidates, key=lambda item: -_stack_size(item[1])
                )
                if other != path
            ]
            if rejected:
                note += (
                    f". It holds more than one transformer stack and the "
                    f"largest was chosen; not used: {', '.join(rejected)}"
                )
            # Said plainly because the number it changes is the one this
            # project refuses launches over: KV cache per token comes from the
            # stack named here, and if the wrong one was picked the fit
            # verdict is wrong in the same proportion.
            note += ". KV cache per token is charged against the stack named here"
            warnings.append(note)

    # A pure state-space model (Mamba, Mamba-2, Falcon-Mamba) has no attention
    # to count heads for, and refusing it for that would be refusing it for
    # not having the one thing it does not need. Only when EVERY layer is
    # recurrent: a hybrid missing its head count is still unreadable.
    attention_free = False
    if num_layers and hidden_size:
        found = recurrent.from_config(cfg, num_layers, hidden_size)
        attention_free = found is not None and found.attention_free_layers >= num_layers
    if attention_free and num_heads is None:
        num_heads = 1
        warnings.append(
            "no attention layer: every layer is recurrent, so no KV cache is "
            "charged and the head count is recorded as 1"
        )

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
        # Name what was looked in, not only what was absent. A checkpoint can
        # reach here for two very different reasons and the operator cannot
        # act on either without knowing which: `Systran/faster-whisper-base`
        # is a CTranslate2 export and `minishlab/potion-base-8M` is a static
        # embedding model with no attention at all -- neither has a stack to
        # find, at any depth, and no amount of searching will produce one.
        searched = len(_discover_stacks(config)) + sum(
            1 for value in config.values() if isinstance(value, dict) and value
        )
        raise KeyError(
            f"config is missing required field(s): {', '.join(missing)}"
            f" -- searched the root and {searched} sub-config(s) and found no "
            f"transformer stack"
        )

    # Grouped-query attention. Absent means multi-head with no grouping, which
    # is num_kv_heads == num_attention_heads, not 1.
    kv_heads = _int(_first(cfg, _KV_HEAD_KEYS))
    if kv_heads is None and attention_free:
        kv_heads = 1
    elif kv_heads is None:
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
    # A head's claim about its TARGET, not about itself. Read from the root as
    # well as the text config because a head's config is flat more often than
    # not.
    target_model_type = str(
        root.get("target_model_type") or cfg.get("target_model_type") or ""
    )
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
            _first(
                cfg,
                (
                    "max_position_embeddings",
                    "n_positions",
                    "max_seq_len",
                    "seq_length",
                    # Encoder-decoder speech models state the two halves
                    # separately; the decoder's limit is the one that bounds a
                    # request. Whisper large-v3 publishes 448 here and nothing
                    # under any of the names above, so without it the planner
                    # would offer a context length vLLM then refuses to start.
                    "max_target_positions",
                ),
            )
        ),
        model_type=model_type,
        target_model_type=target_model_type,
        architectures=arch_names,
        num_nextn_predict_layers=_int(cfg.get("num_nextn_predict_layers")) or 0,
        draft_vocab_size=_int(cfg.get("draft_vocab_size")) or 0,
        dspark_block_size=_int(cfg.get("dspark_block_size")) or 0,
        dspark_target_layer_ids=_int_tuple(cfg.get("dspark_target_layer_ids")),
        dspark_markov_rank=_int(cfg.get("dspark_markov_rank")) or 0,
        warnings=warnings,
        is_encoder_decoder=is_encoder_decoder,
    )

    _map_moe(cfg, mapped)
    _map_recurrent(cfg, mapped)
    _map_sliding_window(cfg, mapped)
    _map_loop(cfg, mapped)
    _map_mla(cfg, mapped)
    _map_nested_dspark(root, mapped)
    return mapped


def _map_nested_dspark(root: dict[str, Any], m: Mapped) -> None:
    """A DSpark head declared in its own sub-config (Inkling 2's ``mtp_config``).

    Same design as DeepSeek-V4's top-level ``dspark_*`` keys -- a block of
    drafted tokens, a rank-256 markov head, the target layers it reads -- under
    another name, so it was never detected and the speculative options said
    nothing about it. Mapped onto the same fields, it is named and refused for
    the same reason: nothing here has derived what the module weighs. The block
    size and markov rank together are the signature; an ``mtp_config`` without
    them is not this design and is left alone.
    """
    if m.dspark_block_size or m.dspark_target_layer_ids:
        return
    head = root.get("mtp_config")
    if not isinstance(head, dict):
        return
    block, rank = _int(head.get("block_size")), _int(head.get("markov_rank"))
    if not block or not rank:
        return
    m.dspark_block_size = block
    m.dspark_markov_rank = rank
    m.dspark_target_layer_ids = _int_tuple(head.get("target_layer_ids"))



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


def _map_recurrent(cfg: dict[str, Any], m: Mapped) -> None:
    """Layers that keep a fixed-size state instead of a KV cache."""
    found = recurrent.from_config(cfg, m.num_layers, m.hidden_size)
    if found is None:
        return
    m.attention_free_layers = found.attention_free_layers
    m.recurrent_state_bytes = found.state_bytes
    m.warnings.append(found.note)


_WINDOW_TYPE_TAGS = ("sliding", "window", "chunk", "local")


def _is_windowed_type(layer_type: Any) -> bool:
    """A `layer_types` entry for attention over a bounded window: Gemma and
    GPT-OSS `sliding_attention`, Llama 4 `chunked_attention`."""
    name = str(layer_type).lower()
    return any(tag in name for tag in _WINDOW_TYPE_TAGS)


def _map_sliding_window(cfg: dict[str, Any], m: Mapped) -> None:
    """Sliding window size and how many layers still cache the full context.

    Windowed layers cache the window, not the context. Getting the count wrong
    changes KV totals by multiples on Gemma, Llama 4 and GPT-OSS.
    """
    window = _int(cfg.get("sliding_window")) or _int(cfg.get("attention_chunk_size"))
    layer_types = cfg.get("layer_types")

    # An explicit per-layer list is the only unambiguous source; use it first.
    # Only a layer type that names a window is windowed. A recurrent type
    # (linear_attention, mamba, conv) caches no tokens at all and is priced by
    # `_map_recurrent`; one this build does not recognise is charged as full
    # attention, which is the over-count in the refusing direction.
    if isinstance(layer_types, list) and layer_types:
        windowed = sum(1 for t in layer_types if _is_windowed_type(t))
        if not windowed:
            return  # every attention layer caches the full context
        m.sliding_window = window
        m.layers_with_full_attention = max(
            0, len(layer_types) - windowed - m.attention_free_layers
        )
        if window is None:
            m.sliding_window = 4096
            m.warnings.append(
                "config interleaves windowed attention but names no window size; "
                "assumed 4096"
            )
        return

    # Inkling 2: the windowed ("local") layers by index; every other is global.
    # Its window key is read only beside that list: a bare `sliding_window_size`
    # would fall through to "every layer windowed" below, the under-charge.
    local = cfg.get("local_layer_ids")
    local_window = window or _int(cfg.get("sliding_window_size"))
    if isinstance(local, list) and local and local_window:
        window = local_window
        flags = windowed_flags_from_ids(local, m.num_layers)
        m.sliding_window = window
        m.layers_with_full_attention = max(
            0, flags.count(False) - m.attention_free_layers
        )
        return

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


def windowed_flags_from_ids(ids: list, num_layers: int) -> list[bool]:
    """One windowed flag per layer from a list of windowed layer indices."""
    local = {i for i in (_int(v) for v in ids) if i is not None}
    return [i in local for i in range(num_layers)]


def loop_cache_layers(
    windowed: list[bool], start: int, end: int, passes: int
) -> tuple[int, int] | None:
    """(cache layers, full-attention cache layers) once a looped core is unrolled.

    ``windowed`` is one flag per PHYSICAL layer; layers ``start`` to ``end``
    (exclusive) run ``passes`` times per token and each visit keeps its own KV
    cache. llama.cpp's own expansion: Inkling 2 Mini is 24 blocks with 6..17
    looped three times, 48 cache slots, of which the 8 visits to its 4 global
    layers cache the context and the other 40 the 1024-token window. None when
    the loop does not describe these layers, so the caller keeps the physical
    count rather than inventing one.
    """
    if passes < 1 or not 0 <= start < end <= len(windowed):
        return None
    extra = passes - 1
    core = windowed[start:end]
    return (
        len(windowed) + extra * len(core),
        windowed.count(False) + extra * core.count(False),
    )


def _map_loop(cfg: dict[str, Any], m: Mapped) -> None:
    """A looped core's extra cache slots (Inkling 2's ``loop_config``).

    Without it the 24 physical layers were charged, half the slots llama.cpp
    allocates. The windowed flags come from ``local_layer_ids`` when the config
    names them; otherwise every looped layer is charged the full context, which
    over-counts in the refusing direction rather than under.
    """
    loop = cfg.get("loop_config")
    if not isinstance(loop, dict):
        return
    start, end = _int(loop.get("core_start")), _int(loop.get("core_end"))
    passes = _int(loop.get("num_passes"))
    if start is None or end is None or passes is None or passes <= 1:
        return
    local = cfg.get("local_layer_ids")
    if m.sliding_window and isinstance(local, list) and local:
        flags = windowed_flags_from_ids(local, m.num_layers)
    else:
        flags = [False] * m.num_layers
        if m.sliding_window:
            m.warnings.append(
                "looped core with a window but no per-layer list; every extra "
                "pass is charged the full context"
            )
    unrolled = loop_cache_layers(flags, start, end, passes)
    if unrolled is None:
        m.warnings.append(
            f"loop_config core {start}..{end} does not fit {m.num_layers} layers; "
            "charged the physical layers only"
        )
        return
    m.cache_layers, full = unrolled
    if m.sliding_window:
        m.layers_with_full_attention = full


def _map_mla(cfg: dict[str, Any], m: Mapped) -> None:
    """Multi-head latent attention: DeepSeek caches a compressed latent.

    ``mla_latent_dim`` is the config's KV compression dimension, ``kv_lora_rank``.
    The runtime also stores a decoupled RoPE component of ``qk_rope_head_dim``
    per layer per token on top of it. That width is carried through to
    ``ModelShape.mla_rope_dim`` (via ``Mapped.qk_rope_head_dim``), so KV
    arithmetic reads ``effective_mla_rope_dim`` rather than assuming anything;
    a warning fires only when the config leaves the key out and the property's
    64-wide fallback will be doing the guessing instead.

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
    if not qk_rope:
        m.warnings.append(
            "MLA config has kv_lora_rank but no qk_rope_head_dim; the resolver "
            f"cannot carry the decoupled RoPE width and falls back to charging "
            f"64, the width every known DeepSeek-family checkpoint uses, so the "
            f"true cached width is charged as {kv_lora + 64}"
        )
