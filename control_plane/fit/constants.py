"""Tuning constants for the fit calculator.

These live in ``contracts/constants.py`` when day 0 has landed them; the local
values are the defaults from the Agent D brief and keep this module importable
against a partially written contracts package. Contracts always win.
"""

from __future__ import annotations

from control_plane.contracts import constants as _k

# Chunked-prefill scratch width, in tokens.
ACTIVATION_CHUNK_TOKENS: int = getattr(_k, "ACTIVATION_CHUNK_TOKENS", 2048)

# Real runtimes land near half the pure memory-bandwidth ceiling.
DECODE_EFFICIENCY: float = getattr(_k, "DECODE_EFFICIENCY", 0.55)

# Context suggestions are reported on this grain. Page sizes are powers of two
# and a suggestion of 18944 is easier to act on than 18991.
CONTEXT_ROUNDING: int = getattr(_k, "CONTEXT_ROUNDING", 512)

# Ceiling for the max-context search. Nothing serves past this today, and an
# unbounded search on a sliding-window model would not terminate usefully.
MAX_CONTEXT_SEARCH: int = 1 << 21  # 2,097,152 tokens

# Ceiling for min_nodes_required. Past this the answer is "buy a different
# machine", not "add another Spark".
MAX_SEARCH_NODES: int = 64

# Bytes per KV cache element. Distinct from BYTES_PER_PARAM: sub-byte weight
# quantization schemes do not apply to the cache, runtimes cache in fp8 at the
# smallest.
KV_ELEM_BYTES: dict[str, float] = {
    "fp32": 4.0,
    "float32": 4.0,
    "fp16": 2.0,
    "float16": 2.0,
    "half": 2.0,
    "bf16": 2.0,
    "bfloat16": 2.0,
    "fp8": 1.0,
    "fp8_e4m3": 1.0,
    "fp8_e5m2": 1.0,
    "float8": 1.0,
    "int8": 1.0,
    "uint8": 1.0,
}

# Fallback when the caller names a cache dtype we do not know. Charging two
# bytes over-estimates; the alternative is a gate that lets an OOM through.
KV_FALLBACK_DTYPE = "bf16"

# Quantizations we are willing to suggest, best quality first.
QUANT_SUGGESTION_ORDER: tuple[str, ...] = (
    "fp32",
    "bf16",
    "fp16",
    "q8_0",
    "fp8",
    "q6_k",
    "q5_k_m",
    "q4_k_m",
    "nvfp4",
    "mxfp4",
    "q3_k_m",
    "q2_k",
)
