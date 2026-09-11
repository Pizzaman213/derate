"""Speculative decoding: what a checkpoint declares, what it costs, what it runs.

Four surfaces, and the tests are grouped by which one a failure would be in:
detection (resolver/speculators.py), memory (fit/calculator.py), throughput (the
same file's range function), and the launch command (deploy/flags.py and
deploy/recipes.py).

The throughput ones are the odd group and worth reading first. There is no
assertion here that speculative decoding is faster, because the code makes no
such claim -- it reports a floor and a ceiling and says the acceptance rate that
decides between them is not measured. What is asserted is that the floor is
below the ordinary rate whenever the draft has weights, which is the half of the
trade a recommendation is tempted to leave out.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest
import yaml

from control_plane.contracts import (
    ModelShape,
    ParallelismKind,
    ParallelismPlan,
    SpeculativeMethod,
    SpeculativeSpec,
)
from control_plane.deploy.flags import (
    render_speculative_config,
    speculative_refusal,
)
from control_plane.deploy.recipes import synthesize
from control_plane.fit.calculator import (
    memory_breakdown,
    predict_decode_tps,
    speculative_decode_tps_range,
)
from control_plane.resolver import speculators
from control_plane.resolver.config_map import map_config, vision_config
from control_plane.resolver.params import analytic_breakdown, reconcile

import tests.fixtures as fx

CORPUS = Path(__file__).resolve().parents[1] / "resolver_data"


def _detect(name: str, weight_bytes: int | None = None):
    """Run the real mapper and the real accounting over one corpus config."""
    config = json.loads((CORPUS / f"{name}.config.json").read_text())
    mapped = map_config(config)
    breakdown = analytic_breakdown(mapped, vision_config(config))
    accounting = reconcile(mapped, breakdown, None)
    options = speculators.detect(
        mapped,
        breakdown,
        total_params=accounting.total_params,
        weight_bytes=weight_bytes,
        bytes_per_param=2.0,
    )
    return {opt.method.value: opt for opt in options}


def _plan(tp: int = 1, pp: int = 1) -> ParallelismPlan:
    kind = ParallelismKind.SINGLE_NODE if tp == pp == 1 else ParallelismKind.HYBRID
    return ParallelismPlan(kind, tp, pp, 1, 1, ["spark-01"], "test", 0.0, [])


# -- detection --------------------------------------------------------------


def test_mtp_is_detected_from_the_config_not_the_architecture():
    found = _detect("deepseek-v3")
    assert "mtp" in found
    mtp = found["mtp"]
    assert mtp.declared_by == "num_nextn_predict_layers"
    assert mtp.source == "checkpoint"
    assert mtp.draft_params > 0
    assert mtp.launchable


def test_a_checkpoint_can_declare_two_mechanisms_at_once():
    """DeepSeek-V4-Flash carries `num_nextn_predict_layers` AND `dspark_*`.

    The reason detection reads config keys rather than keying a table on the
    architecture name: a table would have to pick one of these, and the
    checkpoint declares both.
    """
    found = _detect("deepseek-v4-flash")
    assert {"mtp", "dspark"} <= set(found)


def test_dspark_is_named_and_refused_rather_than_estimated():
    """The cost was not derived, so it is not offered -- and it still appears.

    `dspark_target_layer_ids` points at layers the model already has, so DSpark
    is not a decoder layer's worth of new weights the way the MTP module is.
    But "not that" is not a figure, and derate does not launch what it cannot
    budget. Naming it beats hiding it.
    """
    dspark = _detect("deepseek-v4-flash")["dspark"]
    assert dspark.draft_params is None
    assert dspark.draft_bytes is None
    assert not dspark.launchable
    assert dspark.default_tokens == 5  # dspark_block_size, read off the config


def test_ngram_is_offered_for_a_model_that_declares_nothing():
    """The Qwen3-8B case, and the reason ngram is in this module at all.

    A dense checkpoint with no speculator declares nothing, and an MTP-only
    feature would have nothing to say on its page. ngram loads no weights and
    needs no support from the checkpoint, so it is offered for every model --
    with a DERIVED zero cost, not a missing one.
    """
    for name in ("deepseek-v3", "deepseek-v4-flash"):
        ngram = _detect(name)["ngram"]
        assert ngram.draft_params == 0
        assert ngram.draft_bytes == 0
        assert ngram.source == "method"
        assert ngram.launchable


def test_mtp_bytes_are_exactly_what_the_resolver_subtracted():
    """Turning it on adds back precisely what turning it off took away.

    resolver.py scales a measured `weight_bytes` down by the MTP share the
    moment it excludes those parameters from the total. Deriving the cost from
    `mtp * bytes_per_param` instead would disagree with that subtraction on any
    mixed-precision repo -- and the disagreement lands in the fit gate.
    """
    config = json.loads((CORPUS / "deepseek-v3.config.json").read_text())
    mapped = map_config(config)
    breakdown = analytic_breakdown(mapped, vision_config(config))
    accounting = reconcile(mapped, breakdown, None)

    checkpoint_bytes = 900_000_000_000  # what the shards on disk weigh
    total = accounting.total_params
    # The exact line from resolver.py.
    reduced = int(checkpoint_bytes * total / (total + breakdown.mtp))
    removed = checkpoint_bytes - reduced

    found = _detect("deepseek-v3", weight_bytes=reduced)
    assert abs(found["mtp"].draft_bytes - removed) <= 1


# -- memory -----------------------------------------------------------------


def test_the_draft_is_charged_to_weights_and_its_positions_to_cache():
    plan = _plan()
    base, _ = memory_breakdown(fx.QWEN3_30B_A3B, plan, 8192, 16, "auto")
    spec = SpeculativeSpec(SpeculativeMethod.MTP, 2, 3_000_000_000, 1_500_000_000)
    with_spec, warnings = memory_breakdown(
        fx.QWEN3_30B_A3B, plan, 8192, 16, "auto", speculative=spec
    )

    assert with_spec.weights - base.weights == pytest.approx(3_000_000_000, abs=2)
    assert with_spec.kv_cache > base.kv_cache
    assert with_spec.total > base.total
    assert any("speculative decoding" in w for w in warnings)


def test_nothing_changes_when_no_speculation_was_asked_for():
    """The overwhelmingly common case, and it must be bit-identical."""
    plan = _plan()
    before, warn_before = memory_breakdown(fx.QWEN3_30B_A3B, plan, 8192, 16, "auto")
    after, warn_after = memory_breakdown(
        fx.QWEN3_30B_A3B, plan, 8192, 16, "auto", speculative=None
    )
    assert before == after
    assert warn_before == warn_after
    assert not any("speculative" in w for w in warn_after)


def test_ngram_costs_no_weights_but_still_costs_cache():
    plan = _plan()
    base, _ = memory_breakdown(fx.QWEN3_30B_A3B, plan, 8192, 16, "auto")
    spec = SpeculativeSpec(SpeculativeMethod.NGRAM, 5, 0, 0)
    with_spec, _ = memory_breakdown(
        fx.QWEN3_30B_A3B, plan, 8192, 16, "auto", speculative=spec
    )
    assert with_spec.weights == base.weights
    # Five drafted positions per sequence are still real cache.
    assert with_spec.kv_cache > base.kv_cache


def test_the_draft_head_is_not_divided_by_pipeline_stages():
    """It sits on one stage, so it is charged whole to the rank that holds it.

    Dividing it by PP would under-budget exactly the rank that OOMs, which is
    the failure the uneven-stages warning beside it already exists to prevent.
    """
    spec = SpeculativeSpec(SpeculativeMethod.MTP, 1, 4_000_000_000, 2_000_000_000)
    solo, _ = memory_breakdown(
        fx.LLAMA_3_3_70B, _plan(), 4096, 4, "auto", speculative=spec
    )
    solo_base, _ = memory_breakdown(fx.LLAMA_3_3_70B, _plan(), 4096, 4, "auto")
    piped, warnings = memory_breakdown(
        fx.LLAMA_3_3_70B, _plan(pp=2), 4096, 4, "auto", speculative=spec
    )
    piped_base, _ = memory_breakdown(fx.LLAMA_3_3_70B, _plan(pp=2), 4096, 4, "auto")

    assert solo.weights - solo_base.weights == pytest.approx(4_000_000_000, abs=2)
    assert piped.weights - piped_base.weights == pytest.approx(4_000_000_000, abs=2)
    assert any("one pipeline stage" in w for w in warnings)


def test_tensor_parallel_does_shard_the_draft():
    spec = SpeculativeSpec(SpeculativeMethod.MTP, 1, 4_000_000_000, 2_000_000_000)
    one, _ = memory_breakdown(fx.LLAMA_3_3_70B, _plan(), 4096, 4, "auto", speculative=spec)
    one_base, _ = memory_breakdown(fx.LLAMA_3_3_70B, _plan(), 4096, 4, "auto")
    two, _ = memory_breakdown(
        fx.LLAMA_3_3_70B, _plan(tp=2), 4096, 4, "auto", speculative=spec
    )
    two_base, _ = memory_breakdown(fx.LLAMA_3_3_70B, _plan(tp=2), 4096, 4, "auto")

    assert (one.weights - one_base.weights) == pytest.approx(
        2 * (two.weights - two_base.weights), rel=1e-6
    )


# -- throughput -------------------------------------------------------------


def _range(shape: ModelShape, spec: SpeculativeSpec) -> tuple[float, float, float]:
    base = predict_decode_tps(shape, 273.0, 0.0)
    floor, ceiling = speculative_decode_tps_range(shape, 273.0, 0.0, spec)
    return base, floor, ceiling


def test_a_draft_with_weights_has_a_floor_below_the_ordinary_rate():
    """The half of the trade a recommendation is tempted to leave out.

    Nothing accepted means the draft head was read for nothing, and the launch
    is slower than not speculating at all.
    """
    spec = SpeculativeSpec(SpeculativeMethod.MTP, 2, 0, 1_500_000_000)
    base, floor, ceiling = _range(fx.QWEN3_30B_A3B, spec)
    assert floor < base < ceiling


def test_ngram_cannot_be_slower_than_not_speculating():
    """Its floor IS the ordinary rate: it reads no weights to draft with."""
    spec = SpeculativeSpec(SpeculativeMethod.NGRAM, 5, 0, 0)
    base, floor, ceiling = _range(fx.QWEN3_30B_A3B, spec)
    assert floor == pytest.approx(base)
    assert ceiling == pytest.approx(base * 6)


def test_the_ceiling_is_tighter_than_a_bare_k_plus_one():
    """Because the draft's own bandwidth cost is subtracted from it."""
    spec = SpeculativeSpec(SpeculativeMethod.MTP, 3, 0, 1_500_000_000)
    base, _, ceiling = _range(fx.QWEN3_30B_A3B, spec)
    assert ceiling < base * 4


