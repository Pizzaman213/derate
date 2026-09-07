"""Agent C: model resolver tests.

The offline half runs against captured config.json payloads in
``tests/resolver_data`` and never touches the network. The network half is
skipped automatically when the hub is unreachable; set
``DERATE_TEST_NETWORK=0`` to skip it deliberately.
"""

from __future__ import annotations

import functools
import json
import os
import struct
import tempfile
import time
from pathlib import Path

import pytest

from control_plane.contracts import (
    FitRequest,
    ModelShape,
    ParallelismKind,
    ParallelismPlan,
)
from control_plane.contracts.quant import (
    BYTES_PER_PARAM,
    DEFAULT_DTYPE,
    bytes_per_param,
    bytes_per_param_or_default,
    is_known_dtype,
    normalize_dtype,
    quant_info,
)
from control_plane.resolver import (
    MetadataUnavailable,
    ModelNotFound,
    ModelResolver,
    ShapeCache,
    StubResolver,
    check_nodes,
    quant_requirement,
)
from control_plane.resolver.config_map import map_config, vision_config
from control_plane.resolver.gguf import read_gguf_file
from control_plane.resolver.params import analytic_breakdown
from control_plane.resolver.quant_detect import UnknownQuantization
from control_plane.resolver.support import build_verdict, modality_for, sharding_notes
from control_plane.resolver.types import ParamSource, QuantSource, SupportLevel
from tests.fixtures import DEEPSEEK_V3, GB10_PROFILES, GPT_OSS_120B, LLAMA_3_3_70B, WS_3090

DATA = Path(__file__).parent / "resolver_data"


def load_config(name: str) -> dict:
    return json.loads((DATA / f"{name}.config.json").read_text())


@pytest.fixture
def resolver(tmp_path) -> ModelResolver:
    """A resolver with a cache of its own, so tests never share state."""
    return ModelResolver(cache=ShapeCache(directory=tmp_path / "cache"))


def offline(resolver: ModelResolver, name: str, model_id: str, **kwargs):
    return resolver.resolve_config(load_config(name), model_id, **kwargs)


# --------------------------------------------------------------------------
# The quantization table. Agent C owns it; everything downstream keys off it.
# --------------------------------------------------------------------------


class TestQuantTable:
    def test_frozen_values(self):
        """The values in the architecture doc, to the digit."""
        assert BYTES_PER_PARAM["fp32"] == 4.0
        assert BYTES_PER_PARAM["fp16"] == BYTES_PER_PARAM["bf16"] == 2.0
        assert BYTES_PER_PARAM["fp8"] == BYTES_PER_PARAM["int8"] == 1.0
        assert BYTES_PER_PARAM["q8_0"] == 1.0625
        assert BYTES_PER_PARAM["q6_k"] == 0.82
        assert BYTES_PER_PARAM["q5_k_m"] == 0.711
        assert BYTES_PER_PARAM["q4_k_m"] == 0.6125
        assert BYTES_PER_PARAM["q4_0"] == 0.5625
        assert BYTES_PER_PARAM["q3_k_m"] == 0.456
        assert BYTES_PER_PARAM["q2_k"] == 0.329
        assert BYTES_PER_PARAM["mxfp4"] == 0.53125
        assert BYTES_PER_PARAM["nvfp4"] == 0.5625
        assert BYTES_PER_PARAM["awq_int4"] == 0.5625
        assert BYTES_PER_PARAM["gptq_int4"] == 0.5625
        assert BYTES_PER_PARAM["nf4"] == 0.5163

    def test_four_bit_is_not_half_a_byte(self):
        """The systematic error the table exists to prevent."""
        for key in ("q4_k_m", "q4_0", "mxfp4", "nvfp4", "awq_int4", "gptq_int4", "nf4"):
            assert BYTES_PER_PARAM[key] > 0.5, key

    def test_unknown_dtype_raises_rather_than_being_priced(self):
        """The table refuses to price what it does not know."""
        with pytest.raises(KeyError):
            bytes_per_param("fp3_secret_sauce")
        assert normalize_dtype("fp3_secret_sauce") is None
        assert not is_known_dtype("fp3_secret_sauce")

    def test_the_defaulting_variant_never_guesses_below_bf16(self):
        """The paths that must return a number charge the larger figure."""
        assert bytes_per_param_or_default("fp3_secret_sauce") == BYTES_PER_PARAM[DEFAULT_DTYPE]
        assert bytes_per_param_or_default(None) == 2.0

    def test_no_bare_int4_entry(self):
        """Name the scheme, so its scales and zero points get charged."""
        assert "int4" not in BYTES_PER_PARAM
        assert normalize_dtype("w4a16") == "gptq_int4"

    @pytest.mark.parametrize(
        ("spelling", "expected"),
        [
            ("Q4_K_M", "q4_k_m"), ("q4km", "q4_k_m"), ("Q4_K_S", "q4_k_m"),
            ("bfloat16", "bf16"), ("torch.float16", "fp16"), ("F8_E4M3", "fp8"),
            ("MXFP4", "mxfp4"), ("AWQ", "awq_int4"), ("gptq", "gptq_int4"),
            ("W4A16", "gptq_int4"), ("Q8_0", "q8_0"),
        ],
    )
    def test_spelling_variants(self, spelling, expected):
        assert normalize_dtype(spelling) == expected

    def test_every_key_has_metadata(self):
        for key in BYTES_PER_PARAM:
            info = quant_info(key)
            assert info.key == key
            assert info.bits_per_weight > 0

    def test_effective_bits_match_the_byte_figures(self):
        for key, byte_cost in BYTES_PER_PARAM.items():
            assert quant_info(key).bits_per_weight == pytest.approx(byte_cost * 8, rel=0.01)


# --------------------------------------------------------------------------
# Field mapping. The KV head count is the field that must be exactly right.
# --------------------------------------------------------------------------


class TestFieldMapping:
    def test_llama_3_3_70b(self, resolver):
        """Acceptance: 80 layers, 8192 hidden, 64 heads, 8 KV heads."""
        shape = offline(
            resolver, "llama-3.3-70b", "meta-llama/Llama-3.3-70B-Instruct",
            measured_total=70_553_706_496,
        ).shape
        assert shape.num_layers == 80
        assert shape.hidden_size == 8192
        assert shape.num_attention_heads == 64
        assert shape.num_kv_heads == 8
        assert shape.head_dim == 128
        assert shape.vocab_size == 128256
        assert shape.dtype == "bf16"
        assert not shape.is_moe

    def test_grouped_query_ratio_is_not_flattened(self, resolver):
        """Using num_attention_heads here would over-charge KV eightfold."""
        shape = offline(resolver, "llama-3.3-70b", "llama").shape
        assert shape.num_attention_heads // shape.num_kv_heads == 8

    def test_missing_kv_heads_defaults_to_attention_heads(self, resolver):
        """Acceptance: absent means multi-head, not multi-query."""
        res = offline(resolver, "gpt2", "openai-community/gpt2")
        assert res.shape.num_kv_heads == res.shape.num_attention_heads == 12
        assert any("no num_key_value_heads" in w for w in res.warnings)

    def test_explicit_head_dim_wins_over_the_division(self, resolver):
        """GPT-OSS breaks hidden_size / num_heads: 2880 / 64 is 45, not 64."""
        shape = offline(resolver, "gpt-oss-120b", "openai/gpt-oss-120b").shape
        assert shape.head_dim == 64
        assert shape.effective_head_dim == 64
        assert shape.hidden_size // shape.num_attention_heads == 45

    def test_alternate_key_names(self, resolver):
        """n_layer, n_embd and n_head are the same fields under other names."""
        shape = offline(resolver, "gpt2", "openai-community/gpt2").shape
        assert (shape.num_layers, shape.hidden_size, shape.num_attention_heads) == (12, 768, 12)

    def test_nested_text_config_is_unwrapped(self, resolver):
        """Gemma 3 keeps the language model under text_config."""
        shape = offline(resolver, "gemma-3-27b", "google/gemma-3-27b-it").shape
        assert shape.num_layers == 62
        assert shape.hidden_size == 5376
        assert shape.num_kv_heads == 16

    def test_missing_required_field_is_an_error(self, resolver):
        from control_plane.resolver import UnsupportedArchitecture

        with pytest.raises(UnsupportedArchitecture):
            resolver.resolve_config({"model_type": "mystery"}, "x/y")


# --------------------------------------------------------------------------
# Mixture of experts: capacity and speed differ by an order of magnitude.
# --------------------------------------------------------------------------


class TestMixtureOfExperts:
    def test_gpt_oss_120b(self, resolver):
        """Acceptance: MoE, right totals, MXFP4, and its window pattern."""
        res = offline(
            resolver, "gpt-oss-120b", "openai/gpt-oss-120b",
            measured_total=116_829_156_672,
        )
        shape = res.shape
        assert shape.is_moe
        assert (shape.num_experts, shape.num_experts_per_token) == (128, 4)
        assert shape.dtype == "mxfp4"
        assert res.quant_source is QuantSource.QUANT_CONFIG
        assert shape.total_params == pytest.approx(116.8e9, rel=0.01)
        assert shape.effective_active_params == pytest.approx(5.1e9, rel=0.10)
        assert shape.sliding_window == 128
        assert shape.layers_with_full_attention == 18

    def test_gpt_oss_20b_caches_full_kv_on_half_its_layers(self, resolver):
        shape = offline(resolver, "gpt-oss-20b", "openai/gpt-oss-20b").shape
        assert shape.num_layers == 24
        assert shape.layers_with_full_attention == 12
        assert shape.sliding_window == 128

    def test_active_is_far_below_total(self, resolver):
        res = offline(
            resolver, "gpt-oss-120b", "openai/gpt-oss-120b",
            measured_total=116_829_156_672,
        )
        assert res.shape.effective_active_params < res.shape.total_params / 15

    def test_qwen3_moe(self, resolver):
        res = offline(
            resolver, "qwen3-30b-a3b", "Qwen/Qwen3-30B-A3B", measured_total=30_532_122_624
        )
        shape = res.shape
        assert (shape.num_experts, shape.num_experts_per_token) == (128, 8)
        assert shape.num_kv_heads == 4
        assert shape.effective_active_params == pytest.approx(3.3e9, rel=0.12)

    def test_mixtral_reads_num_local_experts(self, resolver):
        shape = offline(
            resolver, "mixtral-8x7b", "mistralai/Mixtral-8x7B-Instruct-v0.1",
            measured_total=46_702_792_704,
        ).shape
        assert (shape.num_experts, shape.num_experts_per_token) == (8, 2)
        assert shape.effective_active_params == pytest.approx(12.9e9, rel=0.05)

    def test_dense_model_has_no_active_split(self, resolver):
        shape = offline(resolver, "llama-3.3-70b", "llama").shape
        assert shape.active_params is None
        assert shape.effective_active_params == shape.total_params


