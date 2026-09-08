"""Every config in the corpus, every run.

The resolver's other tests each pin one behaviour with a config chosen to show
it. This one is the opposite: it resolves *everything* in
``tests/resolver_data/`` and compares the whole result against
``EXPECTED.json``. No config gets to change outcome quietly, in either
direction -- a model that stops resolving fails, and a model that starts
resolving fails too, because that is a change somebody should look at before it
ships rather than after.

It exists because this build has now been wrong in both directions in one week.
``Gemma4ForConditionalGeneration`` was refused by a stale architecture list
while the pinned image could load it; ``Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice``
was refused by a stale list of config *key* names. Both were found by a person
pasting an error into a chat window, which is not a regression net.

    python3 -m tests.model_sweep              # the same check, with the table
    python3 -m tests.model_sweep --regenerate # after a deliberate change
    python3 -m tests.model_sweep --live       # the coordinator's real catalogue

Adding a config to the corpus fails this test until the expectations are
regenerated, which is deliberate: an un-recorded fixture is a file that runs
and asserts nothing.
"""

from __future__ import annotations

import json

import pytest

from tests import model_sweep


@pytest.fixture(autouse=True)
def _static_tables():
    """No image probe, on any machine.

    `support` prefers a probed registry over its static table when one has been
    recorded, and something earlier in the session may have recorded one. The
    corpus has to describe this checkout, not whichever vLLM image happens to
    be pulled on the box running the suite.
    """
    from control_plane.resolver import support

    support.clear_probes()
    yield
    support.clear_probes()


def test_the_corpus_is_not_empty():
    """A glob that matches nothing passes every assertion below it."""
    assert len(model_sweep.corpus_names()) >= 20


def test_no_model_regressed():
    """A model that got worse. The bug half of the gate."""
    drift = model_sweep.compare_corpus()
    assert not drift.regressions, "\n" + drift.render()


def test_no_model_improved_without_being_recorded():
    """A model that got better, which is still a change nobody looked at.

    Split from the test above rather than folded into it because the two ask
    for different things. A regression wants a fix; an improvement wants a
    reviewed diff and `--regenerate`. Reporting them as one list makes the
    good news read like a breakage and buries which one you actually have.
    """
    drift = model_sweep.compare_corpus()
    assert not drift.improvements, "\n" + drift.render()


def test_every_fixture_has_an_expectation():
    """The corpus and the record cannot drift apart in either direction."""
    expected = json.loads(model_sweep.EXPECTED.read_text())
    assert sorted(expected) == model_sweep.corpus_names()


class TestTheCasesWorthNamingOutLoud:
    """Shapes that were wrong, or nearly wrong, for a reason worth recording."""

    def test_qwen3_omni_is_sized_by_its_thinker(self):
        """The one where a wrong answer would have been dangerous rather than
        merely wrong.

        Its served stack is `thinker_config.text_config`, 48 layers of 2048
        with 128 experts. The only candidate one level down is
        `code2wav_config`, an 8-layer vocoder -- and sizing a 30B
        mixture-of-experts as that would have the fit gate admit a launch that
        runs out of memory.
        """
        found = model_sweep.resolve_corpus("qwen3-omni-30b-a3b")
        assert found.resolved
        assert found.shape[:2] == (48, 2048)
        assert found.experts == 128

    def test_qwen3_tts_resolves_and_is_still_refused(self):
        """Both halves matter. It resolves, so it renders and gets a fit
        verdict; and no runtime here claims it, which is the honest answer
        until somebody has run it through `control_plane/runtimes/tts.py`."""
        found = model_sweep.resolve_corpus("qwen3-tts-custom-voice")
        assert found.resolved and found.shape == (28, 2048, 16, 8)
        assert not found.servable

    def test_a_checkpoint_with_no_transformer_stays_refused(self):
        """`potion-base-8M` is a static embedding model: no attention at any
        depth. The search must not find something to be confident about."""
        found = model_sweep.resolve_corpus("potion-base-8m")
        assert not found.resolved
        assert "found no transformer stack" in found.reason

    def test_an_embedding_model_that_does_resolve_still_does(self):
        """The contrast that stops the case above from being read as "we do
        not do embedding models". Qwen3-Embedding is an ordinary decoder and
        resolves like one."""
        found = model_sweep.resolve_corpus("qwen3-embedding-0.6b")
        assert found.resolved and found.shape == (28, 1024, 16, 8)

    def test_the_tts_runtimes_own_architecture_is_servable(self):
        """`ArkttsModel` is the one checkpoint anybody has actually run
        through derate's own speech server, and it is the only thing the tts
        runtime should claim."""
        found = model_sweep.resolve_corpus("audio8-tts-preview-0.6b")
        assert found.resolved
        assert (found.runtimes or {}).get("tts") == "supported"


