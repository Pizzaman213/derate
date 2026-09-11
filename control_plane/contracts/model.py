"""ModelShape. 00-architecture.md section 4.2.

Day-0 file. Transcribed from the architecture doc, not designed here.
"""

from __future__ import annotations

from dataclasses import dataclass

from .quant import BYTES_PER_PARAM


@dataclass(frozen=True)
class ModelShape:
    model_id: str
    num_layers: int
    hidden_size: int
    num_attention_heads: int
    num_kv_heads: int  # NOT num_attention_heads. GQA ratio matters.
    vocab_size: int
    total_params: int
    dtype: str  # key into BYTES_PER_PARAM

    head_dim: int | None = None

    # Mixture of experts
    num_experts: int = 0
    num_experts_per_token: int = 0
    active_params: int | None = None

    # Sliding window attention
    sliding_window: int | None = None
    layers_with_full_attention: int | None = None

    # Multi-head latent attention (DeepSeek family)
    mla_latent_dim: int | None = None
    # Width of the decoupled RoPE component cached per token alongside the
    # MLA latent (config key qk_rope_head_dim); true cached width per layer
    # per token is mla_latent_dim + mla_rope_dim; None for non-MLA models or
    # when the config key is absent.
    mla_rope_dim: int | None = None

    # Replicated per node when sharding, never split
    vision_params: int = 0

    # Routed expert weights: the mirror of vision_params. That one is held
    # whole by every rank; this one is SPLIT across expert-parallel ranks,
    # because `--enable-expert-parallel` places num_experts/ep of them on
    # each. Shared experts are not counted here -- they are read on every
    # token by every rank and stay whole, like any dense weight.
    #
    # 0 means "not derived", never "no experts", and it degrades in the
    # refusing direction: the fit gate then charges the whole checkpoint to
    # every rank, which is what it did for every model before this field
    # existed.
    routed_expert_params: int = 0

    # Encoder-decoder (Whisper family). KV cache math (fit/kv.py) only prices
    # the decoder's self-attention, so on a shape where this is True that
    # figure is a floor: cross-attention cache over the encoder's own output
    # is real memory it does not count.
    is_encoder_decoder: bool = False

    @property
    def is_moe(self) -> bool:
        return self.num_experts > 0

    @property
    def effective_head_dim(self) -> int:
        if self.head_dim is not None:
            return self.head_dim
        return self.hidden_size // self.num_attention_heads

    @property
    def effective_mla_rope_dim(self) -> int:
        """Decoupled-RoPE width to charge per token alongside the MLA latent.

        0 for non-MLA shapes. When the shape is MLA but the config did not
        carry qk_rope_head_dim, fall back to 64 -- the width every known
        DeepSeek-family checkpoint uses; charging it errs in the OOM-safe
        direction. All KV-cache math must charge
        mla_latent_dim + effective_mla_rope_dim, never the latent alone.
        """
        if self.mla_latent_dim is None:
            return 0
        if self.mla_rope_dim is not None:
            return self.mla_rope_dim
        return 64

    @property
    def effective_active_params(self) -> int:
        if self.active_params is not None:
            return self.active_params
        return self.total_params

    def bytes_per_param(self) -> float:
        try:
            return BYTES_PER_PARAM[self.dtype]
        except KeyError:
            raise KeyError(
                "unknown dtype %r; add it to control_plane/contracts/quant.py" % self.dtype
            ) from None
