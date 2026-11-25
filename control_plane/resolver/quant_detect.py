"""Which quantization scheme a repo actually ships.

Order of trust: an explicit caller override, then ``quantization_config`` in
config.json, then a sidecar ``hf_quant_config.json``, then ``torch_dtype``,
then the repo name. When none of them say, the answer is bf16 and a warning --
never a smaller guess, because a low guess turns into an out-of-memory kill
minutes into a load rather than a refusal before it.
"""

from __future__ import annotations

import re
from typing import Any

from control_plane.contracts.quant import BYTES_PER_PARAM, DEFAULT_DTYPE, normalize_dtype

from .types import QuantSource

#: Patterns in a repo or file name, most specific first. Last resort only.
_NAME_PATTERNS: tuple[tuple[str, str], ...] = (
    # Importance-matrix formats first: "IQ4_XS" contains no substring the
    # K-quant patterns below match, but "IQ4_NL" and friends are close enough
    # to the plain families that ordering them after would risk a looser rule
    # claiming them as the family grows.
    (r"\biq1[._-]?s\b", "iq1_s"),
    (r"\biq1[._-]?m\b", "iq1_m"),
    (r"\biq2[._-]?xxs\b", "iq2_xxs"),
    (r"\biq2[._-]?xs\b", "iq2_xs"),
    (r"\biq2[._-]?s\b", "iq2_s"),
    (r"\biq2[._-]?m\b", "iq2_m"),
    (r"\biq3[._-]?xxs\b", "iq3_xxs"),
    (r"\biq3[._-]?xs\b", "iq3_xs"),
    (r"\biq3[._-]?s\b", "iq3_s"),
    (r"\biq3[._-]?m\b", "iq3_m"),
    (r"\biq4[._-]?xs\b", "iq4_xs"),
    (r"\biq4[._-]?nl\b", "iq4_nl"),
    # K-quant _XL sizes. The [sml] class below cannot match "XL", so without
    # these an Unsloth Dynamic file falls through to the bf16 default and is
    # charged four times what it costs. Priced at the base scheme's _M rung --
    # see contracts/quant.py's _XL aliases for why not a rung higher.
    (r"\bq2[._-]?k[._-]?xl\b", "q2_k"),
    (r"\bq3[._-]?k[._-]?xl\b", "q3_k_m"),
    (r"\bq4[._-]?k[._-]?xl\b", "q4_k_m"),
    (r"\bq5[._-]?k[._-]?xl\b", "q5_k_m"),
    (r"\bq6[._-]?k[._-]?xl\b", "q6_k"),
    (r"\bq8[._-]?k[._-]?xl\b", "q8_0"),
    # Q2_K_S and Q2_K_L had no pattern at all: there is a q3_k_[sml] rule but
    # never a q2_k_[sml] one, and the bare rule below cannot reach them
    # because "_" is a word character, so \b never fires between "k" and "_".
    (r"\bq2[._-]?k[._-]?s\b", "q2_k_s"),
    (r"\bq2[._-]?k[._-]?l\b", "q2_k"),
    # The _L siblings of the two schemes that had no [sml] rule, for the same
    # reason and with the same fix: bartowski ships "Q6_K_L" and "Q8_0_L",
    # which are the base scheme with the token-embedding and output tensors
    # left at F16. Priced at the base scheme -- the file is marginally larger,
    # and the alternative is the bf16 default, which over-charges Q6_K by 2.5x.
    # Below the _XL block above, so "Q6_K_XL" still matches its own rule.
    (r"\bq6[._-]?k[._-]?l\b", "q6_k"),
    (r"\bq8[._-]?0[._-]?l\b", "q8_0"),
    (r"\bq2[._-]?k\b", "q2_k"),
    (r"\bq3[._-]?k[._-]?[sml]\b", "q3_k_m"),
    (r"\bq3[._-]?k\b", "q3_k_m"),
    (r"\bq4[._-]?k[._-]?[sml]\b", "q4_k_m"),
    (r"\bq4[._-]?k\b", "q4_k_m"),
    # llama.cpp's ARM repackings: Q4_0_4_4, Q4_0_4_8, Q4_0_8_8. Not distinct
    # quantizations -- the same Q4_0 blocks interleaved for i8mm/SVE dot
    # products, so the same 4.5 bits per weight and the same bytes on disk.
    # Above the bare q4_0 rule because that one cannot reach them: "_" is a
    # word character, so \b never fires after the "0" in "q4_0_4_4". Without
    # this they fall through to the bf16 default at 2.0 bytes per parameter
    # against a real 0.5625 -- a 3.6x over-charge -- and each one costs a
    # multi-second ranged read of a GGUF header that cannot answer either,
    # because LLAMA_FTYPE 33/34/35 are absent from GGUF_FILE_TYPES.
    (r"\bq4[._-]?0[._-]?[48][._-]?[48]\b", "q4_0"),
    (r"\bq4[._-]?0\b", "q4_0"),
    (r"\bq4[._-]?1\b", "q4_1"),
    (r"\bq5[._-]?0\b", "q5_0"),
    (r"\bq5[._-]?1\b", "q5_1"),
    (r"\bq5[._-]?k[._-]?[sml]\b", "q5_k_m"),
    (r"\bq5[._-]?k\b", "q5_k_m"),
    (r"\bq6[._-]?k\b", "q6_k"),
    (r"\bq8[._-]?0\b", "q8_0"),
    (r"\bnvfp4\b", "nvfp4"),
    (r"\bmxfp4\b", "mxfp4"),
    (r"\bfp8\b|\bw8a8[._-]?fp8\b|\be4m3\b", "fp8"),
    (r"\bawq\b", "awq_int4"),
    (r"\bgptq\b", "gptq_int4"),
    (r"\bnf4\b|\bbnb[._-]?4bit\b", "nf4"),
    # A bare "4bit" says nothing about the packer, so it is charged at the
    # AWQ/GPTQ figure rather than at nf4, which is the cheapest of them.
    (r"\bint4\b|\bw4a16\b|\b4[._-]?bit\b", "gptq_int4"),
    (r"\bint8\b|\bw8a8\b|\b8bit\b", "int8"),
    (r"\bfp16\b|\bf16\b", "fp16"),
    (r"\bbf16\b", "bf16"),
)


