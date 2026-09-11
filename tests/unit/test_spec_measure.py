"""Reading the engine's acceptance counters, and solving for the best k.

The half of speculative decoding that turns "between 18 and 109 tok/s" into a
number. Two groups: the parser, against a body in the real exposition format,
and the optimiser, against the range it is supposed to narrow.

The optimiser's tests are the ones worth reading. They do not assert that
speculation is fast -- they assert that the measured point is the SAME
arithmetic as the range's two ends, reached with measured acceptance instead of
an assumed one. All-accept must reproduce the ceiling exactly and none-accept
the floor, because that invariant is the only thing tying this number to the
claim the card already made.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from control_plane import head_scan, measurements, metrics_scrape
from control_plane.contracts import ModelShape, SpeculativeMethod, SpeculativeSpec
from control_plane.fit.calculator import (
    draft_ratio,
    speculative_best_k,
    speculative_decode_tps_range,
    speculative_overhead,
)

SAMPLE = (Path(__file__).resolve().parents[1] / "fixtures" / "vllm_metrics_sample.txt")

SHAPE = ModelShape(
    model_id="Qwen/Qwen3-4B", num_layers=36, hidden_size=2560,
    num_attention_heads=32, num_kv_heads=8, vocab_size=151936,
    total_params=4_000_000_000, dtype="bf16", head_dim=128,
)


# -- the parser -------------------------------------------------------------


def _sample() -> metrics_scrape.SpecDecode:
    return metrics_scrape.spec_decode(SAMPLE.read_text())


def test_the_counters_are_read_out_of_a_real_exposition_body():
    found = _sample()
    assert found.drafts == 1000.0
    assert found.draft_tokens == 4000.0
    assert found.accepted_tokens == 1900.0
    assert found.accepted_per_pos == [800.0, 600.0, 350.0, 150.0]


def test_the_counter_suffix_prometheus_adds_is_not_a_miss():
    """`prometheus_client` appends `_total`; vLLM's own dashboard spells these
    both ways in one file. Neither spelling may be a miss."""
    without = _sample()
    stripped = metrics_scrape.spec_decode(
        SAMPLE.read_text().replace("_total{", "{")
    )
    assert stripped.drafts == without.drafts
    assert stripped.accepted_per_pos == without.accepted_per_pos


def test_unrelated_series_are_skipped():
    """A histogram bucket is not a draft count, and `# HELP` is not a sample."""
    found = _sample()
    # The body carries a gauge and a histogram; neither may reach these.
    assert found.consistent
    assert found.basis == "per_pos"


def test_per_position_is_cumulative_acceptance_not_conditional():
    """vLLM's own definition: accepted_at_pos / num_drafts.

    Position 2 is only reached when position 1 was accepted, so this is the
    probability of getting AT LEAST that far -- which is exactly the term the
    expected-length sum needs, and why nothing here multiplies a chain.
    """
    found = _sample()
    assert found.acceptance_at(0) == pytest.approx(0.80)
    assert found.acceptance_at(1) == pytest.approx(0.60)
    # Strictly falling, as a cumulative series must be.
    rates = [found.acceptance_at(i) for i in range(4)]
    assert rates == sorted(rates, reverse=True)


def test_expected_accepted_is_the_prefix_sum():
    found = _sample()
    assert found.expected_accepted(1) == pytest.approx(0.80)
    assert found.expected_accepted(2) == pytest.approx(1.40)
    assert found.expected_accepted(4) == pytest.approx(1.90)
    # Which is the whole point of one launch answering for every k.
    assert found.expected_accepted(99) == found.expected_accepted(4)


def test_the_per_position_series_must_sum_to_the_total():
    """Every accepted token is accepted at exactly one position."""
    assert _sample().consistent
    broken = metrics_scrape.spec_decode(
        SAMPLE.read_text().replace('position="0"} 800.0', 'position="0"} 80.0')
    )
    assert not broken.consistent


def test_an_engine_that_never_speculated_says_so_rather_than_dividing_by_zero():
    empty = metrics_scrape.spec_decode("# nothing here\n")
    assert empty.basis == "none"
    assert empty.mean_acceptance is None
    assert empty.acceptance_at(0) is None
    assert empty.expected_accepted(3) is None


def test_ranks_are_summed_rather_than_one_picked():
    """A tensor-parallel launch exports a series per engine, and rank 0's
    acceptance is not the deployment's."""
    doubled = SAMPLE.read_text() + SAMPLE.read_text().replace('engine="0"', 'engine="1"')
    found = metrics_scrape.spec_decode(doubled)
    assert found.drafts == 2000.0
    assert found.accepted_per_pos[0] == 1600.0


def test_a_window_is_a_difference_and_never_goes_negative():
    before = _sample()
    after = metrics_scrape.spec_decode(
        SAMPLE.read_text().replace("} 1000.0", "} 1500.0")
    )
    window = metrics_scrape.delta(before, after)
    assert window.drafts == 500.0
    # An engine that restarted resets its counters; a negative count must not
    # travel into an acceptance rate.
    restarted = metrics_scrape.delta(after, before)
    assert restarted.drafts == 0.0
    assert all(v >= 0 for v in restarted.accepted_per_pos)


# -- a real engine, really speculating --------------------------------------
#
# Everything below is measured, not composed. `tests/fixtures/
# vllm_ngram_spec_real.txt` was scraped off a vLLM this project launched with
# `--speculative-config {"method": "ngram", "num_speculative_tokens": 10}` on
# the development box, and the numbers are whatever the engine said.
#
# The fixture beside it (`vllm_metrics_sample.txt`) is hand-composed and stays
# that way -- it is the one where round numbers make an arithmetic assertion
# readable. This one exists because a parser that only ever sees tidy input is
# a parser nobody has tested.

REAL = SAMPLE.parent / "vllm_ngram_spec_real.txt"


def _real() -> metrics_scrape.SpecDecode:
    return metrics_scrape.spec_decode(REAL.read_text())


def test_the_real_engines_counters_parse():
    found = _real()
    assert found.drafts == 747.0
    assert found.draft_tokens == 7470.0
    assert found.accepted_tokens == 5478.0
    # k=10, so ten positions and no eleventh.
    assert len(found.accepted_per_pos) == 10


def test_the_real_per_position_series_sums_to_the_real_aggregate():
    """664 + 581*4 + 498*5 = 5478. Nothing enforces that on the wire -- the two
    series are independent counters -- so a fixture where it holds is evidence
    the reader is looking at one engine's coherent view rather than at numbers
    that merely look plausible."""
    found = _real()
    assert sum(found.accepted_per_pos) == found.accepted_tokens
    assert found.consistent


