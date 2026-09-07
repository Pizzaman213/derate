"""Shared day-0 test fixtures.

Written once alongside the contracts so every agent tests against the same
model shapes, node profiles, and link measurement. Frozen: extend by adding,
never by editing an existing value, or two agents' tests disagree.

Minimum set required by README.md:
  llama-3.3-70b   dense, GQA
  gpt-oss-120b    MoE, MXFP4, sliding window
  qwen3-30b-a3b   MoE, fits one node
  deepseek-v3     MLA
  two GB10 node profiles, one 3090 profile
  one LinkMeasurement at 10.2 GB/s with gpudirect_rdma=False
"""

from __future__ import annotations

from control_plane.contracts import (
    GB10_ADDRESSABLE,
    GB10_MEM_BANDWIDTH,
    GB10_TOTAL_MEMORY,
    DeviceClass,
    FitResult,
    LinkMeasurement,
    MemoryBreakdown,
    ModelShape,
    NodeProfile,
    NodeState,
    ParallelismKind,
    ParallelismPlan,
    Verdict,
)

GIB = 1024**3

# --------------------------------------------------------------------------
# Model shapes
# --------------------------------------------------------------------------

LLAMA_3_3_70B = ModelShape(
    model_id="meta-llama/Llama-3.3-70B-Instruct",
    num_layers=80,
    hidden_size=8192,
    num_attention_heads=64,
    num_kv_heads=8,
    vocab_size=128256,
    total_params=70_553_706_496,
    dtype="bf16",
    head_dim=128,
)

GPT_OSS_120B = ModelShape(
    model_id="openai/gpt-oss-120b",
    num_layers=36,
    hidden_size=2880,
    num_attention_heads=64,
    num_kv_heads=8,
    vocab_size=201088,
    total_params=116_800_000_000,
    dtype="mxfp4",
    head_dim=64,
    num_experts=128,
    num_experts_per_token=4,
    active_params=5_130_000_000,
    sliding_window=128,
    layers_with_full_attention=18,
)

QWEN3_30B_A3B = ModelShape(
    model_id="Qwen/Qwen3-30B-A3B",
    num_layers=48,
    hidden_size=2048,
    num_attention_heads=32,
    num_kv_heads=4,
    vocab_size=151936,
    total_params=30_530_000_000,
    dtype="bf16",
    head_dim=128,
    num_experts=128,
    num_experts_per_token=8,
    active_params=3_338_000_000,
)

DEEPSEEK_V3 = ModelShape(
    model_id="deepseek-ai/DeepSeek-V3",
    num_layers=61,
    hidden_size=7168,
    num_attention_heads=128,
    num_kv_heads=128,
    vocab_size=129280,
    total_params=671_000_000_000,
    dtype="fp8",
    head_dim=128,
    num_experts=256,
    num_experts_per_token=8,
    active_params=37_000_000_000,
    mla_latent_dim=512,
    mla_rope_dim=64,  # qk_rope_head_dim in the published config
)

MODEL_SHAPES: dict[str, ModelShape] = {
    "llama-3.3-70b": LLAMA_3_3_70B,
    "gpt-oss-120b": GPT_OSS_120B,
    "qwen3-30b-a3b": QWEN3_30B_A3B,
    "deepseek-v3": DEEPSEEK_V3,
}

# --------------------------------------------------------------------------
# Node profiles
# --------------------------------------------------------------------------

SPARK_01 = NodeProfile(
    node_id="spark-01",
    hostname="spark-01",
    address="192.168.11.13",
    device_class=DeviceClass.GB10,
    gpu_name="NVIDIA GB10",
    gpu_count=1,
    total_memory=GB10_TOTAL_MEMORY,
    addressable_memory=GB10_ADDRESSABLE,
    memory_bandwidth_gbps=GB10_MEM_BANDWIDTH,
    compute_capability="12.1",
    driver_version="580.95.05",
)

