"""Fit: the blocking pre-launch out-of-memory gate. Agent D.

    from control_plane.fit import FitCalculator
    result = FitCalculator().check(request, nodes)
    if not result.ok:
        refuse(result.reason)

``FitResult.ok`` covers FITS and FITS_DEGRADED. Degraded is not a failure: the
model loads, it is just slow, and sometimes that is what someone wants.

Agent E calls :func:`kv_bytes_per_token` and :func:`min_nodes_required`.
"""

from .calculator import (
    FitCalculator,
    activation_bytes,
    comm_buffer_bytes,
    memory_breakdown,
    min_nodes_required,
    predict_decode_tps,
    replicated_bytes_per_rank,
    weight_bytes_per_rank,
)
from .kv import (
    cached_layer_tokens,
    kv_bytes_per_token,
    kv_cache_bytes,
    kv_divisor,
    kv_elem_bytes,
    per_layer_per_token_bytes,
    stage_fraction,
)
from .stub import StubFit

__all__ = [
    "FitCalculator",
    "StubFit",
    "activation_bytes",
    "cached_layer_tokens",
    "comm_buffer_bytes",
    "kv_bytes_per_token",
    "kv_cache_bytes",
    "kv_divisor",
    "kv_elem_bytes",
    "memory_breakdown",
    "min_nodes_required",
    "per_layer_per_token_bytes",
    "predict_decode_tps",
    "replicated_bytes_per_rank",
    "stage_fraction",
    "weight_bytes_per_rank",
]