# --------------------------------------------------------------------------
# Sliding window and latent attention: both change KV totals by multiples.
# --------------------------------------------------------------------------


class TestAttentionVariants:
    def test_gemma3_interleaves_five_windowed_layers_per_full_one(self, resolver):
        shape = offline(resolver, "gemma-3-27b", "google/gemma-3-27b-it").shape
        assert shape.sliding_window == 1024
        assert shape.layers_with_full_attention == 62 // 6

    def test_disabled_sliding_window_is_not_charged(self, resolver):
        """Qwen ships sliding_window with use_sliding_window false."""
        for name in ("qwen2.5-vl-7b", "qwen2.5-7b-gptq"):
            shape = offline(resolver, name, "qwen").shape
            assert shape.sliding_window is None
            assert shape.layers_with_full_attention is None

    def test_no_window_keys_at_all(self, resolver):
        shape = offline(resolver, "llama-3.3-70b", "llama").shape
        assert shape.sliding_window is None
        assert shape.layers_with_full_attention is None

    def test_deepseek_populates_mla(self, resolver):
        """Acceptance: a DeepSeek model populates mla_latent_dim."""
        res = offline(
            resolver, "deepseek-v3", "deepseek-ai/DeepSeek-V3",
            measured_total=684_531_386_000,
        )
        assert res.shape.mla_latent_dim == 512
        assert res.shape.mla_latent_dim == DEEPSEEK_V3.mla_latent_dim

    def test_deepseek_populates_the_rope_component_from_the_config(self, resolver):
        """M-10: qk_rope_head_dim=64 in the fixture must reach mla_rope_dim.

        With the field carried, KV math reads it through
        ``effective_mla_rope_dim`` rather than a hardcoded 64, and no warning
        is needed since nothing was guessed.
        """
        res = offline(
            resolver, "deepseek-v3", "deepseek-ai/DeepSeek-V3",
            measured_total=684_531_386_000,
        )
        assert res.shape.mla_rope_dim == 64
        assert res.shape.effective_mla_rope_dim == 64
        assert not any("RoPE" in w for w in res.warnings)

    def test_missing_rope_head_dim_warns_and_leaves_the_field_empty(self, resolver):
        """The 64 fallback only applies when the config truly has nothing to say."""
        config = dict(load_config("deepseek-v3"))
        config.pop("qk_rope_head_dim", None)
        res = resolver.resolve_config(config, "deepseek-ai/DeepSeek-V3-no-rope-key")
        assert res.shape.mla_latent_dim == 512
        assert res.shape.mla_rope_dim is None
        assert res.shape.effective_mla_rope_dim == 64  # the property's fallback
        assert any("qk_rope_head_dim" in w for w in res.warnings)

    def test_deepseek_head_dim_is_read_not_derived(self, resolver):
        """7168 / 128 is 56, which is not this model's head dimension."""
        shape = offline(resolver, "deepseek-v3", "deepseek").shape
        assert shape.effective_head_dim == 128
        assert shape.hidden_size // shape.num_attention_heads == 56

    def test_non_mla_models_leave_the_field_empty(self, resolver):
        shape = offline(resolver, "llama-3.3-70b", "llama").shape
        assert shape.mla_latent_dim is None
        assert shape.mla_rope_dim is None
        assert shape.effective_mla_rope_dim == 0


# --------------------------------------------------------------------------
# Parameter accounting: read the weights, never a formula, when we can.
# --------------------------------------------------------------------------


class TestParameterAccounting:
    @pytest.mark.parametrize(
        ("name", "published"),
        [
            ("llama-3.3-70b", 70.55e9),
            ("gpt-oss-120b", 116.83e9),
            ("gpt-oss-20b", 20.91e9),
            ("qwen3-30b-a3b", 30.53e9),
            ("mixtral-8x7b", 46.70e9),
            ("gemma-3-27b", 27.4e9),
            ("qwen2.5-vl-7b", 8.29e9),
        ],
    )
    def test_analytic_split_tracks_the_published_count(self, name, published):
        """The split is only trustworthy if the arithmetic behind it is."""
        config = load_config(name)
        breakdown = analytic_breakdown(map_config(config), vision_config(config))
        assert breakdown.total == pytest.approx(published, rel=0.02)

    def test_measured_total_wins_over_the_estimate(self, resolver):
        res = offline(resolver, "llama-3.3-70b", "llama", measured_total=70_000_000_000)
        assert res.shape.total_params == 70_000_000_000
        assert res.param_source is ParamSource.SAFETENSORS_HEADERS

    def test_multi_token_prediction_module_is_excluded(self, resolver):
        """The checkpoint carries it; no runtime loads it by default."""
        res = offline(
            resolver, "deepseek-v3", "deepseek-ai/DeepSeek-V3",
            measured_total=684_531_386_000,
        )
        assert res.shape.total_params == pytest.approx(671e9, rel=0.01)
        assert any("multi-token-prediction" in w for w in res.warnings)

    def test_vision_tower_is_counted_separately(self, resolver):
        for name, expected in (("qwen2.5-vl-7b", 675e6), ("gemma-3-27b", 430e6)):
            shape = offline(resolver, name, "vlm").shape
            assert shape.vision_params == pytest.approx(expected, rel=0.15)

    def test_text_only_models_have_no_vision_params(self, resolver):
        assert offline(resolver, "llama-3.3-70b", "llama").shape.vision_params == 0

    def test_weight_bytes_beat_the_dtype_figure_on_mixed_precision(self, resolver):
        """GPT-OSS keeps attention in bf16 while the experts are MXFP4."""
        res = offline(
            resolver, "gpt-oss-120b", "openai/gpt-oss-120b",
            measured_total=116_829_156_672, weight_bytes=65_248_893_184,
        )
        implied = res.shape.total_params * res.shape.bytes_per_param()
        assert res.weight_bytes > implied
        assert res.effective_weight_bytes() == res.weight_bytes
        assert any("mixed precision" in w for w in res.warnings)


# --------------------------------------------------------------------------
# Quantization detection.
# --------------------------------------------------------------------------


class TestQuantDetection:
    @pytest.mark.parametrize(
        ("name", "expected", "source"),
        [
            ("gpt-oss-120b", "mxfp4", QuantSource.QUANT_CONFIG),
            ("deepseek-v3", "fp8", QuantSource.QUANT_CONFIG),
            ("llama-3.1-8b-awq", "awq_int4", QuantSource.QUANT_CONFIG),
            ("qwen2.5-7b-gptq", "gptq_int4", QuantSource.QUANT_CONFIG),
            ("llama-3.1-8b-fp8-ct", "fp8", QuantSource.QUANT_CONFIG),
            ("llama-3.3-70b", "bf16", QuantSource.TORCH_DTYPE),
        ],
    )
    def test_detection(self, resolver, name, expected, source):
        res = offline(resolver, name, name)
        assert res.shape.dtype == expected
        assert res.quant_source is source

    def test_quantization_config_beats_torch_dtype(self, resolver):
        """An AWQ repo still declares float16; the quant config is the truth."""
        assert load_config("llama-3.1-8b-awq")["torch_dtype"] == "float16"
        assert offline(resolver, "llama-3.1-8b-awq", "awq").shape.dtype == "awq_int4"

    def test_unknown_quantization_defaults_to_bf16_with_a_warning(self, resolver):
        """Acceptance: never guess low."""
        res = resolver.resolve_config(
            {"num_hidden_layers": 4, "hidden_size": 64, "num_attention_heads": 4,
             "vocab_size": 100, "quantization_config": {"quant_method": "brand-new"}},
            "someone/brand-new-quant",
        )
        assert res.shape.dtype == "bf16"
        assert res.shape.bytes_per_param() == 2.0
        assert any("brand-new" in w for w in res.warnings)

    def test_every_resolved_dtype_is_a_table_key(self, resolver):
        """The invariant that lets the table be strict."""
        for name in ("gpt-oss-120b", "deepseek-v3", "llama-3.1-8b-awq", "gpt2"):
            assert is_known_dtype(offline(resolver, name, name).shape.dtype)

    def test_repo_name_is_the_last_resort(self, resolver):
        res = resolver.resolve_config(
            {"num_hidden_layers": 4, "hidden_size": 64, "num_attention_heads": 4,
             "vocab_size": 100},
            "someone/Model-AWQ",
        )
        assert res.shape.dtype == "awq_int4"
        assert res.quant_source is QuantSource.REPO_NAME
        assert any("repo name" in w for w in res.warnings)

    def test_caller_override(self, resolver):
        res = offline(resolver, "llama-3.3-70b", "llama", dtype="q4_k_m")
        assert res.shape.dtype == "q4_k_m"
        assert res.quant_source is QuantSource.OVERRIDE

    def test_override_rejects_a_dtype_the_table_cannot_price(self, resolver):
        with pytest.raises(UnknownQuantization):
            offline(resolver, "llama-3.3-70b", "llama", dtype="q1_tiny")