def test_drafting_nothing_is_the_ordinary_rate_at_both_ends():
    spec = SpeculativeSpec(SpeculativeMethod.NGRAM, 0, 0, 0)
    base, floor, ceiling = _range(fx.QWEN3_30B_A3B, spec)
    assert floor == pytest.approx(base)
    assert ceiling == pytest.approx(base)


# -- the launch command -----------------------------------------------------


def test_the_rendered_config_carries_no_quote_to_break_out_with():
    """It is wrapped in single quotes inside a `bash -c` command block.

    The generic extra_args channel cannot carry this flag at all -- its
    allowlist rejects `{`, `"` and spaces -- which is exactly why the control
    plane builds this one itself and validates the RESULT rather than the
    inputs.
    """
    for method in ("mtp", "dspark", "ngram"):
        rendered = render_speculative_config(method, 4)
        assert "'" not in rendered
        assert json.loads(rendered) == {
            "method": method,
            "num_speculative_tokens": 4,
        }


def test_a_method_outside_the_enum_is_refused():
    for bad in ("ngram; curl x | sh", "'", "", "mtp\nrm -rf /"):
        with pytest.raises(ValueError):
            render_speculative_config(bad, 4)


def test_only_vllm_is_told_about_speculative_decoding():
    assert speculative_refusal("vllm", "ngram") is None
    # Both refusals name the runtime and say what to do instead, rather than
    # letting a launch start and quietly decode one token per step.
    for runtime in ("sglang", "tts"):
        refusal = speculative_refusal(runtime, "ngram")
        assert refusal is not None
        assert runtime in refusal


