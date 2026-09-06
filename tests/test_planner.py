"""Acceptance and unit tests for the parallelism planner (Agent E).

The tests that matter most here are the ones that prove a *number* is read
rather than assumed. ``test_answer_flips_when_only_the_measurement_changes``
takes the frozen 10.2 GB/s fixture, changes two fields, and asserts the plan
inverts. ``test_no_bandwidth_is_hardcoded_in_the_planner`` greps the source. If
either of those ever fails, the product thesis has quietly stopped being true.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re

import pytest

from control_plane.contracts import (
    EP_VIABLE_THRESHOLD,
    TP_VIABLE_THRESHOLD,
    LinkMeasurement,
    ModelShape,
    NodeProfile,
    ParallelismKind,
    ParallelismPlan,
    PlannerPort,
)
from control_plane.planner import (
    MIN_NODES_FOR_CROSS_NODE_EP,
    PIPELINE_INFLIGHT_PER_STAGE,
    Planner,
    StubPlanner,
    comm,
)
from control_plane.planner.legality import valid_tp_degrees
from tests.fixtures import (
    DEEPSEEK_V3,
    GB10_PROFILES,
    GPT_OSS_120B,
    LINK_SPARK_10G,
    LLAMA_3_3_70B,
    QWEN3_30B_A3B,
    SPARK_01,
    WS_3090,
)

SPARKS = list(GB10_PROFILES)

# GPT-OSS-120B's native context. At 16 concurrent sequences this is the
# workload that genuinely needs both Sparks, which is the demo.
FULL_CONTEXT = 131072


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


class FixedFit:
    """A stand-in for Agent D that answers a fixed node requirement.

    Used where the point of the test is the *decision*, not the capacity
    arithmetic. Pinning min_nodes keeps those tests from moving when D's real
    budget lands and shifts a boundary by a gigabyte.
    """

    def __init__(self, min_nodes: int) -> None:
        self._n = min_nodes

    def min_nodes_required(self, shape, profile, context, max_seqs, kv_dtype="auto") -> int:
        return self._n

    def kv_bytes_per_token(self, shape, kv_dtype) -> float:
        return 0.0


def spark_cluster(n: int) -> list[NodeProfile]:
    return [
        dataclasses.replace(SPARK_01, node_id=f"spark-{i:02d}", hostname=f"spark-{i:02d}")
        for i in range(1, n + 1)
    ]


def faster_link(link: LinkMeasurement, gbps: float, gdr: bool) -> LinkMeasurement:
    """The same measurement with only the bandwidth and RDMA flag changed."""
    return dataclasses.replace(
        link, all_reduce_gbps=gbps, sendrecv_gbps=gbps * 0.9, gpudirect_rdma=gdr
    )


# --------------------------------------------------------------------------
# Acceptance
# --------------------------------------------------------------------------


def test_measured_slow_link_chooses_pipeline_and_says_why():
    """GPT-OSS-120B, two Sparks, 10.2 GB/s, concurrency 16 -> pipeline parallel.

    This is the demo. NVIDIA's playbook specifies tensor parallel for two
    Sparks; the measured link says otherwise and the reason has to show its
    work, naming both the bandwidth and the concurrency it turned on.
    """
    plan = Planner().plan(
        GPT_OSS_120B, SPARKS, LINK_SPARK_10G, "throughput", 16,
        context_length=FULL_CONTEXT,
    )

    assert plan.kind is ParallelismKind.PIPELINE
    assert plan.pipeline_parallel == 2
    assert plan.tensor_parallel == 1
    assert plan.node_ids == ["spark-01", "spark-02"]

    assert "10.2 GB/s" in plan.reason, "the reason must name the measured bandwidth"
    assert "concurrency 16" in plan.reason, "the reason must name the concurrency"
    assert plan.measured_link_gbps == pytest.approx(10.2)


def test_latency_target_at_single_stream_chooses_tensor_parallel():
    """Concurrency 1 with a latency target inverts the answer.

    Measured on GPT-OSS-120B across two Sparks: roughly 40 tok/s under tensor
    parallel against 29 under pipeline at single stream, because a two-stage
    pipeline with no batch to fill it idles half the time. The link is the same
    10.2 GB/s that chose pipeline for batched serving.
    """
    planner = Planner(fit=FixedFit(2))
    plan = planner.plan(
        GPT_OSS_120B, SPARKS, LINK_SPARK_10G, "latency", 1,
        context_length=FULL_CONTEXT,
    )

    assert plan.kind is ParallelismKind.TENSOR
    assert plan.tensor_parallel == 2
    assert "concurrency 1" in plan.reason
    assert any("PP=2" in r and "bubble" in r for r in plan.rejected)


def test_latency_flip_end_to_end_on_a_model_that_needs_two_nodes():
    """The same flip with real capacity arithmetic rather than a pinned stub.

    Llama 3.3 70B at bf16 is 131 GiB of weights against 107.7 GiB usable, so it
    needs both nodes at any concurrency. That makes it the honest end-to-end
    case for the single-stream rule.
    """
    planner = Planner()
    batched = planner.plan(LLAMA_3_3_70B, SPARKS, LINK_SPARK_10G, "throughput", 16,
                           context_length=8192)
    single = planner.plan(LLAMA_3_3_70B, SPARKS, LINK_SPARK_10G, "latency", 1,
                          context_length=8192)

    assert batched.kind is ParallelismKind.PIPELINE
    assert single.kind is ParallelismKind.TENSOR
    assert single.tensor_parallel == 2


def test_answer_flips_when_only_the_measurement_changes():
    """The proof that nothing is hardcoded.

    Same model, same nodes, same concurrency, same target. One field changes:
    the measured all-reduce goes from below the threshold to above it, as it
    would if a driver update enabled GPUDirect RDMA. The plan must invert
    without anything else being touched.
    """
    planner = Planner()
    kwargs = dict(context_length=FULL_CONTEXT)

    slow = planner.plan(GPT_OSS_120B, SPARKS, LINK_SPARK_10G, "throughput", 16, **kwargs)
    fast = planner.plan(
        GPT_OSS_120B, SPARKS, faster_link(LINK_SPARK_10G, 50.0, gdr=True),
        "throughput", 16, **kwargs,
    )

    assert slow.kind is ParallelismKind.PIPELINE
    assert fast.kind is ParallelismKind.TENSOR
    assert fast.tensor_parallel == 2
    assert "50.0 GB/s" in fast.reason
    assert f"{TP_VIABLE_THRESHOLD:.0f} GB/s" in fast.reason


def test_threshold_is_read_from_contracts_not_from_a_literal():
    """Move the threshold and the decision moves with it.

    A planner that happened to agree with the threshold by coincidence would
    pass every other test in this file. This one only passes if the constant is
    actually consulted.
    """
    planner = Planner()
    just_under = faster_link(LINK_SPARK_10G, TP_VIABLE_THRESHOLD - 0.1, gdr=True)
    just_over = faster_link(LINK_SPARK_10G, TP_VIABLE_THRESHOLD, gdr=True)

    under = planner.plan(GPT_OSS_120B, SPARKS, just_under, "throughput", 16,
                         context_length=FULL_CONTEXT)
    over = planner.plan(GPT_OSS_120B, SPARKS, just_over, "throughput", 16,
                        context_length=FULL_CONTEXT)

    assert under.kind is ParallelismKind.PIPELINE
    assert over.kind is ParallelismKind.TENSOR


@pytest.mark.parametrize("max_nodes", [2, 4, 8, 16])
def test_kv_head_count_bounds_the_tensor_parallel_degree(max_nodes):
    """A model with 8 KV heads never produces TP=16, or any other illegal degree."""
    degrees = valid_tp_degrees(GPT_OSS_120B, max_nodes)

    assert 16 not in degrees
    for tp in degrees:
        assert GPT_OSS_120B.num_attention_heads % tp == 0
        assert GPT_OSS_120B.num_kv_heads % tp == 0
    assert degrees == {d for d in (1, 2, 4, 8) if d <= max_nodes}


def test_no_plan_on_a_large_cluster_uses_an_illegal_degree():
    """The bound holds through the whole planner, not just the helper."""
    planner = Planner(fit=FixedFit(2))
    nodes = spark_cluster(16)

    for shape in (GPT_OSS_120B, LLAMA_3_3_70B, QWEN3_30B_A3B, DEEPSEEK_V3):
        for plan in planner.alternatives(shape, nodes, LINK_SPARK_10G, "throughput", 16):
            assert shape.num_attention_heads % plan.tensor_parallel == 0, plan.reason
            assert shape.num_kv_heads % plan.tensor_parallel == 0, plan.reason
            assert plan.pipeline_parallel <= shape.num_layers
            assert plan.world_size <= len(nodes)


def test_model_that_fits_one_node_is_not_split():
    """Qwen3-30B-A3B on two Sparks stays on one, and says a replica beats splitting.

    Never cross the link for a model that fits. The second node is worth more as
    an independent replica behind the gateway than as a second rank paying
    interconnect cost on every single token.
    """
    plan = Planner().plan(
        QWEN3_30B_A3B, SPARKS, LINK_SPARK_10G, "throughput", 8, context_length=32768
    )

    assert plan.kind is ParallelismKind.SINGLE_NODE
    assert plan.world_size == 1
    assert plan.node_ids == ["spark-01"]
    assert "replica" in plan.reason
    assert "spark-02" in plan.reason


def test_single_node_rejections_do_not_argue_about_bandwidth():
    """When the model fits, the multi-node options lost on principle, not speed.

    A rejected line reading "below the 40 GB/s threshold" here would be a
    non-sequitur: the link is irrelevant to a plan that never touches it.
    """
    plan = Planner().plan(
        QWEN3_30B_A3B, SPARKS, LINK_SPARK_10G, "throughput", 8, context_length=32768
    )

    multi_node = [r for r in plan.rejected if r.startswith(("TP=", "PP="))]
    assert multi_node
    for line in multi_node:
        assert "threshold" not in line, line
        assert "fits on one node" in line


def test_moe_below_threshold_never_returns_cross_node_expert_parallel():
    """DeepEP assumes GPUDirect. Without it the all-to-all loses its overlap.

    Measured internode dispatch on a properly equipped H800 cluster is roughly
    43 GB/s; the Spark link is four times slower than that already-degraded
    case, so cross-node expert parallel is refused rather than merely ranked
    low -- it must not appear anywhere in the ranked list.
    """
    planner = Planner(fit=FixedFit(4))
    nodes = spark_cluster(8)

    for shape in (GPT_OSS_120B, QWEN3_30B_A3B, DEEPSEEK_V3):
        assert shape.is_moe
        plans = planner.alternatives(shape, nodes, LINK_SPARK_10G, "throughput", 32)
        for plan in plans:
            assert plan.expert_parallel <= 1, f"{shape.model_id}: {plan.reason}"
        assert any("EP=" in r and "RDMA" in r for r in plans[0].rejected)


def test_moe_refuses_cross_node_ep_on_bandwidth_even_with_rdma_enabled():
    """Both gates are independent. RDMA on but a slow link is still a refusal."""
    planner = Planner(fit=FixedFit(4))
    slow_but_rdma = faster_link(LINK_SPARK_10G, EP_VIABLE_THRESHOLD - 1.0, gdr=True)

    plans = planner.alternatives(
        DEEPSEEK_V3, spark_cluster(8), slow_but_rdma, "throughput", 32
    )
    assert all(p.expert_parallel <= 1 for p in plans)
    assert any("EP=" in r and "threshold" in r for r in plans[0].rejected)


def test_moe_uses_expert_parallel_when_the_link_and_the_cluster_support_it():
    """The EP branch is real, not dead code: wide cluster, fast link, RDMA on."""
    planner = Planner(fit=FixedFit(5))
    fast = faster_link(LINK_SPARK_10G, 50.0, gdr=True)

    plan = planner.plan(DEEPSEEK_V3, spark_cluster(8), fast, "throughput", 32)

    assert plan.kind is ParallelismKind.EXPERT
    assert plan.expert_parallel == 8
    assert plan.data_parallel == 8
    assert DEEPSEEK_V3.num_experts % plan.expert_parallel == 0, "experts must split evenly"
    assert "GPUDirect RDMA" in plan.reason


def test_expert_degree_always_divides_the_expert_count():
    """An uneven expert split leaves one rank holding an extra expert, and every
    all-to-all waits on the slowest rank."""
    planner = Planner(fit=FixedFit(5))
    fast = faster_link(LINK_SPARK_10G, 50.0, gdr=True)

    for n in range(MIN_NODES_FOR_CROSS_NODE_EP, 13):
        for plan in planner.alternatives(DEEPSEEK_V3, spark_cluster(n), fast, "throughput", 32):
            if plan.expert_parallel > 1:
                assert DEEPSEEK_V3.num_experts % plan.expert_parallel == 0, plan.reason


def test_spare_nodes_become_a_replica_when_they_can_and_ranks_when_they_cannot():
    """Why the planner prefers the fewest nodes -- and when it stops.

    Spare capacity is worth more as an independent replica behind the gateway
    than as extra ranks paying interconnect cost on every token. That argument
    only holds while the spares add up to a whole second copy. When they do not,
    idle hardware is the worse outcome and the plan spreads wider instead.
    """
    fast = faster_link(LINK_SPARK_10G, 50.0, gdr=True)
    nodes = spark_cluster(8)

    # Four nodes needed of eight: the other four are a second replica.
    replica = Planner(fit=FixedFit(4)).plan(DEEPSEEK_V3, nodes, fast, "throughput", 32)
    assert replica.world_size == 4

    # Five needed of eight: three spares cannot host a copy, so use all eight.
    spread = Planner(fit=FixedFit(5)).plan(DEEPSEEK_V3, nodes, fast, "throughput", 32)
    assert spread.world_size == 8


def test_missing_measurement_falls_back_to_pipeline_and_says_so():
    """With link=None the planner is conservative and asks for a measurement."""
    plan = Planner().plan(
        GPT_OSS_120B, SPARKS, None, "throughput", 16, context_length=FULL_CONTEXT
    )

    assert plan.kind is ParallelismKind.PIPELINE
    assert plan.measured_link_gbps == 0.0, "0.0 means unmeasured, never a speed"
    assert "no link measurement is available" in plan.reason
    assert "measure the link" in plan.reason


def test_missing_measurement_stays_conservative_even_at_single_stream():
    """Conservative mode prefers pipeline. An unmeasured link earns no benefit."""
    plan = Planner(fit=FixedFit(2)).plan(GPT_OSS_120B, SPARKS, None, "latency", 1)

    assert plan.kind is ParallelismKind.PIPELINE
    assert "no link measurement" in plan.reason


@pytest.mark.parametrize(
    "shape,link,target,concurrency",
    [
        (GPT_OSS_120B, LINK_SPARK_10G, "throughput", 16),
        (GPT_OSS_120B, LINK_SPARK_10G, "latency", 1),
        (GPT_OSS_120B, None, "throughput", 16),
        (LLAMA_3_3_70B, LINK_SPARK_10G, "throughput", 16),
        (QWEN3_30B_A3B, LINK_SPARK_10G, "throughput", 8),
        (DEEPSEEK_V3, LINK_SPARK_10G, "balanced", 32),
    ],
)
def test_every_plan_carries_a_reason_and_shows_its_work(shape, link, target, concurrency):
    """No plan without a reason. The reason is what the judge reads."""
    planner = Planner()
    plans = planner.alternatives(shape, SPARKS, link, target, concurrency)

    assert plans
    for plan in plans:
        assert plan.reason.strip(), "a plan without a reason is not a plan"
        assert plan.reason[0].isalnum()
        assert plan.reason.rstrip().endswith(".")
    if len(plans) > 1:
        assert plans[0].rejected, "alternatives existed, so something was rejected"
        for line in plans[0].rejected:
            assert ":" in line, f"a rejected line must name what and why: {line}"


# --------------------------------------------------------------------------
# The communication model the decisions rest on
# --------------------------------------------------------------------------


def test_tensor_parallel_moves_160x_what_pipeline_moves_on_a_dense_70b():
    """The measured figures the whole design cites, recomputed from the model.

    A dense 70B at batch 1 across two nodes: tensor parallel moves roughly
    2.6 MB per output token, pipeline roughly 16 KB. If this ratio ever stops
    holding, the rule that pipeline wins on a slow link stops being justified.
    """
    tp_bytes = comm.tensor_bytes_per_step(LLAMA_3_3_70B, tp=2, batch=1)
    pp_bytes = comm.pipeline_bytes_per_step(LLAMA_3_3_70B, pp=2, batch=1)

    assert tp_bytes == pytest.approx(2.6e6, rel=0.05)
    assert pp_bytes == pytest.approx(16e3, rel=0.05)
    assert tp_bytes / pp_bytes == pytest.approx(160, rel=0.05)


def test_exchange_counts_are_per_layer_for_tensor_and_per_stage_for_pipeline():
    """160 cross-node exchanges per token against one. The latency term."""
    assert comm.tensor_exchanges_per_step(LLAMA_3_3_70B, tp=2) == 160
    assert comm.pipeline_exchanges_per_step(pp=2) == 1
    assert comm.tensor_exchanges_per_step(LLAMA_3_3_70B, tp=1) == 0


def test_pipeline_bubble_shrinks_as_concurrency_rises():
    """Concurrency is what amortises the bubble away."""
    at_1 = comm.pipeline_bubble_fraction(2, 1)
    at_16 = comm.pipeline_bubble_fraction(2, 16)

    assert at_1 == pytest.approx(0.5)
    assert at_16 < 0.07
    assert comm.pipeline_bubble_fraction(1, 1) == 0.0


def test_bubble_guard_warns_below_four_in_flight_per_stage():
    """Pipeline at low concurrency gets a warning, not a silent recommendation."""
    planner = Planner(fit=FixedFit(2))
    thin = planner.plan(LLAMA_3_3_70B, SPARKS, LINK_SPARK_10G, "throughput", 3)
    thick = planner.plan(LLAMA_3_3_70B, SPARKS, LINK_SPARK_10G, "throughput", 16)

    assert thin.kind is ParallelismKind.PIPELINE
    assert "bubble" in thin.reason and "Warning" in thin.reason
    assert str(PIPELINE_INFLIGHT_PER_STAGE * 2) in thin.reason
    assert "Warning" not in thick.reason


# --------------------------------------------------------------------------
# Heterogeneous hardware
# --------------------------------------------------------------------------


def test_unlike_hardware_is_not_pooled_and_the_exclusion_is_stated():
    """A 3090 and a Spark are separate deployment targets, and we say so."""
    plan = Planner().plan(
        GPT_OSS_120B, [*SPARKS, WS_3090], LINK_SPARK_10G, "throughput", 16,
        context_length=FULL_CONTEXT,
    )

    assert "ws-3090" not in plan.node_ids
    assert plan.node_ids == ["spark-01", "spark-02"]
    assert "ws-3090" in plan.reason
    assert "excluded" in plan.reason


def test_a_lone_desktop_is_planned_for_on_its_own_terms():
    """Excluding a node from one pool must not make it unplannable."""
    plan = Planner().plan(
        QWEN3_30B_A3B, [WS_3090], None, "throughput", 4, context_length=4096
    )

    assert plan.node_ids == ["ws-3090"]
    assert plan.reason.strip()


# --------------------------------------------------------------------------
# Contract conformance and consistency
# --------------------------------------------------------------------------


def test_planner_and_stub_satisfy_the_port():
    assert isinstance(Planner(), PlannerPort)
    assert isinstance(StubPlanner(), PlannerPort)


def test_stub_returns_valid_contract_types():
    """A stub that returns invalid contract types is worse than no stub."""
    plan = StubPlanner().plan(GPT_OSS_120B, SPARKS, LINK_SPARK_10G, "throughput", 16)

    assert isinstance(plan, ParallelismPlan)
    assert plan.kind is ParallelismKind.PIPELINE
    assert plan.pipeline_parallel == 2
    assert plan.reason.strip() and plan.rejected
    assert "stub" in plan.reason


def test_recommendation_is_the_head_of_the_ranked_list():
    """plan() and alternatives() can never disagree; the UI offers one ordering."""
    planner = Planner()
    args = (GPT_OSS_120B, SPARKS, LINK_SPARK_10G, "throughput", 16)

    chosen = planner.plan(*args, context_length=FULL_CONTEXT)
    ranked = planner.alternatives(*args, context_length=FULL_CONTEXT)

    assert ranked[0] == chosen


def test_alternatives_are_all_distinct_and_legal_overrides():
    """The UI offers these as overrides, so duplicates would be a broken menu."""
    plans = Planner().alternatives(
        LLAMA_3_3_70B, spark_cluster(4), LINK_SPARK_10G, "throughput", 16,
        context_length=8192,
    )

    degrees = [(p.tensor_parallel, p.pipeline_parallel, p.expert_parallel) for p in plans]
    assert len(degrees) == len(set(degrees))
    for plan in plans:
        assert len(plan.node_ids) == plan.world_size
        assert plan.world_size >= 1


def test_a_multi_gpu_node_is_one_host_even_though_its_world_size_is_not_one():
    """Intra-node ranks are GPUs in a box, not machines. NVLink inside a node is
    the case the "tensor parallel inside a node" rule was actually written for,
    so a single-node plan may shard -- but it still names one host."""
    twin = dataclasses.replace(WS_3090, gpu_count=2, node_id="ws-twin")
    plan = Planner(fit=FixedFit(1)).plan(
        QWEN3_30B_A3B, [twin], None, "throughput", 8, context_length=4096
    )

    assert plan.kind is ParallelismKind.SINGLE_NODE
    assert plan.node_ids == ["ws-twin"]
    assert plan.expert_parallel == 2, "MoE shards experts across the GPUs in the box"


def test_multi_gpu_single_node_override_only_emits_legal_degrees():
    """M-2 repro: a 6-GPU node with a 64-head/8-KV-head dense model.

    The override used to build ``Candidate(tp=gpus, ...)`` directly, outside
    ``legality.py``. On this shape TP=6 is illegal (64 % 6 != 0, 8 % 6 != 0)
    and would fail at load. The override must route through the same
    divisibility rule as every other candidate, land on the largest legal
    degree instead (4, here), and say so when GPUs are left idle by it.
    """
    six_gpu = dataclasses.replace(WS_3090, gpu_count=6, node_id="ws-six")
    plans = Planner(fit=FixedFit(1)).alternatives(
        LLAMA_3_3_70B, [six_gpu], None, "throughput", 8, context_length=4096
    )

    assert plans
    for plan in plans:
        tp = max(plan.tensor_parallel, 1)
        assert LLAMA_3_3_70B.num_attention_heads % tp == 0, plan.reason
        assert LLAMA_3_3_70B.num_kv_heads % tp == 0, plan.reason

    chosen = plans[0]
    assert chosen.kind is ParallelismKind.SINGLE_NODE
    assert chosen.tensor_parallel == 4, "largest TP <= 6 dividing 64 and 8 is 4"
    assert "idle" in chosen.reason


def test_multi_gpu_single_node_override_uses_every_gpu_when_it_divides_evenly():
    """The override is not just conservative -- when the degree does divide
    every GPU, none of them sit idle and the reason says nothing about it."""
    four_gpu = dataclasses.replace(WS_3090, gpu_count=4, node_id="ws-four")
    plan = Planner(fit=FixedFit(1)).plan(
        LLAMA_3_3_70B, [four_gpu], None, "throughput", 8, context_length=4096
    )

    assert plan.kind is ParallelismKind.SINGLE_NODE
    assert plan.tensor_parallel == 4
    assert "idle" not in plan.reason


def test_hybrid_splits_are_considered_rather_than_assumed_away():
    """Published sweeps found TP2/PP8 beating TP4/PP4 for one model and the
    reverse for another. Symmetry is not automatically optimal, so the ranking
    has to actually contain the hybrid combinations."""
    plans = Planner(fit=FixedFit(4)).alternatives(
        LLAMA_3_3_70B, spark_cluster(4), LINK_SPARK_10G, "throughput", 16
    )

    hybrids = [p for p in plans if p.tensor_parallel > 1 and p.pipeline_parallel > 1]
    assert hybrids, "hybrid TP x PP splits must be enumerated"
    assert any(p.kind is ParallelismKind.HYBRID for p in hybrids)


def test_explain_renders_the_plan_and_every_rejected_line():
    planner = Planner()
    plan = planner.plan(
        GPT_OSS_120B, SPARKS, LINK_SPARK_10G, "throughput", 16,
        context_length=FULL_CONTEXT,
    )
    text = planner.explain(plan)

    assert plan.reason in text
    for line in plan.rejected:
        assert line in text
    assert "10.2 GB/s" in text


def test_capacity_shortfall_is_stated_rather_than_silently_planned_around():
    """DeepSeek V3 on two Sparks cannot fit. Say so; do not emit a clean plan."""
    plan = Planner().plan(
        DEEPSEEK_V3, SPARKS, LINK_SPARK_10G, "throughput", 16, context_length=16384
    )

    assert "Warning" in plan.reason
    assert "fit check will refuse" in plan.reason


def test_impossible_capacity_at_any_node_count_is_never_reported_as_a_fit():
    """H-2 repro: ``min_nodes_required`` returns -1, meaning impossible at any
    node count in the fit calculator's search range -- a different fact than
    "needs more nodes than this cluster has" (a merely-large positive count,
    covered above). The planner used to clamp -1 into a floor of 1, let a
    single-node candidate through unchallenged, and hand it a reason that lied:
    "the model fits within 107.7 GiB of usable memory on one node." The fit
    gate still refused the launch, but the explanation shown to a user was
    exactly backwards in the one case it exists to catch.

    1 layer, 1 KV head, 10 trillion bf16 params, two Sparks: nothing fits this
    at any node count up to the search ceiling. The reason must never claim a
    fit, and the impossible-capacity warning must fire.
    """
    impossible = ModelShape(
        model_id="test/impossible",
        num_layers=1,
        hidden_size=128,
        num_attention_heads=1,
        num_kv_heads=1,
        vocab_size=32000,
        total_params=10_000_000_000_000,
        dtype="bf16",
    )
    plan = Planner().plan(
        impossible, SPARKS, LINK_SPARK_10G, "throughput", 1, context_length=4096
    )

    assert "the model fits" not in plan.reason
    assert "fits within" not in plan.reason
    assert "no node count in the search range fits this model" in plan.reason
    assert "Warning" in plan.reason
    # The impossibility lives in the REASON. It must NOT also appear as a
    # rejected entry for the plan's own chosen shape -- a plan that lists
    # itself as illegal contradicts itself (WF-5 finding); the multi-node
    # alternatives in `rejected` still carry their own honest lines.
    assert not any(
        r.startswith("single node: illegal") for r in plan.rejected
    ), plan.rejected


def test_planning_with_no_nodes_is_an_error_not_an_empty_plan():
    with pytest.raises(ValueError, match="no nodes"):
        Planner().plan(GPT_OSS_120B, [], LINK_SPARK_10G, "throughput", 16)


def test_no_bandwidth_is_hardcoded_in_the_planner():
    """The measured link is read every time. Never 10.2, never 25 -- and never
    any other two-digit-GB/s-shaped literal smuggled in later, such as 40.0 or
    12.0 typed in place of importing ``TP_VIABLE_THRESHOLD``.

    If a driver update enables GPUDirect RDMA the bandwidth roughly doubles and
    the answer must flip on its own. A literal anywhere in this package would
    silently freeze the decision at whatever the link was the day it was typed.

    The pattern is deliberately shaped like a measured bandwidth or a
    threshold (one or two digits, a decimal point, one or two more digits) so
    it does not fire on the package's legitimate small constants -- byte
    widths (``2.0``, ``4.0``), ratios (``0.99``), fractions of one -- which
    never fall in that range. Contract imports (``TP_VIABLE_THRESHOLD``, a
    name, not a number) and docstrings/comments are unaffected either way.
    """
    package = pathlib.Path(__file__).resolve().parent.parent / "control_plane" / "planner"
    banned = re.compile(r"(?<![\w.])([1-9]\d\.\d{1,2}|9\.0)(?![\w])")

    for path in sorted(package.glob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            code = line.split("#", 1)[0]
            if '"""' in line or line.strip().startswith(("#", '"', "'")):
                continue
            assert not banned.search(code), f"{path.name}:{number} hardcodes a bandwidth"


