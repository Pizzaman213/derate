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

from control_plane.contracts import LinkMeasurement, ModelShape, NodeProfile

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


def tensor_exchanges_per_step(shape: ModelShape, tp: int) -> int:
    """Number of separate cross-node collectives per decode step under TP.

    Latency-bound, not bandwidth-bound: at 40 microseconds a hop, an 80-layer
    model pays 160 round trips per token no matter how small the payload is.
    """
    if tp <= 1:
        return 0
    return shape.num_layers * ALLREDUCES_PER_LAYER


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


def compute_seconds_per_step(
    shape: ModelShape, profile: NodeProfile, world_size: int
) -> float:
    """Weight-read time for one decode step, with the model spread over ranks.

    Decode is memory-bandwidth bound: the step costs one read of the active
    parameters. Active, not total, which is why a 116.8B MoE with 5.1B active
    decodes an order of magnitude faster than a dense 70B on the same hardware.

    This is a ranking aid, not a throughput prediction. Agent D owns
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


def link_seconds(payload_bytes: float, gbps: float, exchanges: int, latency_us: float) -> float:
    """Wire time for a payload plus the fixed cost of the exchanges it takes.

    The latency term is not a rounding error. Tensor parallel's problem on this
    link is as much 160 serialised round trips as it is the byte count.
    """
    if payload_bytes <= 0 and exchanges <= 0:
        return 0.0
    bandwidth_s = payload_bytes / (gbps * 1e9) if gbps > 0 else float("inf")
    latency_s = exchanges * latency_us * 1e-6
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
) -> float:
    """Estimated seconds per decode step. Lower is better. Ranking only.

    Used to order candidates within a family and to put a concrete number in the
    rejected lines. It is deliberately not calibrated against measured
    throughput -- it reproduces the *ordering* of the GPT-OSS-120B measurements,
    not their magnitudes.
    """
    world = max(1, tp * pp * dp)
    batch = max(1, concurrency)
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