def test_the_recipe_survives_yaml_and_the_shell(tmp_path):
    """The whole chain: JSON -> YAML scalar -> substitution -> `bash -c`.

    The failure this guards is specific. The value starts with `{`, which a
    plain YAML scalar reads as a flow mapping -- written bare it would parse
    cleanly into a dict and sparkrun would substitute that dict's Python repr,
    single quotes and all, into a command already wrapped in single quotes.
    """
    spec = SpeculativeSpec(SpeculativeMethod.NGRAM, 5, 0, 0)
    recipe = synthesize(
        fx.QWEN3_30B_A3B,
        _plan(),
        "vllm",
        8192,
        16,
        "qwen3-30b-a3b",
        port=8000,
        gpu_memory_utilization=0.42,
        speculative=spec,
        recipe_dir=tmp_path,
    )

    doc = yaml.safe_load(recipe.content)
    value = doc["defaults"]["speculative_config"]
    assert isinstance(value, str), "a bare flow mapping would arrive as a dict"

    command = doc["command"].format(model=doc["model"], **doc["defaults"])
    tokens = shlex.split(command.replace("\\\n", " "))
    at = tokens.index("--speculative-config")
    assert json.loads(tokens[at + 1]) == {
        "method": "ngram",
        "num_speculative_tokens": 5,
    }