class UnknownQuantization(ValueError):
    """A caller asked for a dtype that is not in the table."""


def normalize_override(dtype: str) -> str:
    key = normalize_dtype(dtype)
    if key is None:
        raise UnknownQuantization(
            f"unknown dtype {dtype!r}; expected one of: {', '.join(sorted(BYTES_PER_PARAM))}"
        )
    return key


def from_name(name: str) -> str | None:
    """Guess a scheme from a repo or file name."""
    lowered = name.lower().replace("/", " ")
    for pattern, key in _NAME_PATTERNS:
        if re.search(pattern, lowered):
            return key
    return None


def _bits_to_int_key(bits: int | None) -> str | None:
    if bits == 8:
        return "int8"
    if bits == 4:
        return "gptq_int4"
    return None


def _from_compressed_tensors(qc: dict[str, Any]) -> tuple[str | None, str]:
    """compressed-tensors describes the scheme in config_groups."""
    groups = qc.get("config_groups")
    if not isinstance(groups, dict):
        return None, ""
    for group in groups.values():
        if not isinstance(group, dict):
            continue
        weights = group.get("weights")
        if not isinstance(weights, dict):
            continue
        bits = weights.get("num_bits")
        kind = str(weights.get("type") or "").lower()
        group_size = weights.get("group_size")
        if bits == 8:
            return ("fp8" if kind.startswith("float") else "int8"), f"{bits}-bit {kind}"
        if bits == 4:
            if kind.startswith("float"):
                return ("nvfp4" if group_size == 16 else "mxfp4"), f"4-bit {kind}"
            return "gptq_int4", f"4-bit {kind}"
    return None, ""