# --------------------------------------------------------------------------
# GGUF.
# --------------------------------------------------------------------------


def _gguf_string(text: str) -> bytes:
    raw = text.encode()
    return struct.pack("<Q", len(raw)) + raw


def _dummy_plan() -> ParallelismPlan:
    """A minimal legal plan, for tests that only care about FitRequest wiring."""
    return ParallelismPlan(
        kind=ParallelismKind.SINGLE_NODE,
        tensor_parallel=1,
        pipeline_parallel=1,
        expert_parallel=1,
        data_parallel=1,
        node_ids=["n0"],
        reason="test plan",
        measured_link_gbps=0.0,
        rejected=[],
    )


def write_gguf(path: Path, metadata: dict, tensors: list[tuple[str, tuple[int, ...], int]]) -> None:
    """Write a header-only GGUF file. Enough to exercise the reader."""
    out = bytearray(b"GGUF" + struct.pack("<IQQ", 3, len(tensors), len(metadata)))
    for key, value in metadata.items():
        out += _gguf_string(key)
        if isinstance(value, str):
            out += struct.pack("<I", 8) + _gguf_string(value)
        elif isinstance(value, bool):
            out += struct.pack("<I", 7) + struct.pack("<?", value)
        elif isinstance(value, int):
            out += struct.pack("<I", 4) + struct.pack("<I", value)
        elif isinstance(value, list):  # array of strings, the token vocabulary
            out += struct.pack("<I", 9) + struct.pack("<IQ", 8, len(value))
            for item in value:
                out += _gguf_string(item)
        else:
            raise TypeError(f"unsupported metadata value {value!r}")
    for name, dims, ggml_type in tensors:
        out += _gguf_string(name) + struct.pack("<I", len(dims))
        out += struct.pack(f"<{len(dims)}Q", *dims)
        out += struct.pack("<IQ", ggml_type, 0)
    path.write_bytes(bytes(out))