def test_a_recipe_without_speculation_gains_no_flag(tmp_path):
    recipe = synthesize(
        fx.QWEN3_30B_A3B,
        _plan(),
        "vllm",
        8192,
        16,
        "qwen3-30b-a3b",
        port=8000,
        gpu_memory_utilization=0.42,
        recipe_dir=tmp_path,
    )
    assert "--speculative-config" not in recipe.content
    assert "speculative_config" not in recipe.content


# -- the wire ---------------------------------------------------------------
#
# The four surfaces above are each correct in isolation; these are the ones
# that would still let a launch speculate differently than the screen said.
# They go through the real ASGI app rather than calling the handler, because
# what is being checked is the request and response shape.


def _client(*, resolver="full"):
    """The real ASGI app over the day-0 ports.

    `resolver="full"` is `control_plane.resolver.stub.StubResolver`, which
    implements `resolve_full` and so can say what a checkpoint declares. The
    gateway's own `StubResolver` deliberately cannot -- it has `resolve` and
    nothing else -- and `resolver="shape-only"` selects it, because "a resolver
    that never told us what the model declares" is a real deployment and has to
    refuse rather than guess.
    """
    import dataclasses

    from fastapi.testclient import TestClient
    from control_plane.gateway.app import create_app
    from control_plane.fit import FitCalculator
    from control_plane.resolver.stub import StubResolver as FullResolver
    from tests.unit.test_gateway import build_deps, FakeRegistry, make_node_profile

    registry = FakeRegistry()
    registry.states = [fx.node_state(make_node_profile("spark-01"))]
    # The REAL fit calculator, not `StubFit`, which returns a canned verdict and
    # so could not show the memory picture moving when a draft is charged. The
    # point of these is that the number beside the control is the number for the
    # request the control made.
    deps = dataclasses.replace(build_deps(registry=registry), fit=FitCalculator())
    if resolver == "full":
        deps = dataclasses.replace(deps, resolver=FullResolver())
    return TestClient(create_app(deps))


def _plan_body(**extra):
    return {"model_id": "meta-llama/Llama-3.3-70B-Instruct", "target": "throughput", **extra}


def test_the_plan_offers_what_it_can_serve_with_before_anybody_asks():
    """The options ride on every plan, so the control needs no second call."""
    with _client() as client:
        body = client.post("/api/plan", json=_plan_body()).json()
    offered = {o["method"] for o in body["speculative_options"]}
    assert "ngram" in offered
    # Nothing was asked for, so nothing was charged, and the echo says so
    # rather than reporting a zero that would read as an answer.
    assert body["speculative"] is None
    assert body["fit"]["speculative_decode_tps_ceiling"] is None


