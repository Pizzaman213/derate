"""The KV width a plan stores is the one the engine stores for THIS checkpoint.

Three facts read off the pinned vLLM image (0.28.1rc1) and the GB10 here:
the model's own cache is the compute dtype and never the weight scheme; a
checkpoint whose quantization config declares a KV scheme is stored at it when
vLLM is told nothing; and a 16-bit name that is not the compute width is never
passed, because `reshape_and_cache_flash` copies the bits unconverted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from control_plane.fit.kv import kv_bytes_per_token
from control_plane.gateway import kv_widths
from control_plane.resolver import ModelResolver, ShapeCache
from control_plane.resolver.quant_detect import checkpoint_kv_dtype, compute_dtype

DATA = Path(__file__).resolve().parents[1] / "resolver_data"


def _config(name: str) -> dict:
    return json.loads((DATA / f"{name}.config.json").read_text())


@pytest.fixture
def resolve(tmp_path, monkeypatch):
    monkeypatch.setenv("DERATE_MODEL_DIRS", str(tmp_path))
    resolver = ModelResolver(cache=ShapeCache(directory=tmp_path / "cache"))
    return lambda name, config=None: resolver.resolve_config(
        config or _config(name), f"acme/{name}"
    )


@pytest.mark.parametrize(
    "quant, sidecar, expected",
    [
        # ModelOpt in config.json: the static 8-bit float scheme is fp8_e4m3.
        ({"quant_method": "modelopt", "quant_algo": "NVFP4",
          "kv_cache_scheme": {"dynamic": False, "num_bits": 8, "type": "float"}}, None, "fp8_e4m3"),
        # A 4-bit float scheme is nvfp4; a dynamic 8-bit one is not mapped.
        ({"quant_method": "modelopt", "quant_algo": "FP8",
          "kv_cache_scheme": {"num_bits": 4, "type": "float"}}, None, "nvfp4"),
        ({"quant_method": "modelopt", "quant_algo": "FP8",
          "kv_cache_scheme": {"dynamic": True, "num_bits": 8, "type": "float"}}, None, ""),
        # The legacy sidecar carries no quant_method; the producer names it.
        (None, {"producer": {"name": "modelopt"},
                "quantization": {"quant_algo": "FP8", "kv_cache_quant_algo": "FP8"}}, "fp8_e4m3"),
        # compressed-tensors: any declared scheme becomes fp8, a null one nothing.
        ({"quant_method": "compressed-tensors", "kv_cache_scheme": {"num_bits": 8, "type": "float"}},
         None, "fp8"),
        ({"quant_method": "compressed-tensors", "kv_cache_scheme": None}, None, ""),
        # A weight scheme says nothing about the cache.
        ({"quant_method": "fp8"}, None, ""),
    ],
)
def test_the_checkpoint_declares_its_cache_the_way_vllm_reads_it(quant, sidecar, expected):
    config = {"quantization_config": quant} if quant else {}
    assert checkpoint_kv_dtype(config, sidecar) == expected


def test_the_compute_dtype_falls_back_to_the_text_config_and_stays_unknown_when_unstated():
    assert compute_dtype({"torch_dtype": "float16"}) == "float16"
    assert compute_dtype({"text_config": {"dtype": "bfloat16"}}) == "bfloat16"
    assert compute_dtype({}) == ""


def test_an_fp8_weight_checkpoint_caches_at_its_compute_width(resolve):
    """`auto` used to read the WEIGHT dtype: an fp8 checkpoint's cache was
    priced at one byte, approving twice the context the engine holds."""
    res = resolve("llama-3.1-8b-fp8-ct")
    assert res.shape.dtype == "fp8"
    assert kv_bytes_per_token(res.shape, "auto") == kv_bytes_per_token(res.shape, "bf16")
    # TurboQuant keeps its boundary layers at the model's own width, 16 bits.
    plain = resolve("llama-3.1-8b-fp8-ct", {**_config("llama-3.1-8b-fp8-ct"),
                                            "quantization_config": None})
    assert kv_bytes_per_token(res.shape, "turboquant_3bit_nc") == pytest.approx(
        kv_bytes_per_token(plain.shape, "turboquant_3bit_nc")
    )


def test_a_declared_scheme_is_priced_by_name_and_never_passed(resolve):
    res = resolve("qwen3-next-80b-nvfp4")
    assert (res.compute_dtype, res.checkpoint_kv_dtype) == ("bfloat16", "fp8_e4m3")
    assert kv_widths.settle(res.shape, res, "vllm", None) == ("fp8_e4m3", None)
    # Only vLLM applies it; elsewhere the own width is the 16-bit compute one.
    assert kv_widths.settle(res.shape, res, "sglang", None) == ("auto", None)
    choices = kv_widths.choices(res.shape, res, "vllm", None)
    assert choices.own.declared and choices.own_ratio == pytest.approx(0.5)
    # vLLM reads it off the checkpoint, so passing it would only make the
    # record look like the operator's pick (and refuse a custom command).
    assert kv_widths.passed("fp8_e4m3", choices.own) is None
    assert "fp8_e4m3" not in choices.flags and "fp8" in choices.flags
    # The compute width overrides it, by name; the other 16-bit one corrupts.
    assert kv_widths.settle(res.shape, res, "vllm", "bfloat16") == ("bfloat16", None)
    assert choices.flags["bfloat16"] == "--kv-cache-dtype bfloat16"
    assert choices.ratios["bfloat16"] == 1.0 and "float16" in choices.refused


def test_the_16_bit_name_that_is_not_the_compute_width_is_refused(resolve):
    bf16 = resolve("qwen3-30b-a3b")
    _, refusal = kv_widths.settle(bf16.shape, bf16, "vllm", "float16")
    assert refusal is not None and "choose bfloat16" in refusal
    # The compute width is the model's own: priced as it, and no flag.
    assert kv_widths.settle(bf16.shape, bf16, "vllm", "bfloat16") == ("auto", None)
    assert "bfloat16" not in kv_widths.choices(bf16.shape, bf16, "vllm", None).flags
    fp16 = resolve("llama-3.1-8b-awq")
    assert kv_widths.settle(fp16.shape, fp16, "vllm", "bfloat16")[1] is not None
    # llama.cpp's default cache is f16 whatever the model computes in.
    assert kv_widths.settle(bf16.shape, bf16, "llamacpp", "bfloat16")[1] is not None


@pytest.mark.parametrize("spelling", [None, "", "auto", "fp16", "bf16", "half"])
def test_derate_s_own_spellings_still_mean_the_model_s_own(resolve, spelling):
    """`fp16` has always meant "the model's own" here (settings, autoadopt)."""
    res = resolve("qwen3-30b-a3b")
    assert kv_widths.settle(res.shape, res, "vllm", spelling) == ("auto", None)


def test_an_unstated_compute_dtype_offers_no_16_bit_name_by_name(resolve):
    res = resolve("gpt-oss-120b")
    assert res.compute_dtype == ""
    refused = kv_widths.choices(res.shape, res, "vllm", None).refused
    assert "does not say what it computes in" in refused["bfloat16"]
    assert kv_widths.settle(res.shape, res, "vllm", None) == ("auto", None)


def test_every_engine_says_how_it_picks_its_own_width():
    from control_plane.engines import SUPPORTED

    assert set(kv_widths._UNTOLD) == set(SUPPORTED)


def test_the_names_that_render_no_flag_are_exactly_the_model_s_own_spellings():
    """Two homes of one set; this change had to move float16/bfloat16 in both."""
    from control_plane.deploy.flags import _KV_DTYPE_TO_VLLM
    from control_plane.fit.kv import MODEL_OWN_KV_DTYPES

    assert {k for k, v in _KV_DTYPE_TO_VLLM.items() if v is None} == set(MODEL_OWN_KV_DTYPES)


def test_every_refused_width_says_why_for_this_attention(resolve):
    gqa = kv_widths.choices(resolve("qwen3-30b-a3b").shape, resolve("qwen3-30b-a3b"), "vllm", None)
    assert "multi-head latent attention" in gqa.refused["fp8_ds_mla"]
    assert "Gaudi" in gqa.refused["fp8_inc"]
    assert not set(gqa.ratios) & set(gqa.refused)
    mla = resolve("deepseek-v3")
    assert "fp8_ds_mla" in kv_widths.choices(mla.shape, mla, "vllm", None).ratios
    mamba = resolve("mamba-2.8b")
    stateful = kv_widths.choices(mamba.shape, mamba, "vllm", None)
    assert not stateful.ratios and stateful.own_ratio is None
    assert "no per-token cache" in stateful.refused["fp8"]


def test_the_machines_judge_what_the_engine_stores(resolve):
    res = resolve("qwen3-next-80b-nvfp4")
    asked = []

    def silicon(*, kv_cache_dtype, kv_label):
        asked.append(kv_cache_dtype)
        return ({"spark-01": f"no {kv_label} kernel"} if kv_label == "nvfp4" else {}), {}

    choices = kv_widths.choices(res.shape, res, "vllm", silicon)
    assert choices.refused["nvfp4"] == "no nvfp4 kernel" and "fp8" in choices.ratios
    # The declared width is never passed, yet the engine stores it: judged.
    assert "fp8_e4m3" in asked
    asked.clear()
    kv_widths.choices(res.shape, res, "sglang", silicon)
    assert asked == []


def test_a_declared_width_the_gate_refuses_is_said_as_the_checkpoint_s(resolve):
    config = _config("deepseek-v3")
    config["quantization_config"] = {
        "quant_method": "modelopt", "quant_algo": "FP8", "kv_cache_quant_algo": "NVFP4",
    }
    res = resolve("deepseek-v3", config)
    _, refusal = kv_widths.settle(res.shape, res, "vllm", None)
    assert refusal is not None and "quantization config declares" in refusal
    assert "choose bfloat16 to override it" in refusal


def _client(res, seen):
    from tests.unit.test_gateway import StubPlanner, StubResolver, TestClient, build_deps, create_app

    class Declaring(StubResolver):
        def resolve_full(self, model_id, dtype=None):
            return res

    class Recording(StubPlanner):
        def plan(self, *args, kv_dtype=None, **kwargs):
            seen.append(kv_dtype)
            return super().plan(*args, kv_dtype=kv_dtype, **kwargs)

    deps = build_deps()
    deps.resolver, deps.planner = Declaring(), Recording()
    return TestClient(create_app(deps))


def test_the_plan_prices_the_declared_width_and_names_it(resolve):
    res = resolve("qwen3-next-80b-nvfp4")
    seen = []
    with _client(res, seen) as client:
        own = client.post("/api/plan", json={"model_id": res.shape.model_id})
        sixteen = client.post("/api/plan", json={"model_id": res.shape.model_id, "kv_dtype": "float16"})
    assert own.status_code == 200, own.text
    assert own.json()["kv_dtype_own"] == {"name": "fp8_e4m3", "declared": True, "ratio": 0.5}
    assert set(seen) == {"fp8_e4m3"}
    assert sixteen.status_code == 400 and "choose bfloat16" in sixteen.json()["error"]["message"]


@pytest.mark.parametrize("extra", ["--kv-cache-dtype=bfloat16", "--kv_cache_dtype bfloat16", "-ctk f32"])
def test_extra_args_cannot_carry_a_kv_width_the_gate_never_priced(resolve, extra):
    res = resolve("qwen3-30b-a3b")
    with _client(res, []) as client:
        reply = client.post("/api/deployments", json={"model_id": res.shape.model_id, "extra_args": extra})
    assert reply.status_code == 400
    assert "is not taken as free text -- the KV width" in reply.json()["error"]["message"]


@pytest.mark.parametrize("runtime, name", [("sglang", "bfloat16"), ("tts", "bfloat16"), ("llamacpp", "float16"), ("vllm", "bfloat16")])
def test_naming_the_model_s_own_16_bit_width_plans_on_every_runtime(resolve, runtime, name):
    res = resolve("qwen3-30b-a3b")
    with _client(res, []) as client:
        reply = client.post("/api/plan", json={"model_id": res.shape.model_id, "runtime": runtime, "kv_dtype": name})
    assert reply.status_code == 200, reply.text


def test_fp8_e5m2_is_refused_where_attention_gets_a_kv_quant_method(resolve):
    from dataclasses import replace

    declared = resolve("qwen3-next-80b-nvfp4")  # modelopt, declares fp8
    assert "choose fp8_e4m3" in kv_widths.settle(declared.shape, declared, "vllm", "fp8_e5m2")[1]
    # Weight-only compressed-tensors keeps fp8_e5m2 (no KV method for attention).
    weight_only = resolve("llama-3.1-8b-fp8-ct")
    assert kv_widths.settle(weight_only.shape, weight_only, "vllm", "fp8_e5m2") == ("fp8_e5m2", None)
    # A forced fp8 weight scheme gives attention the KV method too.
    plain = resolve("qwen3-30b-a3b")
    forced = replace(plain.shape, dtype="fp8")
    assert kv_widths.settle(forced, plain, "vllm", "fp8_e5m2")[1] is not None
    assert kv_widths.settle(plain.shape, plain, "vllm", "fp8_e5m2") == ("fp8_e5m2", None)


def test_mla_ignores_a_compressed_tensors_scheme(resolve):
    config = _config("deepseek-v3")
    config["quantization_config"] = {"quant_method": "compressed-tensors", "config_groups": {},
                                     "kv_cache_scheme": {"num_bits": 8, "type": "float"}}
    res = resolve("deepseek-v3", config)
    assert kv_widths.settle(res.shape, res, "vllm", None) == ("auto", None)


def test_a_model_with_no_per_token_cache_refuses_every_width(resolve):
    res = resolve("mamba-2.8b")
    assert "no per-token cache" in kv_widths.settle(res.shape, res, "vllm", "fp8")[1]


@pytest.mark.parametrize("extra", ["--kv-cache-dtype-skip-layers sliding_window", "--dtype float32", "--dtyp float32", "--kv-cache-dtype-s 0", "-q fp8", "--quantization=fp8"])
def test_extra_args_cannot_change_what_the_cache_is_priced_at(extra):
    with pytest.raises(ValueError):
        kv_widths.refuse_kv_arg("extra_args", tuple(extra.split()))


def test_a_custom_command_cannot_carry_a_kv_width_either():
    with pytest.raises(ValueError, match="custom_command"):
        kv_widths.refuse_kv_arg("custom_command", ("vllm", "serve", "--kv-cache-dtype", "bfloat16"))


def test_no_mla_backend_stores_fp8_e5m2(resolve):
    config = _config("deepseek-v3")
    config.pop("quantization_config", None)
    res = resolve("deepseek-v3", config)
    assert "MLA" in kv_widths.settle(res.shape, res, "vllm", "fp8_e5m2")[1]


@pytest.mark.parametrize("field, tokens, says", [
    ("extra_args", ("-q", "fp8"), "as dtype"),
    ("extra_args", ("-qfp8",), "as dtype"),
    ("extra_args", ("--quant", "fp8"), "as dtype"),
    ("extra_args", ("--kv-cache-dtype", "x"), "as kv_dtype"),
    ("custom_command", ("--kv-cache-dtype", "x"), "drop the custom command"),
])
def test_the_refusal_names_the_field_that_works(field, tokens, says):
    with pytest.raises(ValueError, match=says):
        kv_widths.refuse_kv_arg(field, tokens)
