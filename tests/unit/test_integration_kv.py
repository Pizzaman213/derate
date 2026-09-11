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


# ---------------------------------------------------------------------------
# The fourth site, and the one that was actually wrong: the ENGINE.
#
# The three functions above agree with each other about how many bytes a
# token of KV cache costs at a given width. None of them talks to the
# runtime. The gate takes their answer, multiplies it by the context, and
# passes the product as `--kv-cache-memory-bytes` -- a CAP. It used to pass
# no WIDTH at all, so a request for fp8 halved the cap and the engine filled
# it at full width: the budget honoured, no OOM, and half the approved
# context served with nothing on screen saying so.
# ---------------------------------------------------------------------------


def test_every_width_the_gate_prices_either_reaches_the_engine_or_refuses():
    """The invariant that closes it, stated over `fit.kv`'s own table.

    `render_kv_cache_dtype` returning None means "pass no flag", i.e. the
    model's own dtype -- roughly 2 bytes for every checkpoint derate serves.
    That is a correct answer for the 2-byte names and a silent lie for every
    other one, which is precisely how the defect worked. So: a dtype the
    gate prices at anything other than 2 bytes must produce a flag or an
    error, never silence.
    """
    from control_plane.deploy.flags import render_kv_cache_dtype

    for name, elem in fit_kv.KV_ELEM_BYTES.items():
        try:
            rendered = render_kv_cache_dtype(name)
        except ValueError:
            # Refused. The gate prices it and this build cannot ask a runtime
            # for it -- int8 and uint8 are exactly that -- and a refusal is
            # the honest answer.
            continue
        if elem == 2.0:
            continue  # the model's own width; silence is correct
        assert rendered is not None, (
            "%s is priced at %s bytes/element and renders no flag: the gate "
            "would size the cache at that width and the engine would use the "
            "model's own" % (name, elem)
        )


def test_the_names_that_do_pass_a_flag_keep_the_width_the_gate_charged():
    """A rendered flag has to mean the same number of bytes the gate used,
    or the two agree on a string and disagree on the arithmetic."""
    from control_plane.deploy.flags import render_kv_cache_dtype

    for name in ("fp8", "fp8_e4m3", "fp8_e5m2", "float8"):
        rendered = render_kv_cache_dtype(name)
        assert rendered is not None
        # vLLM's fp8 KV entries are one byte, which is what the gate charged.
        assert fit_kv.KV_ELEM_BYTES[name] == 1.0
        assert rendered.startswith("fp8")


# ==========================================================================
# NCCL tuning selection: one environment, collectives four decades apart
# ==========================================================================


def _rec(tmp, env, *, decode_us, bulk_gbps, src="spark-a", dst="spark-b"):
    from control_plane import measurements as M

    for size, us, bw in (
        (M.DECODE_COLLECTIVE_BYTES, decode_us, 0.3),
        (M.BULK_COLLECTIVE_BYTES, 1.0, bulk_gbps),
    ):
        M.save_nccl(
            M.NcclRecord(
                src=src, dst=dst, nccl_version="2.31.2", image="img",
                size_band=M.band(size), microseconds=us, busbw_gbps=bw,
                env=env, measured_at=1.0,
            ),
            directory=tmp,
        )


def test_a_candidate_that_wins_in_bulk_and_loses_at_decode_is_rejected(tmp_path):
    """The rule, and it is not hypothetical -- it fired on real measurements.

    Numbers from this estate 2026-09-11. `=4` is the BULK WINNER at 18.55 GB/s
    against the default's 8.09 -- and it costs 21.87 us at 8 KB against the
    default's 17.69, which is the regime a tensor-parallel decode step pays
    dozens of times per token. A launch gets ONE environment covering both, so
    the bulk winner is the wrong answer and only this rule says so. `=2` is
    slower in bulk than `=4` and faster than the default at BOTH sizes, which
    is what makes it the one to ship.

    Without the rule the single-shot sweep ships `=4` and every TP decode on
    this cluster gets 24% slower collectives to make prefill faster.
    """
    from control_plane import measurements as M

    _rec(tmp_path, {}, decode_us=17.69, bulk_gbps=8.09)
    _rec(tmp_path, {"NCCL_MAX_NCHANNELS": "2"}, decode_us=17.45, bulk_gbps=17.68)
    _rec(tmp_path, {"NCCL_MAX_NCHANNELS": "4"}, decode_us=21.87, bulk_gbps=18.55)

    assert M.tuning_env("spark-a", "spark-b", directory=tmp_path) == {
        "NCCL_MAX_NCHANNELS": "2"
    }


def test_a_candidate_never_measured_at_the_decode_size_is_not_assumed_harmless(tmp_path):
    """Unmeasured is not "fine". A setting with a huge bulk win and no decode
    row could be wrecking the regime that dominates interactive serving, and
    nothing would know."""
    from control_plane import measurements as M

    _rec(tmp_path, {}, decode_us=17.0, bulk_gbps=7.0)
    M.save_nccl(
        M.NcclRecord(
            src="spark-a", dst="spark-b", nccl_version="2.31.2", image="img",
            size_band=M.band(M.BULK_COLLECTIVE_BYTES),
            microseconds=1.0, busbw_gbps=99.0, env={"NCCL_ALGO": "Tree"},
            measured_at=1.0,
        ),
        directory=tmp_path,
    )
    assert M.tuning_env("spark-a", "spark-b", directory=tmp_path) == {}


