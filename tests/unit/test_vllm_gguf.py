"""A hub GGUF launched on vllm, through vllm-gguf-plugin.

The plugin reads the GGUF's weights and nothing else: the config and tokenizer
come from the repository the file was quantized from, and a vision-language
model's projector is a second `.gguf`. Every test here pins one of the seams
that carries those facts from the hub to the rendered command -- a dropped one
is a launch that clears every gate and dies at load.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from control_plane.contracts import (
    ModelShape,
    ParallelismKind,
    ParallelismPlan,
    SpeculativeMethod,
    SpeculativeSpec,
)
from control_plane.deploy import recipes
from control_plane.deploy.flags import (
    VLLM_GGUF_SPECULATIVE_REFUSAL,
    render_gguf_loader_config,
)
from control_plane.fit.calculator import replicated_bytes_per_rank
from control_plane.gateway.internal_api import _with_gguf_projector
from control_plane.resolver import ModelResolver
from control_plane.resolver.cache import ShapeCache
from control_plane.resolver.hf import ModelInfo
from control_plane.resolver.support import build_verdict
from control_plane.resolver.types import Resolution, ParamSource, QuantSource

BLOB = "unsloth/Qwen3.5-4B-GGUF/Qwen3.5-4B-Q4_K_M.gguf"
BASE = "Qwen/Qwen3.5-4B"
PROJECTOR = "unsloth/Qwen3.5-4B-GGUF/mmproj-BF16.gguf"


def _shape(model_id: str = BLOB, vision_params: int = 0) -> ModelShape:
    return ModelShape(
        model_id=model_id,
        num_layers=32,
        hidden_size=2560,
        num_attention_heads=16,
        num_kv_heads=4,
        vocab_size=248320,
        total_params=4_000_000_000,
        dtype="q4_k_m",
        vision_params=vision_params,
    )


def _plan() -> ParallelismPlan:
    return ParallelismPlan(
        kind=ParallelismKind.SINGLE_NODE,
        tensor_parallel=1,
        pipeline_parallel=1,
        expert_parallel=1,
        data_parallel=1,
        node_ids=["spark-01"],
        reason="one machine holds it",
        measured_link_gbps=None,
        rejected=[],
    )


def _recipe(tmp_path: Path, **kw):
    return recipes.synthesize(
        _shape(),
        _plan(),
        "vllm",
        8192,
        4,
        "qwen3.5-4b",
        port=8100,
        gpu_memory_utilization=0.5,
        recipe_dir=tmp_path,
        **kw,
    )


# ---- the recipe -------------------------------------------------------------


def test_the_recipe_carries_the_base_the_projector_and_the_hub(tmp_path):
    body = _recipe(tmp_path, gguf_base=BASE, gguf_projector=PROJECTOR).content
    assert "model: unsloth/Qwen3.5-4B-GGUF:Q4_K_M\n" in body
    assert f"gguf_base: {BASE}\n" in body
    assert "--hf-config-path {gguf_base} --tokenizer {gguf_base}" in body
    assert "--model-loader-extra-config '{gguf_loader_config}'" in body
    assert 'HF_HUB_OFFLINE: "0"' in body
    parsed = yaml.safe_load(body)
    assert parsed["defaults"]["gguf_loader_config"] == '{"mm_proj": "%s"}' % PROJECTOR
    # A string, not an integer: sparkrun hands it to the container verbatim.
    assert parsed["env"]["HF_HUB_OFFLINE"] == "0"
    assert parsed["env"]["TRANSFORMERS_OFFLINE"] == "0"


def test_a_custom_command_keeps_the_flags_the_gguf_cannot_load_without(tmp_path):
    body = _recipe(
        tmp_path, gguf_base=BASE, custom_command=("--enforce-eager",)
    ).content
    assert "--hf-config-path {gguf_base} --tokenizer {gguf_base}" in body


def test_a_text_gguf_has_no_projector_flag(tmp_path):
    body = _recipe(tmp_path, gguf_base="Qwen/Qwen3-0.6B").content
    assert "--hf-config-path" in body
    assert "--model-loader-extra-config" not in body


def test_a_vllm_gguf_without_its_base_is_refused(tmp_path):
    with pytest.raises(ValueError, match="names no base repository"):
        _recipe(tmp_path)
    with pytest.raises(ValueError, match="names no base repository"):
        _recipe(tmp_path, gguf_projector=PROJECTOR)


def test_a_vllm_gguf_does_not_speculate(tmp_path):
    spec = SpeculativeSpec(
        method=SpeculativeMethod.NGRAM, num_speculative_tokens=3,
        draft_bytes=0, draft_params=0,
    )
    with pytest.raises(ValueError) as refused:
        _recipe(tmp_path, gguf_base=BASE, speculative=spec)
    assert str(refused.value) == VLLM_GGUF_SPECULATIVE_REFUSAL


def test_gguf_facts_on_anything_else_are_refused(tmp_path):
    with pytest.raises(ValueError, match="belong to a vllm launch"):
        recipes.synthesize(
            _shape("Qwen/Qwen3.5-4B"), _plan(), "vllm", 8192, 4, "x",
            port=8100, gpu_memory_utilization=0.5, recipe_dir=tmp_path,
            gguf_base=BASE,
        )


@pytest.mark.parametrize(
    "projector",
    [
        "a/b/../c.gguf",
        "a/b/x.gguf' ; rm -rf /",
        'a/b/x".gguf',
        "a/x.gguf",
        "a/b/x.bin",
    ],
)
def test_the_projector_reaching_bash_is_held_to_a_grammar(projector):
    with pytest.raises(ValueError):
        render_gguf_loader_config(projector)


# ---- the fit gate -----------------------------------------------------------


@pytest.mark.parametrize("projector_bytes", [1, 672_000_000, 1_234_567_891])
def test_the_projector_is_charged_replicated_and_at_least_its_bytes(projector_bytes):
    shape = _shape(vision_params=0)
    charged, weights = _with_gguf_projector(shape, 2_500_000_000, projector_bytes)
    replicated = replicated_bytes_per_rank(charged)
    assert projector_bytes <= replicated < projector_bytes + shape.bytes_per_param()
    assert weights == 2_500_000_000 + projector_bytes


# ---- the hub ----------------------------------------------------------------


class _Hub:
    def __init__(self, tags, siblings=(), sizes=None, gated=()):
        self._info = ModelInfo(
            model_id="unsloth/Qwen3.5-4B-GGUF",
            sha=None,
            siblings=tuple(siblings),
            safetensors=None,
            gguf=None,
            tags=tuple(tags),
            file_sizes=dict(sizes or {}),
        )
        self._gated = set(gated)
        self.info_calls = 0

    def model_info(self, model_id, revision="main"):
        self.info_calls += 1
        return self._info

    def is_gated(self, model_id):
        return model_id in self._gated


def _resolver(tmp_path, hub, *, vision_params=0) -> ModelResolver:
    resolver = ModelResolver(client=hub, cache=ShapeCache(directory=tmp_path / "c"))

    def resolve_full(model_id, dtype=None):
        return Resolution(
            shape=_shape(model_id, vision_params=vision_params),
            revision="main",
            param_source=ParamSource.SAFETENSORS_HEADERS,
            quant_source=QuantSource.QUANT_CONFIG,
            support=build_verdict(("Qwen3_5ForConditionalGeneration",), "bf16"),
            architectures=("Qwen3_5ForConditionalGeneration",),
        )

    resolver.resolve_full = resolve_full
    return resolver


def test_the_base_is_the_one_model_the_hub_says_it_was_quantized_from(tmp_path):
    hub = _Hub(["gguf", f"base_model:quantized:{BASE}", f"base_model:{BASE}"])
    facts = _resolver(tmp_path, hub).gguf_launch_facts("hf://" + BLOB)
    assert facts.base == BASE
    assert facts.architectures == ("Qwen3_5ForConditionalGeneration",)
    assert facts.projector is None


@pytest.mark.parametrize(
    "tags",
    [["gguf"], ["base_model:quantized:a/one", "base_model:quantized:b/two"]],
)
def test_no_base_or_two_is_refused_and_points_at_llamacpp(tmp_path, tags):
    resolver = _resolver(tmp_path, _Hub(tags))
    with pytest.raises(ValueError, match="llamacpp"):
        resolver.gguf_launch_facts(BLOB)


def test_a_gated_base_is_refused(tmp_path):
    hub = _Hub([f"base_model:quantized:{BASE}"], gated={BASE})
    with pytest.raises(ValueError, match="gated"):
        _resolver(tmp_path, hub).gguf_launch_facts(BLOB)


def test_a_refusal_is_remembered_rather_than_asked_again(tmp_path):
    hub = _Hub(["gguf"])
    resolver = _resolver(tmp_path, hub)
    for _ in range(2):
        with pytest.raises(ValueError):
            resolver.gguf_launch_facts(BLOB)
    assert hub.info_calls == 1


def test_a_vision_base_takes_the_bf16_projector_with_its_measured_size(tmp_path):
    hub = _Hub(
        [f"base_model:quantized:{BASE}"],
        siblings=["mmproj-F16.gguf", "mmproj-BF16.gguf", "mmproj-F32.gguf", BLOB.split("/")[-1]],
        sizes={"mmproj-F16.gguf": 672_000_000, "mmproj-BF16.gguf": 671_000_000,
               "mmproj-F32.gguf": 1_340_000_000},
    )
    facts = _resolver(tmp_path, hub, vision_params=308_953_088).gguf_launch_facts(BLOB)
    assert facts.projector == PROJECTOR
    assert facts.projector_bytes == 671_000_000


def test_a_vision_base_with_no_projector_is_refused(tmp_path):
    hub = _Hub([f"base_model:quantized:{BASE}"], siblings=[BLOB.split("/")[-1]])
    with pytest.raises(ValueError, match="no\\s+mmproj"):
        _resolver(tmp_path, hub, vision_params=308_953_088).gguf_launch_facts(BLOB)
