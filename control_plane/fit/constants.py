"""Tuning constants for the fit calculator.

These live in ``contracts/constants.py`` when day 0 has landed them; the local
values are the fit calculator's own defaults and keep this module importable
against a partially written contracts package. Contracts always win.
"""

from __future__ import annotations

from control_plane.contracts import constants as _k

# Chunked-prefill scratch width, in tokens.
ACTIVATION_CHUNK_TOKENS: int = getattr(_k, "ACTIVATION_CHUNK_TOKENS", 2048)

# Real runtimes land near half the pure memory-bandwidth ceiling.
#
# Kept at 0.55, and that is a decision rather than an oversight. Seven models
# measured on a GB10 through `tests/decode_sweep.py` imply efficiencies from
# 0.560 to 0.738 -- a 28% spread -- so no single constant describes them, and
# the best one available (0.65) is still 16% wrong for somebody. This is the
# CONSERVATIVE end: it is at or below every efficiency observed, so the figure
# it produces never over-promises, which is the direction that matters for a
# number `DEGRADED_TPS_THRESHOLD` judges and `gateway/strength.py` routes on.
DECODE_EFFICIENCY: float = getattr(_k, "DECODE_EFFICIENCY", 0.55)

# ...and the other end of the same range: the best any real runtime managed.
#
# The measured corpus, per `measurements.DecodeRecord`:
#
#     LiquidAI/LFM2.5-350M       214.9 tok/s   0.560
#     Qwen/Qwen2.5-0.5B-Instruct 159.0         0.577
#     Qwen/Qwen3-0.6B            123.2         0.692
#     Qwen/Qwen3-4B-AWQ           71.2         0.705
#     Qwen/Qwen3-1.7B             49.2         0.738
#     microsoft/Phi-3.5-mini      22.9         0.649
#     Qwen/Qwen3-4B               21.7         0.642
#
# 0.75 is the highest of those rounded up -- the best observed, deliberately
# NOT their average. Paired with an empty cache it makes the optimistic end of
# the reported range an actual upper bound: every one of those seven measured
# BELOW it, where at 0.70 Qwen3-1.7B escapes above. A range whose top end the
# hardware beats is not a range.
#
# Why a second constant rather than a better single one: the spread is real
# and per-model, so the only honest single answer is a wide one. The range
# carries that uncertainty instead of hiding it in an average, and where a
# measurement exists `measurements.matching_decode` cites it and neither end
# has to be guessed at all.
#
# No MoE checkpoint is in that corpus -- nothing large enough fit on the box
# while it was taken -- so the bound is claimed for dense and quantized
# checkpoints and is untested for a model that reads a fraction of its weights.
DECODE_EFFICIENCY_BEST: float = getattr(_k, "DECODE_EFFICIENCY_BEST", 0.75)

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
