"""deploy/relaunch.py: the launch body that reproduces a deployment.

Every key it writes must be one the launch route's own parsers accept and
read back to the same value -- a body the route refuses, or reads as
something else, is a relaunch that quietly launches a different thing.
"""

from __future__ import annotations

import shlex

from control_plane.contracts import (
    Deployment,
    DeploymentState,
    EngineOptions,
    FitResult,
    Lifecycle,
    MemoryBreakdown,
    ModelShape,
    ParallelismKind,
    ParallelismPlan,
    SpeculativeMethod,
    SpeculativeSpec,
    Verdict,
)
from control_plane.deploy.relaunch import launch_request_from
from control_plane.gateway.internal_api import (
    _parse_degrees,
    _parse_engine_options,
    _parse_node_ids,
    _parse_speculative,
)


def _deployment(**kw) -> Deployment:
    shape = ModelShape(
        model_id="Qwen/Qwen3-8B",
        num_layers=36,
        hidden_size=4096,
        num_attention_heads=32,
        num_kv_heads=8,
        vocab_size=151936,
        total_params=8_000_000_000,
        dtype="bf16",
    )
    plan = ParallelismPlan(
        kind=ParallelismKind.TENSOR,
        tensor_parallel=2,
        pipeline_parallel=1,
        expert_parallel=1,
        data_parallel=1,
        node_ids=["spark-01", "spark-02"],
        reason="test",
        measured_link_gbps=19.2,
        rejected=[],
    )
    fit = FitResult(
        verdict=Verdict.FITS,
        breakdown=MemoryBreakdown(1, 1, 1, 0, 0, 1),
        usable_per_node=10,
        headroom=5,
        reason="fits",
        limiting_term="none",
        max_context_that_fits=32768,
        predicted_decode_tps=20.0,
    )
    base = dict(
        deployment_id="d-1",
        served_name="qwen3-8b",
        shape=shape,
        plan=plan,
        fit=fit,
        runtime="vllm",
        state=DeploymentState.STOPPED,
        backend_url=None,
        context_length=16384,
        max_concurrent_seqs=8,
        started_at=None,
        last_error=None,
    )
    base.update(kw)
    return Deployment(**base)


def test_every_option_round_trips_through_the_launch_parsers():
    dep = _deployment(
        served_name="my-qwen",
        kv_dtype="fp8",
        quantization="fp8",
        speculative=SpeculativeSpec(
            method=SpeculativeMethod.EAGLE3,
            num_speculative_tokens=3,
            draft_bytes=1,
            draft_params=1,
            model="org/head-eagle3",
        ),
        extra_args=("--max-num-partial-prefills", "2"),
        enforce_eager=True,
        trust_remote_code=True,
        engine_options=EngineOptions(
            reasoning_parser="qwen3",
            tool_call_parser="hermes",
            prefix_caching=False,
            max_num_batched_tokens=4096,
            async_scheduling=True,
            limit_mm_per_prompt=(("image", 2),),
            mm_processor_cache_gb=1.5,
            rope_factor=2.0,
            generation_overrides=(("temperature", 0.6), ("top_k", 20.0)),
            env=(("NCCL_MAX_NCHANNELS", "2"),),
        ),
        lifecycle=Lifecycle(idle_unload_s=900, priority="high", start_at_boot=True),
    )
    body = launch_request_from(dep)

    assert body["model_id"] == "Qwen/Qwen3-8B"
    assert body["served_name"] == "my-qwen"
    assert body["runtime"] == "vllm"
    assert body["context"] == 16384
    assert body["concurrency"] == 8
    assert _parse_node_ids(body) == ["spark-01", "spark-02"]
    assert _parse_degrees(body) == {
        "tensor_parallel": 2,
        "pipeline_parallel": 1,
        "expert_parallel": 1,
        "data_parallel": 1,
    }
    assert _parse_speculative(body) == ("eagle3", 3, "org/head-eagle3")
    assert body["kv_dtype"] == "fp8"
    assert body["dtype"] == "fp8"
    assert tuple(shlex.split(body["extra_args"])) == dep.extra_args
    assert body["enforce_eager"] is True
    assert body["trust_remote_code"] is True
    assert _parse_engine_options(body) == dep.engine_options
    assert body["lifecycle"] == {
        "idle_unload_s": 900, "priority": "high", "start_at_boot": True,
    }
    assert None not in body.values()


def test_custom_command_and_capture_sizes_round_trip():
    dep = _deployment(
        custom_command=("--max-model-len", "4096", "--gpu-memory-utilization", "0.5"),
        cudagraph_capture_sizes=(1, 2, 4),
        speculative_tokens=2,
    )
    body = launch_request_from(dep)
    assert tuple(shlex.split(body["custom_command"])) == dep.custom_command
    assert body["cudagraph_capture_sizes"] == [1, 2, 4]
    assert body["speculative_tokens"] == 2


def test_defaults_send_only_the_placement_and_the_model():
    body = launch_request_from(_deployment())
    assert set(body) == {
        "model_id", "served_name", "runtime", "context", "concurrency",
        "node_ids", "parallelism",
    }
    assert _parse_engine_options(body) == EngineOptions()