def test_asking_for_a_method_changes_the_verdict_rather_than_annotating_it():
    with _client() as client:
        plain = client.post("/api/plan", json=_plan_body()).json()
        spec = client.post(
            "/api/plan",
            json=_plan_body(speculative={"method": "ngram", "num_speculative_tokens": 5}),
        ).json()

    assert spec["speculative"] == {
        "method": "ngram",
        "num_speculative_tokens": 5,
        "draft_bytes": 0,
        "draft_params": 0,
        # Null, not absent: a built-in method has no separate head, and a
        # client reading a null knows the question was asked and answered.
        "model": None,
    }
    # The drafted positions are real cache, so the memory picture moved.
    assert spec["fit"]["breakdown"]["kv_cache"] > plain["fit"]["breakdown"]["kv_cache"]
    # And the range is stated at both ends, with the sentence that says derate
    # does not know where between them a workload lands.
    floor = spec["fit"]["speculative_decode_tps_floor"]
    ceiling = spec["fit"]["speculative_decode_tps_ceiling"]
    assert floor is not None and ceiling > floor
    assert "acceptance rate" in spec["fit"]["speculative_reason"]


def test_a_method_this_model_does_not_offer_is_refused_by_name():
    """Named, with what IS on offer -- not silently dropped.

    The URL parser deliberately lets an unknown method through for this: a
    shared link that asks for something impossible has to come back as this
    sentence rather than as a launch that quietly speculates differently.
    """
    with _client() as client:
        r = client.post(
            "/api/plan",
            json=_plan_body(speculative={"method": "mtp", "num_speculative_tokens": 1}),
        )
    assert r.status_code == 400
    message = r.json()["error"]["message"]
    assert "mtp" in message and "ngram" in message


def test_drafting_more_tokens_than_the_method_allows_is_refused():
    with _client() as client:
        r = client.post(
            "/api/plan",
            json=_plan_body(speculative={"method": "ngram", "num_speculative_tokens": 99}),
        )
    assert r.status_code == 400
    assert "at most" in r.json()["error"]["message"]


@pytest.mark.parametrize(
    "bad",
    [
        {"method": "ngram"},                                   # no count
        {"num_speculative_tokens": 5},                         # no method
        {"method": "ngram", "num_speculative_tokens": 0},      # not a request
        {"method": "ngram", "num_speculative_tokens": "five"},
        "ngram:5",                                             # not an object
    ],
)
def test_a_malformed_speculative_request_is_a_400(bad):
    with _client() as client:
        r = client.post("/api/plan", json=_plan_body(speculative=bad))
    assert r.status_code == 400


def test_a_runtime_with_no_flag_for_it_refuses_before_the_launch():
    """sglang has no `--speculative-config` template in this build.

    Refused at plan time rather than discovered by a launch that starts and
    then decodes one token per step with the draft's memory charged against it.
    """
    with _client() as client:
        r = client.post(
            "/api/plan",
            json=_plan_body(
                runtime="sglang",
                speculative={"method": "ngram", "num_speculative_tokens": 5},
            ),
        )
    assert r.status_code == 400
    assert "sglang" in r.json()["error"]["message"]


def test_a_resolver_that_cannot_say_what_a_model_declares_refuses(): 
    """It offers nothing and refuses a request, rather than guessing.

    The gateway composes ports it does not own, and one without `resolve_full`
    never told us whether this checkpoint carries an MTP module. Offering ngram
    anyway would be defensible and still wrong on the point that matters: the
    draft cost the fit gate charges has to come from a resolution, and there
    isn't one.
    """
    with _client(resolver="shape-only") as client:
        body = client.post("/api/plan", json=_plan_body()).json()
        assert body["speculative_options"] == []
        assert body["speculative"] is None

        r = client.post(
            "/api/plan",
            json=_plan_body(speculative={"method": "ngram", "num_speculative_tokens": 5}),
        )
    assert r.status_code == 400
    assert "resolver" in r.json()["error"]["message"]


# -- external heads ---------------------------------------------------------
#
# A head ships in its OWN repository, so the target's config says nothing about
# it -- Qwen3-Next declares no `num_nextn_predict_layers` while the pinned image
# loads `Qwen3NextMTP` from a separate repo. These check the half that prices
# what an operator names, without going near the network: `head_option` takes
# two already-resolved objects.