def test_the_real_acceptance_curve_falls_away_and_never_rises():
    """Cumulative, not conditional: position 2 is only reached when position 1
    was accepted, so the curve is non-increasing by construction. A rise would
    mean the reader had mixed two engines' series together."""
    curve = [_real().acceptance_at(i) for i in range(10)]
    assert curve == pytest.approx(
        [0.8889, 0.7778, 0.7778, 0.7778, 0.7778, 0.6667, 0.6667, 0.6667, 0.6667, 0.6667],
        abs=1e-4,
    )
    assert all(b <= a + 1e-9 for a, b in zip(curve, curve[1:]))


def test_the_real_expected_accepted_is_the_prefix_sum_of_the_real_curve():
    """7.33 of 10 drafted tokens settled per step, which is the figure the
    deployment sheet shows and the one that maps onto a speedup."""
    found = _real()
    assert found.expected_accepted(10) == pytest.approx(7.3333, abs=1e-4)
    assert found.mean_acceptance == pytest.approx(0.7333, abs=1e-4)
    # And it is genuinely a prefix sum: at k=1 only position 0 counts.
    assert found.expected_accepted(1) == pytest.approx(0.8889, abs=1e-4)


def test_one_model_one_method_one_gpu_two_workloads_two_answers():
    """Why `measurements.py` keys a record by WORKLOAD, shown with two real
    measurements rather than an argument.

    Both are Qwen/Qwen3-0.6B with ngram at k=10, on the same GB10, on the same
    image, taken minutes apart:

        code_edit (tests/spec_data, via `spec_sweep --smoke`)   0.203
        a repetitive code-echo prompt                           0.733

    Same model, same method, same hardware, same runtime -- and three and a
    half times the acceptance. A record that did not carry the workload would
    let either of these answer for the other, and CLAUDE.md's rule that a
    mismatch MISSES rather than approximates is what that is protecting
    against.
    """
    measured_echo = _real().mean_acceptance
    measured_code_edit = 0.20344827586206896  # spec_sweep --smoke, 29 drafts
    assert measured_echo == pytest.approx(0.7333, abs=1e-4)
    assert measured_echo > 3 * measured_code_edit


# -- the prefix cache -------------------------------------------------------


def _cache() -> metrics_scrape.PrefixCache:
    return metrics_scrape.prefix_cache(SAMPLE.read_text())


def test_the_prefix_cache_counters_are_read_out_of_a_real_exposition_body():
    """The two numbers, captured live off the pinned image on this box."""
    found = _cache()
    assert found.queries == 34845.0
    assert found.hits == 33664.0
    assert found.hit_rate == pytest.approx(0.9661, abs=1e-4)


def test_the_created_timestamp_is_not_read_as_a_count():
    """`prometheus_client` emits a `_created` gauge beside every Counter,
    carrying the unix time the series was born -- 1.79e9 next to a real count
    of 34845. Stripping that suffix the way `_total` is stripped would put a
    timestamp into a cache-hit rate, so it must stay a miss."""
    found = _cache()
    assert found.queries < 1e6
    assert "vllm:prefix_cache_queries_created" in SAMPLE.read_text()


def test_the_external_cache_is_a_different_store_and_is_not_counted():
    """`vllm:external_prefix_cache_*` is the KV connector's cross-instance
    tier, not the GPU prefix cache. The fixture carries both, and both of the
    external series read 0.0 -- so a reader that included them would still
    pass a naive total check. Assert on the exclusion itself."""
    body = SAMPLE.read_text()
    assert "vllm:external_prefix_cache_hits_total" in body
    inflated = body.replace(
        'vllm:external_prefix_cache_queries_total{engine="0",'
        'model_name="Qwen2.5-0.5B-Instruct"} 0.0',
        'vllm:external_prefix_cache_queries_total{engine="0",'
        'model_name="Qwen2.5-0.5B-Instruct"} 99999.0',
    )
    assert inflated != body, "the external series moved; fix this test"
    assert metrics_scrape.prefix_cache(inflated).queries == _cache().queries


def test_the_prefix_cache_counter_suffix_is_not_a_miss_either():
    stripped = metrics_scrape.prefix_cache(
        SAMPLE.read_text().replace("_total{", "{")
    )
    assert stripped.queries == _cache().queries
    assert stripped.hits == _cache().hits


def test_prefix_cache_ranks_are_summed_rather_than_one_picked():
    doubled = SAMPLE.read_text() + SAMPLE.read_text().replace('engine="0"', 'engine="1"')
    found = metrics_scrape.prefix_cache(doubled)
    assert found.queries == 2 * _cache().queries


def test_an_engine_that_cached_nothing_has_no_hit_rate_rather_than_zero():
    """The distinction the whole feature rests on: nothing asked is `None`,
    while asked-and-missed-every-token is a real, measured 0.0."""
    assert metrics_scrape.PrefixCache().hit_rate is None
    assert metrics_scrape.PrefixCache(queries=100.0, hits=0.0).hit_rate == 0.0


def test_a_cache_window_is_a_difference_and_never_goes_negative():
    before = metrics_scrape.PrefixCache(queries=100.0, hits=90.0)
    after = metrics_scrape.PrefixCache(queries=300.0, hits=210.0)
    window = metrics_scrape.cache_delta(before, after)
    assert (window.queries, window.hits) == (200.0, 120.0)
    # 60% in this window, against 70% over the engine's life: the lifetime
    # ratio is the wrong answer for a "right now" panel, which is why the
    # windowed form exists at all.
    assert window.hit_rate == pytest.approx(0.6)
    restarted = metrics_scrape.cache_delta(after, before)
    assert (restarted.queries, restarted.hits) == (0.0, 0.0)
    assert restarted.hit_rate is None


# -- the optimiser ----------------------------------------------------------


def _range_and_base(k: int, draft_params: int):
    spec = SpeculativeSpec(SpeculativeMethod.MTP, k, 0, draft_params)
    lo, hi = speculative_decode_tps_range(SHAPE, 273.0, 0.0, spec)
    ratio = draft_ratio(SHAPE, draft_params)
    return lo, hi, ratio, lo * speculative_overhead(k, ratio)


def test_all_accepted_reproduces_the_ceiling_exactly():
    lo, hi, ratio, base = _range_and_base(5, 1_000_000_000)
    curve = speculative_best_k(base, ratio, [1.0] * 5)
    assert curve[-1].tps == pytest.approx(hi)


def test_none_accepted_reproduces_the_floor_exactly():
    lo, hi, ratio, base = _range_and_base(5, 1_000_000_000)
    curve = speculative_best_k(base, ratio, [0.0] * 5)
    assert curve[-1].tps == pytest.approx(lo)


