"""What each parallelism strategy actually moves across the wire.

This module is the arithmetic behind the planner's decisions. It exists as its
own file so the numbers in the reason strings are computed, never asserted, and
so a reviewer can check them without reading the decision logic.

The headline comparison, for a dense 70B at batch 1 across two nodes:

    tensor parallel     2.6 MB per output token, over 160 cross-node exchanges
    pipeline parallel  16 KB  per output token, over 1 cross-node exchange

A factor of roughly 160 in volume and 160 in exchange count. That ratio is why
the "tensor parallel across a slow link" default is wrong here, and
``test_planner.py`` asserts both figures against this module.
"""

from __future__ import annotations

from control_plane.contracts import (
    UNMEASURED_COLLECTIVE_LATENCY_US,
    LinkMeasurement,
    ModelShape,
    NodeProfile,
)

from .constants import ACTIVATION_DTYPE_BYTES, ALLREDUCES_PER_LAYER


def tensor_bytes_per_step(shape: ModelShape, tp: int, batch: int = 1) -> float:
    """Bytes each rank moves per decode step under tensor parallel.

    Two all-reduces per layer, each over the hidden state for every sequence in
    the batch. A ring all-reduce moves ``2 * S * (n-1) / n`` bytes per rank for a
    payload of ``S``, which is the standard bandwidth-optimal figure and what
    NCCL implements.
    """
    if tp <= 1:
        return 0.0
    payload = shape.hidden_size * ACTIVATION_DTYPE_BYTES * batch
    per_all_reduce = 2.0 * payload * (tp - 1) / tp
    return shape.num_layers * ALLREDUCES_PER_LAYER * per_all_reduce


def pipeline_bytes_per_step(shape: ModelShape, pp: int, batch: int = 1) -> float:
    """Bytes moved per decode step under pipeline parallel.

    One activation handoff per stage boundary. The hidden state crosses once,
    point to point, and is not reduced. There is no per-layer term: that is the
    whole reason pipeline survives a slow link.
    """
    if pp <= 1:
        return 0.0
    return (pp - 1) * shape.hidden_size * ACTIVATION_DTYPE_BYTES * batch


def expert_bytes_per_step(shape: ModelShape, ep: int, batch: int = 1) -> float:
    """Bytes moved per decode step by the MoE all-to-all under expert parallel.

    Each token is dispatched to ``num_experts_per_token`` experts and combined
    back, so two hidden-state-sized payloads per routed expert per MoE layer.
    The ``(ep-1)/ep`` factor is the share that leaves the local rank.
    """
    if ep <= 1 or not shape.is_moe:
        return 0.0
    payload = shape.hidden_size * ACTIVATION_DTYPE_BYTES * batch
    per_layer = 2.0 * payload * shape.num_experts_per_token * (ep - 1) / ep
    return shape.num_layers * per_layer


def tensor_exchanges_per_step(
    shape: ModelShape, tp: int, *, allreduces_per_layer: int = ALLREDUCES_PER_LAYER
) -> int:
    """Number of separate cross-node collectives per decode step under TP.

    Latency-bound, not bandwidth-bound: at 40 microseconds a hop, an 80-layer
    model pays 160 round trips per token no matter how small the payload is.

    **This count does not depend on the degree.** tp=2 and tp=32 both pay
    ``num_layers * allreduces_per_layer``, so the wire cost is a fixed floor
    while the compute it hides behind falls as ``1/tp``. Measured against
    DeepSeek-V3 on this fabric, that floor is 4.88 ms: under a tenth of the
    step at tp=2, and **more than half of it at tp=32** -- which is to say the
    floor, not the bandwidth, is what stops tensor parallel scaling.
    ``test_planner.py`` keeps the figures, where they can be recomputed.

    *allreduces_per_layer* is a parameter rather than the module constant so
    the attention-replicated variant can be priced: shard only the MLP and
    replicate attention on every rank and this halves. See
    ``attention_replicated_is_cheap``.
    """
    if tp <= 1:
        return 0
    return shape.num_layers * max(1, allreduces_per_layer)


