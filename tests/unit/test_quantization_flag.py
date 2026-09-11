"""Forcing a weight quantization: the flag, and what refuses it.

`contracts/quant.py` prices 33 schemes and `/api/plan` has always taken a
`dtype` override that re-prices the weights with one. The launch route refused
the field outright, and the reason given was correct at the time: neither serve
command carried `--quantization`, so honouring one would have budgeted 4-bit
weights and started 16-bit ones -- the precise out-of-memory kill the fit gate
exists to refuse.

Both templates carry it now. What replaces the blanket refusal is the rule
`kv_cache_dtype_refusal` already follows -- refuse, never drop -- and it
matters more here than there: `BYTES_PER_PARAM` differs by 3.5x between bf16
and nvfp4, against 2x for a cache width.
"""

from __future__ import annotations

import pytest

from control_plane.contracts.quant import BYTES_PER_PARAM, quant_info
from control_plane.deploy.flags import (
    VLLM_QUANTIZATION_METHODS,
    forceable_quantizations,
    quantization_refusal,
    render_quantization,
    runtime_spec,
)
from control_plane.deploy.recipes import _serve_command
from tests.fixtures import single_node_plan



class TestForcingAWeightQuantization:
    """`--quantization` exists now, and it is a FIT-GATE input.

    `contracts/quant.py` prices 33 schemes and `/api/plan` has always taken a
    `dtype` override that re-prices the weights with one. The launch route
    refused the field outright, because neither serve command carried the
    flag -- so honouring it would have budgeted 4-bit weights and started
    16-bit ones.

    Both templates carry it now. The rule that replaces the blanket refusal is
    the one `kv_cache_dtype_refusal` already follows, and it matters more
    here: `BYTES_PER_PARAM` differs by 3.5x between bf16 and nvfp4, against
    2x for a cache width.
    """

    def test_the_unquantized_widths_render_no_flag(self):
        """Choosing bf16 is a real answer -- "serve this as it ships" -- and
        it must not put a --quantization on the command."""
        for dtype in ("fp32", "fp16", "bf16"):
            assert render_quantization(dtype) is None

    def test_absence_renders_no_flag(self):
        for dtype in (None, "", "auto", "model", "none"):
            assert render_quantization(dtype) is None

    def test_a_scheme_maps_to_the_name_the_image_registry_holds(self):
        """derate's key is not vLLM's name. `nvfp4` is `modelopt_fp4` there,
        and that mapping was read off the pinned image's own
        QUANTIZATION_METHODS rather than out of documentation."""
        assert render_quantization("nvfp4") == "modelopt_fp4"
        assert render_quantization("awq_int4") == "awq"
        assert render_quantization("gptq_int4") == "gptq"
        assert render_quantization("fp8") == "fp8"
        assert render_quantization("mxfp4") == "mxfp4"

    def test_every_rendered_name_is_one_the_image_can_load(self):
        """The evidence rule: a name the table claims and the image cannot
        load clears every gate and dies at load."""
        for dtype in forceable_quantizations():
            rendered = render_quantization(dtype)
            assert rendered is None or rendered in VLLM_QUANTIZATION_METHODS

    def test_a_gguf_scheme_raises_rather_than_rendering_nothing(self):
        """Silently rendering no flag is the `--kv-cache-dtype` bug with a
        bigger term: the gate has already priced the weights at 4.9 bits and
        the engine would load the checkpoint's own 16."""
        with pytest.raises(ValueError) as caught:
            render_quantization("q4_k_m")
        assert "different repository" in str(caught.value)

    def test_nf4_is_refused_because_the_image_has_no_loader_for_it(self):
        """Not an oversight. `bitsandbytes` is absent from the pinned image's
        registry -- read, not assumed -- so mapping nf4 onto it would be a
        launch that clears every gate and dies."""
        with pytest.raises(ValueError):
            render_quantization("nf4")
        assert "bitsandbytes" not in VLLM_QUANTIZATION_METHODS

    def test_only_schemes_a_runtime_can_serve_are_offered(self):
        """Never all 33. The GGUF ladder is llama.cpp's format and
        `resolver.py` already marks every GGUF variant unlaunchable, so
        offering one would be offering a launch that cannot start."""
        offered = set(forceable_quantizations())
        assert not any(quant_info(d).family == "gguf" for d in offered)
        assert offered == {
            "fp32", "fp16", "bf16", "fp8", "awq_int4", "gptq_int4",
            "nvfp4", "mxfp4",
        }

    def test_the_ladder_is_ordered_by_cost(self):
        offered = forceable_quantizations()
        widths = [BYTES_PER_PARAM[d] for d in offered]
        assert widths == sorted(widths, reverse=True)

    def test_vllm_and_sglang_are_told_but_tts_refuses(self):
        """tts loads through transformers in the checkpoint's own dtype --
        there is no quantization path at all, so it refuses rather than
        dropping the flag."""
        assert quantization_refusal("vllm", "nvfp4") is None
        assert quantization_refusal("sglang", "nvfp4") is None
        refusal = quantization_refusal("tts", "nvfp4")
        assert refusal and "silently not reach" in refusal

    def test_no_runtime_refuses_an_absent_scheme(self):
        for runtime in ("vllm", "sglang", "tts"):
            assert quantization_refusal(runtime, None) is None
            assert quantization_refusal(runtime, "") is None

    def test_the_flag_reaches_the_rendered_command(self):
        """The whole point. A knob whose recipe_key never lands in the
        template is dropped in silence, which is what this test exists to
        stop happening again."""
        spec = runtime_spec("vllm")
        command = _serve_command(
            spec, single_node_plan("spark-01"), (), quantization="modelopt_fp4"
        )
        assert "--quantization {quantization}" in command

    def test_no_flag_lands_when_nothing_was_forced(self):
        spec = runtime_spec("vllm")
        command = _serve_command(spec, single_node_plan("spark-01"), (), quantization=None)
        assert "--quantization" not in command