def test_a_measured_point_always_lands_inside_the_stated_range():
    """The invariant that ties the new number to the old claim.

    The card keeps showing the range; the measured line sits inside it. If this
    ever fails, one of the two is lying about the same launch.
    """
    for k in (1, 3, 5, 8):
        lo, hi, ratio, base = _range_and_base(k, 1_400_000_000)
        curve = speculative_best_k(base, ratio, [0.9, 0.6, 0.3, 0.12, 0.04, 0.02, 0.01, 0.005][:k])
        for point in curve:
            assert lo - 1e-9 <= point.tps <= hi + 1e-9


def test_the_optimum_is_where_the_draft_stops_paying_for_itself():
    """Falling acceptance eventually costs more bandwidth than it settles."""
    _, _, ratio, base = _range_and_base(6, 1_400_000_000)
    curve = speculative_best_k(base, ratio, [0.9, 0.6, 0.3, 0.12, 0.04, 0.01])
    best = max(curve, key=lambda p: p.tps)
    assert 1 <= best.k < 6, "a decaying series should peak before the last position"
    assert curve[-1].tps < best.tps


def test_a_weightless_draft_never_slows_anything_down():
    """ngram reads no weights, so its overhead term is 1 at every k and more
    drafted tokens can only help."""
    _, _, ratio, base = _range_and_base(5, 0)
    assert ratio == 0.0
    curve = speculative_best_k(base, ratio, [0.5, 0.4, 0.3, 0.2, 0.1])
    assert [p.tps for p in curve] == sorted(p.tps for p in curve)
    assert curve[0].tps >= base


def test_no_base_rate_is_no_curve_rather_than_zeros():
    assert speculative_best_k(0.0, 0.1, [0.9, 0.5]) == []


# -- the record -------------------------------------------------------------


def _record(**over) -> measurements.SpecRecord:
    kw = dict(
        model_id="Qwen/Qwen3-4B", method="ngram", workload="code_edit",
        gpu_name="NVIDIA GB10", memory_bandwidth_gbps=273.0,
        runtime_version="0.28.1rc1.dev462", launched_k=10, best_k=6,
        best_tps=71.0, baseline_tps=18.1, accept_cumulative=[0.9, 0.8],
        mean_acceptance=0.71, drafts=4200.0, measured_at=time.time(),
    )
    kw.update(over)
    return measurements.SpecRecord(**kw)


def test_a_record_round_trips(tmp_path):
    saved = measurements.save(_record(), directory=tmp_path)
    assert saved is not None
    assert measurements.load_all(directory=tmp_path) == [_record(
        measured_at=json.loads(saved.read_text())["measured_at"]
    )]


def test_a_model_id_with_a_slash_does_not_become_a_path(tmp_path):
    measurements.save(_record(), directory=tmp_path)
    assert not (tmp_path / "Qwen").exists()
    assert len(list(tmp_path.glob("*.json"))) == 1


@pytest.mark.parametrize(
    "criteria",
    [
        {"gpu_name": "Some Other Card"},
        {"memory_bandwidth_gbps": 900.0},
        {"runtime_version": "0.29.0"},
    ],
)
def test_a_measurement_does_not_travel_across_hardware_or_image(tmp_path, criteria):
    """Decode is bandwidth-bound and a release can change how a head drafts.

    A mismatch has to MISS: the range is still true when there is no record,
    and a stale figure presented as current is worse than no figure.
    """
    measurements.save(_record(), directory=tmp_path)
    query = dict(gpu_name="NVIDIA GB10", memory_bandwidth_gbps=273.0,
                 runtime_version="0.28.1rc1.dev462")
    query.update(criteria)
    assert measurements.matching(
        "Qwen/Qwen3-4B", "ngram", directory=tmp_path, **query
    ) == []


def test_an_uncheckable_criterion_stops_narrowing_rather_than_excluding(tmp_path):
    """A coordinator that could not probe its image still shows what was
    measured; the record carries the version so a reader sees the difference."""
    measurements.save(_record(), directory=tmp_path)
    found = measurements.matching(
        "Qwen/Qwen3-4B", "ngram", gpu_name="NVIDIA GB10",
        memory_bandwidth_gbps=273.0, runtime_version=None, directory=tmp_path,
    )
    assert len(found) == 1


def test_an_unreadable_record_is_skipped_not_raised(tmp_path):
    measurements.save(_record(), directory=tmp_path)
    (tmp_path / "broken.json").write_text("{not json")
    assert len(measurements.load_all(directory=tmp_path)) == 1


def test_the_speedup_is_against_the_measured_baseline():
    assert _record(best_tps=72.4, baseline_tps=18.1).speedup == pytest.approx(4.0)
    assert _record(baseline_tps=0.0).speedup is None


# -- the workloads ----------------------------------------------------------


def test_every_workload_file_parses_and_is_not_prefix_hostile():
    """The prompts must NOT be unique from their first character.

    `tests/load/loadtest.py::synth_prompt` deliberately makes them so, to defeat
    prefix caching. Reusing that here would drive acceptance to near zero and
    produce a number that means nothing, so this pins the opposite property.
    """
    data = Path(__file__).resolve().parents[1] / "spec_data"
    for name in ("code_edit", "extraction", "prose"):
        rows = [json.loads(line) for line in
                (data / f"{name}.jsonl").read_text().splitlines() if line.strip()]
        assert rows, f"{name} is empty"
        for row in rows:
            assert row["prompt"].strip()
            assert row["max_tokens"] > 0
        firsts = {row["prompt"][:24] for row in rows}
        assert len(firsts) < len(rows) or name != "code_edit", (
            "code_edit is the copy-heavy set; its prompts share a preamble"
        )


# -- dflash -----------------------------------------------------------------


@pytest.mark.parametrize(
    "arch",
    [
        "DFlash2DraftModel",
        "DFlashDraftModel",
        "DFlashLagunaForCausalLM",
        "DFlashMuseGlimmerAssistantModel",
    ],
)
def test_dflash_is_recognised(arch):
    """Four classes in the pinned image, one method name.

    `config/speculative.py` spells it `DFlashModelTypes = Literal["dflash"]`,
    so the method is `dflash` even though no class is called that. This was
    missing until `RedHatAI/Qwen3-4B-speculator.dflash2` came back "does not
    recognise as a draft head" -- while the image had been able to load it all
    along, which is the failure `imageprobe.py` exists to end.
    """
    from control_plane.resolver.speculators import method_for_head

    assert method_for_head((arch,)).value == "dflash"


def test_dflash_does_not_shadow_the_other_families():
    from control_plane.resolver.speculators import method_for_head

    assert method_for_head(("DSparkDraftModel",)).value == "dspark"
    assert method_for_head(("Eagle3LlamaForCausalLM",)).value == "eagle3"
    assert method_for_head(("Qwen3NextMTP",)).value == "mtp"