@functools.lru_cache(maxsize=1)
def _special_gguf_bytes() -> bytes:
    """A GGUF whose *name* says nothing but whose header says q8_0.

    Backs the rule that a file with an unfashionable name is read, not
    discarded: the header is the file's own account of itself and it outranks a
    naming convention somebody chose not to follow.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "special.gguf"
        write_gguf(
            path,
            {
                "general.architecture": "llama",
                "general.file_type": 7,  # q8_0
                "llama.block_count": 4,
                "llama.embedding_length": 512,
                "llama.attention.head_count": 8,
                "llama.attention.head_count_kv": 8,
                "llama.vocab_size": 32000,
            },
            [("token_embd.weight", (512, 32000), 8)],
        )
        return path.read_bytes()


class TestGGUF:
    def _moe_file(self, tmp_path: Path) -> Path:
        path = tmp_path / "toy-moe-Q4_K_M.gguf"
        write_gguf(
            path,
            {
                "general.architecture": "llama",
                "general.file_type": 15,  # Q4_K_M
                "llama.block_count": 4,
                "llama.embedding_length": 512,
                "llama.attention.head_count": 8,
                "llama.attention.head_count_kv": 2,
                "llama.expert_count": 8,
                "llama.expert_used_count": 2,
                "llama.context_length": 8192,
                "tokenizer.ggml.tokens": [f"t{i}" for i in range(1000)],
            },
            [
                ("token_embd.weight", (512, 1000), 8),  # Q8_0
                ("blk.0.attn_q.weight", (512, 512), 12),  # Q4_K
                ("blk.0.ffn_gate_exps.weight", (512, 1024, 8), 12),
                ("blk.0.ffn_down_exps.weight", (1024, 512, 8), 12),
                ("output_norm.weight", (512,), 0),  # F32
            ],
        )
        return path

    def test_reads_the_kv_block(self, tmp_path, resolver):
        shape = resolver.resolve_gguf(str(self._moe_file(tmp_path)))
        assert shape.num_layers == 4
        assert shape.hidden_size == 512
        assert shape.num_attention_heads == 8
        assert shape.num_kv_heads == 2
        assert shape.vocab_size == 1000
        assert shape.num_experts == 8
        assert shape.num_experts_per_token == 2
        assert shape.dtype == "q4_k_m"

    def test_sums_tensor_bytes_rather_than_assuming_one_width(self, tmp_path, resolver):
        """Every GGUF mixes types; the embedding here is Q8_0, the rest Q4_K."""
        res = resolver.resolve_gguf_full(str(self._moe_file(tmp_path)))
        expected_params = 512 * 1000 + 512 * 512 + 2 * (512 * 1024 * 8) + 512
        expected_bytes = (
            (512 * 1000 // 32) * 34
            + (512 * 512 // 256) * 144
            + 2 * ((512 * 1024 * 8 // 256) * 144)
            + 512 * 4
        )
        assert res.shape.total_params == expected_params
        assert res.weight_bytes == expected_bytes
        assert res.param_source is ParamSource.GGUF_TENSORS
        assert res.weight_bytes != int(expected_params * BYTES_PER_PARAM["q4_k_m"])

    def test_measured_gguf_bytes_feed_a_fit_request(self, tmp_path, resolver):
        """H-10: a caller must be able to hand the measured bytes to FitRequest."""
        res = resolver.resolve_gguf_full(str(self._moe_file(tmp_path)))
        assert res.weight_bytes and res.weight_bytes > 0
        effective = res.effective_weight_bytes()
        assert effective >= res.weight_bytes
        request = FitRequest(
            shape=res.shape,
            context_length=4096,
            max_concurrent_seqs=1,
            kv_dtype="bf16",
            plan=_dummy_plan(),
            weight_bytes=effective,
        )
        assert request.weight_bytes == effective

    def test_expert_tensors_drive_the_active_count(self, tmp_path, resolver):
        shape = resolver.resolve_gguf(str(self._moe_file(tmp_path)))
        assert shape.active_params < shape.total_params / 2

    def test_missing_kv_head_count_warns(self, tmp_path, resolver):
        path = tmp_path / "no-kv.gguf"
        write_gguf(
            path,
            {
                "general.architecture": "llama",
                "general.file_type": 1,
                "llama.block_count": 2,
                "llama.embedding_length": 64,
                "llama.attention.head_count": 4,
                "llama.vocab_size": 32,
            },
            [("token_embd.weight", (64, 32), 1)],
        )
        res = resolver.resolve_gguf_full(str(path))
        assert res.shape.num_kv_heads == res.shape.num_attention_heads == 4
        assert any("head_count_kv" in w for w in res.warnings)

    def test_a_file_that_is_not_gguf(self, tmp_path, resolver):
        path = tmp_path / "not.gguf"
        path.write_bytes(b"NOPE" + b"\x00" * 64)
        with pytest.raises(Exception, match="not a GGUF"):
            resolver.resolve_gguf(str(path))

    def test_missing_file(self, resolver):
        with pytest.raises(ModelNotFound):
            resolver.resolve_gguf("/nonexistent/model.gguf")

    def test_large_vocabulary_is_walked_not_materialised(self, tmp_path):
        path = tmp_path / "big-vocab.gguf"
        write_gguf(
            path,
            {
                "general.architecture": "llama",
                "general.file_type": 1,
                "llama.block_count": 1,
                "llama.embedding_length": 8,
                "llama.attention.head_count": 2,
                "tokenizer.ggml.tokens": [f"token{i}" for i in range(5000)],
            },
            [("token_embd.weight", (8, 5000), 1)],
        )
        header = read_gguf_file(str(path))
        assert len(header.metadata["tokenizer.ggml.tokens"]) == 5000


def _small_gguf(path: Path) -> None:
    write_gguf(
        path,
        {
            "general.architecture": "llama",
            "general.file_type": 1,
            "llama.block_count": 2,
            "llama.embedding_length": 64,
            "llama.attention.head_count": 4,
            "llama.attention.head_count_kv": 2,
            "llama.vocab_size": 32,
        },
        [("token_embd.weight", (64, 32), 1)],
    )


class _CountingRangeClient:
    """Serves a GGUF blob through ``read_range``, counting calls.

    Standing in for ``HubClient`` so a test can prove a resolve did -- or did
    not -- reach the network, without any real HTTP.
    """

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.calls = 0

    def read_range(self, model_id: str, filename: str, start: int, length: int) -> bytes:
        self.calls += 1
        return self._data[start : start + length]

    def model_info(self, model_id: str, revision: str = "main"):
        raise AssertionError("GGUF resolution must not call model_info")

    def config(self, model_id: str, revision: str = "main"):
        raise AssertionError("GGUF resolution must not fetch config.json")


class TestGGUFResolveFullRouting:
    """M-24: resolve_full's .gguf/hf:// routing must respect offline and cache.

    Before this fix, the GGUF branch returned before the offline gate and
    before any cache lookup, so offline mode still performed Range reads and
    every resolve of the same file re-read it.
    """

    def test_local_gguf_is_never_cached(self, tmp_path):
        """A local path can be replaced or deleted out from under a
        long-lived resolver, unlike an ``hf://`` blob, so it goes through
        neither the cache nor the offline gate -- see ``_resolve_local_dir``,
        which extends the same courtesy to a config directory.
        """
        path = tmp_path / "local.gguf"
        _small_gguf(path)
        resolver = ModelResolver(cache=ShapeCache(directory=tmp_path / "cache"))

        first = resolver.resolve_full(str(path))
        assert not first.from_cache
        second = resolver.resolve_full(str(path))
        assert not second.from_cache
        assert second.shape == first.shape

    def test_local_gguf_reflects_a_file_replaced_in_place(self, tmp_path):
        """A stale cache entry would keep reporting the old shape; there must
        be no cache entry to go stale.
        """
        path = tmp_path / "local.gguf"
        _small_gguf(path)
        resolver = ModelResolver(cache=ShapeCache(directory=tmp_path / "cache"))
        first = resolver.resolve_full(str(path))
        assert first.shape.num_layers == 2

        write_gguf(
            path,
            {
                "general.architecture": "llama",
                "general.file_type": 1,
                "llama.block_count": 40,
                "llama.embedding_length": 128,
                "llama.attention.head_count": 8,
                "llama.attention.head_count_kv": 8,
                "llama.vocab_size": 32,
            },
            [("token_embd.weight", (128, 32), 1)],
        )
        second = resolver.resolve_full(str(path))
        assert second.shape.num_layers == 40
        assert second.shape.hidden_size == 128

    def test_local_gguf_deleted_after_a_resolve_raises(self, tmp_path):
        """A cached entry would keep 'resolving' a file that no longer exists."""
        path = tmp_path / "local.gguf"
        _small_gguf(path)
        resolver = ModelResolver(cache=ShapeCache(directory=tmp_path / "cache"))
        resolver.resolve_full(str(path))

        path.unlink()
        with pytest.raises(ModelNotFound):
            resolver.resolve_full(str(path))

    def test_local_gguf_ignores_offline_mode(self, tmp_path):
        """A local file touches no network, so offline must not refuse it."""
        path = tmp_path / "local.gguf"
        _small_gguf(path)
        resolver = ModelResolver(
            cache=ShapeCache(directory=tmp_path / "cache"), offline=True
        )
        res = resolver.resolve_full(str(path))
        assert res.shape.num_layers == 2

    def test_offline_remote_gguf_raises_without_touching_the_network(self, tmp_path):
        path = tmp_path / "src.gguf"
        _small_gguf(path)
        client = _CountingRangeClient(path.read_bytes())
        resolver = ModelResolver(
            client=client, cache=ShapeCache(directory=tmp_path / "cache"), offline=True
        )
        with pytest.raises(MetadataUnavailable, match="offline"):
            resolver.resolve_full("hf://someone/repo/model.gguf")
        assert client.calls == 0

    def test_offline_direct_resolve_gguf_full_also_raises(self, tmp_path):
        """The public ``resolve_gguf_full`` is a door below ``resolve_full``'s
        cache and offline gate; a caller reaching it directly for a remote
        blob must get the same offline honesty rather than a bypass.
        """
        path = tmp_path / "src.gguf"
        _small_gguf(path)
        client = _CountingRangeClient(path.read_bytes())
        resolver = ModelResolver(client=client, offline=True)
        with pytest.raises(MetadataUnavailable, match="offline"):
            resolver.resolve_gguf_full("hf://someone/repo/model.gguf")
        assert client.calls == 0

    def test_remote_gguf_resolve_full_is_cached_not_refetched(self, tmp_path):
        path = tmp_path / "src.gguf"
        _small_gguf(path)
        client = _CountingRangeClient(path.read_bytes())
        cache = ShapeCache(directory=tmp_path / "cache")
        resolver = ModelResolver(client=client, cache=cache)

        first = resolver.resolve_full("hf://someone/repo/model.gguf")
        assert not first.from_cache
        calls_after_first = client.calls
        assert calls_after_first > 0

        second = resolver.resolve_full("hf://someone/repo/model.gguf")
        assert second.from_cache
        assert second.shape == first.shape
        assert second.weight_bytes == first.weight_bytes  # H-10: survives the cache round trip
        assert client.calls == calls_after_first  # no further Range reads

    def test_resolve_full_gguf_weight_bytes_feed_a_fit_request(self, tmp_path):
        """H-10, on the actual ``resolve_full`` path rather than
        ``resolve_gguf_full`` directly: the measured bytes must still reach a
        ``FitRequest`` after going through the cached GGUF route.
        """
        path = tmp_path / "src.gguf"
        _small_gguf(path)
        client = _CountingRangeClient(path.read_bytes())
        resolver = ModelResolver(client=client, cache=ShapeCache(directory=tmp_path / "cache"))

        res = resolver.resolve_full("hf://someone/repo/model.gguf")
        assert res.weight_bytes and res.weight_bytes > 0
        effective = res.effective_weight_bytes()
        request = FitRequest(
            shape=res.shape,
            context_length=4096,
            max_concurrent_seqs=1,
            kv_dtype="bf16",
            plan=_dummy_plan(),
            weight_bytes=effective,
        )
        assert request.weight_bytes == effective

    def test_once_cached_a_remote_gguf_serves_offline_too(self, tmp_path):
        path = tmp_path / "src.gguf"
        _small_gguf(path)
        client = _CountingRangeClient(path.read_bytes())
        cache_dir = tmp_path / "cache"
        warm = ModelResolver(client=client, cache=ShapeCache(directory=cache_dir))
        warm.resolve_full("hf://someone/repo/model.gguf")
        calls_after_warm = client.calls

        cold = ModelResolver(
            client=client, cache=ShapeCache(directory=cache_dir), offline=True
        )
        res = cold.resolve_full("hf://someone/repo/model.gguf")
        assert res.from_cache
        assert res.shape.num_layers == 2
        assert client.calls == calls_after_warm


class TestWeightIndexArbitration:
    """When the hub's tally and the config disagree, the bytes decide."""

    class FakeClient:
        def __init__(self, config: dict, info):
            self._config = config
            self._info = info

        def model_info(self, model_id, revision="main"):
            return self._info

        def config(self, model_id, revision="main"):
            return self._config

        def file_json(self, model_id, filename, revision="main"):
            return None

    def _info(self, model_id: str, tally: int | None, files: dict[str, int]):
        from control_plane.resolver.hf import ModelInfo

        return ModelInfo(
            model_id=model_id,
            sha="a" * 40,
            siblings=tuple(files),
            safetensors={"total": tally} if tally else None,
            gguf=None,
            tags=(),
            file_sizes=files,
        )

    def _resolve(self, tmp_path, name, model_id, tally, files, dtype=None):
        config = load_config(name)
        info = self._info(model_id, tally, files)
        resolver = ModelResolver(
            client=self.FakeClient(config, info),
            cache=ShapeCache(directory=tmp_path / "cache"),
        )
        return resolver.resolve_full(model_id, dtype)

    def test_duplicate_full_copies_are_not_double_counted(self, tmp_path):
        """Mistral ships consolidated.safetensors beside the sharded copy."""
        res = self._resolve(
            tmp_path, "llama-3.3-70b", "mistralai/Mistral-7B-Instruct-v0.3",
            7_248_023_552,
            {
                "consolidated.safetensors": 14_496_078_512,
                "model-00001-of-00002.safetensors": 7_248_039_256,
                "model-00002-of-00002.safetensors": 7_248_039_256,
            },
        )
        assert res.weight_bytes == 2 * 7_248_039_256

    def test_index_measured_bytes_feed_a_fit_request(self, tmp_path):
        """H-10: shard-index bytes must survive into effective_weight_bytes."""
        res = self._resolve(
            tmp_path, "llama-3.3-70b", "mistralai/Mistral-7B-Instruct-v0.3",
            7_248_023_552,
            {
                "model-00001-of-00002.safetensors": 7_248_039_256,
                "model-00002-of-00002.safetensors": 7_248_039_256,
            },
        )
        assert res.weight_bytes == 2 * 7_248_039_256
        effective = res.effective_weight_bytes()
        assert effective >= res.weight_bytes
        request = FitRequest(
            shape=res.shape, context_length=4096, max_concurrent_seqs=1,
            kv_dtype="bf16", plan=_dummy_plan(), weight_bytes=effective,
        )
        assert request.weight_bytes == effective

    def test_shard_bytes_ignore_subdirectory_copies(self, tmp_path):
        res = self._resolve(
            tmp_path, "llama-3.3-70b", "openai/gpt-oss-120b", 70_553_706_496,
            {
                "model-00001-of-00001.safetensors": 1_000,
                "original/model.safetensors": 999_999,
            },
        )
        assert res.weight_bytes == 1_000

    def test_bytes_backing_the_hub_beat_a_config_that_cannot_describe_the_model(
        self, tmp_path
    ):
        """Nemotron's layers differ from each other, so the formula over-counts."""
        params = 49_867_145_216
        res = self._resolve(
            tmp_path, "llama-3.3-70b", "nvidia/Nemotron-like", params,
            {"model-00001-of-00001.safetensors": params * 2},
        )
        assert res.shape.total_params == params
        assert res.param_source is ParamSource.HUB_SAFETENSORS_INDEX
        assert any("shards" in w and "back the hub" in w for w in res.warnings)

    def test_bytes_backing_the_config_reject_a_packed_element_tally(self, tmp_path):
        """A 4-bit repo whose hub tally counts storage elements, not weights."""
        res = self._resolve(
            tmp_path, "llama-3.3-70b", "nvidia/Llama-3.3-70B-Instruct-FP4",
            40_606_376_096,
            {"model-00001-of-00001.safetensors": 42_700_000_000},
            dtype="nvfp4",
        )
        assert res.shape.total_params == pytest.approx(70.55e9, rel=0.01)
        assert res.param_source is ParamSource.CONFIG_ESTIMATE
        assert any("back the config" in w for w in res.warnings)

    def test_without_bytes_the_larger_figure_wins(self, tmp_path):
        """Over-stating costs a refusal; under-stating costs an out-of-memory."""
        res = self._resolve(
            tmp_path, "llama-3.3-70b", "someone/odd", 40_000_000_000, {}
        )
        assert res.shape.total_params == pytest.approx(70.55e9, rel=0.01)
        res = self._resolve(
            tmp_path, "llama-3.3-70b", "someone/odd2", 140_000_000_000, {}
        )
        assert res.shape.total_params == 140_000_000_000


