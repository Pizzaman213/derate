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

    # Recurrent layers: linear attention (Gated DeltaNet, KDA), Mamba, short
    # convolution. Such a layer keeps a FIXED-SIZE state per sequence instead
    # of a per-token KV cache, so it costs the same at 1k tokens as at 1M.
    #
    # `attention_free_layers` is how many of `num_layers` hold no KV cache at
    # all; the attention cache is charged on the rest. It is not always the
    # recurrent count: Falcon-H1 runs attention AND Mamba in every layer (0
    # here, state on all 44), and Nemotron-H counts MLP-only blocks in
    # num_layers that hold neither.
    #
    # `recurrent_state_bytes` is ONE copy of that state for ONE sequence,
    # summed over every layer that keeps one, before tensor parallel splits
    # it by head. How many copies a sequence holds is the runtime's business
    # and lives in fit/constants.py, not here. 0 means a model with no
    # recurrent layers -- or one this build cannot read, which then degrades
    # to charging every layer a full-context cache, the over-count in the
    # refusing direction.
    attention_free_layers: int = 0
    recurrent_state_bytes: int = 0

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

    # Parameters one decoded token streams from memory. The input embedding is
    # a row gather, so it is excluded; the output head is counted, and when the
    # embedding is tied the embedding IS the head, so it stays. The vision
    # tower is excluded, and so are the routed experts the router skipped.
    # 0 means "not derived", and decode then falls back to
    # effective_active_params -- the figure every estimate used before this.
    decode_read_params: int = 0

    # Encoder-decoder (Whisper family). KV cache math (fit/kv.py) only prices
    # the decoder's self-attention, so on a shape where this is True that
    # figure is a floor: cross-attention cache over the encoder's own output
    # is real memory it does not count.
    is_encoder_decoder: bool = False

    # A speech checkpoint's audio codec, as derate's own tts server drives it:
    # what the torch allocator holds beyond the weight index once the codec is
    # resident, and what decoding one generated frame back to audio adds on
    # top -- the codec decodes the whole clip in one call, so it grows with
    # the frames generated, which the server bounds by the context window.
    # Measured per architecture, never derived (`resolver/support.py::
    # TTS_WORKING_SETS`), and applied by the gateway for the tts runtime only.
    # 0 for every other shape.
    codec_resident_bytes: int = 0
    codec_bytes_per_frame: int = 0

    @property
    def is_moe(self) -> bool:
        return self.num_experts > 0

    @property
    def effective_head_dim(self) -> int:
        if self.head_dim is not None:
            return self.head_dim
        return self.hidden_size // self.num_attention_heads

    @property
    def attention_layers(self) -> int:
        """Layers that keep a per-token KV cache."""
        return max(0, self.num_layers - max(0, self.attention_free_layers))

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

    @property
    def effective_decode_params(self) -> int:
        """What one decoded token reads, in parameters. See decode_read_params."""
        return self.decode_read_params or self.effective_active_params

    def codec_working_set(self, context: int) -> int:
        """Bytes the codec holds while a clip of up to *context* frames is
        decoded. One clip at a time: the tts server synthesises under a lock,
        so concurrency does not multiply it."""
        return max(0, self.codec_resident_bytes) + max(
            0, self.codec_bytes_per_frame
        ) * max(0, int(context))

    def bytes_per_param(self) -> float:
        try:
            return BYTES_PER_PARAM[self.dtype]
        except KeyError:
            raise KeyError(
                "unknown dtype %r; add it to control_plane/contracts/quant.py" % self.dtype
            ) from None
