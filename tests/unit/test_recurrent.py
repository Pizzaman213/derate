"""Recurrent layers read off a GGUF header and a config.json.

The per-family byte counts for configs are pinned by the corpus
(`tests/resolver_data/EXPECTED.json`), each one checked against the pinned
vLLM image's own `get_mamba_state_shape_from_config` when it was recorded.
What is here is the GGUF side, which the corpus does not reach, and the rules
that decide which layers are charged at all.
"""

from __future__ import annotations

import json

from control_plane.resolver import recurrent
from control_plane.resolver.config_map import map_config
from control_plane.resolver.gguf import GGUFHeader
from tests import model_sweep


def _header(arch: str, **keys) -> GGUFHeader:
    meta = {"general.architecture": arch}
    meta.update({f"{arch}.{k}": v for k, v in keys.items()})
    return GGUFHeader(version=3, tensor_count=0, metadata=meta, tensors=[])


def _corpus(name: str) -> dict:
    return json.loads((model_sweep.DATA / f"{name}.config.json").read_text())


def test_a_qwen3next_gguf_without_an_interval_uses_llamacpps_own_period():
    """qwen3next GGUFs carry no full_attention_interval; llama.cpp then marks
    every 4th layer full attention. Without the default all 48 layers were
    charged a full-context cache. And the GGUF must price the same layers the
    safetensors build does, at float32 -- what llama.cpp holds them in."""
    header = _header(
        "qwen3next",
        **{
            "ssm.conv_kernel": 4, "ssm.state_size": 128, "ssm.group_count": 16,
            "ssm.time_step_rank": 32, "ssm.inner_size": 4096,
        },
    )
    found = recurrent.from_gguf(header, 48, 2048)
    base = map_config(_corpus("qwen3-next-80b-a3b"))

    assert found is not None
    assert found.attention_free_layers == base.attention_free_layers == 36
    # The base checkpoint stores both states in bf16; llama.cpp in float32.
    assert found.state_bytes == 2 * base.recurrent_state_bytes


def test_a_nemotron_h_mlp_block_is_neither_mamba_nor_attention():
    """llama.cpp's rule: a block with no KV heads is Mamba only if it also has
    no feed-forward. Counting MLP blocks as Mamba doubles the state."""
    kv = [0, 0, 2, 0]
    ff = [0, 1856, 0, 0]
    header = _header(
        "nemotron_h_moe",
        **{
            "attention.head_count_kv": kv, "feed_forward_length": ff,
            "ssm.conv_kernel": 4, "ssm.state_size": 128, "ssm.group_count": 8,
            "ssm.time_step_rank": 64, "ssm.inner_size": 4096,
        },
    )
    found = recurrent.from_gguf(header, 4, 2688)
    per_layer = (3 * (4096 + 2 * 8 * 128) + 128 * 4096) * 4

    assert found is not None
    assert found.attention_free_layers == 3
    assert found.state_bytes == 2 * per_layer


def test_an_unrecognised_layer_type_is_charged_as_full_attention():
    """Only a type that names a window is windowed, and only a recurrent
    family this build can size is charged as state. Anything else keeps a
    full-context cache -- the over-count in the refusing direction, never the
    4096-token stand-in that used to under-price it."""
    cfg = _corpus("qwen3-next-80b-a3b")
    cfg["layer_types"] = ["full_attention", "mystery"] * 24
    del cfg["linear_num_value_heads"]  # no longer a recognisable GDN config

    mapped = map_config(cfg)

    assert mapped.attention_free_layers == 0
    assert mapped.recurrent_state_bytes == 0
    assert mapped.sliding_window is None
