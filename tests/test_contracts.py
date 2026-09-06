"""Day-0 baseline. Pins the frozen contracts and the shared fixtures.

This file is the T+8h tripwire. It does not test anyone's component; it tests
that the things all nine components agree on have not moved. If an agent
quietly changes a contract to unblock themselves, this goes red before the
integration checkpoint rather than after it.

Owned by whoever owns day 0. Extend by adding; do not relax an assertion to
make it pass.
"""

from __future__ import annotations

import pytest

from control_plane.contracts import (
    COMM_BUFFER_BYTES,
    DEFAULT_GUARDRAIL,
    DEGRADED_TPS_THRESHOLD,
    EP_EXTRA_BUFFER_BYTES,
    EP_VIABLE_THRESHOLD,
    FRAMEWORK_OVERHEAD,
    GB10_ADDRESSABLE,
    GB10_MEM_BANDWIDTH,
    GB10_TOTAL_MEMORY,
    TP_VIABLE_THRESHOLD,
    BYTES_PER_PARAM,
    DeploymentState,
    DeviceClass,
    FitRequest,
    MemoryBreakdown,
    ModelShape,
    ParallelismKind,
    RoutingPolicy,
    TargetKind,
    Verdict,
)
from control_plane.contracts.quant import bytes_per_param, is_known_dtype, normalize_dtype
from tests.fixtures import (
    DEEPSEEK_V3,
    GPT_OSS_120B,
    LINK_SPARK_10G,
    LINK_SPARK_ETH_3090,
    LINK_SPARK_FAST,
    LLAMA_3_3_70B,
    MODEL_SHAPES,
    NODE_PROFILES,
    NODE_STATES,
    QWEN3_30B_A3B,
    SPARK_01,
    WS_3090,
    fits,
    fits_degraded,
    pp2_plan,
    wont_fit,
)

GIB = 1024**3


# ---------------------------------------------------------------------------
# Constants. Section 4.1.
# ---------------------------------------------------------------------------


def test_gb10_constants_are_the_measured_slice_not_the_nameplate():
    assert GB10_TOTAL_MEMORY == 128 * GIB
    assert GB10_ADDRESSABLE == int(119.7 * GIB)
    assert GB10_ADDRESSABLE < GB10_TOTAL_MEMORY, (
        "addressable must stay below nameplate; planning against 128 GiB is the "
        "bug this constant exists to prevent"
    )
    assert GB10_MEM_BANDWIDTH == 273.0


def test_thresholds_and_budget_terms():
    assert TP_VIABLE_THRESHOLD == 40.0
    assert EP_VIABLE_THRESHOLD == 40.0
    assert DEGRADED_TPS_THRESHOLD == 10.0
    assert DEFAULT_GUARDRAIL == 0.90
    assert COMM_BUFFER_BYTES == int(1.5 * GIB)
    assert EP_EXTRA_BUFFER_BYTES == int(2.0 * GIB)
    assert FRAMEWORK_OVERHEAD == int(1.0 * GIB)


def test_gb10_usable_memory_is_about_107_7_gib():
    usable = SPARK_01.usable_memory(DEFAULT_GUARDRAIL)
    assert 107.0 < usable / GIB < 108.0
    assert usable == int(GB10_ADDRESSABLE * DEFAULT_GUARDRAIL)


def test_discrete_profile_reserves_a_gib_for_display_and_driver():
    assert WS_3090.device_class is DeviceClass.DISCRETE
    assert WS_3090.total_memory - WS_3090.addressable_memory == GIB


# ---------------------------------------------------------------------------
# Quantization table. The systematic error this project exists to avoid.
# ---------------------------------------------------------------------------


def test_q4_k_m_is_real_bytes_not_nominal_bit_width():
    assert BYTES_PER_PARAM["q4_k_m"] == 0.6125, "4.90 bpw, not 4.0"
    assert BYTES_PER_PARAM["q4_k_m"] > 0.5, (
        "a 4-bit scheme costs more than 4 bits/param once block scales and zero "
        "points are counted; charging 0.5 under-counts and produces an OOM"
    )