class TestLocalDirectory:
    """A directory of weights on disk: read the headers, not a formula."""

    def _model_dir(self, tmp_path: Path, tensors: dict) -> Path:
        directory = tmp_path / "model"
        directory.mkdir()
        (directory / "config.json").write_text(
            json.dumps(
                {
                    "architectures": ["LlamaForCausalLM"], "model_type": "llama",
                    "num_hidden_layers": 2, "hidden_size": 64,
                    "num_attention_heads": 4, "num_key_value_heads": 2,
                    "intermediate_size": 128, "vocab_size": 1000,
                    "torch_dtype": "bfloat16", "tie_word_embeddings": False,
                    "hidden_act": "silu",
                }
            )
        )
        header = json.dumps(tensors).encode()
        payload = max(
            (entry["data_offsets"][1] for entry in tensors.values()), default=0
        )
        (directory / "model.safetensors").write_bytes(
            struct.pack("<Q", len(header)) + header + b"\0" * payload
        )
        return directory

    def test_counts_parameters_from_the_headers(self, tmp_path, resolver):
        directory = self._model_dir(
            tmp_path,
            {
                "model.embed_tokens.weight": {
                    "dtype": "BF16", "shape": [1000, 64], "data_offsets": [0, 128000]
                },
                "lm_head.weight": {
                    "dtype": "BF16", "shape": [1000, 64], "data_offsets": [128000, 256000]
                },
            },
        )
        res = resolver.resolve_full(str(directory))
        assert res.shape.total_params == 2 * 1000 * 64
        assert res.weight_bytes == 256000
        assert res.param_source is ParamSource.SAFETENSORS_HEADERS

    def test_packed_four_bit_weights_are_unpacked(self, tmp_path, resolver):
        """An int32 holds eight 4-bit weights; counting elements loses 8x."""
        from control_plane.resolver.hf import count_safetensors_params

        params, nbytes = count_safetensors_params(
            {
                "model.layers.0.mlp.down_proj.qweight": {
                    "dtype": "I32", "shape": [512, 64], "data_offsets": [0, 131072]
                },
                "model.layers.0.mlp.down_proj.qzeros": {
                    "dtype": "I32", "shape": [4, 64], "data_offsets": [131072, 132096]
                },
                "model.layers.0.mlp.down_proj.scales": {
                    "dtype": "F16", "shape": [4, 512], "data_offsets": [132096, 136192]
                },
            }
        )
        assert params == 512 * 64 * 8
        assert nbytes == 136192

    def test_mxfp4_blocks_count_two_weights_per_byte(self):
        from control_plane.resolver.hf import count_safetensors_params

        params, _ = count_safetensors_params(
            {
                "model.layers.0.mlp.experts.gate_up_proj_blocks": {
                    "dtype": "U8", "shape": [128, 2880, 90, 16], "data_offsets": [0, 530841600]
                },
                "model.layers.0.mlp.experts.gate_up_proj_scales": {
                    "dtype": "U8", "shape": [128, 2880, 90], "data_offsets": [530841600, 564019200]
                },
            }
        )
        assert params == 128 * 2880 * 90 * 16 * 2


# --------------------------------------------------------------------------
# Runtime support. Fitting and loading are different questions.
# --------------------------------------------------------------------------


class TestSpeechModels:
    """Whisper and its descendants: encoder-decoder, and served on a different
    endpoint family from everything else in the support tables."""

    def test_whisper_resolves_despite_publishing_its_widths_per_half(self, resolver):
        """The gate that used to reject it outright.

        `_first` is an exact-key lookup, and whisper-large-v3 publishes
        `decoder_attention_heads` rather than `num_attention_heads`, so
        map_config raised KeyError before anything else could run. It does
        carry `num_hidden_layers`, so the head count was the whole gap.
        """
        res = offline(resolver, "whisper-large-v3", "openai/whisper-large-v3")
        assert res.shape.num_layers == 32
        assert res.shape.hidden_size == 1280
        assert res.shape.num_attention_heads == 20
        assert res.architectures == ("WhisperForConditionalGeneration",)

    def test_the_shape_says_it_describes_only_the_decoder(self, resolver):
        """An encoder-decoder shape charged as if it were decoder-only
        understates the weights, so the guess is stated rather than hidden."""
        res = offline(resolver, "whisper-large-v3", "openai/whisper-large-v3")
        assert any("encoder-decoder" in w for w in res.warnings)

    def test_the_decoder_position_limit_is_read(self, resolver):
        """448, not absent.

        Whisper states `max_target_positions` and nothing under any of the
        usual names. Absent, the planner would offer a context length that vLLM
        then refuses to start with -- a launch that fails minutes later for a
        reason chosen here.
        """
        res = offline(resolver, "whisper-large-v3", "openai/whisper-large-v3")
        assert res.max_position_embeddings == 448

    def test_vllm_supports_whisper_and_sglang_says_it_does_not(self):
        """vLLM serves transcription; sglang has no transcription server. The
        table says so rather than implying both runtimes are equivalent."""
        verdict = build_verdict(("WhisperForConditionalGeneration",), "fp16")
        assert verdict.for_runtime("vllm").ok
        assert not verdict.for_runtime("sglang").ok

    def test_a_speech_architecture_reports_its_endpoint_family(self):
        """Being loadable and being answerable on the chat route are different
        claims; this is the second one."""
        assert modality_for(("WhisperForConditionalGeneration",)) == "transcription"
        assert modality_for(("LlamaForCausalLM",)) == "text"
        # An unresolved shape reports no architecture, and text is the safe
        # reading: the model stays on the routes it was always offered on.
        assert modality_for(()) == "text"

    def test_modality_of_reads_the_cache_and_says_text_when_it_is_cold(self, resolver):
        """`modality_of` reaches back into the shape cache, exactly as
        `supported_by` does, so a shape the cache has never seen reports text.

        That is the safe reading -- the model stays on the routes it was always
        offered on -- but it is also why the launch path does **not** use this:
        it takes the modality from the resolution in hand, so a cold cache
        cannot silently record a speech model as text. See
        `internal_api._modality_of`.
        """
        res = offline(resolver, "whisper-large-v3", "openai/whisper-large-v3")
        # resolve_config is documented "no network, no cache", so nothing was
        # written and the lookup has nothing to find.
        assert resolver.modality_of(res.shape) == "text"
        # The architectures the launch path actually uses say otherwise.
        assert modality_for(res.architectures) == "transcription"


class TestRuntimeSupport:
    def test_supported_architecture_and_quant(self):
        verdict = build_verdict(("LlamaForCausalLM",), "bf16")
        assert verdict.for_runtime("vllm").ok
        assert verdict.for_runtime("sglang").ok

    def test_unknown_architecture_is_not_waved_through(self):
        verdict = build_verdict(("SomethingBrandNewForCausalLM",), "bf16")
        assert not verdict.for_runtime("vllm").ok
        assert "not in" in verdict.for_runtime("vllm").reason

    def test_gguf_quant_is_refused_by_sglang(self):
        entry = build_verdict(("LlamaForCausalLM",), "q4_k_m").for_runtime("sglang")
        assert entry.level is SupportLevel.UNSUPPORTED
        assert "llama.cpp" in entry.reason

    def test_ggml_architecture_names_are_understood(self):
        assert build_verdict(("qwen2",), "bf16").for_runtime("vllm").ok

    def test_nvfp4_needs_blackwell(self):
        ok, problems = check_nodes("nvfp4", [WS_3090])
        assert not ok and "sm_8.6" in problems[0]
        assert check_nodes("nvfp4", list(GB10_PROFILES))[0]

    def test_mxfp4_runs_emulated_below_blackwell_and_says_so(self):
        ok, problems = check_nodes("mxfp4", [WS_3090])
        assert ok
        assert "emulated" in problems[0]
        assert check_nodes("mxfp4", list(GB10_PROFILES)) == (True, [])

    def test_quant_requirement_reports_the_threshold(self):
        assert quant_requirement("nvfp4").native_compute_capability == 10.0
        assert quant_requirement("bf16").check("12.1")[0]

    def test_supported_by_takes_a_shape(self, resolver):
        ok, reason = resolver.supported_by(LLAMA_3_3_70B, "vllm")
        assert isinstance(ok, bool) and isinstance(reason, str)

    def test_supported_on_checks_the_nodes_too(self, resolver):
        shape = ModelShape(
            model_id="x/y", num_layers=4, hidden_size=64, num_attention_heads=4,
            num_kv_heads=4, vocab_size=100, total_params=1_000_000, dtype="nvfp4",
        )
        assert not resolver.supported_on(shape, "vllm", [WS_3090])[0]

    def test_sharding_notes_flag_indivisible_kv_heads(self):
        notes = sharding_notes(LLAMA_3_3_70B, 3)
        assert notes and "3" in notes[0]


# --------------------------------------------------------------------------
# Cache, stub, and the port contract.
# --------------------------------------------------------------------------


