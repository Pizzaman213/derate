"""The flag catalogue: what the engine says it accepts, and what derate sends.

Hermetic. The catalogue under ``tests/fixtures/engine_flags/`` was produced by
``flagcatalog.probe()`` against the pinned image and is checked in for the
reason ``resolver_data/EXPECTED.json`` is: the probe needs a 24 GB container
and a GPU, and a test that needs those is a test nobody runs.

Regenerate it when the pinned image moves -- the version is recorded in the
fixture, so a stale one is visible rather than silent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from control_plane.deploy.flags import VLLM_KV_CACHE_DTYPES
from control_plane.engines import vllm
from control_plane.engines.flagcatalog import (
    Catalogue,
    flag_name,
    flag_refusal,
    parse_probe_output,
)

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "engine_flags" / "vllm.json"


@pytest.fixture(scope="module")
def vllm_flags() -> Catalogue:
    return Catalogue.from_payload(json.loads(_FIXTURE.read_text()))


def test_the_image_declares_far_more_flags_than_derate_emits(vllm_flags):
    """The measurement this whole module exists because of.

    Not an arbitrary floor: it is here so that a catalogue which silently
    collapsed to a handful of flags -- a probe half-failing, a parser that
    raised partway -- fails instead of quietly making every real flag
    "unknown" and refusing a launch that was fine.
    """
    assert len(vllm_flags.flags) > 250
    assert len(vllm_flags.names) > len(vllm_flags.flags)  # aliases exist


def test_every_flag_derate_emits_is_one_the_image_declares(vllm_flags):
    """The `--arch` sweep's argument, applied to flags.

    A flag in our template that the image does not have is a launch that
    clears every gate and dies at argv parsing.
    """
    import re

    emitted = set(re.findall(r"(?<![\w-])--[a-z0-9][a-z0-9-]*", vllm.SPEC.command_template))
    for attr in ("expert_parallel_arg", "kv_cache_bytes_arg", "speculative_config_arg",
                 "enforce_eager_arg", "cudagraph_capture_sizes_arg",
                 "kv_cache_dtype_arg", "quantization_arg"):
        value = getattr(vllm.SPEC, attr)
        if value:
            emitted |= set(re.findall(r"(?<![\w-])--[a-z0-9][a-z0-9-]*", value))
    emitted.add(vllm.SPEC.served_name_arg)

    assert emitted, "found no flags in the template; the regex stopped matching"
    assert emitted <= vllm_flags.names, sorted(emitted - vllm_flags.names)


def test_the_kv_dtype_table_still_matches_the_image(vllm_flags):
    """`VLLM_KV_CACHE_DTYPES` was read off the image's own `CacheConfig`.

    This asks the parser instead, which is a different surface reaching the
    same answer -- so the two disagreeing means one of them has drifted.
    """
    declared = vllm_flags.get("--kv-cache-dtype")
    assert declared is not None and declared.choices
    assert set(declared.choices) == set(VLLM_KV_CACHE_DTYPES)


def test_a_switch_is_not_recorded_as_taking_a_value(vllm_flags):
    """vLLM spells switches with a paired custom action, not `store_true`.

    Reading only the action class name records `--enforce-eager` as
    value-taking and lets `--enforce-eager=yes` through to a launch that exits
    on it. `nargs == 0` is what actually settles it.
    """
    eager = vllm_flags.get("--enforce-eager")
    assert eager is not None
    assert eager.takes_value is False
    assert "--no-enforce-eager" in eager.names


class TestRefusals:
    def test_an_unknown_flag_is_refused_and_named(self, vllm_flags):
        said = flag_refusal(vllm_flags, ["--gpu-mem-util", "0.9"], runtime="vllm")
        assert said is not None
        assert "--gpu-mem-util" in said
        assert "--gpu-memory-utilization" in said  # the suggestion

    def test_a_typo_finds_its_flag(self, vllm_flags):
        said = flag_refusal(vllm_flags, ["--tensor-paralel-size", "2"], runtime="vllm")
        assert said is not None and "--tensor-parallel-size" in said

    def test_an_invented_flag_gets_no_suggestion(self, vllm_flags):
        """A wrong suggestion is worse than none, so silence is allowed."""
        said = flag_refusal(vllm_flags, ["--completely-invented"], runtime="vllm")
        assert said is not None
        assert "Did you mean" not in said

    def test_a_value_on_a_switch_is_refused(self, vllm_flags):
        said = flag_refusal(vllm_flags, ["--enforce-eager=yes"], runtime="vllm")
        assert said is not None and "takes no value" in said

    def test_a_value_outside_the_parsers_choices_is_refused(self, vllm_flags):
        said = flag_refusal(vllm_flags, ["--kv-cache-dtype=fp9"], runtime="vllm")
        assert said is not None
        assert "fp9" in said and "fp8" in said

    def test_good_flags_are_not_refused(self, vllm_flags):
        assert flag_refusal(
            vllm_flags,
            ["--max-model-len", "4096", "--enforce-eager", "--kv-cache-dtype=fp8"],
            runtime="vllm",
        ) is None

    def test_an_unreadable_catalogue_refuses_nothing(self):
        """Absence is "could not be asked", never "has no flags".

        The mistake `SparkrunAdapter.is_running` was rewritten to stop making:
        a missing image must not read as a bad request.
        """
        assert flag_refusal(None, ["--anything-at-all"], runtime="vllm") is None


class TestParsing:
    @pytest.mark.parametrize(
        "token,expected",
        [
            ("--max-model-len", "--max-model-len"),
            ("--max-model-len=4096", "--max-model-len"),
            ("-tp", "-tp"),
            ("4096", None),
            ("--", None),
            ("", None),
        ],
    )
    def test_flag_name(self, token, expected):
        assert flag_name(token) == expected

    def test_probe_output_is_found_amid_the_engines_own_chatter(self):
        noisy = (
            "INFO 09-11 22:05:01 [__init__.py:112] Registered model loader\n"
            'derate-flagprobe:{"engine":"x","version":"1","flags":'
            '[{"names":["--a"],"takes_value":false}]}\n'
        )
        cat = parse_probe_output(noisy)
        assert cat is not None and cat.engine == "x"
        assert cat.get("--a").takes_value is False

    def test_output_without_a_marker_is_none(self):
        assert parse_probe_output("nothing useful here") is None
        assert parse_probe_output("") is None

    def test_a_corrupt_payload_is_none_rather_than_an_exception(self):
        assert parse_probe_output("derate-flagprobe:{not json") is None