def test_blackwell_formats_carry_their_scales():
    assert BYTES_PER_PARAM["mxfp4"] == 0.53125  # 4.25 bpw
    assert BYTES_PER_PARAM["nvfp4"] == 0.5625   # 4.50 bpw


def test_bare_fp4_alias_resolves_to_nvfp4_not_mxfp4():
    """A bare 'fp4' tag names no packer; NVFP4 is the more plausible read on
    this hardware than MXFP4, so it wins the ambiguous alias."""
    assert normalize_dtype("fp4") == "nvfp4"
    assert bytes_per_param("fp4") == 0.5625


def test_no_nominal_int4_entry():
    assert "int4" not in BYTES_PER_PARAM, (
        "a bare int4 at 0.5 is a nominal figure; name the actual scheme "
        "(awq_int4, gptq_int4, nvfp4, q4_0) so the scales get charged"
    )


@pytest.mark.parametrize("name,shape", sorted(MODEL_SHAPES.items()))
def test_every_fixture_dtype_resolves(name, shape):
    assert is_known_dtype(shape.dtype), f"{name}: dtype {shape.dtype!r} not in the table"
    assert shape.bytes_per_param() > 0


def test_dtype_spellings_normalize():
    assert bytes_per_param("Q4_K_M") == bytes_per_param("q4_k_m")
    assert bytes_per_param("bfloat16") == bytes_per_param("bf16")
    assert bytes_per_param("torch.float16") == 2.0


def test_unknown_dtype_raises_rather_than_guessing():
    with pytest.raises(KeyError):
        bytes_per_param("fp3_secret_sauce")


# ---------------------------------------------------------------------------
# Model shapes. Section 4.2.
# ---------------------------------------------------------------------------


def test_llama_70b_shape_and_the_kv_head_count_that_must_be_exact():
    s = LLAMA_3_3_70B
    assert (s.num_layers, s.hidden_size, s.num_attention_heads) == (80, 8192, 64)
    assert s.num_kv_heads == 8, "GQA: 8 KV heads, not 64. Agent D's KV math turns on this."
    assert not s.is_moe
    assert s.effective_active_params == s.total_params, "dense: every param is active"


def test_using_attention_heads_for_kv_overestimates_by_the_grouping_ratio():
    s = LLAMA_3_3_70B
    correct = 2 * s.num_kv_heads * s.effective_head_dim
    naive = 2 * s.num_attention_heads * s.effective_head_dim
    assert naive == correct * 8, "the classic 8x KV over-estimate"


def test_head_dim_is_not_hidden_over_heads_on_gpt_oss():
    s = GPT_OSS_120B
    assert s.hidden_size // s.num_attention_heads == 45
    assert s.effective_head_dim == 64, "config states head_dim; never divide"


def test_head_dim_is_not_hidden_over_heads_on_qwen3():
    s = QWEN3_30B_A3B
    assert s.hidden_size // s.num_attention_heads == 64
    assert s.effective_head_dim == 128


def test_moe_active_params_are_an_order_of_magnitude_below_total():
    s = GPT_OSS_120B
    assert s.is_moe and s.num_experts == 128 and s.num_experts_per_token == 4
    assert s.effective_active_params < s.total_params / 20, (
        "capacity depends on total, decode speed on active; that gap is why "
        "GPT-OSS-120B outruns a dense 70B on identical hardware"
    )


def test_gpt_oss_interleaves_full_and_windowed_attention():
    s = GPT_OSS_120B
    assert s.sliding_window == 128
    assert s.layers_with_full_attention == 18
    assert 0 < s.layers_with_full_attention < s.num_layers


def test_deepseek_uses_multi_head_latent_attention():
    assert DEEPSEEK_V3.mla_latent_dim == 512
    assert DEEPSEEK_V3.is_moe