def test_thresholds_come_from_contracts():
    """The planner must not keep its own copy of a shared constant."""
    source = (
        pathlib.Path(__file__).resolve().parent.parent
        / "control_plane" / "planner" / "constants.py"
    ).read_text()

    assert "TP_VIABLE_THRESHOLD" not in source
    assert "EP_VIABLE_THRESHOLD" not in source


def test_expert_parallel_requires_a_wide_enough_cluster():
    """Below the node floor, EP is refused with that stated as the reason."""
    planner = Planner(fit=FixedFit(2))
    fast = faster_link(LINK_SPARK_10G, 50.0, gdr=True)
    narrow = spark_cluster(MIN_NODES_FOR_CROSS_NODE_EP - 1)

    plans = planner.alternatives(DEEPSEEK_V3, narrow, fast, "throughput", 32)

    assert all(p.expert_parallel <= 1 for p in plans)
    assert any(
        "EP=" in r and str(MIN_NODES_FOR_CROSS_NODE_EP) in r for r in plans[0].rejected
    )


def test_illegal_tensor_degrees_appear_in_the_rejected_list():
    """Showing the work includes showing what was never legal in the first place."""
    odd = ModelShape(
        model_id="test/odd-heads",
        num_layers=32, hidden_size=4096,
        num_attention_heads=24, num_kv_heads=6,
        vocab_size=32000, total_params=200_000_000_000, dtype="bf16",
    )
    plan = Planner().plan(odd, spark_cluster(4), LINK_SPARK_10G, "throughput", 16)

    assert any("TP=4: illegal" in r and "num_kv_heads=6" in r for r in plan.rejected)
    assert plan.tensor_parallel in (1, 2)


def test_best_available_single_node_reason_never_claims_a_fit():
    """WF-5 finding: when the model needs more nodes than the group has, the
    single-node best-available plan's lead clause claimed 'the model fits
    within N GiB' and listed its own shape in rejected."""
    plan = Planner().plan(
        LLAMA_3_3_70B, [SPARK_01], LINK_SPARK_10G, "throughput", 16,
        context_length=131072,
    )
    assert plan.kind is ParallelismKind.SINGLE_NODE
    assert "fits within" not in plan.reason
    assert "cannot hold" in plan.reason and "closest available shape" in plan.reason
    assert not any(r.startswith("single node: illegal") for r in plan.rejected), (
        "a plan must not reject its own chosen shape"
    )
