"""The quantization table. Owned by Agent C.

Real bytes per parameter, including block scales and zero points -- not nominal
bit width. Nominal bit width understates footprint and is the systematic error
in every napkin calculator: Q4_K_M costs 4.90 bits per weight once its
super-block scales and mins are counted, not 4.00, and MXFP4 costs 4.25 because
every 32 elements carry an E8M0 scale.

Everything downstream keys off ``BYTES_PER_PARAM``. ``ModelShape.dtype`` is
always one of its keys.

The day-0 placeholder that stood here carried q5_k_m at 0.6875 and q3_k_m at
0.4921875, which are the pure-K-block figures. The _M mixes name a per-tensor
mix that lands higher and lower respectively; the values below are the ones
frozen in the architecture doc and the Agent C brief, and they supersede it.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Bytes of storage per model parameter, scales and zero points included.
BYTES_PER_PARAM: dict[str, float] = {
    "fp32": 4.0,
    "fp16": 2.0,
    "bf16": 2.0,
    "fp8": 1.0,
    "int8": 1.0,
    "q8_0": 1.0625,  # 8.5 bpw
    "q6_k": 0.82,  # 6.56 bpw
    "q5_k_m": 0.711,  # 5.69 bpw
    "q4_k_m": 0.6125,  # 4.90 bpw, not 4.0
    "q4_0": 0.5625,  # 4.5 bpw
    "q3_k_m": 0.456,  # 3.65 bpw
    "q2_k": 0.329,  # 2.63 bpw
    "mxfp4": 0.53125,  # 4.25 bpw, E2M1 plus E8M0 scale per 32 elements
    "nvfp4": 0.5625,  # 4.5 bpw, E2M1 plus FP8 scale per 16, Blackwell native
    "awq_int4": 0.5625,
    "gptq_int4": 0.5625,
    "nf4": 0.5163,
}

#: What we fall back to when quantization cannot be determined. Never guess low:
#: an optimistic default here becomes an out-of-memory kill downstream.
DEFAULT_DTYPE = "bf16"

#: Spellings seen in configs, file names and GGUF headers, mapped onto the
#: canonical keys above. Lookup is case-insensitive and ignores separators.
_ALIASES: dict[str, str] = {
    "float32": "fp32",
    "f32": "fp32",
    "torch.float32": "fp32",
    "float16": "fp16",
    "f16": "fp16",
    "half": "fp16",
    "torch.float16": "fp16",
    "bfloat16": "bf16",
    "torch.bfloat16": "bf16",
    "fp8e4m3": "fp8",
    "fp8e5m2": "fp8",
    "f8e4m3": "fp8",
    "f8e4m3fn": "fp8",
    "e4m3": "fp8",
    "e5m2": "fp8",
    "float8": "fp8",
    "fp8w8a8": "fp8",
    "w8a8": "int8",
    "i8": "int8",
    "q80": "q8_0",
    "q8k": "q8_0",
    "q6k": "q6_k",
    "q5km": "q5_k_m",
    "q5k": "q5_k_m",
    "q5ks": "q5_k_m",
    "q5kl": "q5_k_m",
    "q4km": "q4_k_m",
    "q4k": "q4_k_m",
    "q4ks": "q4_k_m",
    "q4kl": "q4_k_m",
    "q40": "q4_0",
    "q41": "q4_0",
    "q3km": "q3_k_m",
    "q3k": "q3_k_m",
    "q3ks": "q3_k_m",
    "q3kl": "q3_k_m",
    "q2k": "q2_k",
    "mxfp4moe": "mxfp4",
    # A bare "fp4" tag names no packer; NVFP4 (4.5 bpw) is the more
    # plausible read on this hardware than MXFP4 (4.25 bpw), so it wins
    # the ambiguous alias.
    "fp4": "nvfp4",
    "nvfp4a16": "nvfp4",
    "awq": "awq_int4",
    "awqint4": "awq_int4",
    "awqmarlin": "awq_int4",
    "gptq": "gptq_int4",
    "gptqint4": "gptq_int4",
    "gptqmarlin": "gptq_int4",
    "w4a16": "gptq_int4",
    "bnbnf4": "nf4",
}


@dataclass(frozen=True)
class QuantInfo:
    """What a quantization scheme costs and what silicon it needs."""

    key: str
    bits_per_weight: float  # effective, scales included
    family: str  # "float" | "int" | "gguf" | "block-float"
    #: Minimum CUDA compute capability for a *native* kernel. Below this the
    #: scheme either runs emulated (slower, and often wider in memory) or not
    #: at all.
    native_compute_capability: float | None
    #: True when the scheme still runs below ``native_compute_capability``.
    emulated_below_native: bool
    note: str = ""


QUANT_INFO: dict[str, QuantInfo] = {
    "fp32": QuantInfo("fp32", 32.0, "float", None, False),
    "fp16": QuantInfo("fp16", 16.0, "float", None, False),
    "bf16": QuantInfo("bf16", 16.0, "float", 8.0, True, "bf16 tensor cores from Ampere"),
    "fp8": QuantInfo("fp8", 8.0, "float", 8.9, True, "native FP8 from Ada and Hopper"),
    "int8": QuantInfo("int8", 8.0, "int", 7.5, True),
    "q8_0": QuantInfo("q8_0", 8.5, "gguf", None, False, "llama.cpp block format"),
    "q6_k": QuantInfo("q6_k", 6.56, "gguf", None, False, "llama.cpp block format"),
    "q5_k_m": QuantInfo("q5_k_m", 5.69, "gguf", None, False, "llama.cpp block format"),
    "q4_k_m": QuantInfo("q4_k_m", 4.90, "gguf", None, False, "llama.cpp block format"),
    "q4_0": QuantInfo("q4_0", 4.5, "gguf", None, False, "llama.cpp block format"),
    "q3_k_m": QuantInfo("q3_k_m", 3.65, "gguf", None, False, "llama.cpp block format"),
    "q2_k": QuantInfo("q2_k", 2.63, "gguf", None, False, "llama.cpp block format"),
    "mxfp4": QuantInfo(
        "mxfp4", 4.25, "block-float", 10.0, True,
        "native MXFP4 tensor cores on Blackwell; Hopper and Ada dequantize in kernel, "
        "and pre-Hopper runtimes upcast the weights to bf16, which quadruples them",
    ),
    "nvfp4": QuantInfo(
        "nvfp4", 4.5, "block-float", 10.0, False, "Blackwell only; no pre-Blackwell kernel",
    ),
    "awq_int4": QuantInfo("awq_int4", 4.5, "int", 7.5, False, "Marlin and GEMM kernels from Turing"),
    "gptq_int4": QuantInfo("gptq_int4", 4.5, "int", 7.5, False, "Marlin and GEMM kernels from Turing"),
    "nf4": QuantInfo("nf4", 4.13, "int", 7.5, False, "bitsandbytes"),
}


def normalize_dtype(name: str | None) -> str | None:
    """Map a dtype spelling onto a ``BYTES_PER_PARAM`` key, or ``None``.

    Returns ``None`` rather than a default so callers can decide whether an
    unknown value deserves a warning. Never silently picks a smaller type.
    """
    if not name:
        return None
    raw = str(name).strip().lower()
    if raw in BYTES_PER_PARAM:
        return raw
    if raw in _ALIASES:
        return _ALIASES[raw]
    squashed = "".join(ch for ch in raw if ch.isalnum())
    if squashed in BYTES_PER_PARAM:
        return squashed
    return _ALIASES.get(squashed)


def is_known_dtype(dtype: str | None) -> bool:
    """Whether this dtype can be priced. ``ModelShape.dtype`` must satisfy it."""
    return normalize_dtype(dtype) is not None


def bytes_per_param(dtype: str | None) -> float:
    """Bytes per parameter for a dtype.

    Raises rather than defaulting. A dtype that reached this table without being
    in it is a bug upstream, and pricing it silently at bf16 would under-count a
    32-bit model by half. Deciding what an *undeclared* quantization costs is the
    resolver's job, not the table's: it charges bf16 and says so in a warning.
    """
    key = normalize_dtype(dtype)
    if key is None:
        raise KeyError(
            f"unknown dtype {dtype!r}; expected one of: {', '.join(sorted(BYTES_PER_PARAM))}"
        )
    return BYTES_PER_PARAM[key]


def bytes_per_param_or_default(dtype: str | None) -> float:
    """As ``bytes_per_param``, but bf16 for anything unrecognised.

    For the paths that have to produce a number rather than an exception. Never
    guesses low among the common formats.
    """
    key = normalize_dtype(dtype)
    return BYTES_PER_PARAM[key or DEFAULT_DTYPE]


def weight_bytes(params: int, dtype: str | None) -> int:
    """Storage for ``params`` parameters at ``dtype``."""
    return int(params * bytes_per_param_or_default(dtype))


def quant_info(dtype: str | None) -> QuantInfo:
    """Scheme metadata, defaulting to bf16 when unknown."""
    return QUANT_INFO[normalize_dtype(dtype) or DEFAULT_DTYPE]


def is_quantized(dtype: str | None) -> bool:
    """True for anything narrower than a 16-bit float."""
    return quant_info(dtype).bits_per_weight < 16.0