def test_dense_shape_has_no_mla_or_window():
    assert LLAMA_3_3_70B.mla_latent_dim is None
    assert LLAMA_3_3_70B.sliding_window is None


def test_model_shape_constructs_without_mla_rope_dim_and_defaults_none():
    # A fresh construction, not a fixture: fixtures may later opt into the field,
    # but code that predates it must keep working without passing it.
    bare = ModelShape(
        model_id="test/bare",
        num_layers=2,
        hidden_size=64,
        num_attention_heads=4,
        num_kv_heads=4,
        vocab_size=1000,
        total_params=1_000_000,
        dtype="bf16",
    )
    assert bare.mla_rope_dim is None


def test_model_shape_carries_mla_rope_dim_when_passed():
    import dataclasses

    shaped = dataclasses.replace(DEEPSEEK_V3, mla_rope_dim=64)
    assert shaped.mla_rope_dim == 64
    assert shaped.mla_latent_dim == 512
    assert shaped.mla_latent_dim + shaped.mla_rope_dim == 576, (
        "true cached width per layer per token is latent + rope"
    )


# ---------------------------------------------------------------------------
# The arithmetic the demo depends on. Weights only; Agent D owns the full budget.
# ---------------------------------------------------------------------------


def _weight_bytes(shape) -> float:
    return shape.total_params * shape.bytes_per_param()


def test_llama_70b_bf16_does_not_fit_one_spark():
    assert _weight_bytes(LLAMA_3_3_70B) > SPARK_01.usable_memory(), (
        "weights alone exceed one node: the WONT_FIT case, minimum 2 nodes"
    )


def test_llama_70b_fp8_weights_do_fit_one_spark():
    import dataclasses

    fp8 = dataclasses.replace(LLAMA_3_3_70B, dtype="fp8")
    assert _weight_bytes(fp8) < SPARK_01.usable_memory()


def test_gpt_oss_120b_mxfp4_fits_one_spark():
    assert _weight_bytes(GPT_OSS_120B) < SPARK_01.usable_memory()


def test_qwen3_30b_fits_one_spark_so_the_planner_must_not_cross_the_link():
    assert _weight_bytes(QWEN3_30B_A3B) < SPARK_01.usable_memory()


def test_deepseek_v3_needs_more_than_five_sparks():
    import math

    needed = math.ceil(_weight_bytes(DEEPSEEK_V3) / SPARK_01.usable_memory())
    assert needed >= 6


# ---------------------------------------------------------------------------
# Link measurement. Section 4.1. The number the product turns on.
# ---------------------------------------------------------------------------


def test_the_measured_spark_link_is_below_the_tensor_parallel_threshold():
    assert LINK_SPARK_10G.all_reduce_gbps == 10.2
    assert LINK_SPARK_10G.gpudirect_rdma is False
    assert LINK_SPARK_10G.all_reduce_gbps < TP_VIABLE_THRESHOLD, (
        "this single comparison is the demo: NVIDIA's playbook says TP=2, the "
        "measured link says otherwise"
    )
    assert LINK_SPARK_10G.sendrecv_gbps < LINK_SPARK_10G.all_reduce_gbps


def test_a_faster_link_would_cross_the_threshold_the_other_way():
    assert LINK_SPARK_FAST.all_reduce_gbps >= TP_VIABLE_THRESHOLD
    assert LINK_SPARK_FAST.gpudirect_rdma is True


def test_the_ethernet_path_to_a_discrete_box_is_two_orders_down():
    assert LINK_SPARK_ETH_3090.all_reduce_gbps < LINK_SPARK_10G.all_reduce_gbps / 5
    assert LINK_SPARK_ETH_3090.method == "tcp"


# ---------------------------------------------------------------------------
# Plan and fit. Section 4.3.
# ---------------------------------------------------------------------------