class _FakeShape:
    def __init__(self, model_id, hidden, vocab, params=0, layers=1):
        self.model_id = model_id
        self.hidden_size = hidden
        self.vocab_size = vocab
        self.total_params = params
        self.num_layers = layers


class _FakeHead:
    """The shape of a `Resolution`, with only what `head_option` reads."""

    def __init__(self, model_id, arch, hidden, vocab, params, layers=1, weight_bytes=1):
        self.shape = _FakeShape(model_id, hidden, vocab, params, layers)
        self.architectures = (arch,)
        self.weight_bytes = weight_bytes


BASE = _FakeShape("Qwen/Qwen3-4B", hidden=2560, vocab=151936, params=4_000_000_000)


def _eagle_head(**over):
    kw = dict(
        model_id="AngelSlim/Qwen3-4B_eagle3",
        arch="Eagle3LlamaForCausalLM",
        hidden=2560,
        vocab=151936,
        params=218_429_056,
        weight_bytes=436_899_680,
    )
    kw.update(over)
    return _FakeHead(**kw)


@pytest.mark.parametrize(
    "arch,expected",
    [
        ("Eagle3LlamaForCausalLM", "eagle3"),
        ("LlamaForCausalLMEagle3", "eagle3"),
        ("EagleLlamaForCausalLM", "eagle"),
        ("MedusaModel", "medusa"),
        ("DSparkDraftModel", "dspark"),
        ("Qwen3DSparkModel", "dspark"),
        ("Qwen3NextMTP", "mtp"),
        ("MiMoMTPModel", "mtp"),
    ],
)
def test_the_method_is_read_off_the_head_class(arch, expected):
    """All eight names are real classes in the pinned image's registry.

    Prefix matching rather than a table, because that registry holds 61 of
    these and grows every release -- an exact list would refuse a head the
    runtime can load the day after it ships.
    """
    assert speculators.method_for_head((arch,)).value == expected


def test_a_class_nothing_recognises_is_refused_not_defaulted():
    assert speculators.method_for_head(("Qwen3ForCausalLM",)) is None
    option = speculators.head_option(
        _eagle_head(arch="Qwen3ForCausalLM"), BASE
    )
    assert not option.launchable
    assert "does not recognise" in option.note


def test_a_compatible_head_is_priced_from_its_measured_weights():
    option = speculators.head_option(_eagle_head(), BASE)
    assert option.launchable
    assert option.method is SpeculativeMethod.EAGLE3
    assert option.draft_bytes == 436_899_680
    assert option.source == "head"
    # An EAGLE head is one layer RE-RUN, so its count is an operator choice --
    # unlike an MTP module, which drafts one position per nextn layer.
    assert option.default_tokens == speculators.HEAD_DEFAULT_TOKENS
    assert option.max_tokens == speculators.HEAD_MAX_TOKENS


def test_a_head_for_a_different_width_is_refused_with_both_numbers():
    """A head reads the target's residual stream directly.

    Checkable arithmetic, not a judgement about tokenizer families: the two
    numbers are on the wire and a mismatch is a launch that dies at load.
    """
    option = speculators.head_option(_eagle_head(hidden=4096), BASE)
    assert not option.launchable
    assert "4096" in option.note and "2560" in option.note


def test_a_head_for_a_different_vocabulary_is_refused():
    option = speculators.head_option(_eagle_head(vocab=32000), BASE)
    assert not option.launchable
    assert "verify" in option.note


def test_a_head_the_image_cannot_load_is_refused_before_the_launch():
    option = speculators.head_option(
        _eagle_head(), BASE, image_speculators=frozenset({"MedusaModel"})
    )
    assert not option.launchable
    assert "does not register" in option.note


def test_no_probe_is_no_opinion_rather_than_a_refusal():
    """Same contract as every other probe result: absence never refuses."""
    assert speculators.head_option(_eagle_head(), BASE, image_speculators=None).launchable


def test_a_head_with_no_weight_index_is_named_and_refused():
    """`AngelSlim/Qwen3-8B_eagle3` is this case on the real hub.

    The analytic split for a head is a floor -- the projection folding the
    target's hidden states together is not modeled, and it lands 16 percent low
    on the one head whose shards can be counted. Under-charging is the wrong
    direction for a memory gate.
    """
    option = speculators.head_option(_eagle_head(weight_bytes=None), BASE)
    assert not option.launchable
    assert "measure" in option.note