def test_no_baseline_means_no_winner(tmp_path):
    """A candidate has to beat something. Crowning the only row measured would
    be reporting a number as an improvement over nothing."""
    from control_plane import measurements as M

    _rec(tmp_path, {"NCCL_MAX_NCHANNELS": "2"}, decode_us=10.0, bulk_gbps=99.0)
    assert M.tuning_env("spark-a", "spark-b", directory=tmp_path) == {}


def test_the_selection_misses_on_a_different_pair_or_image(tmp_path):
    """A value measured between A and B is not a fact about A and C -- NIC
    placement, PCIe slot and which rails are up all differ. Same for a library
    version: it is the thing being measured."""
    from control_plane import measurements as M

    _rec(tmp_path, {}, decode_us=17.0, bulk_gbps=7.0)
    _rec(tmp_path, {"NCCL_MAX_NCHANNELS": "2"}, decode_us=16.0, bulk_gbps=18.0)

    assert M.tuning_env("spark-a", "spark-b", directory=tmp_path)
    assert M.tuning_env("spark-a", "spark-c", directory=tmp_path) == {}
    assert M.tuning_env("spark-a", "spark-b", image="other", directory=tmp_path) == {}
    assert M.tuning_env(
        "spark-a", "spark-b", nccl_version="2.28.9", directory=tmp_path
    ) == {}


def test_the_pair_is_symmetric(tmp_path):
    """A collective has no direction, and storing the pair twice would let the
    two copies disagree."""
    from control_plane import measurements as M

    _rec(tmp_path, {}, decode_us=17.0, bulk_gbps=7.0, src="spark-b", dst="spark-a")
    _rec(tmp_path, {"NCCL_MAX_NCHANNELS": "2"}, decode_us=16.0, bulk_gbps=18.0,
         src="spark-b", dst="spark-a")
    assert M.tuning_env("spark-a", "spark-b", directory=tmp_path) == {
        "NCCL_MAX_NCHANNELS": "2"
    }


# ==========================================================================
# Traffic shape: the only thing that may lift the decode guard
# ==========================================================================


def test_prefill_dominated_traffic_takes_the_bulk_winner(tmp_path):
    """The bump the guard otherwise refuses.

    A deployment whose tokens arrive overwhelmingly as PROMPT spends its
    collectives on the megabyte prefill all-reduce, where `=4` measured
    18.55 GB/s against the default's 8.09. Its 24% decode regression is a
    rounding error in a step time that is mostly prefill -- so for THAT
    traffic, and only with evidence of it, the trade is right.
    """
    from control_plane import measurements as M

    _rec(tmp_path, {}, decode_us=17.69, bulk_gbps=8.09)
    _rec(tmp_path, {"NCCL_MAX_NCHANNELS": "2"}, decode_us=17.45, bulk_gbps=17.68)
    _rec(tmp_path, {"NCCL_MAX_NCHANNELS": "4"}, decode_us=21.87, bulk_gbps=18.55)

    balanced = M.tuning_env("spark-a", "spark-b", directory=tmp_path)
    bulk = M.tuning_env("spark-a", "spark-b", prefer="bulk", directory=tmp_path)
    assert balanced == {"NCCL_MAX_NCHANNELS": "2"}
    assert bulk == {"NCCL_MAX_NCHANNELS": "4"}


def test_the_preference_comes_from_measured_tokens_not_from_configuration(tmp_path):
    """`context_length` is a CAPACITY. A 131k-context deployment may serve
    nothing but short prompts, and tuning it for prefill it never does would be
    the same class of error as any other assumed number. The tokens moved."""
    from control_plane import measurements as M

    assert M.preference_for("never-run", directory=tmp_path) == "balanced"

    M.save_workload(M.WorkloadRecord("chatty", 0.06, 50, 800), directory=tmp_path)
    assert M.preference_for("chatty", directory=tmp_path) == "balanced"

    M.save_workload(M.WorkloadRecord("summariser", 0.99, 8000, 50), directory=tmp_path)
    assert M.preference_for("summariser", directory=tmp_path) == "bulk"


def test_merely_prompt_leaning_traffic_does_not_earn_the_regression(tmp_path):
    """The threshold is 0.8, not 0.5, because the trade is asymmetric and the
    asymmetry is measured: the bulk winner buys ~5% more bandwidth over the
    balanced choice and costs 24% on decode."""
    from control_plane import measurements as M

    M.save_workload(M.WorkloadRecord("mixed", 0.6, 600, 400), directory=tmp_path)
    assert M.preference_for("mixed", directory=tmp_path) == "balanced"


def test_an_idle_window_records_nothing_rather_than_pure_decode(tmp_path):
    """0.0 would read as "all generation" and tune a deployment for a regime it
    was simply not asked to do that round."""
    from control_plane.metrics_scrape import EngineLoad

    assert EngineLoad(prompt_tokens=0.0, generation_tokens=0.0).prefill_share is None
    assert EngineLoad(prompt_tokens=8000.0, generation_tokens=50.0).prefill_share > 0.99
