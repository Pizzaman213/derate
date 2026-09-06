"""Cross-package KV-math agreement (P-GW-A audit fix).

Three places charge KV cache bytes for a multi-head-latent-attention (MLA)
shape: the fit calculator's own module, the planner's fallback bridge (used
before Agent D's real calculator is wired), and the gateway's admission
fallback (used when the fit port exposes no ``kv_bytes_per_token``
estimator). All three must charge the same true cached width per layer per
token -- ``mla_latent_dim + effective_mla_rope_dim`` -- never the latent
alone, which under-counts a DeepSeek-family cache by about 11 percent in the
OOM direction (M-10 / the audit's central KV-math finding).

This is the regression test for that agreement: given the frozen
DeepSeek-V3 fixture (``mla_latent_dim=512``, ``mla_rope_dim=64``), all three
sites must be traceable back to the same ``(512 + 64) * elem`` figure. Two of
the three report a *total-across-all-layers* rate; one reports a *per-layer*
rate. That unit difference is the one legitimate divergence and is asserted
explicitly below rather than papered over.
"""

from __future__ import annotations

from control_plane.fit import kv as fit_kv
from control_plane.gateway.admission import kv_bytes_per_token_fallback
from control_plane.planner import fit_bridge

from tests.fixtures import DEEPSEEK_V3

KV_DTYPE = "bf16"  # bytes-per-element = 2.0 in every one of the three tables


def test_deepseek_kv_math_agrees_across_fit_kv_fit_bridge_and_admission():
    shape = DEEPSEEK_V3
    elem = 2.0  # bf16
    expected_per_layer = (shape.mla_latent_dim + shape.effective_mla_rope_dim) * elem
    assert expected_per_layer == (512 + 64) * elem

    # Site 1: control_plane.fit.kv, per LAYER per token. This is the one
    # function of the three that reports a per-layer rate rather than a
    # whole-model rate.
    per_layer = fit_kv.per_layer_per_token_bytes(shape, KV_DTYPE)
    assert per_layer == expected_per_layer

    # fit.kv's own whole-model total (num_layers * the per-layer rate above)
    # is the reference the other two total-across-layers sites are compared
    # against.
    fit_kv_total = fit_kv.kv_bytes_per_token(shape, KV_DTYPE)
    assert fit_kv_total == shape.num_layers * expected_per_layer

    # Site 2: planner.fit_bridge's fallback (used until Agent D's real
    # calculator is wired). Whole-model total, same formula.
    bridge_total = fit_bridge.kv_bytes_per_token(shape, KV_DTYPE)
    assert bridge_total == shape.num_layers * expected_per_layer

    # Site 3: gateway.admission's fallback (used when the fit port exposes
    # no kv_bytes_per_token estimator of its own). Whole-model total, same
    # formula.
    admission_total = kv_bytes_per_token_fallback(shape, KV_DTYPE)
    assert admission_total == shape.num_layers * expected_per_layer

    # The legitimate divergence: fit.kv.per_layer_per_token_bytes reports a
    # PER-LAYER rate; fit_bridge and admission both report the TOTAL across
    # all layers. Related by exactly num_layers, not a coincidence of
    # rounding -- and once that unit difference is accounted for, all three
    # sites charge the identical figure.
    assert fit_kv_total == bridge_total == admission_total
    assert admission_total == shape.num_layers * per_layer

    # And the number every one of them is ultimately traceable to is exactly
    # (512 + 64) * elem, per the resolver's NOTES.md gotcha #4.
    assert per_layer == 576.0 * elem