def _from_quant_config(qc: dict[str, Any], warnings: list[str]) -> str | None:
    """Read config.json's ``quantization_config``."""
    method = str(qc.get("quant_method") or qc.get("quant_algo") or "").lower().strip()
    bits = qc.get("bits") if isinstance(qc.get("bits"), int) else qc.get("w_bit")
    bits = bits if isinstance(bits, int) else None

    if method in ("mxfp4", "mxfp4_moe"):
        return "mxfp4"
    if method in ("nvfp4", "modelopt_fp4"):
        return "nvfp4"
    if method in ("fp8", "fbgemm_fp8", "fp8_e4m3", "finegrained_fp8"):
        return "fp8"
    if method == "awq":
        if bits in (None, 4):
            return "awq_int4"
        key = _bits_to_int_key(bits)
        if key:
            return key
        warnings.append(f"AWQ config declares {bits}-bit weights, which is not in the table")
        return None
    if method in ("gptq", "gptq_marlin", "exllama"):
        if bits in (None, 4):
            return "gptq_int4"
        key = _bits_to_int_key(bits)
        if key:
            return key
        warnings.append(f"GPTQ config declares {bits}-bit weights, which is not in the table")
        return None
    if method == "compressed-tensors":
        key, detail = _from_compressed_tensors(qc)
        if key:
            return key
        warnings.append(f"compressed-tensors config was unreadable{': ' + detail if detail else ''}")
        return None
    if method == "bitsandbytes":
        if qc.get("load_in_4bit"):
            quant_type = str(qc.get("bnb_4bit_quant_type") or "nf4").lower()
            if quant_type not in ("nf4", "fp4"):
                warnings.append(f"bitsandbytes 4-bit type {quant_type!r} charged as nf4")
            return "nf4"
        if qc.get("load_in_8bit"):
            return "int8"
        return None
    if method in ("modelopt", "modelopt_fp8"):
        algo = str(qc.get("quant_algo") or qc.get("algorithm") or "").lower()
        return normalize_dtype(algo)
    if method in ("torchao", "quark", "hqq", "aqlm", "quip", "eetq", "marlin"):
        key = normalize_dtype(str(qc.get("quant_algo") or qc.get("dtype") or ""))
        if key:
            return key
        if bits:
            key = _bits_to_int_key(bits)
            if key:
                return key
        warnings.append(
            f"quantization method {method!r} is not one this table sizes; "
            "charging bf16, which over-states the footprint"
        )
        return None
    if method:
        warnings.append(f"unrecognised quant_method {method!r} in config.json")
    return None


def _from_hf_quant_config(data: dict[str, Any]) -> str | None:
    """Read the ModelOpt sidecar ``hf_quant_config.json``."""
    quant = data.get("quantization")
    if not isinstance(quant, dict):
        return None
    algo = str(quant.get("quant_algo") or "").lower()
    if not algo:
        return None
    if "nvfp4" in algo:
        return "nvfp4"
    if "mxfp4" in algo:
        return "mxfp4"
    if "fp8" in algo:
        return "fp8"
    if "awq" in algo:
        return "awq_int4"
    if "int4" in algo or "w4a16" in algo:
        return "gptq_int4"
    if "int8" in algo or "w8a8" in algo:
        return "int8"
    return normalize_dtype(algo)


def detect(
    config: dict[str, Any],
    model_id: str,
    *,
    override: str | None = None,
    hf_quant_config: dict[str, Any] | None = None,
) -> tuple[str, QuantSource, list[str]]:
    """Return ``(dtype key, where it came from, warnings)``."""
    warnings: list[str] = []

    if override:
        return normalize_override(override), QuantSource.OVERRIDE, warnings

    qc = config.get("quantization_config")
    if isinstance(qc, dict) and qc:
        key = _from_quant_config(qc, warnings)
        if key:
            return key, QuantSource.QUANT_CONFIG, warnings

    if hf_quant_config:
        key = _from_hf_quant_config(hf_quant_config)
        if key:
            return key, QuantSource.HF_QUANT_CONFIG, warnings

    key = normalize_dtype(_raw_dtype(config))
    if key:
        return key, QuantSource.TORCH_DTYPE, warnings

    key = from_name(model_id)
    if key:
        warnings.append(
            f"quantization was not declared in config.json; inferred {key} from the "
            "repo name, which is the least reliable source we have"
        )
        return key, QuantSource.REPO_NAME, warnings

    warnings.append(
        "quantization could not be determined from config, sidecar or repo name; "
        f"charging {DEFAULT_DTYPE} at {BYTES_PER_PARAM[DEFAULT_DTYPE]} bytes per "
        "parameter, which is the largest plausible footprint"
    )
    return DEFAULT_DTYPE, QuantSource.DEFAULTED, warnings