def attention_replicated_is_cheap(shape: ModelShape) -> bool:
    """Whether replicating attention to halve the exchange count is a good deal.

    Sharding only the MLP takes ``ALLREDUCES_PER_LAYER`` from 2 to 1 -- an
    exact halving of the fixed floor above. It is paid for with a KV cache
    duplicated on every rank, which on a memory-bound decode is normally the
    wrong currency.

    It is the right currency for multi-head latent attention. Measured through
    ``fit/kv.py`` at fp16: DeepSeek-V3 caches 70,272 bytes per token against a
    dense GQA 70B's 327,680 -- **4.7x less** -- so the duplication is cheapest
    on exactly the huge MoE checkpoints that need the most nodes and therefore
    suffer most from the floor. (vLLM already replicates the MLA latent across
    TP ranks for this reason.)

    This is a statement about what a checkpoint WANTS, not about what derate
    can arrange: sharding is the engine's decision and derate owns flags and
    environment. It exists so a plan can say which variant a model would
    prefer instead of leaving the difference theoretical.
    """
    return shape.mla_latent_dim is not None


def pipeline_exchanges_per_step(pp: int) -> int:
    return 0 if pp <= 1 else pp - 1


def expert_exchanges_per_step(shape: ModelShape, ep: int) -> int:
    if ep <= 1 or not shape.is_moe:
        return 0
    return shape.num_layers * 2  # dispatch and combine


def pipeline_bubble_fraction(pp: int, in_flight: int) -> float:
    """Fraction of pipeline time spent idle waiting for the pipe to fill.

    Standard GPipe figure: ``(p - 1) / (m + p - 1)`` for ``m`` microbatches over
    ``p`` stages. In serving, in-flight requests are the microbatches, so
    concurrency is what amortises the bubble away.
    """
    if pp <= 1:
        return 0.0
    m = max(1, in_flight)
    return (pp - 1) / (m + pp - 1)


# A consequence of the formula above that is worth stating outright, because it
# is exact and it is not what anyone expects:
#
#     At in_flight=1 the bubble EXACTLY cancels the compute a longer pipeline
#     saves. compute falls as 1/p, the bubble is (p-1)/p, and 1/(1-bubble) is
#     p. DeepSeek-V3 therefore costs 135.53 ms per step at pp=1, 2, 4, 8, 16
#     AND 32 -- identical, not approximately.
#
# So adding machines to a pipeline at concurrency 1 buys CAPACITY and never
# speed, while tensor parallel halves with every doubling. That is the whole
# reason a huge model at single stream wants TP even though TP's wire costs
# 72x more: PP's wire is nearly free and its idle time is not.
#
# NOT MODELLED, ON PURPOSE: speculative decoding puts k+1 positions through the
# model per step, and if a runtime pipelined them as microbatches then `m`
# would rise from 1 to k+1 and the bubble would collapse -- 50% to 8% at pp=2.
# Worked through, that flips the answer for a small model (gpt-oss-120b: PP
# wins 1.5-1.9x at every degree, while sending 72x less) and does not flip it
# for a large one (DeepSeek-V3: TP still wins at 2, 4 and 8). It is not applied
# here because vLLM's PP microbatching operates on scheduler batches -- whole
# requests -- not on token positions inside one verification pass, so the
# premise may simply be false. `tests/spec_sweep.py` launching PP=2 with and
# without a draft is what would settle it; it is blocked while the NCCL
# collective aborts on this fabric. Measured, not assumed -- the same rule that
# keeps an acceptance rate out of the speculative range on the Verdict card.


def compute_seconds_per_step(
    shape: ModelShape, profile: NodeProfile, world_size: int
) -> float:
    """Weight-read time for one decode step, with the model spread over ranks.

    Decode is memory-bandwidth bound: the step costs one read of the active
    parameters. Active, not total, which is why a 116.8B MoE with 5.1B active
    decodes an order of magnitude faster than a dense 70B on the same hardware.

    This is a ranking aid, not a throughput prediction. The fit calculator owns
    ``predict_decode_tps`` and its efficiency factor; nothing here should be
    shown to a user as a tokens-per-second figure.
    """
    active_bytes = shape.effective_active_params * shape.bytes_per_param()
    aggregate_bw = profile.memory_bandwidth_gbps * 1e9 * max(1, world_size)
    if aggregate_bw <= 0:
        # A profile the registry could not probe reports 0 GB/s, and it reaches
        # here the moment an operator names such a machine by hand. Infinity is
        # the honest ranking answer -- a machine whose memory bandwidth is
        # unknown cannot be shown to decode any faster than never -- and it
        # sorts the candidate last instead of raising ZeroDivisionError inside
        # the scorer. Whether the machine may be placed on at all is a
        # placement question, refused earlier and with a better sentence.
        return float("inf")
    return active_bytes / aggregate_bw


