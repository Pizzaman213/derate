"""A GGUF's weight figure is the sum of its tensors' block-quantized sizes, so
a ggml type this reader does not know is a wrong number on every card and in
the fit gate. NVFP4 (type 40) was priced as F32 and put Inkling 2 Mini's
15.5 GiB Q4_K_M at 80.7 GiB -- and its architecture was missing from the
llama.cpp table, so no runtime offered it a Serve button at all."""

from __future__ import annotations

import io
import struct

import pytest

from control_plane.resolver import support
from control_plane.resolver.gguf import FileReader, read_header


def _one_tensor(ggml_type: int, dims: tuple[int, ...]) -> bytes:
    name = b"blk.0.ffn_gate_up_exps.weight"
    return (
        b"GGUF" + struct.pack("<IQQ", 3, 1, 0)
        + struct.pack("<Q", len(name)) + name
        + struct.pack("<I", len(dims)) + struct.pack(f"<{len(dims)}Q", *dims)
        + struct.pack("<IQ", ggml_type, 0)
    )


# Upstream ggml.h's enum and ggml-common.h's static_asserts.
@pytest.mark.parametrize(
    "ggml_type, name, block, size",
    [(40, "NVFP4", 64, 36), (41, "Q1_0", 128, 18), (42, "Q2_0", 64, 18), (43, "F8_E4M3", 1, 1)],
)
def test_a_tensor_weighs_its_own_block_size_never_the_f32_fallback(ggml_type, name, block, size):
    # Inkling 2 Mini's own expert tensor shape; NVFP4 is what it ships in.
    dims = (2304, 2304, 128)
    header = read_header(FileReader(io.BytesIO(_one_tensor(ggml_type, dims))))
    assert header.tensors[0].type_name == name
    assert header.total_bytes == 2304 * 2304 * 128 // block * size


def test_an_inkling2_loop_gguf_is_llamacpp_and_its_checkpoint_is_not_vllm():
    levels = lambda arch, dtype: {  # noqa: E731
        r.runtime: r.level for r in support.build_verdict((arch,), dtype).runtimes
    }
    assert levels("inkling2_loop", "q4_k_m")["llamacpp"] is support.SupportLevel.SUPPORTED
    checkpoint = levels("Inkling2LoopForConditionalGeneration", "bf16")
    assert checkpoint["vllm"] is support.SupportLevel.UNSUPPORTED
    assert checkpoint["sglang"] is support.SupportLevel.UNSUPPORTED