class TestCache:
    def test_round_trip_preserves_every_field(self, tmp_path, resolver):
        res = offline(
            resolver, "gpt-oss-120b", "openai/gpt-oss-120b",
            measured_total=116_829_156_672, weight_bytes=65_248_893_184,
        )
        cache = ShapeCache(directory=tmp_path / "c")
        cache.put("openai/gpt-oss-120b", "main", None, res)
        hit = ShapeCache(directory=tmp_path / "c").get("openai/gpt-oss-120b", "main", None)
        assert hit is not None
        assert hit.shape == res.shape
        assert hit.weight_bytes == res.weight_bytes
        assert hit.support.for_runtime("vllm").reason == res.support.for_runtime("vllm").reason
        assert hit.from_cache

    def test_expired_entries_are_ignored(self, tmp_path, resolver):
        res = offline(resolver, "llama-3.3-70b", "llama")
        res.resolved_at = time.time() - 10_000
        cache = ShapeCache(directory=tmp_path / "c", ttl_seconds=60)
        cache.put("llama", "main", None, res)
        cache._memory.clear()
        assert cache.get("llama", "main", None) is None

    def test_commit_pinned_entries_never_expire(self, tmp_path, resolver):
        res = offline(resolver, "llama-3.3-70b", "llama")
        res.resolved_at = 0.0
        sha = "b5c939de8f754692c1647ca79fbf85e8c1e70f8a"
        cache = ShapeCache(directory=tmp_path / "c", ttl_seconds=60)
        cache.put("llama", sha, None, res)
        cache._memory.clear()
        assert cache.get("llama", sha, None) is not None

    def test_a_long_branch_name_is_not_treated_as_pinned(self, tmp_path, resolver):
        """Low fix: only a full 40-char commit sha is immortal.

        Before the fix, any revision spelling that was not literally ``main``
        and at least 32 characters long was treated as pinned, which made a
        long-lived branch name immortal in the cache.
        """
        branch = "feature/a-rather-long-branch-name-worth-more-than-32-chars"
        assert len(branch) >= 32
        res = offline(resolver, "llama-3.3-70b", "llama")
        res.resolved_at = time.time() - 10_000
        cache = ShapeCache(directory=tmp_path / "c", ttl_seconds=60)
        cache.put("llama", branch, None, res)
        cache._memory.clear()
        assert cache.get("llama", branch, None) is None

    def test_an_uppercase_commit_sha_still_counts_as_pinned(self, tmp_path, resolver):
        res = offline(resolver, "llama-3.3-70b", "llama")
        res.resolved_at = 0.0
        sha = "B5C939DE8F754692C1647CA79FBF85E8C1E70F8A"
        cache = ShapeCache(directory=tmp_path / "c", ttl_seconds=60)
        cache.put("llama", sha, None, res)
        cache._memory.clear()
        assert cache.get("llama", sha, None) is not None

    def test_non_positive_ttl_disables_the_cache_for_floating_refs(self, tmp_path, resolver):
        """Low fix: TTL<=0 means the cache is disabled, not that it never expires.

        0 meaning infinite is a trap a caller reaches for when they actually
        want "no TTL configured, so do not cache."
        """
        res = offline(resolver, "llama-3.3-70b", "llama")
        for ttl in (0, -1):
            cache = ShapeCache(directory=tmp_path / f"c{ttl}", ttl_seconds=ttl)
            cache.put("llama", "main", None, res)
            cache._memory.clear()  # force the on-disk read path too
            assert cache.get("llama", "main", None) is None

    def test_non_positive_ttl_still_exempts_a_pinned_commit(self, tmp_path, resolver):
        """The disable-switch is about floating refs; pinned content still holds."""
        res = offline(resolver, "llama-3.3-70b", "llama")
        sha = "b5c939de8f754692c1647ca79fbf85e8c1e70f8a"
        cache = ShapeCache(directory=tmp_path / "c", ttl_seconds=0)
        cache.put("llama", sha, None, res)
        cache._memory.clear()
        assert cache.get("llama", sha, None) is not None

    def test_a_dtype_override_is_a_different_entry(self, tmp_path, resolver):
        cache = ShapeCache(directory=tmp_path / "c")
        cache.put("m", "main", None, offline(resolver, "llama-3.3-70b", "m"))
        assert cache.get("m", "main", "q4_k_m") is None


class TestStub:
    def test_returns_the_frozen_fixtures(self):
        stub = StubResolver()
        assert stub.resolve("llama-3.3-70b") == LLAMA_3_3_70B
        assert stub.resolve("openai/gpt-oss-120b") == GPT_OSS_120B
        assert stub.resolve("deepseek-ai/DeepSeek-V3").mla_latent_dim == 512

    def test_unknown_id_is_a_clear_error_not_a_guess(self):
        with pytest.raises(ModelNotFound, match="stub"):
            StubResolver().resolve("someone/unheard-of")

    def test_returns_contract_types(self):
        shape = StubResolver().resolve("gpt-oss-120b")
        assert isinstance(shape, ModelShape)
        assert shape.dtype in BYTES_PER_PARAM

    def test_satisfies_the_port(self):
        from control_plane.contracts import ResolverPort

        def use(port: ResolverPort) -> ModelShape:
            return port.resolve("gpt-oss-120b")

        assert use(StubResolver()).is_moe


def test_real_resolver_satisfies_the_port(resolver):
    from control_plane.contracts import ResolverPort

    port: ResolverPort = resolver
    assert callable(port.resolve)


# --------------------------------------------------------------------------
# Network. Skipped when the hub is unreachable.
# --------------------------------------------------------------------------


def _hub_reachable() -> bool:
    if os.environ.get("DERATE_TEST_NETWORK") == "0":
        return False
    try:
        import requests

        return requests.get("https://huggingface.co/api/models/gpt2", timeout=5).ok
    except Exception:
        return False


HUB = _hub_reachable()
needs_hub = pytest.mark.skipif(not HUB, reason="huggingface.co is not reachable")

#: Llama 3.3 70B is gated, so the live check runs against a mirror of the same
#: weights. The shape is what is being asserted, not the licence.
LLAMA_MIRROR = "unsloth/Llama-3.3-70B-Instruct"


@needs_hub
class TestLive:
    def test_llama_3_3_70b_kv_heads(self, resolver):
        shape = resolver.resolve(LLAMA_MIRROR)
        assert (shape.num_layers, shape.hidden_size) == (80, 8192)
        assert (shape.num_attention_heads, shape.num_kv_heads) == (64, 8)

    def test_total_params_within_one_percent_of_published(self, resolver):
        """Acceptance: read the index, do not trust the card."""
        for model_id, published in (
            (LLAMA_MIRROR, 70.55e9),
            ("openai/gpt-oss-120b", 116.8e9),
            ("Qwen/Qwen3-30B-A3B", 30.53e9),
        ):
            shape = resolver.resolve(model_id)
            assert shape.total_params == pytest.approx(published, rel=0.01), model_id

    def test_gpt_oss_120b(self, resolver):
        res = resolver.resolve_full("openai/gpt-oss-120b")
        assert res.shape.dtype == "mxfp4"
        assert res.shape.num_experts == 128
        assert res.shape.layers_with_full_attention == 18
        assert res.param_source is ParamSource.HUB_SAFETENSORS_INDEX
        assert res.weight_bytes and res.weight_bytes > 0

    def test_deepseek_mla(self, resolver):
        res = resolver.resolve_full("deepseek-ai/DeepSeek-V3")
        assert res.shape.mla_latent_dim == 512
        assert res.shape.total_params == pytest.approx(671e9, rel=0.01)

    def test_matches_the_frozen_fixtures(self, resolver):
        """Live resolution and the day-0 fixtures must not drift apart."""
        for model_id, fixture in (
            (LLAMA_MIRROR, LLAMA_3_3_70B),
            ("openai/gpt-oss-120b", GPT_OSS_120B),
            ("deepseek-ai/DeepSeek-V3", DEEPSEEK_V3),
        ):
            shape = resolver.resolve(model_id)
            assert shape.num_layers == fixture.num_layers, model_id
            assert shape.hidden_size == fixture.hidden_size, model_id
            assert shape.num_attention_heads == fixture.num_attention_heads, model_id
            assert shape.num_kv_heads == fixture.num_kv_heads, model_id
            assert shape.vocab_size == fixture.vocab_size, model_id
            assert shape.dtype == fixture.dtype, model_id
            assert shape.num_experts == fixture.num_experts, model_id
            assert shape.total_params == pytest.approx(fixture.total_params, rel=0.01), model_id

    def test_cold_under_two_seconds_and_cached_under_fifty_milliseconds(self, resolver):
        start = time.perf_counter()
        resolver.resolve("openai/gpt-oss-120b")
        cold_ms = (time.perf_counter() - start) * 1000
        assert cold_ms < 2000, f"cold resolve took {cold_ms:.0f}ms"

        start = time.perf_counter()
        resolver.resolve("openai/gpt-oss-120b")
        warm_ms = (time.perf_counter() - start) * 1000
        assert warm_ms < 50, f"cached resolve took {warm_ms:.0f}ms"

    def test_cache_survives_a_new_process(self, tmp_path):
        directory = tmp_path / "shared"
        ModelResolver(cache=ShapeCache(directory=directory)).resolve("openai/gpt-oss-20b")
        second = ModelResolver(cache=ShapeCache(directory=directory))
        start = time.perf_counter()
        shape = second.resolve("openai/gpt-oss-20b")
        assert (time.perf_counter() - start) * 1000 < 50
        assert shape.num_layers == 24

    def test_gated_model_says_what_to_do(self, resolver):
        from control_plane.resolver import MetadataUnavailable

        if os.environ.get("HF_TOKEN"):
            pytest.skip("a token is configured, so the gated repo may resolve")
        with pytest.raises(MetadataUnavailable, match="HF_TOKEN"):
            resolver.resolve("meta-llama/Llama-3.3-70B-Instruct")

    def test_available_quants(self, resolver):
        quants = resolver.available_quants("Qwen/Qwen3-30B-A3B")
        assert "bf16" in quants
        assert all(q in BYTES_PER_PARAM for q in quants)

    def test_remote_gguf(self, resolver):
        res = resolver.resolve_gguf_full(
            "hf://bartowski/Qwen2.5-0.5B-Instruct-GGUF/Qwen2.5-0.5B-Instruct-Q4_K_M.gguf"
        )
        assert res.shape.num_kv_heads == 2
        assert res.shape.dtype == "q4_k_m"
        assert res.weight_bytes and res.weight_bytes > 0


