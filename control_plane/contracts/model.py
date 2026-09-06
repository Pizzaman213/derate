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

    # Replicated per node when sharding, never split
    vision_params: int = 0

    @property
    def is_moe(self) -> bool:
        return self.num_experts > 0

    @property
    def effective_head_dim(self) -> int:
        if self.head_dim is not None:
            return self.head_dim
        return self.hidden_size // self.num_attention_heads

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