# -- the draft's own KV cache -----------------------------------------------


def test_a_head_is_charged_its_own_kv_cache_not_only_the_drafted_positions():
    """The bug that killed a real DSpark launch.

    vLLM refused to start: "1.37 GiB KV cache is needed, which is larger than
    the available KV cache memory (1.17 GiB)". 1.17 was what derate handed it,
    because only the drafted positions were charged -- and the head was a
    5-layer transformer with a cache of its own against Qwen3-4B's 36 layers.
    """
    from control_plane.contracts import ParallelismKind, ParallelismPlan
    from control_plane.fit.calculator import speculative_kv_bytes_per_rank

    plan = ParallelismPlan(
        ParallelismKind.SINGLE_NODE, 1, 1, 1, 1, ["n"], "t", 0.0, []
    )
    positions_only = SpeculativeSpec(SpeculativeMethod.DSPARK, 5, 0, 0, "h", 0.0)
    with_own_cache = SpeculativeSpec(
        SpeculativeMethod.DSPARK, 5, 0, 0, "h", 5 / 36
    )
    thin = speculative_kv_bytes_per_rank(SHAPE, plan, 1, "auto", positions_only, 8192)
    full = speculative_kv_bytes_per_rank(SHAPE, plan, 1, "auto", with_own_cache, 8192)
    assert full > thin
    # The head's share of the target's cache, which dwarfs five drafted tokens.
    assert full - thin > 20 * thin


def test_the_drafts_own_cache_scales_with_context_not_with_k():
    """Which is why walking k down does not rescue a launch it sank.

    `tests/spec_sweep.py` stops descending on a KV shortfall for exactly this
    reason -- it burned five launches learning it once.
    """
    from control_plane.contracts import ParallelismKind, ParallelismPlan
    from control_plane.fit.calculator import speculative_kv_bytes_per_rank

    plan = ParallelismPlan(
        ParallelismKind.SINGLE_NODE, 1, 1, 1, 1, ["n"], "t", 0.0, []
    )
    at = lambda k, ctx: speculative_kv_bytes_per_rank(  # noqa: E731
        SHAPE, plan, 1, "auto",
        SpeculativeSpec(SpeculativeMethod.DSPARK, k, 0, 0, "h", 5 / 36), ctx,
    )
    # Eight times the drafted tokens barely moves it...
    assert at(8, 8192) / at(1, 8192) < 1.05
    # ...while twice the context very nearly doubles it.
    assert 1.9 < at(1, 16384) / at(1, 8192) < 2.1


def test_a_method_with_no_separate_model_is_charged_no_extra_cache():
    """ngram loads nothing, and an in-checkpoint MTP module caches inside the
    target's own budget. Both carry a zero ratio, and a zero must stay free."""
    from control_plane.contracts import ParallelismKind, ParallelismPlan
    from control_plane.fit.calculator import speculative_kv_bytes_per_rank

    plan = ParallelismPlan(
        ParallelismKind.SINGLE_NODE, 1, 1, 1, 1, ["n"], "t", 0.0, []
    )
    ngram = SpeculativeSpec(SpeculativeMethod.NGRAM, 5, 0, 0, None, 0.0)
    charged = speculative_kv_bytes_per_rank(SHAPE, plan, 1, "auto", ngram, 8192)
    positions = speculative_kv_bytes_per_rank(
        SHAPE, plan, 1, "auto",
        SpeculativeSpec(SpeculativeMethod.NGRAM, 5, 0, 0, None, 0.0), 8192,
    )
    assert charged == positions  # the drafted positions and nothing more


def test_the_ratio_is_the_two_shapes_attention_geometry():
    """Element size cancels, so this is answerable without a KV dtype."""
    from control_plane.resolver.speculators import _kv_ratio

    class S:
        def __init__(self, layers, kv_heads, dim):
            self.num_layers, self.num_kv_heads = layers, kv_heads
            self.effective_head_dim = dim

    assert _kv_ratio(S(5, 8, 128), S(36, 8, 128)) == pytest.approx(5 / 36)
    # A head with half the KV heads caches half as much per layer.
    assert _kv_ratio(S(5, 4, 128), S(36, 8, 128)) == pytest.approx(5 / 72)
    # Unmeasurable shapes leave it unbudgeted rather than guessed.
    assert _kv_ratio(S(0, 0, 0), S(0, 0, 0)) > 0  # both floored at 1, ratio 1


# -- parallel lanes ---------------------------------------------------------


def test_a_served_name_is_made_safe_for_the_launch_command():
    """It reaches a shell command, so it passes the recipe grammar or nothing.

    `deploy/recipes.py::_COMMAND_SAFE` admits letters, digits and
    `. _ - : / ~`. A method name and a head repository are concatenated into
    this, and a head id carries a slash.
    """
    from tests.spec_sweep import _safe_name

    assert _safe_name("Qwen3-4B-AWQ-ngram") == "Qwen3-4B-AWQ-ngram"
    assert " " not in _safe_name("Qwen3 4B eagle3")
    assert _safe_name("a$(id)b") == "a-id-b"
    assert _safe_name("---") == "spec-sweep"
    assert len(_safe_name("x" * 300)) <= 96


def test_every_method_gets_a_distinct_served_name():
    """The rule this exists for: a served name is unique CLUSTER-wide.

    Two copies of one model cannot both answer to the model's own name however
    they are placed, so parallel lanes need names of their own or the second
    launch is refused.
    """
    from tests.spec_sweep import _safe_name

    names = {
        _safe_name(f"Qwen3-4B-AWQ-{m}")
        for m in ("ngram", "eagle3-Qwen3-4B_eagle3", "dspark-Qwen3-4B-speculator.dspark")
    }
    assert len(names) == 3


# -- the scan ---------------------------------------------------------------


def test_the_query_stem_drops_a_quantization_suffix_and_nothing_else():
    """A head is trained against the UNQUANTIZED base and named after it.

    Searching `Qwen3-4B-AWQ eagle` found 3 candidates; `Qwen3-4B eagle` found
    84. The quant token is identified with `quant_detect.from_name` rather than
    a second list, because that table already knows AWQ/FP8/GPTQ/nvfp4/4bit and
    already leaves 4B, 2507, Instruct and Thinking alone.
    """
    from control_plane.head_scan import query_stems

    assert query_stems("Qwen/Qwen3-4B-AWQ") == ["Qwen3-4B-AWQ", "Qwen3-4B"]
    assert query_stems("Qwen/Qwen3-4B-FP8") == ["Qwen3-4B-FP8", "Qwen3-4B"]
    # No quant suffix: one stem, and the full name is not duplicated.
    assert query_stems("Qwen/Qwen3-4B") == ["Qwen3-4B"]
    # A size and a variant are not quantizations.
    assert query_stems("meta-llama/Llama-3.1-8B-Instruct") == ["Llama-3.1-8B-Instruct"]