from control_plane.contracts.quant import QUANT_INFO  # noqa: E402
from control_plane.resolver.quant_detect import from_name as _from_name  # noqa: E402


class TestQuantLadderCoverage:
    """The llama.cpp ladder, including the formats Unsloth actually publishes.

    Before this, ``from_name`` recognised 13 of the 27 ``.gguf`` files in
    ``unsloth/Qwen3-30B-A3B-GGUF``. The other 14 -- every Unsloth Dynamic quant
    and the whole importance-matrix family -- returned ``None``, fell through to
    the bf16 default, and were charged three to eight times their real size. The
    visible symptom was ``unsloth/DeepSeek-R1-GGUF`` planning as 671B bf16 and
    being refused outright by the fit gate.
    """

    #: Real file names from unsloth/Qwen3-30B-A3B-GGUF, with the scheme each
    #: must resolve to. Not a sample: this is the whole repo listing.
    UNSLOTH_FILES = [
        ("Qwen3-30B-A3B-UD-Q2_K_XL.gguf", "q2_k"),
        ("Qwen3-30B-A3B-UD-Q3_K_XL.gguf", "q3_k_m"),
        ("Qwen3-30B-A3B-UD-Q4_K_XL.gguf", "q4_k_m"),
        ("Qwen3-30B-A3B-UD-Q5_K_XL.gguf", "q5_k_m"),
        ("Qwen3-30B-A3B-UD-Q6_K_XL.gguf", "q6_k"),
        ("Qwen3-30B-A3B-UD-Q8_K_XL.gguf", "q8_0"),
        ("Qwen3-30B-A3B-UD-IQ1_S.gguf", "iq1_s"),
        ("Qwen3-30B-A3B-UD-IQ1_M.gguf", "iq1_m"),
        ("Qwen3-30B-A3B-UD-IQ2_M.gguf", "iq2_m"),
        ("Qwen3-30B-A3B-UD-IQ2_XXS.gguf", "iq2_xxs"),
        ("Qwen3-30B-A3B-UD-IQ3_XXS.gguf", "iq3_xxs"),
        ("Qwen3-30B-A3B-IQ4_NL.gguf", "iq4_nl"),
        ("Qwen3-30B-A3B-IQ4_XS.gguf", "iq4_xs"),
        ("Qwen3-30B-A3B-Q2_K.gguf", "q2_k"),
        ("Qwen3-30B-A3B-Q2_K_L.gguf", "q2_k"),
        ("Qwen3-30B-A3B-Q3_K_M.gguf", "q3_k_m"),
        ("Qwen3-30B-A3B-Q3_K_S.gguf", "q3_k_m"),
        ("Qwen3-30B-A3B-Q4_0.gguf", "q4_0"),
        ("Qwen3-30B-A3B-Q4_1.gguf", "q4_1"),
        ("Qwen3-30B-A3B-Q4_K_M.gguf", "q4_k_m"),
        ("Qwen3-30B-A3B-Q4_K_S.gguf", "q4_k_m"),
        ("Qwen3-30B-A3B-Q5_K_M.gguf", "q5_k_m"),
        ("Qwen3-30B-A3B-Q5_K_S.gguf", "q5_k_m"),
        ("Qwen3-30B-A3B-Q6_K.gguf", "q6_k"),
        ("Qwen3-30B-A3B-Q8_0.gguf", "q8_0"),
        ("BF16/Qwen3-30B-A3B-BF16-00001-of-00002.gguf", "bf16"),
        ("BF16/Qwen3-30B-A3B-BF16-00002-of-00002.gguf", "bf16"),
    ]

    def test_every_real_unsloth_filename_is_recognised(self):
        missed = [f for f, _ in self.UNSLOTH_FILES if _from_name(f) is None]
        assert not missed, f"{len(missed)} of {len(self.UNSLOTH_FILES)} undetected: {missed}"

    def test_each_real_filename_resolves_to_the_right_scheme(self):
        wrong = [
            (f, want, _from_name(f))
            for f, want in self.UNSLOTH_FILES
            if _from_name(f) != want
        ]
        assert not wrong, f"misdetected: {wrong}"

    def test_the_ud_prefix_is_stripped_rather_than_defeating_detection(self):
        """"UD-" says how a file was built, not what it costs."""
        for spelling in ("UD-Q4_K_XL", "ud-q4_k_xl", "UD_Q4_K_XL", "udq4kxl"):
            assert normalize_dtype(spelling) == "q4_k_m", spelling

    def test_bare_ud_is_not_a_quantization(self):
        assert normalize_dtype("ud") is None
        assert normalize_dtype("ud-nonsense") is None

    def test_each_family_is_strictly_monotonic_in_cost(self):
        """A mistyped constant shows up here and almost nowhere else.

        Checked within a family rather than across all of them, because the two
        families are not currently comparable -- see the xfail below.
        """
        families = {
            "importance-matrix": [
                "iq1_s", "iq1_m", "iq2_xxs", "iq2_xs", "iq2_s", "iq2_m",
                "iq3_xxs", "iq3_xs", "iq3_s", "iq3_m", "iq4_xs", "iq4_nl",
            ],
            "llama.cpp block": [
                "q2_k", "q3_k_m", "q4_0", "q4_k_m", "q4_1",
                "q5_0", "q5_k_m", "q5_1", "q6_k", "q8_0",
            ],
            "float": ["fp8", "fp16", "fp32"],
        }
        for name, ladder in families.items():
            for lower, higher in zip(ladder, ladder[1:]):
                assert BYTES_PER_PARAM[lower] < BYTES_PER_PARAM[higher], (
                    f"{name}: {lower} ({BYTES_PER_PARAM[lower]}) must cost less "
                    f"than {higher} ({BYTES_PER_PARAM[higher]})"
                )

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "q2_k and q3_k_m carry the pure-block figures (2.63 and 3.65 bpw). "
            "llama.cpp's own measured table says 3.1593 and 3.9960, and the real "
            "files in unsloth/Qwen3-30B-A3B-GGUF measure 2.96 and 3.85 -- so both "
            "guess LOW, the one direction contracts/quant.py's docstring forbids. "
            "They are not corrected here because that docstring calls them frozen "
            "in the architecture doc and the Agent C brief; changing them is a "
            "contract decision, not a bug fix. When it is taken, this test starts "
            "passing and should have the xfail removed."
        ),
    )
    def test_k_quants_price_at_least_what_the_real_files_cost(self):
        """Measured bpw, from hub file sizes / 30.53B params on Qwen3-30B-A3B."""
        for key, measured in (("q2_k", 2.95), ("q3_k_m", 3.85)):
            assert BYTES_PER_PARAM[key] * 8 >= measured, key

    def test_every_priced_scheme_has_a_quant_info_row(self):
        assert set(BYTES_PER_PARAM) == set(QUANT_INFO)

    def test_quant_info_bits_agree_with_the_byte_table(self):
        for key, info in QUANT_INFO.items():
            assert info.bits_per_weight == pytest.approx(
                BYTES_PER_PARAM[key] * 8, rel=1e-3
            ), key

    def test_the_formats_that_used_to_guess_low_no_longer_do(self):
        """Q4_1, Q5_0 and Q5_1 were all priced as q4_0's 4.5 bpw.

        Their real block costs are 5.0, 5.5 and 6.0. Under-counting is the one
        direction this table may not be wrong in, because it becomes an
        out-of-memory kill minutes into a load rather than a refusal before it.
        """
        for key, floor in (("q4_1", 5.0), ("q5_0", 5.5), ("q5_1", 6.0)):
            assert BYTES_PER_PARAM[key] * 8 >= floor, key

    def test_every_ftype_maps_to_a_priced_scheme(self):
        from control_plane.resolver.gguf import GGUF_FILE_TYPES

        unknown = {v for v in GGUF_FILE_TYPES.values() if v not in BYTES_PER_PARAM}
        assert not unknown, f"ftype map names unpriced schemes: {unknown}"

    def test_the_importance_matrix_ftypes_are_present(self):
        """22-31 were absent, so those files fell through to the bf16 default."""
        from control_plane.resolver.gguf import GGUF_FILE_TYPES

        for ftype in range(19, 32):
            assert ftype in GGUF_FILE_TYPES, f"LLAMA_FTYPE {ftype} is unmapped"

    def test_gguf_schemes_have_a_verdict_on_both_runtimes(self):
        """An absent key defaults to UNSUPPORTED, which would silently make the
        IQ family stricter on vLLM than the K-quants beside it."""
        from control_plane.resolver.support import _SGLANG_QUANTS, _VLLM_QUANTS

        gguf = {k for k in BYTES_PER_PARAM if quant_info(k).family == "gguf"}
        assert not gguf - set(_VLLM_QUANTS)
        assert not gguf - set(_SGLANG_QUANTS)


from control_plane.resolver.support import build_verdict  # noqa: E402
from control_plane.resolver.types import (  # noqa: E402
    ParamSource,
    QuantSource,
    Resolution,
)
from tests.fixtures import MODEL_SHAPES  # noqa: E402


