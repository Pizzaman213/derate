"""A launch runs the image built for the machine it lands on.

The DGX Spark images here are arm64-only; an x86 machine joined to the same
cluster has to get an amd64 image or its launch dies at `exec format error`
after every gate said yes.
"""

from __future__ import annotations

import dataclasses

from control_plane.deploy import flags
from control_plane.deploy.recipes import synthesize
from control_plane.registry.probe import local_arch
from control_plane.registry.serde import profile_from_dict, profile_to_dict
from tests import fixtures as fx

VLLM = flags.RUNTIMES["vllm"]


def test_vllm_gets_upstream_on_amd64_and_the_spark_image_on_arm64(monkeypatch):
    monkeypatch.delenv("DERATE_VLLM_IMAGE", raising=False)
    monkeypatch.delenv("DERATE_VLLM_IMAGE_AMD64", raising=False)
    assert flags.image_for_arch(VLLM, "amd64") == "vllm/vllm-openai:latest"
    assert flags.image_for_arch(VLLM, "arm64") == VLLM.default_image


def test_an_agent_that_reports_no_arch_keeps_todays_image(monkeypatch):
    monkeypatch.delenv("DERATE_VLLM_IMAGE", raising=False)
    assert flags.image_for_arch(VLLM, "") == VLLM.default_image
    assert flags.arch_refusal("sglang", "old-agent", "") is None


def test_each_architecture_has_its_own_override(monkeypatch):
    monkeypatch.setenv("DERATE_VLLM_IMAGE", "example/arm:1")
    monkeypatch.setenv("DERATE_VLLM_IMAGE_AMD64", "example/x86:1")
    assert flags.image_for_arch(VLLM, "arm64") == "example/arm:1"
    assert flags.image_for_arch(VLLM, "amd64") == "example/x86:1"


def test_a_runtime_with_no_image_for_the_machine_is_refused_by_name():
    reason = flags.arch_refusal("sglang", "rtx", "amd64")
    assert reason is not None and "'rtx' is amd64" in reason and "DERATE_SGLANG_IMAGE" in reason
    # A multi-arch image runs anywhere.
    assert flags.arch_refusal("llamacpp", "rtx", "amd64") is None


def test_upstream_vllm_clears_its_entrypoint_and_the_spark_image_keeps_its_own(monkeypatch, tmp_path):
    """Upstream's ENTRYPOINT is `vllm serve`; sparkrun starts the container
    with `sleep infinity`, which would become `vllm serve sleep infinity`."""
    monkeypatch.delenv("DERATE_VLLM_IMAGE", raising=False)
    monkeypatch.delenv("DERATE_VLLM_IMAGE_AMD64", raising=False)
    plan = fx.single_node_plan(fx.WS_3090.node_id)
    common = dict(port=8100, gpu_memory_utilization=0.8, recipe_dir=tmp_path)
    x86 = synthesize(
        fx.LLAMA_3_3_70B, plan, "vllm", 8192, 4, "llama",
        image=flags.image_for_arch(VLLM, "amd64"), **common,
    ).content
    arm = synthesize(fx.LLAMA_3_3_70B, plan, "vllm", 8192, 4, "llama", **common).content
    assert 'entrypoint: ""' in x86 and "container: vllm/vllm-openai:latest" in x86
    assert "entrypoint" not in arm


def test_arch_from_a_peer_is_one_of_two_names_or_unknown():
    base = profile_to_dict(fx.WS_3090)
    assert profile_from_dict({**base, "arch": "amd64"}).arch == "amd64"
    assert profile_from_dict({**base, "arch": "s390x"}).arch == ""
    assert profile_from_dict({k: v for k, v in base.items() if k != "arch"}).arch == ""
    assert profile_from_dict(profile_to_dict(dataclasses.replace(fx.SPARK_01, arch="arm64"))).arch == "arm64"


def test_the_kernels_names_map_to_image_platforms():
    assert local_arch("x86_64") == "amd64"
    assert local_arch("aarch64") == "arm64"
    assert local_arch("riscv64") == ""