def test_the_launch_command_carries_the_head_repository():
    rendered = render_speculative_config("eagle3", 3, "AngelSlim/Qwen3-4B_eagle3")
    assert json.loads(rendered) == {
        "method": "eagle3",
        "model": "AngelSlim/Qwen3-4B_eagle3",
        "num_speculative_tokens": 3,
    }
    assert "'" not in rendered


@pytest.mark.parametrize(
    "bad", ["a b", 'x"y', "q'r", "--flag", "$(id)", "a/b;curl x|sh", "~", ".."]
)
def test_operator_text_cannot_reach_the_shell_through_the_head_field(bad):
    """The one field here that carries operator text.

    It goes through recipes.py's own model-id grammar -- the same one
    `shape.model_id` passes -- before the whole-string check.
    """
    with pytest.raises(ValueError):
        render_speculative_config("eagle3", 3, bad)


# --------------------------------------------------------------------------
# a vision head for a text model: the one thing geometry cannot see
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "declared,actual,conflict",
    [
        # Granularity is not a conflict. A head that names the family while the
        # target names the variant is consistent with it.
        ("qwen3", "qwen3_moe", False),
        ("qwen3_moe", "qwen3", False),
        ("qwen3_moe", "qwen3_moe", False),
        # Siblings are. This is the real case: `qwen3_vl` and `qwen3_moe` share
        # the `qwen3` stem, so a bare `startswith` would have let the vision
        # head through -- it never sees the end of the segment.
        ("qwen3_vl", "qwen3_moe", True),
        ("qwen3_moe", "qwen3_vl", True),
        ("llama", "qwen3_moe", True),
        # Silence is never a conflict, in either direction. Most heads declare
        # nothing at all, including every good one this project has tested.
        ("", "qwen3_moe", False),
        ("qwen3_vl", "", False),
        ("", "", False),
        # Case and whitespace are not disagreements.
        ("  Qwen3_VL  ", "qwen3_vl", False),
    ],
)
def test_a_declared_target_type_conflicts_only_with_a_sibling(declared, actual, conflict):
    assert speculators.target_type_conflict(declared, actual) is conflict


def test_a_head_declaring_another_targets_type_is_refused_with_both_names():
    # Measured on the real repositories: AngelSlim/Qwen3-VL-30B-A3B-Instruct_eagle3,
    # nvidia/Qwen3-30B-A3B-Thinking-2507-Eagle3 and Qwen/Qwen3-30B-A3B all report
    # hidden_size 2048, vocab_size 151936 and vision_params 0. Only the VL head's
    # own config says `target_model_type: "qwen3_vl"`.
    head = _eagle_head()
    head.target_model_type = "qwen3_vl"
    option = speculators.head_option(head, BASE, target_model_type="qwen3_moe")
    assert not option.launchable
    assert "qwen3_vl" in option.note and "qwen3_moe" in option.note


def test_a_head_that_declares_nothing_is_unaffected():
    # The overwhelmingly common case, and it must stay launchable: refusing on
    # absence would drop every good head that simply does not say.
    head = _eagle_head()
    assert not getattr(head, "target_model_type", "")
    assert speculators.head_option(head, BASE, target_model_type="qwen3_moe").launchable


def test_a_target_whose_type_is_unknown_refuses_nothing():
    # An older resolution has no `model_type`, and that degrades to the
    # geometry gates rather than to a refusal -- the same contract as the
    # image probe and the live-memory kwarg.
    head = _eagle_head()
    head.target_model_type = "qwen3_vl"
    assert speculators.head_option(head, BASE, target_model_type="").launchable


def test_the_declared_type_is_checked_after_the_dimensions():
    # A head that is wrong in both ways should name the dimension, which is the
    # more actionable of the two: it is a fact about the head, not a claim.
    head = _eagle_head(hidden=4096)
    head.target_model_type = "qwen3_vl"
    option = speculators.head_option(head, BASE, target_model_type="qwen3_moe")
    assert not option.launchable
    assert "hidden size" in option.note