class TestQuantVariants:
    """The ladder as something you can actually pick from.

    ``available_quants`` returned scheme keys and threw away the repository each
    came from, which is most of the answer: a quantization is a different
    repository, not a flag, so a bare key is not launchable and not browsable.
    """

    class _Hub:
        """A hub with one model published at several quantizations."""

        def __init__(self):
            self.searched = []

        def search(self, query, limit=50):
            self.searched.append(query)
            return [
                {"id": "acme/Widget-7B", "downloads": 900},
                {"id": "acme/Widget-7B-AWQ", "downloads": 120},
                {"id": "acme/Widget-7B-GGUF", "downloads": 400},
                # A GGUF repo whose name does not end in "gguf". Real: a
                # requantizer's second pass carries a qualifier after the
                # format.
                {"id": "acme/Widget-7B-GGUF-v2", "downloads": 300},
                # A different model whose name merely contains the stem.
                {"id": "acme/Widget-7B-Thinking-2507-GGUF", "downloads": 999},
                # Apple silicon; nothing in this cluster loads it.
                {"id": "acme/Widget-7B-mlx", "downloads": 50},
            ]

        def read_range(self, model_id, filename, start, length, revision="main"):
            """Serve the header of the one file whose name refuses to say."""
            blob = _special_gguf_bytes()
            return blob[start : start + length]

        def model_info(self, model_id, revision="main"):
            from control_plane.resolver.hf import ModelInfo

            if model_id == "acme/Widget-7B-GGUF-v2":
                files = {"Widget-7B-Q5_K_M.gguf": 5_100_000_000}
                return ModelInfo(
                    model_id, "sha", tuple(files), None, None, (), file_sizes=files
                )
            if model_id != "acme/Widget-7B-GGUF":
                return ModelInfo(model_id, "sha", (), None, None, ())
            files = {
                "Widget-7B-UD-Q4_K_XL.gguf": 4_000_000_000,
                "Widget-7B-IQ4_XS.gguf": 3_700_000_000,
                "Widget-7B-Q8_0.gguf": 7_500_000_000,
                # One quantization split across two shards.
                "BF16/Widget-7B-BF16-00001-of-00002.gguf": 9_000_000_000,
                "BF16/Widget-7B-BF16-00002-of-00002.gguf": 5_000_000_000,
                # Not weights. A projector is loaded beside a model, an
                # importance matrix is calibration data for building the IQ
                # quants, and neither is a thing you serve.
                "mmproj-F16.gguf": 600_000_000,
                "imatrix.gguf": 20_000_000,
                # A real quantization whose name says nothing recognisable.
                "Widget-7B-special.gguf": 3_100_000_000,
            }
            return ModelInfo(
                model_id, "sha", tuple(files), None, None, (), file_sizes=files
            )

    def _resolver(self, monkeypatch, model_id="acme/Widget-7B-GGUF"):
        resolver = ModelResolver(client=self._Hub())
        shape = MODEL_SHAPES["llama-3.3-70b"]
        monkeypatch.setattr(
            resolver,
            "resolve_full",
            lambda mid, dtype=None: Resolution(
                shape=shape,
                revision="main",
                param_source=ParamSource.CONFIG_ESTIMATE,
                quant_source=QuantSource.TORCH_DTYPE,
                support=build_verdict(("LlamaForCausalLM",), shape.dtype),
            ),
        )
        return resolver

    def test_each_variant_carries_the_repository_you_would_launch(self, monkeypatch):
        variants = self._resolver(monkeypatch).quant_variants("acme/Widget-7B-GGUF")
        assert variants, "no variants found"
        assert all(v.repo_id for v in variants)
        assert {"acme/Widget-7B-AWQ"} <= {v.repo_id for v in variants}

    def test_a_sibling_model_is_not_offered_as_a_quantization(self, monkeypatch):
        """Widget-7B-Thinking-2507 contains the stem but is a different model.

        Offering it would present an unrelated multi-gigabyte download as a
        smaller build of the thing that was asked for.
        """
        variants = self._resolver(monkeypatch).quant_variants("acme/Widget-7B-GGUF")
        assert not [v for v in variants if "Thinking" in v.repo_id]

    def test_mlx_repositories_are_still_excluded(self, monkeypatch):
        variants = self._resolver(monkeypatch).quant_variants("acme/Widget-7B-GGUF")
        assert not [v for v in variants if "mlx" in v.repo_id.lower()]

    def test_shards_are_summed_into_one_variant(self, monkeypatch):
        """Two shards are one 14 GB variant, not two undersized ones.

        Listed separately they would both look too small to be the model, and
        picking either would fetch half of it.
        """
        variants = self._resolver(monkeypatch).quant_variants("acme/Widget-7B-GGUF")
        bf16 = [v for v in variants if v.gguf_file and "BF16" in v.gguf_file]
        assert len(bf16) == 1, [v.label for v in bf16]
        assert bf16[0].file_bytes == 14_000_000_000

    def test_gguf_file_sizes_are_measured_not_estimated(self, monkeypatch):
        variants = self._resolver(monkeypatch).quant_variants("acme/Widget-7B-GGUF")
        by_label = {v.label: v for v in variants}
        assert by_label["UD-Q4_K_XL"].file_bytes == 4_000_000_000

    def test_the_published_name_is_kept_beside_the_canonical_key(self, monkeypatch):
        """Rendering "q4_k_m" for a file called "UD-Q4_K_XL" would paraphrase a
        name somebody else chose, which this codebase does not do to server
        strings."""
        variants = self._resolver(monkeypatch).quant_variants("acme/Widget-7B-GGUF")
        ud = [v for v in variants if v.label == "UD-Q4_K_XL"]
        assert ud and ud[0].dtype == "q4_k_m"

    def test_a_gguf_repository_is_not_launchable_by_its_own_id(self, monkeypatch):
        """These repos keep the original config.json, so torch_dtype reads
        bfloat16 and the repo resolves to a confident bf16 that is not a set of
        weights anything here could load."""
        variants = self._resolver(monkeypatch).quant_variants("acme/Widget-7B-GGUF")
        own = [v for v in variants if v.source == "self"][0]
        assert own.launchable is False
        assert "choose one" in own.note

    def test_no_gguf_file_is_launchable(self, monkeypatch):
        """Both serve command templates take a repository path, not a file."""
        variants = self._resolver(monkeypatch).quant_variants("acme/Widget-7B-GGUF")
        assert all(not v.launchable for v in variants if v.source == "gguf_file")

    def test_a_safetensors_quantization_repo_is_launchable(self, monkeypatch):
        variants = self._resolver(monkeypatch).quant_variants("acme/Widget-7B-GGUF")
        awq = [v for v in variants if v.repo_id.endswith("-AWQ")][0]
        assert awq.launchable is True and awq.dtype == "awq_int4"

    def test_variants_are_unique(self, monkeypatch):
        variants = self._resolver(monkeypatch).quant_variants("acme/Widget-7B-GGUF")
        keys = [(v.repo_id, v.gguf_file) for v in variants]
        assert len(keys) == len(set(keys))

    def test_available_quants_is_derived_and_still_sorted_keys(self, monkeypatch):
        """The old API keeps working, and cannot drift from the new one."""
        resolver = self._resolver(monkeypatch)
        keys = resolver.available_quants("acme/Widget-7B-GGUF")
        assert keys == sorted(set(keys))
        assert set(keys) == {v.dtype for v in resolver.quant_variants("acme/Widget-7B-GGUF")}

    def test_files_that_are_not_weights_are_never_offered(self, monkeypatch):
        """A projector and an importance matrix share the folder and the
        extension with the weights, and neither is a thing you can serve.

        Offered as variants they read as unusually small builds of the model --
        a 600 MB row beside a 4 GB one looks like the efficient choice.
        """
        variants = self._resolver(monkeypatch).quant_variants("acme/Widget-7B-GGUF")
        files = [v.gguf_file or "" for v in variants]
        assert not [f for f in files if "mmproj" in f]
        assert not [f for f in files if f.endswith("imatrix.gguf")]

    def test_a_file_whose_name_says_nothing_is_read_not_dropped(self, monkeypatch):
        """The header is the file's own account of itself.

        Dropping the file because its name missed a convention hides a
        quantization that exists; reading a few KiB of it costs one ranged
        request and answers the question exactly.
        """
        variants = self._resolver(monkeypatch).quant_variants("acme/Widget-7B-GGUF")
        special = [v for v in variants if v.gguf_file == "Widget-7B-special.gguf"]
        assert special, [v.gguf_file for v in variants]
        assert special[0].dtype == "q8_0"
        assert "header" in special[0].note
        assert special[0].file_bytes == 3_100_000_000

    def test_a_gguf_repo_whose_name_does_not_end_in_gguf_is_found(self, monkeypatch):
        """"-GGUF-v2" is a requantizer's second pass, not a different model."""
        variants = self._resolver(monkeypatch).quant_variants("acme/Widget-7B-GGUF")
        from_v2 = [v for v in variants if v.repo_id == "acme/Widget-7B-GGUF-v2"]
        assert from_v2, sorted({v.repo_id for v in variants})
        assert from_v2[0].label == "Q5_K_M"

    def test_a_shard_family_reports_how_many_files_it_is(self, monkeypatch):
        """Without the count, one filename and a summed size disagree about how
        much is being described."""
        variants = self._resolver(monkeypatch).quant_variants("acme/Widget-7B-GGUF")
        bf16 = [v for v in variants if v.gguf_file and "BF16" in v.gguf_file][0]
        assert bf16.shard_count == 2
        assert "2 of 2 shards" in bf16.note
        single = [v for v in variants if v.label == "UD-Q4_K_XL"][0]
        assert single.shard_count == 1

    def test_the_first_shard_is_the_one_named(self, monkeypatch):
        """Shard one carries the header, so it is the file worth naming and the
        one a reader would open."""
        variants = self._resolver(monkeypatch).quant_variants("acme/Widget-7B-GGUF")
        bf16 = [v for v in variants if v.gguf_file and "BF16" in v.gguf_file][0]
        assert bf16.gguf_file.endswith("-00001-of-00002.gguf")

    def test_offline_returns_the_repository_itself_and_no_search(self, monkeypatch):
        """Never an empty list masquerading as "no quantizations exist"."""
        resolver = self._resolver(monkeypatch)
        resolver.offline = True
        variants = resolver.quant_variants("acme/Widget-7B-GGUF")
        assert [v.source for v in variants] == ["self"]
        assert resolver.client.searched == []
