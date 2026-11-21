"""Typed engine options: parsed, sized, rendered, refused and stored.

Same contract as `kv_dtype`: a knob the gate or the request saw must reach
the serve command or be refused -- never dropped.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from control_plane.contracts import (
    Deployment,
    DeploymentState,
    EngineOptions,
    FitRequest,
    Lifecycle,
    ModelShape,
    ParallelismKind,
    ParallelismPlan,
    Verdict,
)
from control_plane.deploy import recipes
from control_plane.deploy.flags import (
    HF_ROPE_KEY,
    engine_options_refusal,
    render_engine_options,
)
from control_plane.deploy.store import decode, encode
from control_plane.fit import FitCalculator
from control_plane.gateway.internal_api import _parse_engine_options, _without_vision
from tests.fixtures import SPARK_01

JSON_GRAMMAR = re.compile(r'^\{[A-Za-z0-9_":.,{}-]*\}$')

EVERYTHING = EngineOptions(
    reasoning_parser="qwen3",
    tool_call_parser="hermes",
    prefix_caching=True,
    max_num_batched_tokens=8192,
    async_scheduling=True,
    limit_mm_per_prompt=(("image", 4),),
    mm_processor_cache_gb=2.0,
    language_model_only=True,
    rope_factor=4.0,
    generation_overrides=(("temperature", 0.7), ("top_k", 40.0), ("top_p", 0.9)),
    env=(("NCCL_MAX_NCHANNELS", "2"),),
)


def _shape(**kw) -> ModelShape:
    base = dict(
        model_id="Qwen/Qwen3-0.6B",
        num_layers=28,
        hidden_size=1024,
        num_attention_heads=16,
        num_kv_heads=8,
        vocab_size=151936,
        total_params=600_000_000,
        dtype="bf16",
    )
    base.update(kw)
    return ModelShape(**base)


def _plan(tp: int = 1) -> ParallelismPlan:
    return ParallelismPlan(
        kind=ParallelismKind.SINGLE_NODE,
        tensor_parallel=tp,
        pipeline_parallel=1,
        expert_parallel=1,
        data_parallel=1,
        node_ids=["spark-01"],
        reason="test",
        measured_link_gbps=None,
        rejected=[],
    )


# ---- rendering ---------------------------------------------------------------


def test_every_field_renders_in_order_and_json_passes_its_grammar():
    tokens = render_engine_options(EVERYTHING, original_window=32768)
    flags = [t for t in tokens if t.startswith("--")]
    assert flags == [
        "--reasoning-parser",
        "--enable-auto-tool-choice",
        "--tool-call-parser",
        "--enable-prefix-caching",
        "--max-num-batched-tokens",
        "--async-scheduling",
        "--limit-mm-per-prompt",
        "--mm-processor-cache-gb",
        "--language-model-only",
        "--hf-overrides",
        "--override-generation-config",
    ]
    after = {tokens[i]: tokens[i + 1] for i in range(len(tokens) - 1)}
    assert after["--reasoning-parser"] == "qwen3"
    assert after["--tool-call-parser"] == "hermes"
    assert after["--max-num-batched-tokens"] == "8192"
    assert after["--mm-processor-cache-gb"] == "2"
    for flag in ("--limit-mm-per-prompt", "--hf-overrides", "--override-generation-config"):
        assert JSON_GRAMMAR.match(after[flag]), after[flag]
    assert json.loads(after["--limit-mm-per-prompt"]) == {"image": 4}
    assert json.loads(after["--hf-overrides"]) == {
        HF_ROPE_KEY: {
            "rope_type": "yarn",
            "factor": 4.0,
            "original_max_position_embeddings": 32768,
        }
    }
    assert json.loads(after["--override-generation-config"]) == {
        "temperature": 0.7, "top_k": 40, "top_p": 0.9,
    }


def test_prefix_caching_off_renders_the_negative_flag():
    assert render_engine_options(
        EngineOptions(prefix_caching=False), original_window=None
    ) == ("--no-enable-prefix-caching",)


def test_the_recipe_carries_every_flag_and_the_json_as_quoted_defaults(tmp_path: Path):
    recipe = recipes.synthesize(
        _shape(),
        _plan(),
        "vllm",
        4096,
        2,
        "qwen3-0.6b",
        port=8100,
        gpu_memory_utilization=0.5,
        recipe_dir=tmp_path,
        engine_options=EngineOptions(
            reasoning_parser="qwen3",
            max_num_batched_tokens=4096,
            limit_mm_per_prompt=(("image", 2),),
        ),
    )
    text = recipe.content
    assert "--reasoning-parser qwen3" in text
    assert "--max-num-batched-tokens 4096" in text
    assert "--limit-mm-per-prompt '{limit_mm_per_prompt}'" in text
    assert '  limit_mm_per_prompt: "{\\"image\\":2}"\n' in text


def test_a_custom_command_refuses_engine_options(tmp_path: Path):
    with pytest.raises(ValueError, match="custom command"):
        recipes.synthesize(
            _shape(), _plan(), "vllm", 4096, 2, "qwen3-0.6b",
            port=8100, gpu_memory_utilization=0.5, recipe_dir=tmp_path,
            custom_command=("--max-model-len", "4096"),
            engine_options=EngineOptions(async_scheduling=True),
        )


# ---- refusals ----------------------------------------------------------------


def test_sglang_refuses_a_vllm_option_by_name():
    refusal = engine_options_refusal(EngineOptions(reasoning_parser="qwen3"), "sglang")
    assert refusal == "reasoning_parser is a vLLM option; sglang launches do not take it."


def test_env_alone_is_not_a_vllm_only_option():
    assert engine_options_refusal(
        EngineOptions(env=(("NCCL_MAX_NCHANNELS", "2"),)), "sglang"
    ) is None


def test_env_on_a_single_rank_launch_is_refused():
    assert engine_options_refusal(
        EngineOptions(env=(("NCCL_MAX_NCHANNELS", "2"),)), "vllm", world_size=1
    )


def test_a_parser_outside_the_image_registry_is_refused():
    with pytest.raises(ValueError, match="not a reasoning parser in this vLLM build"):
        render_engine_options(EngineOptions(reasoning_parser="nope"), original_window=None)
    with pytest.raises(ValueError, match="not a reasoning parser in this vLLM build"):
        _parse_engine_options({"engine_options": {"reasoning_parser": "nope"}})
    with pytest.raises(ValueError, match="not a tool-call parser"):
        _parse_engine_options({"engine_options": {"tool_call_parser": "nope"}})


@pytest.mark.parametrize(
    "raw",
    [
        {"unknown": 1},
        {"rope_factor": 1.0},
        {"rope_factor": 9},
        {"max_num_batched_tokens": "4096"},
        {"limit_mm_per_prompt": {"image": 65}},
        {"limit_mm_per_prompt": {"pdf": 1}},
        {"generation_overrides": {"top_p": 0}},
        {"generation_overrides": {"top_k": 1.5}},
        {"env": {"NOT_NCCL": "1"}},
        {"env": {"NCCL_MAX_NCHANNELS": "2 3"}},
        {"async_scheduling": "yes"},
    ],
)
def test_bad_engine_options_are_refused(raw):
    with pytest.raises(ValueError):
        _parse_engine_options({"engine_options": raw})


def test_the_wire_form_parses_to_the_contract():
    parsed = _parse_engine_options({"engine_options": {
        "reasoning_parser": "qwen3",
        "tool_call_parser": "hermes",
        "prefix_caching": True,
        "max_num_batched_tokens": 8192,
        "async_scheduling": True,
        "limit_mm_per_prompt": {"image": 4},
        "mm_processor_cache_gb": 2,
        "language_model_only": True,
        "rope_factor": 4,
        "generation_overrides": {"top_p": 0.9, "temperature": 0.7, "top_k": 40},
        "env": {"NCCL_MAX_NCHANNELS": "2"},
    }})
    assert parsed == EVERYTHING


# ---- sizing ------------------------------------------------------------------


def test_language_model_only_on_a_text_model_raises():
    with pytest.raises(ValueError, match="no vision tower"):
        _without_vision(_shape(vision_params=0), None)


def test_language_model_only_drops_the_replicated_vision_tower():
    vl = _shape(model_id="Qwen/Qwen3-VL-8B", total_params=8_700_000_000,
                vision_params=600_000_000)
    text, weight_bytes = _without_vision(vl, 17_400_000_000)
    assert text.vision_params == 0
    assert weight_bytes == 17_400_000_000 - int(600_000_000 * vl.bytes_per_param())

    calc = FitCalculator()

    def check(shape, wb):
        return calc.check(
            FitRequest(shape=shape, context_length=8192, max_concurrent_seqs=1,
                       kv_dtype="auto", plan=_plan(), weight_bytes=wb),
            [SPARK_01],
        )

    with_vision = check(vl, 17_400_000_000)
    without = check(text, weight_bytes)
    assert with_vision.breakdown.replicated > 0
    assert without.breakdown.replicated == 0
    # The tower was replicated, not sharded: the shardable weights are the
    # same either way and the whole saving is the replicated term.
    assert without.breakdown.weights == with_vision.breakdown.weights
    assert with_vision.breakdown.total - without.breakdown.total == int(
        600_000_000 * vl.bytes_per_param()
    )


def test_yarn_stretches_the_window_the_gate_allows():
    calc = FitCalculator()

    def check(native):
        return calc.check(
            FitRequest(shape=_shape(), context_length=131072, max_concurrent_seqs=1,
                       kv_dtype="auto", plan=_plan(), native_window=native),
            [SPARK_01],
        )

    assert check(32768).verdict is Verdict.WONT_FIT
    assert check(int(32768 * 4.0)).verdict is not Verdict.WONT_FIT


def test_batched_tokens_and_processor_cache_reach_the_breakdown():
    calc = FitCalculator()
    base = FitRequest(shape=_shape(), context_length=8192, max_concurrent_seqs=1,
                      kv_dtype="auto", plan=_plan())
    plain = calc.check(base, [SPARK_01])
    cached = calc.check(
        FitRequest(**{**base.__dict__, "extra_replicated_bytes": 2 * 2**30}), [SPARK_01]
    )
    assert cached.breakdown.replicated - plain.breakdown.replicated == 2 * 2**30
    assert any("multimodal processor cache" in w for w in cached.warnings)
    chunked = calc.check(
        FitRequest(**{**base.__dict__, "chunk_tokens": 512}), [SPARK_01]
    )
    assert chunked.breakdown.activations <= plain.breakdown.activations


# ---- storage -----------------------------------------------------------------


def test_the_store_round_trips_every_field():
    from tests.unit.test_relaunch import _deployment

    dep = _deployment(
        engine_options=EVERYTHING,
        lifecycle=Lifecycle(idle_unload_s=3600, priority="pinned", start_at_boot=True),
        original_window=32768,
    )
    back = decode(json.loads(json.dumps(encode(dep))))
    assert back.engine_options == EVERYTHING
    assert back.lifecycle == dep.lifecycle
    assert back.original_window == 32768


def test_a_record_without_the_fields_decodes_to_defaults():
    from tests.unit.test_relaunch import _deployment

    raw = encode(_deployment())
    for key in ("engine_options", "lifecycle", "original_window"):
        raw.pop(key)
    back = decode(raw)
    assert back.engine_options == EngineOptions()
    assert back.lifecycle == Lifecycle()
    assert back.original_window is None
    assert isinstance(back, Deployment) and back.state is DeploymentState.STOPPED