#: ModelOpt's FP8 spellings, which vLLM routes to its `modelopt` method.
_MODELOPT_FP8_ALGOS = frozenset({"FP8", "FP8_PER_CHANNEL_PER_TOKEN", "FP8_PB_WO"})


def vllm_quant_method(
    config: dict[str, Any], hf_quant_config: dict[str, Any] | None = None
) -> str:
    """The name vLLM gives this checkpoint's packing, or "" when it has none.

    Not the dtype key above, which prices bytes: this names the LOADER, and
    the loader is what has a minimum compute capability. They part ways
    exactly where it matters -- an NVFP4 checkpoint is `modelopt_fp4` (vLLM's
    floor sm_75) from ModelOpt and `compressed-tensors` (sm_70) from
    llm-compressor, and a ModelOpt FP8 one is `modelopt` (sm_80) where a
    plain `fp8` one is sm_75.

    Mirrors what vLLM does with the same files (read off 0.28.1's
    `transformers_utils/config.py`, `model_arch_config_convertor.py` and
    `ModelConfig._verify_quantization`): `quantization_config` (or
    `compression_config`), else the ModelOpt `hf_quant_config.json` sidecar;
    `compressed_tensors` spelled with a hyphen; and a ModelOpt config named
    by its `quant_algo`, the way the `modelopt*` overrides pick. The other
    overrides (`gptq` -> `gptq_marlin`, `awq` -> `awq_marlin`) are not
    followed because both names carry the same floor.
    """
    qc = _quant_config(config, hf_quant_config)
    if not qc:
        return ""
    method = str(qc.get("quant_method") or "").strip().lower()
    method = method.replace("compressed_tensors", "compressed-tensors")
    producer = qc.get("producer")
    inner = qc.get("quantization")
    modelopt = method.startswith("modelopt") or (
        isinstance(producer, dict) and producer.get("name") == "modelopt"
    ) or (isinstance(inner, dict) and "modelopt_quant_config" in inner)
    if modelopt:
        raw = inner.get("quant_algo") if isinstance(inner, dict) else qc.get("quant_algo")
        algo = str(raw or "").upper()
        if algo in _MODELOPT_FP8_ALGOS:
            return "modelopt"
        if "NVFP4" in algo or "FP4" in algo:
            return "modelopt_fp4"
        if "MXFP8" in algo:
            return "modelopt_mxfp8"
        if algo == "MIXED_PRECISION":
            return "modelopt_mixed"
        return method or "modelopt"
    return method


def _quant_config(
    config: dict[str, Any], hf_quant_config: dict[str, Any] | None
) -> dict[str, Any] | None:
    """The quantization config vLLM reads: `quantization_config` (or
    `compression_config`), else the ModelOpt `hf_quant_config.json` sidecar."""
    qc = config.get("quantization_config") or config.get("compression_config")
    if not isinstance(qc, dict) or not qc:
        qc = hf_quant_config if isinstance(hf_quant_config, dict) else None
    return qc or None


#: ModelOpt's KV algorithm names, as `utils/torch_utils.py::
#: MODELOPT_TO_VLLM_KV_CACHE_DTYPE_MAP` maps them to a `--kv-cache-dtype`.
_MODELOPT_KV: dict[str, str] = {"fp8": "fp8_e4m3", "nvfp4": "nvfp4"}