def link_seconds(
    payload_bytes: float, gbps: float, exchanges: int, latency_us: float | None
) -> float:
    """Wire time for a payload plus the fixed cost of the exchanges it takes.

    The latency term is not a rounding error. Tensor parallel's problem on this
    link is as much 160 serialised round trips as it is the byte count -- at an
    honest collective latency the round trips are the larger half by far.

    *latency_us* of None means no rung measured a collective.
    ``UNMEASURED_COLLECTIVE_LATENCY_US`` is charged instead, which is
    deliberately pessimistic for tensor parallel: dropping the term would make
    72 round trips free, which is the direction that already cost this project
    a string of plans that won on paper and lost on the box. A caller that
    reports the result has to say the figure was not measured --
    ``_Facts.latency_measured`` is how the planner knows.
    """
    if payload_bytes <= 0 and exchanges <= 0:
        return 0.0
    bandwidth_s = payload_bytes / (gbps * 1e9) if gbps > 0 else float("inf")
    charged = UNMEASURED_COLLECTIVE_LATENCY_US if latency_us is None else latency_us
    latency_s = exchanges * charged * 1e-6
    return bandwidth_s + latency_s


def estimated_step_seconds(
    shape: ModelShape,
    profile: NodeProfile,
    link: LinkMeasurement | None,
    tp: int,
    pp: int,
    ep: int,
    dp: int,
    concurrency: int,
    speculative_window: int = 1,
) -> float:
    """Estimated seconds per decode step. Lower is better. Ranking only.

    Used to order candidates within a family and to put a concrete number in the
    rejected lines. It is deliberately not calibrated against measured
    throughput -- it reproduces the *ordering* of the GPT-OSS-120B measurements,
    not their magnitudes.
    """
    world = max(1, tp * pp * dp)
    # The wire carries every position the step puts through the model, and a
    # speculative step puts k+1 of them through -- the draft's window, verified
    # in one pass. The fit gate already charges those positions; until
    # 2026-09-11 the planner did not see them at all and costed every plan as
    # if one token crossed per step.
    #
    # PAYLOAD ONLY. It does NOT go into pipeline_bubble_fraction below: that
    # would claim the drafted positions fill the pipe as microbatches, which is
    # the unverified half. See the note under pipeline_bubble_fraction.
    batch = max(1, concurrency) * max(1, speculative_window)
    compute = compute_seconds_per_step(shape, profile, world)

    bubble = pipeline_bubble_fraction(pp, concurrency)
    # A bubble of b wastes b of every unit of wall clock, so useful work costs
    # 1/(1-b). Guard the degenerate case where the pipe never fills.
    compute *= 1.0 / (1.0 - bubble) if bubble < 0.99 else 100.0

    if link is None or world == 1:
        return compute

    comm = 0.0
    comm += link_seconds(
        tensor_bytes_per_step(shape, tp, batch),
        link.all_reduce_gbps,
        tensor_exchanges_per_step(shape, tp),
        link.latency_us,
    )
    comm += link_seconds(
        pipeline_bytes_per_step(shape, pp, batch),
        link.sendrecv_gbps,
        pipeline_exchanges_per_step(pp),
        link.latency_us,
    )
    comm += link_seconds(
        expert_bytes_per_step(shape, ep, batch),
        link.sendrecv_gbps,
        expert_exchanges_per_step(shape, ep),
        link.latency_us,
    )
    return compute + comm


def human_bytes(n: float) -> str:
    """Format a byte count the way it will read in a UI reason string."""
    for unit, scale in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= scale:
            value = n / scale
            return f"{value:.1f} {unit}" if value < 100 else f"{value:.0f} {unit}"
    return f"{n:.0f} B"
