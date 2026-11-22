"""A launch runs the image built for the machine it lands on.

The DGX Spark images here are arm64-only; an x86 machine joined to the same
cluster has to get an amd64 image or its launch dies at `exec format error`
after every gate said yes.
"""

from __future__ import annotations

import dataclasses

from control_plane.deploy import flags
from control_plane.deploy.recipes import synthesize
from control_plane.registry.probe import local_arch, local_infiniband
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


def test_a_runtime_with_no_image_for_the_machine_is_refused_by_name(monkeypatch):
    monkeypatch.setitem(
        flags.RUNTIMES, "sglang", dataclasses.replace(flags.RUNTIMES["sglang"], arch_images=())
    )
    reason = flags.arch_refusal("sglang", "rtx", "amd64")
    assert reason is not None and "'rtx' is amd64" in reason and "DERATE_SGLANG_IMAGE" in reason
    # A multi-arch image runs anywhere.
    assert flags.arch_refusal("llamacpp", "rtx", "amd64") is None


def test_every_gpu_runtime_has_an_amd64_image_that_clears_its_entrypoint(monkeypatch):
    """sglang and tts were refused on the RTX box for want of an amd64 image.
    Both amd64 images inherit an ENTRYPOINT (NVIDIA's wrapper; upstream
    vLLM's `vllm serve` under the tts build), and sparkrun starts the
    container with `sleep infinity` -- under `vllm serve` that is a dead
    container, so each must be cleared."""
    for runtime in ("vllm", "sglang", "tts"):
        spec = flags.RUNTIMES[runtime]
        monkeypatch.delenv(spec.default_image_env, raising=False)
        for _, var, _, _ in spec.arch_images:
            monkeypatch.delenv(var, raising=False)
        assert flags.arch_refusal(runtime, "rtx", "amd64") is None, runtime
        image = flags.image_for_arch(spec, "amd64")
        assert image and image != spec.default_image, runtime
        assert ("entrypoint", "") in flags.image_executor_config(spec, image), runtime


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


def test_infiniband_survives_the_wire_and_the_roster_file_and_is_unknown_when_absent():
    """The join payload and registry.json are both profile_to_dict/from_dict,
    through JSON. Dropping False on the way would put `--device
    /dev/infiniband` back on a machine docker refuses it on."""
    import json

    for value in (True, False, None):
        profile = dataclasses.replace(fx.WS_3090, infiniband=value)
        wire = json.loads(json.dumps(profile_to_dict(profile)))
        assert profile_from_dict(wire).infiniband is value
    base = profile_to_dict(fx.WS_3090)
    assert profile_from_dict({k: v for k, v in base.items() if k != "infiniband"}).infiniband is None
    # A peer's "false" string is not evidence either way.
    assert profile_from_dict({**base, "infiniband": "false"}).infiniband is None


def test_infiniband_is_read_off_sysfs_so_an_agent_in_a_container_still_sees_it(tmp_path):
    verbs, dev = tmp_path / "infiniband_verbs", tmp_path / "dev-infiniband"
    assert local_infiniband(verbs, dev) is False
    verbs.mkdir()
    assert local_infiniband(verbs, dev) is False  # the class exists, no device in it
    (verbs / "uverbs0").mkdir()
    assert local_infiniband(verbs, dev) is True
    assert local_infiniband(tmp_path / "absent", tmp_path) is True  # the device dir itself


def test_every_probe_stamps_its_infiniband_answer_and_a_change_is_named(monkeypatch):
    """`probe_local` is what an agent reports at join and on every re-probe,
    so the answer has to ride it; and a refresh that finds the device gone
    (or newly there) says so by name, like any hardware change."""
    from control_plane.registry import probe as probe_mod
    from control_plane.registry.profiles import profile_diff

    monkeypatch.setattr(probe_mod, "_host_bw", [None])  # no memory copy in a unit test
    for present in (True, False):
        monkeypatch.setattr(probe_mod, "local_infiniband", lambda present=present: present)
        rtx = probe_mod.probe_local(
            node_id="ws", hostname="ws", address="10.0.0.50",
            rows=[["NVIDIA GeForce RTX 3090", "24576", "550.54", "8.6"]],
        )
        assert rtx.infiniband is present
    after = dataclasses.replace(fx.WS_3090, infiniband=False)
    assert profile_diff(fx.WS_3090, after) == {"infiniband": {"from": None, "to": False}}


def test_a_node_row_carries_the_arch_a_sweep_keys_by_and_the_host_bandwidth(monkeypatch):
    """`/api/cluster`'s node row is what `spec_sweep.node_profile` reads: its
    `arch` picks which image version a record is filed under, and its host
    bandwidth is the figure a host-pool decode is priced at."""
    from control_plane.gateway import serialize
    from tests import spec_sweep

    rtx = dataclasses.replace(fx.WS_3090, arch="amd64", host_memory_bandwidth_gbps=46.3)
    row = serialize.node_payload(fx.node_state(rtx), None, "")
    assert row["host_memory_bandwidth_gbps"] == 46.3
    rows = [{"name": "vllm", "image_versions": {"amd64": "0.31.0", "arm64": "0.28.1rc1"}}]
    monkeypatch.setattr(spec_sweep, "_call", lambda base, path, **_kw: rows)
    assert spec_sweep.runtime_version("http://x", row) == "0.31.0"
    old_agent = serialize.node_payload(fx.node_state(fx.WS_3090), None, "")
    assert old_agent["arch"] == "" and old_agent["host_memory_bandwidth_gbps"] is None