def checkpoint_kv_dtype(
    config: dict[str, Any], hf_quant_config: dict[str, Any] | None = None
) -> str:
    """The `--kv-cache-dtype` vLLM stores this checkpoint's cache at when it
    is told nothing, because the checkpoint declares one; "" when it does not.

    Mirrors the pinned image (0.28.1rc1): `resolve_kv_cache_dtype_string`
    turns `auto` into a ModelOpt checkpoint's `kv_cache_scheme` /
    `kv_cache_quant_algo` (8-bit float static -> fp8_e4m3, 4-bit float ->
    nvfp4, anything else -> auto), and `Attention.__init__` turns it into
    fp8 for any quant config carrying `kv_cache_scheme`, which only
    compressed-tensors does. The weight scheme says nothing about the cache:
    an fp8-weight DeepSeek-V3 caches at the model's compute width. A scheme
    nested under `text_config` (Qwen3-Omni) is not read: whether vLLM applies
    it depends on which config its quant object is built from, and "" prices
    16 bits -- an over-charge, the safe direction.
    """
    qc = _quant_config(config, hf_quant_config)
    if not qc:
        return ""
    method = vllm_quant_method(config, hf_quant_config)
    if method.startswith("modelopt"):
        inner = qc.get("quantization")
        inner = inner if isinstance(inner, dict) else qc
        algo = (
            inner.get("kv_cache_scheme") or qc.get("kv_cache_scheme")
            or inner.get("kv_cache_quant_algo") or qc.get("kv_cache_quant_algo")
        )
        if isinstance(algo, dict):
            floating = algo.get("type") == "float"
            if floating and algo.get("num_bits") == 8 and algo.get("dynamic") is False:
                algo = "fp8"
            elif floating and algo.get("num_bits") == 4:
                algo = "nvfp4"
        return _MODELOPT_KV.get(algo.lower(), "") if isinstance(algo, str) else ""
    if method == "compressed-tensors" and qc.get("kv_cache_scheme") is not None:
        return "fp8"
    return ""


#: The compute dtypes, from `normalize_dtype`'s keys (the one home of their
#: spellings) onto torch's name, which is how `--kv-cache-dtype` spells them.
#: `gateway/kv_widths.py` tells a 16-bit KV width from the rest with it.
TORCH_NAMES: dict[str, str] = {"bf16": "bfloat16", "fp16": "float16", "fp32": "float32"}


def _raw_dtype(sub: dict[str, Any]) -> str | None:
    """A config's `torch_dtype` (or `dtype`), a per-tower dict read through."""
    raw = sub.get("torch_dtype") or sub.get("dtype")
    if isinstance(raw, dict):  # some composite configs nest per-tower dtypes
        raw = raw.get("") or next(iter(raw.values()), None)
    return raw if isinstance(raw, str) else None


def compute_dtype(config: dict[str, Any]) -> str:
    """The dtype the checkpoint says it computes in, by torch's name: the
    config's own, then its text, vision and encoder sub-configs, as vLLM
    looks; "" when none says. vLLM then reads the weights' own dtype, which a
    config cannot tell, so absent stays unknown rather than becoming float32."""
    for key in (None, "text_config", "vision_config", "encoder_config"):
        sub = config if key is None else config.get(key)
        raw = _raw_dtype(sub) if isinstance(sub, dict) else None
        if raw:
            return TORCH_NAMES.get(normalize_dtype(raw) or "", "")
    return ""


def loader_facts(
    config: dict[str, Any], hf_quant_config: dict[str, Any] | None = None
) -> dict[str, str]:
    """What the engine reads off the checkpoint beyond its weight scheme, as
    `Resolution` keyword arguments: the loader, the compute dtype and the
    cache width it declares."""
    return {
        "quant_method": vllm_quant_method(config, hf_quant_config),
        "compute_dtype": compute_dtype(config),
        "checkpoint_kv_dtype": checkpoint_kv_dtype(config, hf_quant_config),
    }