class TestDiffusionModels:
    """Block-diffusion text models, which are shaped unlike anything else here.

    `DiffusionGemmaForBlockDiffusion` is the only one the pinned image
    registers. It is worth its own class because it is the first checkpoint in
    this corpus whose *layers do not all have the same KV geometry*, and the
    fit gate has exactly one `num_kv_heads` and one `head_dim` to describe a
    model with.
    """

    CONFIG = "diffusiongemma-26b"

    def _text_config(self):
        return json.loads(
            (model_sweep.DATA / f"{self.CONFIG}.config.json").read_text()
        )["text_config"]

    def test_it_is_sized_by_its_text_stack_not_its_vision_tower(self):
        found = model_sweep.resolve_corpus(self.CONFIG)
        assert found.resolved
        assert found.shape[:2] == (30, 2816)

    def test_the_interleave_is_read_from_layer_types(self):
        """25 sliding layers to 5 full ones. Getting this wrong is the
        difference between caching 1024 tokens a layer and caching all
        262144 of them."""
        cfg = self._text_config()
        full = sum(1 for t in cfg["layer_types"] if t == "full_attention")
        fit = model_sweep.resolve_corpus(self.CONFIG).fit_fields
        assert fit["layers_with_full_attention"] == full == 5
        assert fit["sliding_window"] == 1024

    def test_the_experts_per_token_comes_from_the_config(self):
        """It says `top_k_experts: 8`. That spelling was not in
        `_EXPERT_TOPK_KEYS`, so the mapper fell through to its assumed 2 and
        under-stated active parameters four-fold -- while warning about a
        number the config stated plainly two lines away."""
        cfg = self._text_config()
        found = model_sweep.resolve_corpus(self.CONFIG)
        assert found.fit_fields["num_experts_per_token"] == cfg["top_k_experts"] == 8
        assert not any("experts-per-token" in w for w in _warnings_for(self.CONFIG))

    @pytest.mark.xfail(
        reason="ModelShape carries one num_kv_heads and one head_dim, and this "
               "model needs two. The five full-attention layers are "
               "num_global_key_value_heads=2 x global_head_dim=512, not "
               "8 x 256, so they are charged at twice their real rate -- and "
               "they are the layers charged at full context. Fixing it means "
               "adding fields to control_plane/contracts, which the README "
               "says is changed in one place and announced, never to unblock "
               "a test.",
        strict=True,
    )
    def test_the_global_layers_are_charged_at_their_own_kv_geometry(self):
        """The overcharge, in bytes, at this model's own context length.

        Not a rounding error and not conservative-in-a-safe-direction: it
        refuses launches that would have fit, which is the failure this whole
        corpus exists to catch.
        """
        from control_plane.contracts.model import ModelShape
        from control_plane.fit import kv

        cfg = self._text_config()
        shape = ModelShape(
            model_id="google/diffusiongemma-26B-A4B-it",
            num_layers=cfg["num_hidden_layers"],
            hidden_size=cfg["hidden_size"],
            num_attention_heads=cfg["num_attention_heads"],
            num_kv_heads=cfg["num_key_value_heads"],
            vocab_size=cfg["vocab_size"],
            total_params=26_000_000_000,
            dtype="bf16",
            head_dim=cfg["head_dim"],
            sliding_window=cfg["sliding_window"],
            layers_with_full_attention=sum(
                1 for t in cfg["layer_types"] if t == "full_attention"
            ),
        )
        context = 131072
        charged = kv.kv_cache_bytes(shape, context, 1, "bf16")

        full = shape.layers_with_full_attention
        windowed = shape.num_layers - full
        elem = 2
        truth = (
            2 * cfg["num_global_key_value_heads"] * cfg["global_head_dim"] * elem
            * full * context
            + 2 * cfg["num_key_value_heads"] * cfg["head_dim"] * elem
            * windowed * min(cfg["sliding_window"], context)
        )
        assert charged == pytest.approx(truth, rel=0.01), (
            f"charged {charged / 2**30:.2f} GiB against a true "
            f"{truth / 2**30:.2f} GiB -- {charged / truth:.2f}x too much"
        )


def _warnings_for(name: str) -> list[str]:
    from control_plane.resolver.config_map import map_config

    config = json.loads((model_sweep.DATA / f"{name}.config.json").read_text())
    return map_config(config).warnings