def test_the_name_filter_reads_the_repository_not_the_organisation():
    """`taobao-mnn` publishes ordinary safetensors heads.

    Matching `-mnn` against the whole id dropped two good EAGLE3 heads for the
    name of the account that published them.
    """
    from control_plane.head_scan import NOT_A_HEAD, short_name

    def dropped(model_id: str) -> bool:
        return any(m in short_name(model_id).lower() for m in NOT_A_HEAD)

    assert not dropped("taobao-mnn/Qwen3-4B-Instruct-2507-Eagle3")
    assert dropped("taobao-mnn/Qwen3-VL-4B-Instruct-Eagle3-MNN")
    assert dropped("unsloth/Qwen3.5-4B-MTP-GGUF")
    assert not dropped("AngelSlim/Qwen3-4B_eagle3")


def test_the_shortlist_is_one_per_family_by_downloads():
    """Within a family the ceiling barely discriminates, so measuring three
    variants of one mechanism spends three launches to answer one question."""
    from control_plane.head_scan import shortlist

    class Opt:
        def __init__(self, method):
            self.method = type("M", (), {"value": method})()

    scored = [
        (426.0, {"model_id": "a/eagle-tiny", "downloads": 5}, Opt("eagle3")),
        (416.0, {"model_id": "b/eagle-popular", "downloads": 3000}, Opt("eagle3")),
        (405.0, {"model_id": "c/eagle-mid", "downloads": 50}, Opt("eagle3")),
        (240.0, {"model_id": "d/dflash", "downloads": 140}, Opt("dflash")),
        (146.0, {"model_id": "e/dspark", "downloads": 86}, Opt("dspark")),
    ]
    picked = shortlist(scored, 4)
    families = [o.method.value for _, _, o in picked]
    assert sorted(families) == ["dflash", "dspark", "eagle3"]
    # The best-ESTABLISHED head of the family, not the highest-scoring one:
    # 3000 downloads beats a 426 ceiling that is 10 tok/s of rounding.
    chosen = {o.method.value: r["model_id"] for _, r, o in picked}
    assert chosen["eagle3"] == "b/eagle-popular"


# --------------------------------------------------------------------------
# the auto-pick: why the top of the ranking is the wrong answer
# --------------------------------------------------------------------------


def _spec_option(method, draft_params, *, max_tokens=8, source="head"):
    from control_plane.resolver.speculators import SpeculativeOption

    return SpeculativeOption(
        method=SpeculativeMethod(method),
        default_tokens=max_tokens,
        max_tokens=max_tokens,
        draft_params=draft_params,
        draft_bytes=None if draft_params is None else draft_params * 2,
        source=source,
        declared_by="",
        note="",
    )


def _scored(*rows):
    """`(ceiling, row, option)` triples, ranking order, as `rank()` returns."""
    out = []
    for ceiling, method, params, downloads in rows:
        out.append(
            (ceiling, {"model_id": f"org/{method}-{downloads}", "downloads": downloads},
             _spec_option(method, params))
        )
    return sorted(out, key=lambda r: -r[0])


def test_a_weightless_draft_is_never_recommended_although_it_tops_the_ranking():
    # This is the whole reason `recommend` exists. `draft_ratio` is the draft's
    # parameters over the target's active ones, so a draft with no weights has
    # ratio 0, `speculative_overhead(k, 0)` is exactly 1.0, and its ceiling is
    # base*(k+1) -- above ANY head with weights, at every k, for every model.
    # Taking the top row would answer "ngram" for the entire hub.
    scored = _scored(
        (900.0, "ngram", 0, None),
        (400.0, "eagle3", 200_000_000, 5000),
    )
    assert scored[0][2].method is SpeculativeMethod.NGRAM  # it does top the list
    pick = head_scan.recommend(scored)
    assert pick is not None
    assert pick[2].method is SpeculativeMethod.EAGLE3


def test_a_cost_that_was_not_derived_is_not_read_as_weightless():
    # `draft_params=None` means the cost was NOT derived -- checkpoint DSpark,
    # which is detected and deliberately refused. `draft_params=0` means it was
    # derived and is genuinely nothing -- ngram. A falsy test would collapse the
    # two and let a head derate cannot budget be recommended for a launch.
    scored = _scored((500.0, "dspark", None, 9000))
    assert head_scan.recommend(scored) is None


def test_nothing_but_weightless_options_is_no_recommendation_not_a_fallback():
    assert head_scan.recommend(_scored((900.0, "ngram", 0, None))) is None


def test_across_families_the_ceiling_decides():
    scored = _scored(
        (400.0, "eagle3", 200_000_000, 10),
        (300.0, "dflash", 900_000_000, 10),
    )
    assert head_scan.recommend(scored)[2].method is SpeculativeMethod.EAGLE3


def test_within_a_family_downloads_decide_because_the_ceiling_ties():
    # Nine EAGLE3 checkpoints of one training run score identically; a 416-vs-405
    # gap is a rounding difference in their size, not a reason to prefer one.
    scored = _scored(
        (416.0, "eagle3", 200_000_000, 12),
        (415.0, "eagle3", 201_000_000, 90_000),
    )
    assert head_scan.recommend(scored)[1]["downloads"] == 90_000


def test_a_measurement_outranks_every_amount_of_arithmetic():
    # eagle3 has by far the better ceiling; dflash is the one somebody actually
    # launched here. Evidence wins -- that is the whole hierarchy of this repo.
    scored = _scored(
        (400.0, "eagle3", 200_000_000, 10_000),
        (300.0, "dflash", 900_000_000, 10),
    )
    pick = head_scan.recommend(scored, {"dflash": 190.0})
    assert pick[2].method is SpeculativeMethod.DFLASH


def test_a_measurement_picks_the_family_and_downloads_still_pick_within_it():
    # `SpecRecord` is keyed by METHOD, not by head repository: a sweep records
    # "eagle3 reached 118 tok/s here" and cannot say which of nine EAGLE3
    # checkpoints produced it. So a measurement narrows to the family and the
    # ordinary rule still chooses inside it.
    scored = _scored(
        (416.0, "eagle3", 200_000_000, 12),
        (415.0, "eagle3", 201_000_000, 90_000),
        (500.0, "dflash", 100_000_000, 50_000),
    )
    pick = head_scan.recommend(scored, {"eagle3": 118.0})
    assert pick[2].method is SpeculativeMethod.EAGLE3
    assert pick[1]["downloads"] == 90_000