SPARK_02 = NodeProfile(
    node_id="spark-02",
    hostname="spark-02",
    address="192.168.11.14",
    device_class=DeviceClass.GB10,
    gpu_name="NVIDIA GB10",
    gpu_count=1,
    total_memory=GB10_TOTAL_MEMORY,
    addressable_memory=GB10_ADDRESSABLE,
    memory_bandwidth_gbps=GB10_MEM_BANDWIDTH,
    compute_capability="12.1",
    driver_version="580.95.05",
)

WS_3090 = NodeProfile(
    node_id="ws-3090",
    hostname="workstation",
    address="192.168.11.20",
    device_class=DeviceClass.DISCRETE,
    gpu_name="NVIDIA GeForce RTX 3090",
    gpu_count=1,
    total_memory=24 * GIB,
    # Agent A's probe subtracts 1 GiB from a discrete card's memory.total
    # for display and driver context. The fixture has to match the probe.
    addressable_memory=23 * GIB,
    memory_bandwidth_gbps=936.0,
    compute_capability="8.6",
    driver_version="580.95.05",
)

NODE_PROFILES: dict[str, NodeProfile] = {
    p.node_id: p for p in (SPARK_01, SPARK_02, WS_3090)
}

# --------------------------------------------------------------------------
# Link measurement: the number the whole product turns on
# --------------------------------------------------------------------------

LINK_SPARK_10G = LinkMeasurement(
    src="spark-01",
    dst="spark-02",
    all_reduce_gbps=10.2,
    sendrecv_gbps=9.0,
    latency_us=40.0,
    gpudirect_rdma=False,
    measured_at=1757193600.0,
    method="nccl-tests",
)

# --------------------------------------------------------------------------
# Builders. Plans and fits are Agent E's and Agent D's output; these are
# hand-built stand-ins so downstream tests do not need those components.
# --------------------------------------------------------------------------


def node_state(
    profile: NodeProfile,
    *,
    healthy: bool = True,
    memory_used_pct: float = 20.0,
    last_seen: float = 1757193600.0,
) -> NodeState:
    """A live NodeState wrapping *profile* at a given memory percentage."""
    return NodeState(
        profile=profile,
        healthy=healthy,
        last_seen=last_seen,
        memory_used=int(profile.total_memory * memory_used_pct / 100.0),
        power_watts=71.0,
        temperature_c=62.0,
        utilization_pct=40.0,
        # A fixture node is one that reports: its readings are as current as
        # its last_seen. Leaving this 0.0 would make every fixture look like a
        # node whose telemetry has died.
        sample_ts=last_seen,
    )


def pp2_plan(node_ids: list[str] | None = None) -> ParallelismPlan:
    """The demo plan: PP=2 across two Sparks on a measured 10.2 GB/s link."""
    return ParallelismPlan(
        kind=ParallelismKind.PIPELINE,
        tensor_parallel=1,
        pipeline_parallel=2,
        expert_parallel=1,
        data_parallel=1,
        node_ids=list(node_ids or ["spark-01", "spark-02"]),
        reason=(
            "Pipeline parallel across 2 nodes: measured all-reduce of 10.2 GB/s "
            "is far below the 40 GB/s tensor-parallel threshold."
        ),
        measured_link_gbps=10.2,
        rejected=["TP=2: link 10.2 GB/s below 40 GB/s threshold"],
    )


def single_node_plan(node_id: str = "spark-01") -> ParallelismPlan:
    return ParallelismPlan(
        kind=ParallelismKind.SINGLE_NODE,
        tensor_parallel=1,
        pipeline_parallel=1,
        expert_parallel=1,
        data_parallel=1,
        node_ids=[node_id],
        reason="Fits on one node; no interconnect involved.",
        measured_link_gbps=0.0,
        rejected=[],
    )


def fits(reason: str = "Fits with 24.1 GiB headroom per node.") -> FitResult:
    return FitResult(
        verdict=Verdict.FITS,
        breakdown=MemoryBreakdown(
            weights=62 * GIB,
            kv_cache=12 * GIB,
            activations=2 * GIB,
            comm_buffers=1536 * 1024**2,
            replicated=0,
            framework_overhead=GIB,
        ),
        usable_per_node=int(GB10_ADDRESSABLE * 0.90),
        headroom=24 * GIB,
        reason=reason,
        limiting_term="weights",
        max_context_that_fits=131072,
        predicted_decode_tps=42.0,
        warnings=[],
    )