def test_host_bandwidth_survives_the_wire_and_the_roster_file_and_is_unmeasured_when_absent():
    """The join payload and registry.json are both profile_to_dict/from_dict.
    Anything but a positive real number from a peer is "not measured", never
    0 -- a 0 would read as a machine that cannot move memory."""
    import json

    from control_plane.registry.profiles import profile_diff

    for value in (46.3, None):
        profile = dataclasses.replace(fx.WS_3090, host_memory_bandwidth_gbps=value)
        wire = json.loads(json.dumps(profile_to_dict(profile)))
        assert profile_from_dict(wire).host_memory_bandwidth_gbps == value
    base = profile_to_dict(fx.WS_3090)
    assert profile_from_dict(
        {k: v for k, v in base.items() if k != "host_memory_bandwidth_gbps"}
    ).host_memory_bandwidth_gbps is None
    for junk in ("46.3", True, 0, -5.0, float("inf"), [46.3]):
        assert profile_from_dict(
            {**base, "host_memory_bandwidth_gbps": junk}
        ).host_memory_bandwidth_gbps is None, junk
    assert profile_from_dict({**base, "host_memory_bandwidth_gbps": 47}).host_memory_bandwidth_gbps == 47.0
    # A re-probe that moved it says so by name.
    after = dataclasses.replace(fx.WS_3090, host_memory_bandwidth_gbps=46.3)
    assert profile_diff(fx.WS_3090, after) == {
        "host_memory_bandwidth_gbps": {"from": None, "to": 46.3}
    }


def test_host_bandwidth_is_measured_by_copying_memory_and_bounded_by_what_is_free():
    from control_plane.registry.probe import measure_host_bandwidth

    mib = 2**20
    got = measure_host_bandwidth(buffer=4 * mib, threads=(1, 2), available=10**12)
    assert got is not None and got > 0
    # An eighth of what is free must hold one thread's two buffers, or it
    # does not run at all rather than pushing a serving box into swap.
    assert measure_host_bandwidth(buffer=4 * mib, available=8 * 8 * mib - 1) is None
    assert measure_host_bandwidth(buffer=4 * mib, available=8 * 8 * mib) is not None
    assert measure_host_bandwidth(buffer=4 * mib, max_bytes=8 * mib - 1, available=10**12) is None


def test_the_default_host_bandwidth_measurement_is_fast(monkeypatch):
    """It runs inside the agent's probe: well under two seconds."""
    import time

    from control_plane.registry.probe import measure_host_bandwidth

    started = time.perf_counter()
    measure_host_bandwidth(available=8 * 2**30)
    assert time.perf_counter() - started < 2.0


def test_host_bandwidth_is_measured_once_and_only_where_a_host_pool_runtime_decodes(monkeypatch):
    """A CPU node and a discrete box get it; a GB10 (whose pool bandwidth is
    already the planning figure), a Mac and an unknown machine do not. Once
    per process: the probe re-runs on a timer, and a second reading would
    publish noise as a hardware change and copy memory on a serving box."""
    from control_plane.contracts import DeviceClass
    from control_plane.registry import probe as probe_mod

    calls = []

    def measure():
        calls.append(1)
        return 46.3

    monkeypatch.setattr(probe_mod, "_host_bw", [])
    for cls in (DeviceClass.GB10, DeviceClass.APPLE, DeviceClass.UNKNOWN):
        assert probe_mod.local_host_bandwidth(cls, measure) is None
    assert not calls
    assert probe_mod.local_host_bandwidth(DeviceClass.DISCRETE, measure) == 46.3
    assert probe_mod.local_host_bandwidth(DeviceClass.CPU, measure) == 46.3
    assert len(calls) == 1

    # Stamped on whatever branch the probe took.
    rtx = probe_mod.probe_local(
        node_id="ws", hostname="ws", address="10.0.0.50",
        rows=[["NVIDIA GeForce RTX 3090", "24576", "550.54", "8.6"]],
    )
    assert rtx.host_memory_bandwidth_gbps == 46.3
    gb10 = probe_mod.probe_local(
        node_id="spark", hostname="spark", address="10.0.0.11",
        rows=[["NVIDIA GB10", "[N/A]", "580.95.05", "12.1"]],
    )
    assert gb10.host_memory_bandwidth_gbps is None

    # A failure is "not measured", and is not retried every re-probe either.
    monkeypatch.setattr(probe_mod, "_host_bw", [])
    assert probe_mod.local_host_bandwidth(DeviceClass.CPU, lambda: 1 / 0) is None
    assert probe_mod.local_host_bandwidth(DeviceClass.CPU, measure) is None