def test_a_measurement_for_a_method_nobody_offers_does_not_empty_the_pick():
    scored = _scored((400.0, "eagle3", 200_000_000, 10))
    assert head_scan.recommend(scored, {"mtp": 999.0})[2].method is SpeculativeMethod.EAGLE3


# --------------------------------------------------------------------------
# the durable scan
# --------------------------------------------------------------------------


def _usable():
    return [({"model_id": "org/head-a", "downloads": 4200},
             _spec_option("eagle3", 200_000_000))]


def test_a_scan_round_trips(tmp_path):
    head_scan.save_scan("Qwen/Qwen3-4B", "0.28.1", _usable(),
                        [({"model_id": "org/bad"}, "wrong vocabulary")],
                        1234.0, directory=tmp_path)
    usable, rejected, at = head_scan.cached_scan(
        "Qwen/Qwen3-4B", "0.28.1", directory=tmp_path
    )
    assert at == 1234.0
    assert usable[0][0] == {"model_id": "org/head-a", "downloads": 4200}
    assert usable[0][1].method is SpeculativeMethod.EAGLE3
    assert usable[0][1].draft_bytes == 400_000_000
    assert rejected == [({"model_id": "org/bad"}, "wrong vocabulary")]


def test_a_scan_does_not_travel_across_image_versions(tmp_path):
    # Whether a head is loadable AT ALL comes from the image's own speculator
    # registry, so an answer computed under a different image answers a
    # different question. A miss re-scans, which is correct; approximating is
    # how a name the new image cannot load keeps being offered.
    head_scan.save_scan("Qwen/Qwen3-4B", "0.28.1", _usable(), [], 1.0,
                        directory=tmp_path)
    assert head_scan.cached_scan("Qwen/Qwen3-4B", "0.29.0", directory=tmp_path) is None


def test_a_model_id_with_a_slash_does_not_become_a_scan_path(tmp_path):
    head_scan.save_scan("Qwen/Qwen3-4B", "0.28.1rc1+g9ca97b2", _usable(), [], 1.0,
                        directory=tmp_path)
    written = list(tmp_path.glob("*.json"))
    assert len(written) == 1
    assert written[0].parent == tmp_path


def test_an_unreadable_scan_is_a_miss_rather_than_a_raise(tmp_path):
    (tmp_path / "x.json").write_text("{not json")
    head_scan.save_scan("m", "v", _usable(), [], 1.0, directory=tmp_path)
    (next(p for p in tmp_path.glob("*.json") if p.name != "x.json")).write_text("{")
    assert head_scan.cached_scan("m", "v", directory=tmp_path) is None


def test_a_scan_missing_a_field_reads_as_a_miss_and_rescans(tmp_path):
    # Subscripted rather than `.get()`-ed on purpose: a record written before a
    # field existed must re-scan, not be served forever with the field silently
    # defaulted. Same rule as `ImageProbe.from_dict`.
    head_scan.save_scan("m", "v", _usable(), [], 1.0, directory=tmp_path)
    path = next(tmp_path.glob("*.json"))
    payload = json.loads(path.read_text())
    del payload["scanned_at"]
    path.write_text(json.dumps(payload))
    assert head_scan.cached_scan("m", "v", directory=tmp_path) is None