class TestTheSweepItself:
    """The reporter is a test tool; a broken one hides what it is measuring."""

    def test_a_hub_refusal_is_not_counted_as_unreadable(self):
        """A gated repository is a fact about the hub, not a bug in this
        build. Conflating the two buries the one signal that means derate is
        wrong under a list of models nobody has access to."""
        gated = model_sweep.Outcome("x/y", False, reason="401: gated repository")
        assert gated.hub_fault and not gated.unreadable

    def test_a_config_this_build_cannot_read_is_counted(self):
        broken = model_sweep.Outcome(
            "x/y", False, reason="config is missing required field(s): hidden_size"
        )
        assert broken.unreadable and not broken.hub_fault

    def test_resolved_but_refused_by_every_runtime_is_not_flagged(self):
        """It resolved. The support table then said no, which is it working."""
        found = model_sweep.Outcome(
            "x/y", True, shape=(1, 1, 1, 1),
            runtimes={"vllm": "unsupported", "sglang": "unsupported"},
        )
        assert not found.unreadable and not found.servable

    def test_a_provider_model_is_not_a_missing_shape(self):
        """`amazon/nova-lite-v1` is served over somebody's API and has no
        weights near this cluster. Counting it as unreadable would count 427
        of the catalogue's 516 rows as bugs."""
        found = model_sweep.Outcome(
            "amazon/nova-lite-v1", False, remote=True,
            reason="502: cannot read config.json (HTTP 401)",
        )
        assert not found.unreadable

    def test_the_same_id_offered_and_on_disk_is_local(self):
        """A model can be both offered by a provider and sitting on a disk
        here. It has a shape, so it is swept as a local model."""
        found = model_sweep.Outcome("x/y", False, remote=False, reason="broken config")
        assert found.unreadable

    def test_drift_is_falsey_when_there_is_none(self):
        assert not model_sweep.Drift([], [])
        assert model_sweep.Drift([], ["got better"])
        assert model_sweep.Drift(["got worse"], [])


@pytest.mark.slow
class TestAgainstTheImage:
    """The half the static tables cannot see.

    All of these need the pinned vLLM image on this machine, and reading it
    costs a container start on a cold cache -- which is why the class is
    `slow` and out of `pytest -m "not slow"`. Deselected, not skipped: the
    default suite says it did not run these rather than counting them green.
    `python3 -m tests.model_sweep --arch` is the same check on demand.

    When the image is genuinely absent they report that they could not look --
    never that they looked and agreed, which is the failure mode
    `ui/check.mjs` exists to prevent one directory over.
    """

    @pytest.fixture
    def divergence(self):
        found = model_sweep.architecture_divergence(
            cache_dir=model_sweep._default_probe_cache()
        )
        if found is None:
            pytest.skip(
                f"the vllm image ({model_sweep.vllm_image()}) is not readable "
                f"here, so nothing was compared"
            )
        return found

    def test_the_static_table_matches_the_image(self, divergence):
        """`VLLM_ARCHITECTURES` is a copy of a fact that lives in the image.
        This is the assertion that the copy is still true, and it fails in
        both directions because both are real: a name the image loads and the
        table refuses is a 400 on a servable model, and a name the table
        claims and the image cannot load clears every gate and dies at load.
        """
        assert not divergence, "\n" + divergence.render()

    def test_it_actually_compared_something(self, divergence):
        """A comparison over an empty registry agrees with everything."""
        assert divergence.agreed > 100

    def test_a_divergence_in_either_direction_is_reported(self):
        """Proof the check bites, without waiting for the image to move."""
        loads_but_refused = model_sweep.Divergence(
            "img", "1.0", agreed=250, refused_anyway=["Gemma4ForConditionalGeneration"],
            claimed_but_absent=[],
        )
        assert loads_but_refused
        assert "would have served" in loads_but_refused.render()

        claimed_but_gone = model_sweep.Divergence(
            "img", "1.0", agreed=250, refused_anyway=[],
            claimed_but_absent=["MllamaForConditionalGeneration"],
        )
        assert claimed_but_gone
        assert "dies at load" in claimed_but_gone.render()

        assert not model_sweep.Divergence("img", "1.0", 256, [], [])

    def test_a_shape_does_not_depend_on_the_installed_image(self):
        """The corpus again, with the image's registry recorded.

        The mapper never asks a runtime anything, so every shape must come out
        identical. If one does not, something has reached across a boundary it
        has no business crossing -- and it would do so on a coordinator and
        not in the static tests, which is the gap this closes.
        """
        found = model_sweep.compare_probed(cache_dir=model_sweep._default_probe_cache())
        if found is None:
            pytest.skip(f"the vllm image ({model_sweep.vllm_image()}) is not readable here")
        problems, _gained = found
        assert not problems, "\n" + "\n".join(problems)