def test_world_size_excludes_expert_parallel():
    plan = pp2_plan()
    assert plan.world_size == plan.tensor_parallel * plan.pipeline_parallel * plan.data_parallel
    assert plan.world_size == 2


def test_a_plan_always_shows_its_work():
    plan = pp2_plan()
    assert plan.kind is ParallelismKind.PIPELINE
    assert plan.reason.strip(), "the reason is displayed verbatim in the UI"
    assert str(plan.measured_link_gbps) in plan.reason
    assert plan.rejected, "an alternative existed, so it must be listed with a why"


def test_memory_breakdown_total_is_the_sum_of_its_terms():
    b = MemoryBreakdown(
        weights=10, kv_cache=20, activations=30,
        comm_buffers=40, replicated=50, framework_overhead=60,
    )
    assert b.total == 210


def _fit_request(shape=LLAMA_3_3_70B, **kwargs) -> FitRequest:
    return FitRequest(
        shape=shape,
        context_length=8192,
        max_concurrent_seqs=16,
        kv_dtype="auto",
        plan=pp2_plan(),
        **kwargs,
    )


def test_fit_request_constructs_without_weight_bytes_and_defaults_none():
    req = _fit_request()
    assert req.weight_bytes is None, (
        "weight_bytes is additive; every existing FitRequest() call site must "
        "keep working without naming it"
    )


def test_fit_request_carries_weight_bytes_when_passed():
    measured = 141_107_412_992
    req = _fit_request(weight_bytes=measured)
    assert req.weight_bytes == measured


def test_degraded_is_not_failure():
    assert fits().ok is True
    assert fits_degraded().ok is True, (
        "FITS_DEGRADED loads fine and sometimes that is what someone wants; "
        "callers must not treat it as a refusal"
    )
    assert wont_fit().ok is False


def test_a_refusal_names_the_term_and_the_fix():
    r = wont_fit()
    assert r.verdict is Verdict.WONT_FIT
    assert r.limiting_term, "every refusal names the term that blew the budget"
    assert len(r.reason) > 20, "'does not fit' is a failed refusal"


def test_degraded_result_carries_a_throughput_number_below_threshold():
    r = fits_degraded()
    assert r.verdict is Verdict.FITS_DEGRADED
    assert r.predicted_decode_tps is not None
    assert r.predicted_decode_tps < DEGRADED_TPS_THRESHOLD


# ---------------------------------------------------------------------------
# Enum surfaces the UI and gateway code against.
# ---------------------------------------------------------------------------


def test_enums_are_string_valued_so_they_serialize_to_json_unchanged():
    assert Verdict.WONT_FIT == "wont_fit"
    assert ParallelismKind.PIPELINE == "pipeline"
    assert DeploymentState.READY == "ready"
    assert DeviceClass.GB10 == "gb10"
    assert RoutingPolicy.LOCAL_FIRST == "local_first"
    assert TargetKind.REMOTE == "remote"


def test_the_seven_deployment_states_exist():
    assert {s.value for s in DeploymentState} == {
        "planned", "launching", "ready", "degraded", "failed", "stopping", "stopped",
    }


# ---------------------------------------------------------------------------
# Fixture set completeness. README section "Day 0".
# ---------------------------------------------------------------------------


def test_the_minimum_fixture_set_is_present():
    assert set(MODEL_SHAPES) == {
        "llama-3.3-70b", "gpt-oss-120b", "qwen3-30b-a3b", "deepseek-v3",
    }
    gb10 = [p for p in NODE_PROFILES.values() if p.device_class is DeviceClass.GB10]
    discrete = [p for p in NODE_PROFILES.values() if p.device_class is DeviceClass.DISCRETE]
    assert len(gb10) == 2 and len(discrete) == 1


def test_node_states_cover_every_profile_and_are_healthy():
    assert set(NODE_STATES) == set(NODE_PROFILES)
    assert all(s.healthy for s in NODE_STATES.values())
    assert all(s.memory_used <= s.profile.total_memory for s in NODE_STATES.values())