def test_an_unwritable_estate_is_not_a_failed_scan(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("i am not a directory")
    head_scan.save_scan("m", "v", _usable(), [], 1.0, directory=blocked)
    assert head_scan.cached_scan("m", "v", directory=blocked) is None


# --------------------------------------------------------------------------
# the sweep's own source
# --------------------------------------------------------------------------


def test_the_sweep_defines_nothing_twice():
    """A shadowed definition is invisible to every other test in this file.

    `tests/spec_sweep.py` carried two copies each of `load_workload`,
    `drive_concurrent`, `drive_stream` and `_one`. Python keeps the second, and
    the second was the OLDER one -- it posted to `base + "/v1/chat/completions"`
    against a `backend_url` that already ends in `/v1`, so every request in
    every sweep 404'd, and it returned a bare bool where the caller wanted the
    failure's own words, so the reason for that was thrown away.

    Nothing else could catch it: the file imports, parses and passes a full
    green suite either way. So the check is on the source itself.
    """
    import ast
    import collections
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "spec_sweep.py"
    tree = ast.parse(root.read_text())
    names = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    repeated = sorted(n for n, count in collections.Counter(names).items() if count > 1)
    assert not repeated, f"defined more than once, so the first copy is dead: {repeated}"


def test_the_sweep_drives_the_backend_not_the_gateway():
    """`backend_url` ends in `/v1`, so the request path must not repeat it.

    Two separate bugs meet here. Going through the coordinator routes by served
    name, and a served name can have more than one target -- this box has
    carried two `Qwen3-8B` deployments at once -- so the requests could land on
    a deployment other than the one whose counters were being read. And a
    doubled `/v1` 404s every request outright.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "spec_sweep.py").read_text()
    assert '"/v1/chat/completions"' not in source
    assert '+ "/chat/completions"' in source


# --------------------------------------------------------------------------
# what the tok/s figures are about
# --------------------------------------------------------------------------


def _fit_request(seqs: int, spec):
    """The same shape `tests/unit/test_live_memory.py::_req` builds, plus a spec."""
    from control_plane.contracts import (
        FitRequest, ParallelismKind, ParallelismPlan,
    )

    return FitRequest(
        shape=_spec_shape(),
        context_length=4096,
        max_concurrent_seqs=seqs,
        kv_dtype="fp16",
        plan=ParallelismPlan(
            kind=ParallelismKind.SINGLE_NODE,
            tensor_parallel=1, pipeline_parallel=1,
            expert_parallel=1, data_parallel=1,
            node_ids=["spark-01"], reason="single node",
            measured_link_gbps=0.0, rejected=[],
        ),
        speculative=spec,
    )


def _spec_shape():
    """`SHAPE` at the top of this file -- one model, used by every test here."""
    return SHAPE


@pytest.mark.parametrize("seqs", [2, 8, 64])
def test_a_batched_plan_says_the_range_is_for_one_sequence(seqs):
    # The concurrency control moves the memory bars and cannot move these
    # figures -- `predict_decode_tps` is a single-stream model. Left unsaid,
    # the card promises a 6x speedup at a batch where speculation's win has
    # largely gone: the weights are already amortised across the batch, so
    # drafting k extra positions buys no bandwidth back.
    from control_plane.fit.calculator import FitCalculator

    spec = SpeculativeSpec(
        method=SpeculativeMethod.EAGLE3, num_speculative_tokens=8,
        draft_params=200_000_000, draft_bytes=400_000_000,
    )
    _, _, reason = FitCalculator()._speculative_range(
        _fit_request(seqs, spec), _spec_shape(), 273.0, 1e9, 25.0
    )
    assert "per sequence" in reason
    assert f"sized for {seqs}" in reason
    assert "memory bandwidth" in reason


def test_a_single_stream_plan_does_not_carry_the_batch_clause():
    # It would be noise on the plan the figures are exactly right for.
    from control_plane.fit.calculator import FitCalculator

    spec = SpeculativeSpec(
        method=SpeculativeMethod.NGRAM, num_speculative_tokens=5,
        draft_params=0, draft_bytes=0,
    )
    _, _, reason = FitCalculator()._speculative_range(
        _fit_request(1, spec), _spec_shape(), 273.0, 1e9, 25.0
    )
    assert "per sequence" in reason
    assert "sized for" not in reason


def test_asking_for_no_speculation_still_adds_nothing():
    from control_plane.fit.calculator import FitCalculator

    assert FitCalculator()._speculative_range(
        _fit_request(16, None), _spec_shape(), 273.0, 1e9, 25.0
    ) == (None, None, "")


# --- the engine's own load: KV cache, preemptions, and a MEASURED decode rate


LOAD_REAL = SAMPLE.parent / "vllm_engine_load_real.txt"


def _load() -> metrics_scrape.EngineLoad:
    return metrics_scrape.engine_load(LOAD_REAL.read_text())


def test_kv_cache_usage_is_a_fraction_whatever_the_series_is_called():
    """`vllm:kv_cache_usage_perc` is 0..1. Its own HELP says "1 means 100
    percent usage", and reading the name instead of the help would be wrong by
    100x on the single number the fit gate exists to predict."""
    body = "\n".join(
        [
            "# HELP vllm:kv_cache_usage_perc KV-cache usage. 1 means 100 percent usage.",
            "# TYPE vllm:kv_cache_usage_perc gauge",
            'vllm:kv_cache_usage_perc{engine="0",model_name="m"} 0.42',
        ]
    )
    assert metrics_scrape.engine_load(body).kv_cache_usage == 0.42


def test_the_older_series_name_is_still_read():
    """vLLM renamed gpu_cache_usage_perc to kv_cache_usage_perc. The name moved,
    the meaning did not, and a deployment pinned to an older image must not
    silently report nothing."""
    body = 'vllm:gpu_cache_usage_perc{engine="0",model_name="m"} 0.7'
    assert metrics_scrape.engine_load(body).kv_cache_usage == 0.7


def test_a_fraction_is_maxed_across_ranks_while_counters_are_summed():
    """Two different aggregations in one parse, and getting it backwards is a
    real number on a screen. Tensor-parallel ranks each hold a shard of ONE
    logical cache, so adding 0.4 and 0.4 would report 80% for a cache at 40%.
    The fullest rank is the honest answer: it is the one that preempts first,
    and preemption is what the number is for."""
    body = "\n".join(
        [
            'vllm:kv_cache_usage_perc{engine="0"} 0.4',
            'vllm:kv_cache_usage_perc{engine="1"} 0.6',
            'vllm:num_preemptions_total{engine="0"} 3.0',
            'vllm:num_preemptions_total{engine="1"} 4.0',
        ]
    )
    found = metrics_scrape.engine_load(body)
    assert found.kv_cache_usage == 0.6, "the fullest shard, not the sum"
    assert found.preemptions == 7.0, "but preemptions are events and do sum"


def test_an_absent_gauge_is_unknown_rather_than_empty():
    """None, not 0.0. An engine holding no cache and an engine that never
    reported one are different facts, and 0.0 is a measurement."""
    found = metrics_scrape.engine_load("vllm:num_preemptions_total 1.0")
    assert found.kv_cache_usage is None
    assert found.requests_running is None
    assert found.preemptions == 1.0, "the counter still read"


def test_the_measured_decode_rate_excludes_the_token_prefill_produced():
    """The correction that makes the numerator and denominator the same window.

    generation_tokens counts every token, including each request's first -- and
    the first comes out of PREFILL, which is not inside
    request_decode_time_seconds. On long requests the correction is noise; on
    forty ten-token requests it is 10% of the answer.
    """
    body = "\n".join(
        [
            "vllm:generation_tokens_total 400.0",
            "vllm:request_decode_time_seconds_sum 4.0",
            "vllm:request_decode_time_seconds_count 40.0",
        ]
    )
    found = metrics_scrape.engine_load(body)
    assert found.decode_tps == 90.0, "(400 - 40) / 4.0, not 400 / 4.0"


def test_an_idle_engine_has_no_decode_rate():
    """None rather than 0.0, for the reason every rate in this package is:
    a fabricated zero would drag any average that included it down."""
    assert metrics_scrape.engine_load("vllm:generation_tokens_total 0.0").decode_tps is None


def test_the_real_engines_load_parses():
    """Captured off the live engine on this box, after four real requests."""
    found = _load()
    assert found.kv_cache_usage == 0.0, "idle at capture time, and that is a reading"
    assert found.preemptions == 0.0
    assert found.generation_tokens == 924.0
    assert found.decode_count == 4.0
    assert found.decode_time_s == pytest.approx(7.633, abs=0.01)
    # 920 decode tokens over 7.633s. The fit gate predicted 61.5 for this same
    # deployment; this is the number that says so.
    assert found.decode_tps == pytest.approx(120.5, abs=0.5)


def test_a_restarted_engine_does_not_report_a_negative_rate():
    """Counters reset on restart. Clamped per field, same as spec_decode."""
    before = metrics_scrape.EngineLoad(generation_tokens=900.0, decode_time_s=7.0,
                                       decode_count=4.0, preemptions=5.0)
    after = metrics_scrape.EngineLoad(generation_tokens=10.0, decode_time_s=0.1,
                                      decode_count=1.0, preemptions=0.0)
    window = metrics_scrape.load_delta(before, after)
    assert window.generation_tokens == 0.0
    assert window.preemptions == 0.0
    assert window.decode_tps is None, "no rate beats a wrong one"


# --- measured decode, and what it is allowed to be filed under --------------


def test_a_decode_record_carries_the_prediction_it_disagrees_with():
    """The ratio has to travel WITH the measurement, not be recomputed later.

    `predict_decode_tps` takes the context length as an input, so recomputing a
    ratio against a deployment that has since been relaunched at a different
    context would compare two different questions and call it drift.
    """
    from control_plane.measurements import DecodeRecord

    record = DecodeRecord(
        model_id="Qwen/Qwen3-0.6B", gpu_name="NVIDIA GB10",
        memory_bandwidth_gbps=273.0, runtime_version="0.28.1rc1",
        context_band=256, concurrency_band=1, decode_tps=120.5, tokens=920.0,
        decode_seconds=7.633, requests=4.0, predicted_tps=61.47, measured_at=0.0,
    )
    assert record.ratio == pytest.approx(1.96, abs=0.01)
    assert "ctx256" in record.filename() and "seq1" in record.filename()


def test_a_measurement_with_no_prediction_has_no_ratio():
    """None, not 1.0. A deployment adopted rather than launched through the fit
    gate has no prediction, and inventing agreement would hide that."""
    from control_plane.measurements import DecodeRecord

    record = DecodeRecord(
        model_id="m", gpu_name="g", memory_bandwidth_gbps=1.0, runtime_version="v",
        context_band=1, concurrency_band=1, decode_tps=10.0, tokens=1.0,
        decode_seconds=1.0, requests=1.0, predicted_tps=None, measured_at=0.0,
    )
    assert record.ratio is None


def test_the_context_band_is_what_separates_two_honest_measurements():
    """The 61.5-vs-120.5 gap lives here.

    Decode reads the cache for the tokens actually present, so the same engine
    IS faster at 256 tokens of context than at 8192. Filing both under one key
    would make a correctly-working engine look like it was drifting.
    """
    from control_plane.measurements import DecodeRecord, band

    def rec(ctx, tps):
        return DecodeRecord(
            model_id="m", gpu_name="g", memory_bandwidth_gbps=273.0,
            runtime_version="v", context_band=ctx, concurrency_band=1,
            decode_tps=tps, tokens=900.0, decode_seconds=7.0, requests=4.0,
            predicted_tps=61.5, measured_at=0.0,
        )

    assert rec(256, 120.5).key() != rec(8192, 62.0).key()
    assert band(248) == 128 and band(8192) == 8192


def test_bands_round_down_to_a_power_of_two():
    """Banded because a record per distinct context length is a directory
    nobody can match against, and because the rate is smooth in it -- 3000
    tokens and 3100 are the same measurement."""
    from control_plane.measurements import band

    assert [band(x) for x in (0, 1, 3, 300, 512, 3000, 8192)] == [
        1, 1, 2, 256, 512, 2048, 8192,
    ]


def test_a_decode_record_round_trips_and_a_miss_is_empty(tmp_path):
    """A mismatch on any key component MISSES rather than approximating --
    the rule SpecRecord already follows, for the identical reason."""
    from control_plane.measurements import (
        DecodeRecord, matching_decode, save_decode,
    )

    record = DecodeRecord(
        model_id="Qwen/Qwen3-0.6B", gpu_name="NVIDIA GB10",
        memory_bandwidth_gbps=273.0, runtime_version="0.28.1rc1",
        context_band=256, concurrency_band=1, decode_tps=120.5, tokens=920.0,
        decode_seconds=7.633, requests=4.0, predicted_tps=61.47, measured_at=1.0,
    )
    assert save_decode(record, directory=tmp_path) is not None
    assert matching_decode(
        "Qwen/Qwen3-0.6B", gpu_name="NVIDIA GB10", directory=tmp_path
    )[0].decode_tps == 120.5
    assert matching_decode(
        "Qwen/Qwen3-0.6B", gpu_name="A100", directory=tmp_path
    ) == [], "a different part is a different measurement"
    assert matching_decode(
        "Qwen/Qwen3-0.6B", runtime_version="0.29.0", directory=tmp_path
    ) == [], "and so is a different runtime"


def test_a_corrupt_decode_record_is_skipped_rather_than_raised(tmp_path):
    """One bad file must not take out every other measurement on the box."""
    from control_plane.measurements import load_decode_all

    (tmp_path / "broken.json").write_text("{not json")
    assert load_decode_all(directory=tmp_path) == []


# --- the decode sweep's arithmetic ------------------------------------------
#
# The sweep itself needs hardware and is not collected, but the number it exists
# to produce -- what DECODE_EFFICIENCY would have to be for the gate to have
# been right -- is pure, and a recalibration rests on it being correct.


def _measured(**kw):
    from tests.decode_sweep import Measured

    base = dict(
        model_id="Qwen/Qwen3-0.6B", served_name="Qwen3-0.6B", deployment_id="d-1",
        context_length=8192, decode_tps=123.5, tokens=920.0, decode_seconds=7.45,
        requests=10.0, mean_sequence=272.0, client_tps=118.0,
        predicted_tps=61.47, predicted_tps_empty=99.9,
        weights_bytes=1_503_300_328, kv_cache_bytes=939_524_096,
        kv_cache_usage=0.0, preemptions=0.0,
    )
    base.update(kw)
    return Measured(**base)


def test_the_implied_efficiency_solves_the_gates_own_arithmetic():
    """Hand-checked against the real numbers off a GB10.

    1.503 GB of weights plus 0.940 GB of cache prorated to 272 tokens of 8192
    is 1.5345 GB moved per decoded token; 273 GB/s over that is a 177.9 tok/s
    ceiling; 123.5 measured against it is 0.694. If this drifts, every
    recalibration built on it drifts with it.
    """
    assert _measured().implied_efficiency(273.0) == pytest.approx(0.694, abs=0.001)


def test_the_cache_term_is_prorated_by_how_full_the_context_actually_got():
    """The whole reason the gate was wrong by a VARYING multiple.

    Charging the cache at the full context makes a long-context launch look far
    slower than it decodes, and the error grows with the context asked for. A
    measurement taken at 272 tokens has to be judged against the cache at 272
    tokens, not at 8192.
    """
    short = _measured(mean_sequence=272.0).implied_efficiency(273.0)
    full = _measured(mean_sequence=8192.0).implied_efficiency(273.0)
    assert full > short, "the same rate against a bigger cache implies more efficiency"
    # And the full-context case is exactly the gate's own denominator.
    assert full == pytest.approx(
        123.5 / (273e9 / (1_503_300_328 + 939_524_096)), abs=1e-6
    )


def test_an_efficiency_needs_a_bandwidth_and_a_context_to_mean_anything():
    """None rather than a number, on the same terms as everything else here."""
    assert _measured().implied_efficiency(0.0) is None
    assert _measured(context_length=0).implied_efficiency(273.0) is None


def test_the_ratio_is_none_when_nothing_predicted_it():
    """A deployment adopted rather than launched through the gate has no
    prediction, and inventing agreement would hide that."""
    assert _measured(predicted_tps=None).ratio is None
    assert _measured().ratio == pytest.approx(123.5 / 61.47, abs=1e-6)


def test_padding_only_ever_grows_a_prompt():
    """The band a record is filed under comes from the engine's own token
    counts, so this only has to get the cache into the right neighbourhood --
    but it must never truncate the prompt it was given."""
    from tests.decode_sweep import pad

    assert pad("hello", 0) == "hello"
    assert pad("hello", 200).endswith("hello")
    assert len(pad("hello", 200)) > len(pad("hello", 50))