def wont_fit(
    reason: str = (
        "Needs 142.3 GiB per node but only 107.7 GiB is usable. "
        "Drop context to 16384 or add a third node."
    ),
) -> FitResult:
    return FitResult(
        verdict=Verdict.WONT_FIT,
        breakdown=MemoryBreakdown(
            weights=118 * GIB,
            kv_cache=21 * GIB,
            activations=2 * GIB,
            comm_buffers=1536 * 1024**2,
            replicated=0,
            framework_overhead=GIB,
        ),
        usable_per_node=int(GB10_ADDRESSABLE * 0.90),
        headroom=-(35 * GIB),
        reason=reason,
        # "combined": weights plus KV together blow the budget, so the 16384
        # context suggestion is meaningful. (A pure weights-limited refusal
        # carries max_context_that_fits=None — no context change can fix it.)
        limiting_term="combined",
        max_context_that_fits=16384,
        predicted_decode_tps=None,
        warnings=["MoE expert buffers are the dominant term"],
    )


def fits_degraded(
    reason: str = "Loads, but predicted decode is 4.1 tok/s. Usable for batch, not chat.",
) -> FitResult:
    r = fits(reason)
    r.verdict = Verdict.FITS_DEGRADED
    r.predicted_decode_tps = 4.1
    r.limiting_term = "bandwidth"
    return r


# --------------------------------------------------------------------------
# --- day-0 additions: link variants, states, and registries ---------------
#
# LINK_SPARK_10G above is the measurement the demo turns on. These are the
# other link states the planner and the UI have to handle. They live here so
# that Agent E's "what if the link were fast" test and Agent H's edge-rendering
# test are talking about the same numbers.
# --------------------------------------------------------------------------

#: The counterfactual. A driver update enables GPUDirect RDMA, bandwidth roughly
#: doubles past TP_VIABLE_THRESHOLD, and the planner's answer must flip to
#: tensor parallel on its own. Agent E's proof that nothing is hardcoded.
LINK_SPARK_FAST = LinkMeasurement(
    src="spark-01",
    dst="spark-02",
    all_reduce_gbps=50.0,
    sendrecv_gbps=45.0,
    latency_us=12.0,
    gpudirect_rdma=True,
    measured_at=1757193600.0,
    method="nccl-tests",
)

#: Heterogeneous pairing over the management LAN. Two orders of magnitude down
#: from the ConnectX-7 path; the planner must not pool these nodes by default.
LINK_SPARK_ETH_3090 = LinkMeasurement(
    src="spark-01",
    dst="ws-3090",
    all_reduce_gbps=1.1,
    sendrecv_gbps=1.0,
    latency_us=180.0,
    gpudirect_rdma=False,
    measured_at=1757193600.0,
    method="tcp",
)

#: Eight days old. Past the seven-day window, so it reports stale. The planner
#: may still use it; the UI marks it. Same figures as LINK_SPARK_10G otherwise.
LINK_SPARK_STALE = LinkMeasurement(
    src="spark-01",
    dst="spark-02",
    all_reduce_gbps=10.2,
    sendrecv_gbps=9.0,
    latency_us=40.0,
    gpudirect_rdma=False,
    measured_at=1757193600.0 - 8 * 24 * 3600,
    method="nccl-tests",
)

#: Keyed by unordered node pair, which is how Agent B persists them.
LINKS: dict[tuple[str, str], LinkMeasurement] = {
    tuple(sorted((m.src, m.dst))): m  # type: ignore[misc]
    for m in (LINK_SPARK_10G, LINK_SPARK_ETH_3090)
}

#: Live states for the three profiles, all healthy. Agent A's day-0 stub
#: returns exactly these.
NODE_STATES: dict[str, NodeState] = {
    "spark-01": node_state(SPARK_01, memory_used_pct=78.0),
    "spark-02": node_state(SPARK_02, memory_used_pct=31.0),
    "ws-3090": node_state(WS_3090, memory_used_pct=22.0),
}

GB10_PROFILES: tuple[NodeProfile, NodeProfile] = (SPARK_01, SPARK_02)
