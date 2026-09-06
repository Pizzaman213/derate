"""Parameter accounting.

Two jobs. First, split a model's parameters into buckets -- embedding,
attention, dense MLP, routed experts, shared experts, vision tower, multi-token
prediction -- because capacity depends on the total while decode speed depends
on what is read per token, and on a model like GPT-OSS-120B those differ by an
order of magnitude.

Second, reconcile that analytic split against the real parameter count read off
the weight index. The analytic figure is never the answer for ``total_params``:
it is the ratio that lets us split a measured total. The measured total wins.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .config_map import Mapped, _int


@dataclass
class ParamBreakdown:
    """Analytic parameter split. Every field is a count, not bytes."""

    embedding: int = 0
    lm_head: int = 0
    attention: int = 0
    dense_mlp: int = 0
    routed_experts: int = 0
    shared_experts: int = 0
    router: int = 0
    norms: int = 0
    #: Multi-token-prediction / "nextn" module. Present in the checkpoint,
    #: not loaded by a runtime unless speculative decoding is turned on.
    mtp: int = 0
    #: Vision tower. Replicated on every node when sharding, never split.
    vision: int = 0

    @property
    def total(self) -> int:
        """Everything a runtime loads by default. Excludes the MTP module."""
        return (
            self.embedding
            + self.lm_head
            + self.attention
            + self.dense_mlp
            + self.routed_experts
            + self.shared_experts
            + self.router
            + self.norms
            + self.vision
        )

    @property
    def total_with_mtp(self) -> int:
        """Everything in the checkpoint, which is what a weight index counts."""
        return self.total + self.mtp

    def as_dict(self) -> dict[str, int]:
        data = {k: int(v) for k, v in asdict(self).items()}
        data["total"] = self.total
        return data


def attention_params_per_layer(m: Mapped) -> int:
    """Projection weights for one attention block."""
    hidden = m.hidden_size
    heads = m.num_attention_heads

    if m.kv_lora_rank:  # multi-head latent attention
        qk_nope = m.qk_nope_head_dim or 0
        qk_rope = m.qk_rope_head_dim or 0
        v_dim = m.v_head_dim or qk_nope or (m.head_dim or hidden // heads)
        q_head = qk_nope + qk_rope or (m.head_dim or hidden // heads)
        if m.q_lora_rank:
            q = hidden * m.q_lora_rank + m.q_lora_rank * heads * q_head
        else:
            q = hidden * heads * q_head
        kv_a = hidden * (m.kv_lora_rank + qk_rope)
        kv_b = m.kv_lora_rank * heads * (qk_nope + v_dim)
        out = heads * v_dim * hidden
        return q + kv_a + kv_b + out

    head_dim = m.head_dim or (hidden // heads)
    q = hidden * heads * head_dim
    k = hidden * m.num_kv_heads * head_dim
    v = k
    out = heads * head_dim * hidden
    total = q + k + v + out
    if m.attention_bias:
        total += heads * head_dim + 2 * m.num_kv_heads * head_dim + hidden
    return total


def _mlp_params(hidden: int, intermediate: int, gated: bool) -> int:
    return (3 if gated else 2) * hidden * intermediate


def vision_tower_params(vision_cfg: dict[str, Any] | None, text_hidden: int) -> int:
    """Rough parameter count for a vision tower, from its own sub-config.

    An estimate, and flagged as one: vision configs are the least standardised
    corner of the ecosystem. It is small next to the language model and it is
    replicated per node, so being a few percent out costs little.
    """
    if not vision_cfg:
        return 0
    layers = _int(
        vision_cfg.get("num_hidden_layers")
        or vision_cfg.get("depth")
        or vision_cfg.get("n_layer")
        or vision_cfg.get("num_layers")
    )
    hidden = _int(
        vision_cfg.get("hidden_size")
        or vision_cfg.get("embed_dim")
        or vision_cfg.get("width")
        or vision_cfg.get("d_model")
    )
    if not layers or not hidden:
        return 0
    intermediate = _int(
        vision_cfg.get("intermediate_size")
        or vision_cfg.get("mlp_dim")
        or vision_cfg.get("ffn_hidden_size")
    ) or 4 * hidden
    act = str(vision_cfg.get("hidden_act") or vision_cfg.get("act_layer") or "").lower()
    gated = any(tag in act for tag in ("silu", "swiglu", "geglu", "glu"))
    per_layer = 4 * hidden * hidden + _mlp_params(hidden, intermediate, gated) + 6 * hidden
    total = layers * per_layer

    patch = _int(vision_cfg.get("patch_size") or vision_cfg.get("spatial_patch_size")) or 14
    channels = _int(vision_cfg.get("num_channels") or vision_cfg.get("in_chans")) or 3
    temporal = _int(vision_cfg.get("temporal_patch_size")) or 1
    total += channels * patch * patch * temporal * hidden

    image_size = _int(vision_cfg.get("image_size"))
    if image_size:
        total += (image_size // patch) ** 2 * hidden  # learned position embedding

    out_hidden = _int(vision_cfg.get("out_hidden_size")) or text_hidden
    total += hidden * out_hidden * 2  # projector into the language model
    return int(total)


def analytic_breakdown(m: Mapped, vision_cfg: dict[str, Any] | None = None) -> ParamBreakdown:
    """Parameter split derived from the config alone."""
    hidden = m.hidden_size
    layers = m.num_layers
    b = ParamBreakdown()

    b.embedding = m.vocab_size * hidden
    b.lm_head = 0 if m.tie_word_embeddings else m.vocab_size * hidden
    b.attention = layers * attention_params_per_layer(m)
    b.norms = layers * 2 * hidden + hidden

    moe_layers = set(m.moe_layer_indices) if m.is_moe else set()
    dense_layers = layers - len(moe_layers)
    b.dense_mlp = dense_layers * _mlp_params(hidden, m.intermediate_size, m.gated_mlp)

    if moe_layers:
        moe_inter = m.moe_intermediate_size or m.intermediate_size
        b.routed_experts = len(moe_layers) * m.num_experts * _mlp_params(
            hidden, moe_inter, m.gated_mlp
        )
        b.router = len(moe_layers) * hidden * m.num_experts
        shared = 0
        if m.num_shared_experts:
            shared = m.num_shared_experts * _mlp_params(hidden, moe_inter, m.gated_mlp)
        elif m.shared_expert_intermediate_size:
            shared = _mlp_params(hidden, m.shared_expert_intermediate_size, m.gated_mlp)
        b.shared_experts = len(moe_layers) * shared

    if m.num_nextn_predict_layers:
        # One extra decoder layer plus its own embedding, head and projection.
        per_layer_mlp = (
            _mlp_params(hidden, m.moe_intermediate_size or m.intermediate_size, m.gated_mlp)
            * m.num_experts
            if m.is_moe
            else _mlp_params(hidden, m.intermediate_size, m.gated_mlp)
        )
        one = (
            attention_params_per_layer(m)
            + per_layer_mlp
            + (hidden * m.num_experts if m.is_moe else 0)
            + 2 * hidden * hidden  # eh_proj
            + 2 * m.vocab_size * hidden  # its own embedding and head
        )
        b.mtp = m.num_nextn_predict_layers * one

    b.vision = vision_tower_params(vision_cfg, hidden)
    return b


@dataclass
class ParamAccounting:
    """The reconciled answer: what to put in ``ModelShape``."""

    total_params: int
    active_params: int | None
    vision_params: int
    breakdown: ParamBreakdown
    warnings: list[str]


def reconcile(
    m: Mapped,
    breakdown: ParamBreakdown,
    measured_total: int | None,
) -> ParamAccounting:
    """Combine a measured parameter count with the analytic split.

    The measured total is authoritative for capacity. The analytic split only
    decides how that total divides into "read every token" and "read when the
    router picks you".
    """
    warnings: list[str] = []
    analytic_total = breakdown.total

    if measured_total and measured_total > 0:
        total = measured_total
        if breakdown.mtp:
            # A weight index counts the MTP module; no runtime loads it unless
            # speculative decoding is enabled. Charging it would refuse launches
            # that would in fact fit.
            total = measured_total - breakdown.mtp
            warnings.append(
                f"excluded a {breakdown.mtp / 1e9:.1f}B-parameter multi-token-prediction "
                "module that the checkpoint carries but runtimes do not load by "
                "default; enabling speculative decoding adds it back"
            )
        if analytic_total > 0:
            drift = abs(total - analytic_total) / analytic_total
            if drift > 0.05:
                warnings.append(
                    f"weight index total ({total / 1e9:.1f}B) and the config-derived "
                    f"estimate ({analytic_total / 1e9:.1f}B) disagree by {drift * 100:.0f} "
                    "percent; the active-parameter split is approximate"
                )
    else:
        total = analytic_total
        warnings.append(
            "no trusted weight count was available; total parameters are derived "
            "from the config and may be a few percent out"
        )

    active: int | None = None
    if m.is_moe:
        # Kimi K2 is 98.9 percent routed experts, so the cap that stops a
        # nonsense split has to sit above that.
        routed = min(breakdown.routed_experts, int(total * 0.995))
        if routed <= 0:
            warnings.append(
                "MoE model whose expert parameters could not be sized; active "
                "parameters reported as the total, which under-states decode speed"
            )
        else:
            fraction = m.num_experts_per_token / m.num_experts
            # Read per token: everything except the experts the router skipped.
            # The input embedding is a lookup of one row, not a matrix read, so
            # it does not stream; the output head does and stays counted.
            active = total - breakdown.embedding - int(routed * (1.0 - fraction))
            active = max(active, int(total * 0.01))

    return ParamAccounting(
        total_params=int(total),
        active_params=int(active) if active is not None else None,
        vision_params=int(breakdown.vision),
        breakdown=breakdown,
        warnings=warnings,
    )
