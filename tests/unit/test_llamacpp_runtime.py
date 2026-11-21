"""The CPU runtime: what reaches llama-server, and what comes back.

Sibling to `test_tts_runtime.py`, and here for the same reason that file gives:
a runtime is a set of seams between this control plane and somebody else's
program, every seam is a string, and nothing type-checks a string. What is
different about this one is that the program on the other side runs on a
machine with no GPU, so it also has to be the first runtime whose PLACEMENT is
a decision rather than an assumption.

Every literal asserted below was read off the pinned image rather than written
from documentation -- `ghcr.io/ggml-org/llama.cpp:server`, version 0.4.0-dev
build 10902, aarch64, 2026-09-11. Where a test cites an observation it says
which one.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from control_plane.contracts import (
    DeviceClass,
    ModelShape,
    NodeProfile,
    ParallelismKind,
    ParallelismPlan,
)
from control_plane.deploy import recipes
from control_plane.deploy.adopt import parse_serve_command
from control_plane.deploy.flags import (
    RUNTIMES,
    SUPPORTED_RUNTIMES,
    budget_profile,
    is_hub_gguf_blob,
    llamacpp_model_refusal,
    llamacpp_model_spec,
    placement_refusal,
    runtime_spec,
    sharding_refusal,
)

GGUF_REF = "hf://unsloth/Qwen3-0.6B-GGUF/Qwen3-0.6B-Q4_K_M.gguf"
#: The SAME blob as GGUF_REF, spelled the way it comes back out of a resolve.
#: `ModelResolver.resolve_gguf_full` sets `name = f"{repo}/{filename}"`, so the
#: scheme is gone by the time `manager.launch` and `recipes.synthesize` see it
#: -- and both of them are what call the two functions below.
RESOLVED_GGUF_REF = "unsloth/Qwen3-0.6B-GGUF/Qwen3-0.6B-Q4_K_M.gguf"


def _shape(model_id: str = GGUF_REF) -> ModelShape:
    return ModelShape(
        model_id=model_id,
        num_layers=28,
        hidden_size=1024,
        num_attention_heads=16,
        num_kv_heads=8,
        vocab_size=151936,
        total_params=600_000_000,
        dtype="q4_k_m",
    )


def _plan(nodes: list[str] | None = None, tp: int = 1, pp: int = 1) -> ParallelismPlan:
    return ParallelismPlan(
        kind=ParallelismKind.SINGLE_NODE,
        tensor_parallel=tp,
        pipeline_parallel=pp,
        expert_parallel=1,
        data_parallel=1,
        node_ids=nodes or ["connor-pi"],
        reason="one machine holds it",
        measured_link_gbps=None,
        rejected=[],
    )


def _recipe(tmp_path: Path, *, context: int = 4096, seqs: int = 2, **kw):
    return recipes.synthesize(
        _shape(kw.pop("model_id", GGUF_REF)),
        kw.pop("plan", _plan()),
        "llamacpp",
        context,
        seqs,
        "qwen3-0.6b",
        port=8100,
        gpu_memory_utilization=0.9,
        recipe_dir=tmp_path,
        **kw,
    )


# ---- the runtime exists, once ------------------------------------------------


def test_the_runtime_is_registered_under_one_name():
    assert "llamacpp" in SUPPORTED_RUNTIMES
    assert runtime_spec("llamacpp").sparkrun_runtime == "llama-cpp"


def test_it_names_a_sparkrun_plugin_that_exists_rather_than_borrowing_one():
    """`tts` sets `sparkrun_runtime="vllm"` because sparkrun has no plugin for
    it and we cannot add one to somebody else's binary. This one does not have
    to: sparkrun ships `runtimes/llama_cpp.py` with `runtime_name =
    "llama-cpp"`, which renders the recipe's own `command:` verbatim and
    additionally resolves the GGUF into the container cache before exec.

    Asserted as a distinct fact from the name itself because the two are easy
    to conflate and the consequence of being wrong is a launch that reports
    `unknown runtime` from sparkrun rather than anything derate says.
    """
    assert runtime_spec("llamacpp").sparkrun_runtime != "vllm"


# ---- the context trap --------------------------------------------------------


def test_the_context_flag_carries_the_whole_pool_not_one_sequence(tmp_path):
    """The quietest failure available in this runtime, and the reason
    `RuntimeSpec.context_is_shared_pool` exists.

    llama.cpp divides `--ctx-size` by its slot count. Measured on the pinned
    image: `--ctx-size 2048 --parallel 2` prints `n_slots = 2, n_ctx_slot =
    1024`, and `--ctx-size 8192 --parallel 4` prints `n_ctx_slot = 2048`.
    derate's `context_length` is the per-sequence number -- it is what the fit
    gate approves and what the Verdict card puts on screen -- so handing the
    flag that number directly would serve every request a FRACTION of the
    approved window, with nothing anywhere saying so.

    Nothing errors when this is wrong. The launch comes up READY and the
    deployment record, the card and the gate all agree with each other and
    disagree with the server.
    """
    body = _recipe(tmp_path, context=4096, seqs=2).content

    assert "ctx_size: 8192" in body
    # `max_model_len` keeps meaning what it means everywhere else in derate.
    # Rewriting it instead would have made the deployment record and the
    # adoption parser disagree with the card.
    assert "max_model_len: 4096" in body
    assert "parallel: 2" in body
    assert "--ctx-size {ctx_size}" in body


@pytest.mark.parametrize("context,seqs", [(4096, 1), (8192, 4), (1024, 16)])
def test_the_pool_round_trips_through_adoption(tmp_path, context, seqs):
    """The other half of the same fact, and the one a restart depends on.

    `deploy/adopt.py` rebuilds a Deployment from a running container's command
    line. Reading `--ctx-size` back as the context would report a window twice
    (or sixteen times) the one being served, and the fit record rebuilt from it
    would be priced against that.
    """
    body = _recipe(tmp_path, context=context, seqs=seqs).content
    command = body.split("command: |\n", 1)[1]
    # What sparkrun substitutes, done here by hand: the recipe's own defaults.
    rendered = (
        command.replace("{model}", "unsloth/Qwen3-0.6B-GGUF:Q4_K_M")
        .replace("{served_model_name}", "qwen3-0.6b")
        .replace("{host}", "0.0.0.0")
        .replace("{port}", "8100")
        .replace("{ctx_size}", str(context * seqs))
        .replace("{parallel}", str(seqs))
    )

    adopted = parse_serve_command(rendered)
    assert adopted is not None
    assert adopted.runtime == "llamacpp"
    assert adopted.context_length == context
    assert adopted.max_concurrent_seqs == seqs


def test_adoption_survives_sparkruns_own_rewrite_of_the_model_flag():
    """sparkrun's llama-cpp plugin swaps `-hf <repo>` for `-m <cache path>`
    once it has resolved the file. A container adopted after that carries the
    path, and the parser has to find it -- otherwise every llama.cpp
    deployment becomes unadoptable the moment the control plane restarts."""
    base = (
        "llama-server {model} --alias q --host 0.0.0.0 --port 8100 "
        "--ctx-size 4096 --parallel 1 --n-gpu-layers 0 --jinja --no-webui"
    )
    by_repo = parse_serve_command(base.replace("{model}", "-hf owner/repo-GGUF:Q4_K_M"))
    by_path = parse_serve_command(base.replace("{model}", "-m /cache/huggingface/x.gguf"))

    assert by_repo is not None and by_repo.model_id == "owner/repo-GGUF:Q4_K_M"
    assert by_path is not None and by_path.model_id == "/cache/huggingface/x.gguf"


def test_a_short_flag_is_not_matched_inside_a_long_one():
    """`-m` must miss `--model`, `--max-model-len` and `--no-webui`. A plain
    substring search finds all three, and would adopt a vLLM container as a
    llama.cpp one with a nonsense model id."""
    vllm = (
        "vllm serve meta-llama/Llama-3.1-8B --served-model-name x --port 8100 "
        "--max-model-len 4096"
    )
    adopted = parse_serve_command(vllm)
    assert adopted is not None and adopted.runtime == "vllm"


# ---- the container itself ----------------------------------------------------


def test_the_recipe_turns_off_the_gpu_request_and_the_image_entrypoint(tmp_path):
    """Both are `docker run` facts, and one of them is fatal.

    sparkrun's executor defaults are written for a DGX Spark GPU workload and
    include `gpus: "all"`, which on a machine with no NVIDIA runtime is not a
    wasted flag -- it is a `docker run` that fails outright. The image also
    ships `ENTRYPOINT ["/app/llama-server"]` (`docker inspect`), and sparkrun
    starts the container with `sleep infinity` before exec'ing the serve
    command, so the entrypoint has to go too.

    Both values are QUOTED in the YAML. That is the whole test: plain-scalar
    YAML reads `gpus:` followed by nothing as null, null means "not set", and
    "not set" falls through to sparkrun's default -- reinstating the exact flag
    this block exists to remove, silently.
    """
    body = _recipe(tmp_path).content

    assert "executor_config:\n" in body
    assert '  gpus: ""\n' in body
    assert '  entrypoint: ""\n' in body


def test_the_other_runtimes_carry_only_the_hardening_entries(tmp_path):
    """`()` is a decision, not an omission: sparkrun's defaults were written
    for the hardware those three run on, so nothing here clears their `gpus`
    or their image's entrypoint. The one block they do carry is the privilege
    hardening every runtime gets (M8): no --privileged, IPC_LOCK for pinned
    memory."""
    body = recipes.synthesize(
        _shape("meta-llama/Llama-3.1-8B"),
        _plan(["spark-01"]),
        "vllm",
        4096,
        2,
        "llama",
        port=8100,
        gpu_memory_utilization=0.9,
        recipe_dir=tmp_path,
    ).content
    assert "executor_config:\n  privileged: false\n  cap_add: [IPC_LOCK]\n" in body
    assert body.count("executor_config:") == 1
    assert "gpus:" not in body
    assert "entrypoint:" not in body


def test_the_command_names_no_flag_the_binary_does_not_take(tmp_path):
    """llama-server EXITS on an unrecognised argument rather than ignoring it,
    so a flag borrowed from another runtime's template is a launch that dies at
    argv parsing. These four are the ones a copy-paste would bring."""
    body = _recipe(tmp_path).content
    command = body.split("command: |\n", 1)[1]

    for absent in (
        "--gpu-memory-utilization",
        "--tensor-parallel-size",
        "--served-model-name",
        "--trust-remote-code",
    ):
        assert absent not in command, absent
    # And the one it does spell differently.
    assert "--alias {served_model_name}" in command


def test_the_served_name_flag_is_per_runtime_and_reaches_a_custom_command(tmp_path):
    """`_custom_serve_command` appends host, port and the served name to an
    operator's own command. It wrote all three literally, justified by every
    runtime taking them under the same names -- true of three runtimes and
    false of this one."""
    body = _recipe(tmp_path, custom_command=("--foo", "1")).content
    command = body.split("command: |\n", 1)[1]

    assert "--alias {served_model_name}" in command
    assert "--served-model-name" not in command


# ---- the model id ------------------------------------------------------------


@pytest.mark.parametrize(
    "given,expected",
    [
        (GGUF_REF, "unsloth/Qwen3-0.6B-GGUF:Q4_K_M"),
        # The same blob without the scheme, which is the ONLY form that
        # actually reaches this function on a real launch. Testing just the
        # `hf://` spelling above is how this shipped translating nothing:
        # `recipes.synthesize` passes `shape.model_id`, the resolver has
        # already dropped the scheme, and the untranslated three-segment id
        # left sparkrun's `parse_gguf_model_spec` treating the whole string as
        # a repository that does not exist.
        (RESOLVED_GGUF_REF, "unsloth/Qwen3-0.6B-GGUF:Q4_K_M"),
        (
            "unsloth/Qwen3-30B-A3B-GGUF/UD-Q4_K_XL/"
            "Qwen3-30B-A3B-UD-Q4_K_XL-00001-of-00002.gguf",
            "unsloth/Qwen3-30B-A3B-GGUF:UD-Q4_K_XL",
        ),
        # A quant in a subdirectory, sharded. The publisher's own token is
        # carried -- `UD-Q4_K_XL` rather than the `q4_k_m` derate prices it as
        # -- because sparkrun globs the cache for a filename CONTAINING it.
        (
            "hf://unsloth/Qwen3-30B-A3B-GGUF/UD-Q4_K_XL/"
            "Qwen3-30B-A3B-UD-Q4_K_XL-00001-of-00002.gguf",
            "unsloth/Qwen3-30B-A3B-GGUF:UD-Q4_K_XL",
        ),
        # Already sparkrun's spelling: returned untouched. This must not
        # paraphrase an id an operator typed.
        ("unsloth/Qwen3-0.6B-GGUF:Q4_K_M", "unsloth/Qwen3-0.6B-GGUF:Q4_K_M"),
        ("meta-llama/Llama-3.1-8B", "meta-llama/Llama-3.1-8B"),
    ],
)
def test_the_gguf_reference_becomes_the_spelling_sparkrun_parses(given, expected):
    """derate resolves `hf://owner/repo/file.gguf` because that names the exact
    blob, which is what lets `resolver/gguf.py` measure it by summing its
    tensor directory. sparkrun takes `owner/repo:QUANT`. Same file, two
    spellings, and the translation is the seam."""
    assert llamacpp_model_spec(given) == expected


def test_the_translated_id_is_what_lands_in_the_recipe(tmp_path):
    body = _recipe(tmp_path).content
    assert "model: unsloth/Qwen3-0.6B-GGUF:Q4_K_M\n" in body
    assert "hf://" not in body


def test_a_local_gguf_path_is_refused_rather_than_launched():
    """It resolves here and names nothing on the node the container starts on,
    which is the whole point of a CPU runtime: that node is somewhere else."""
    refusal = llamacpp_model_refusal("llamacpp", "/home/somebody/models/x.gguf")
    assert refusal and "repository" in refusal
    # And the refusal is this runtime's, not a general rule: the other three
    # launch onto machines a model is distributed to first.
    assert llamacpp_model_refusal("vllm", "/home/somebody/models/x.gguf") is None
    assert llamacpp_model_refusal("llamacpp", GGUF_REF) is None
    # Relative and home-anchored paths are paths too, and neither carries a
    # scheme to tell them apart from a hub reference.
    for local in ("./x.gguf", "../models/x.gguf", "~/models/x.gguf"):
        assert llamacpp_model_refusal("llamacpp", local), local


def test_a_resolved_hub_gguf_is_not_mistaken_for_a_local_path():
    """The refusal above reads "does this end in .gguf and lack a scheme", and
    a RESOLVED hub blob answers yes to both -- `manager.launch` passes
    `shape.model_id`, and the resolver drops the scheme. So every legitimate
    hub GGUF was refused as a file on the coordinator, which is the opposite of
    what the refusal is for and the first thing that fires on a llamacpp
    launch. `is_hub_gguf_blob` is the distinction; it is narrow on purpose,
    because the thing it must not swallow is a genuine local path."""
    assert llamacpp_model_refusal("llamacpp", RESOLVED_GGUF_REF) is None
    assert is_hub_gguf_blob(RESOLVED_GGUF_REF)
    # A path that also ends in .gguf and also has three segments, but is a
    # path. The leading sigil is what separates them.
    assert not is_hub_gguf_blob("/home/somebody/models/x.gguf")
    assert not is_hub_gguf_blob("./a/b/x.gguf")
    # sparkrun's own two-segment spellings are not blobs and must pass through
    # this predicate untouched.
    assert not is_hub_gguf_blob("unsloth/Qwen3-0.6B-GGUF:Q4_K_M")
    assert not is_hub_gguf_blob("meta-llama/Llama-3.1-8B")


def test_the_serve_command_finds_the_binary_in_the_image_derate_defaults_to():
    """derate does not use sparkrun's llama-cpp image -- that one links libcuda
    and cannot run on the machines this runtime exists for -- so the binary is
    the upstream CPU build's `/app/llama-server`, which is the image ENTRYPOINT
    and is NOT on PATH. derate then clears that entrypoint, because a
    `sleep infinity` container cannot also run it. A bare `llama-server` then
    resolves against PATH alone and dies with `llama-server: not found`.

    Prepending rather than hardcoding an absolute path is what keeps
    DERATE_LLAMACPP_IMAGE pointed back at sparkrun's CUDA image working, where
    the binary is at /usr/local/bin and PATH already finds it."""
    spec = runtime_spec("llamacpp")
    for template in (spec.command_template, spec.custom_command_prefix):
        assert 'PATH="/app:$PATH"' in template, template
        # The prefix has to come before the binary, or it is just a variable
        # nothing reads.
        assert template.index('PATH="/app:$PATH"') < template.index("llama-server")


# ---- placement ---------------------------------------------------------------


PI = NodeProfile(
    node_id="connor-pi",
    hostname="connor-pi",
    address="10.0.0.9",
    device_class=DeviceClass.CPU,
    gpu_name="",
    gpu_count=0,
    total_memory=0,
    addressable_memory=0,
    memory_bandwidth_gbps=0.0,
    compute_capability="",
    driver_version="",
)


SPARK = NodeProfile(
    node_id="spark-01",
    hostname="spark-01",
    address="10.0.0.11",
    device_class=DeviceClass.GB10,
    gpu_name="NVIDIA GB10",
    gpu_count=1,
    total_memory=128 * 1024**3,
    addressable_memory=120 * 1024**3,
    memory_bandwidth_gbps=273.0,
    compute_capability="12.1",
    driver_version="580.95",
    arch="arm64",
)

#: The RTX 3090 Ti that joined this estate: VRAM of its own, host RAM of its own.
RTX = NodeProfile(
    node_id="connor3090-system-product-name",
    hostname="connor3090-system-product-name",
    address="192.168.0.69",
    device_class=DeviceClass.DISCRETE,
    gpu_name="NVIDIA GeForce RTX 3090 Ti",
    gpu_count=1,
    total_memory=24 * 1024**3,
    addressable_memory=23 * 1024**3,
    memory_bandwidth_gbps=1008.0,
    compute_capability="8.6",
    driver_version="580.95",
    arch="amd64",
)


def test_a_gpu_runtime_is_refused_on_a_machine_with_no_gpu():
    refusal = placement_refusal("vllm", PI, None)
    assert refusal and "no addressable GPU memory" in refusal


def test_a_cpu_runtime_is_refused_on_a_unified_memory_machine():
    """The refusal that survives the narrowing, with the reason that justifies it.

    Not "llama.cpp would not use the GPU" -- a preference, and it would run on a
    Spark's CPU perfectly well. It is that the GPU and the server would spend ONE
    pool, which derate budgets as GPU memory, so the gate could approve a launch
    that exhausts RAM. Refused even with a live reading in hand: the reading is
    of the wrong pool.
    """
    refusal = placement_refusal("llamacpp", SPARK, 50 * 1024**3)
    assert refusal and "unified memory" in refusal
    assert "vllm" in refusal, "it names the runtimes that do run there"
    apple = dataclasses.replace(SPARK, device_class=DeviceClass.APPLE)
    assert placement_refusal("llamacpp", apple, 50 * 1024**3)
    unknown = dataclasses.replace(SPARK, device_class=DeviceClass.UNKNOWN)
    assert placement_refusal("llamacpp", unknown, 50 * 1024**3)


def test_a_cpu_runtime_is_placed_on_a_discrete_gpu_box_with_a_host_reading():
    """Narrowed from a blanket "has a GPU" refusal: a discrete box's host RAM is
    its own pool, so `allocatable_bytes(pool="host")` answers for it and the
    launch is budgetable. Refusing it was an accounting gap, not a fact."""
    assert placement_refusal("llamacpp", RTX, 40 * 1024**3) is None
    # And it is still a GPU machine to the GPU runtimes.
    assert placement_refusal("vllm", RTX, None) is None


@pytest.mark.parametrize("profile", [PI, RTX], ids=["cpu", "discrete"])
def test_a_cpu_runtime_refuses_a_machine_nothing_has_measured(profile):
    """The one place in derate where a missing live reading refuses instead of
    degrading, and it does so because degrading would mean fabricating.

    A GPU node with no sample falls back to its nameplate, which describes a
    real device. Host RAM has no nameplate here -- on a discrete box the
    nameplate that exists is the CARD's, which is exactly the number this
    runtime must never be sized against.
    """
    refusal = placement_refusal("llamacpp", profile, None)
    assert refusal and "measured" in refusal

    # With a reading, it is a serving node like any other.
    assert placement_refusal("llamacpp", profile, 5 * 1024**3) is None


def test_the_fit_gate_sees_a_discrete_box_without_its_card_under_llamacpp():
    """What the planner and the gate are handed for a host-pool launch.

    With the card left in, the static ceiling is VRAM, the remedy sentence
    names a GPU, and decode is predicted at ~1 TB/s for a server reading host
    RAM -- every term about hardware the container never touches (its recipe
    clears `--gpus`). The live host budget is then the only memory in force.
    """
    seen = budget_profile("llamacpp", RTX)
    assert seen.addressable_memory == 0 and seen.usable_memory() == 0
    assert seen.memory_bandwidth_gbps == 0.0
    assert seen.device_class is DeviceClass.CPU
    # Identity and architecture survive: the image and the links need them.
    assert (seen.node_id, seen.address, seen.arch) == (RTX.node_id, RTX.address, "amd64")
    # A GPU runtime sees the card, and a machine with no GPU is already itself.
    assert budget_profile("vllm", RTX) is RTX
    assert budget_profile("llamacpp", PI) == PI


def test_a_host_pool_runtime_decodes_at_the_measured_host_bandwidth():
    """The agent measures host RAM (`probe.measure_host_bandwidth`); a
    llamacpp launch is planned at THAT figure, on a discrete box with its card
    cleared and on a CPU node alike, so the decode prediction and the routing
    fallback have a real number. A GPU runtime never reads it."""
    rtx = dataclasses.replace(RTX, host_memory_bandwidth_gbps=46.3)
    pi = dataclasses.replace(PI, host_memory_bandwidth_gbps=4.1)
    assert budget_profile("llamacpp", rtx).memory_bandwidth_gbps == 46.3
    assert budget_profile("llamacpp", rtx).device_class is DeviceClass.CPU
    assert budget_profile("llamacpp", pi).memory_bandwidth_gbps == 4.1
    assert budget_profile("vllm", rtx).memory_bandwidth_gbps == RTX.memory_bandwidth_gbps
    spark = dataclasses.replace(SPARK, host_memory_bandwidth_gbps=99.0)
    assert budget_profile("llamacpp", spark) is spark  # unified: refused upstream, never repriced


def test_it_refuses_to_shard_rather_than_launching_one_rank_of_two():
    """`shards=False` is a claim about what has been verified, not about what
    llama.cpp can do -- it has an RPC mode that spans machines and nothing here
    has run it. Either way a plan above one rank must not launch."""
    assert sharding_refusal("llamacpp", 2, 1, 1, 1) is not None
    assert sharding_refusal("llamacpp", 1, 1, 1, 1) is None


# ---- what it cannot be told --------------------------------------------------


def test_every_knob_it_cannot_take_refuses_rather_than_being_dropped():
    """A knob whose recipe_key is absent from a template is dropped in silence.
    For most flags that is merely wasteful; for these the FIT GATE has already
    priced the launch at the value being dropped, so the caller has been handed
    a verdict computed against something that will not happen."""
    spec = runtime_spec("llamacpp")
    for field in (
        "kv_cache_dtype_arg",
        "quantization_arg",
        "speculative_config_arg",
        "enforce_eager_arg",
        "cudagraph_capture_sizes_arg",
        "kv_cache_bytes_arg",
        "expert_parallel_arg",
    ):
        assert getattr(spec, field) is None, field


def test_its_cache_is_pointed_at_the_one_writable_mount():
    """Without this every launch re-downloads the GGUF into a container that is
    removed on exit. `env:` is the only channel that reaches a launched
    container -- the recipe format has no `volumes:` key."""
    spec = runtime_spec("llamacpp")
    assert dict(spec.cache_env)["LLAMA_CACHE"].startswith(
        "/cache/huggingface/derate-runtime-cache"
    )


def test_the_flags_table_and_the_resolver_table_know_the_same_runtimes():
    """`tests/unit/test_single_source.py` holds this too, through the contracts
    manifest. Asserted here as well because the failure it prevents is specific
    and silent: a runtime the launcher can start and the resolver has never
    heard of answers `unknown runtime` for every model."""
    from control_plane.resolver.support import RUNTIMES as RESOLVER_RUNTIMES

    assert set(RUNTIMES) == set(RESOLVER_RUNTIMES)


# ---- the gate, end to end, on the estate this was narrowed for ---------------


def _pooled_client(gpu: dict[str, int], host: dict[str, int], resolver=None, ws=None):
    """The real planner and fit gate behind a registry that answers BOTH pools,
    each with a different number, so a budget taken from the wrong one shows.
    *ws* replaces the 3090's profile."""
    from fastapi.testclient import TestClient

    from control_plane.fit import FitCalculator
    from control_plane.gateway.app import create_app
    from control_plane.planner import Planner
    from control_plane.resolver.stub import StubResolver
    from tests.fixtures import SPARK_01, WS_3090, node_state
    from tests.unit.test_gateway import FakeRegistry, build_deps

    class PooledRegistry(FakeRegistry):
        def allocatable_or_none(self, node_id, guardrail=0.90, pool="gpu"):
            return (host if pool == "host" else gpu).get(node_id)

    registry = PooledRegistry([node_state(SPARK_01), node_state(ws or WS_3090)])
    deps = dataclasses.replace(
        build_deps(registry=registry),
        fit=FitCalculator(),
        resolver=resolver or StubResolver(),
        planner=Planner(),
    )
    return TestClient(create_app(deps))


def test_a_llamacpp_plan_predicts_decode_at_the_measured_host_bandwidth():
    """With no host figure a llamacpp plan on the 3090 had no decode
    prediction (the card's bandwidth is cleared with the card) and its users
    chart priced decode at the CARD's bandwidth. With the agent's measured host
    figure both read host RAM; unmeasured stays as it was."""
    from control_plane.fit.calculator import predict_decode_tps
    from control_plane.fit.kv import kv_cache_bytes
    from control_plane.resolver.stub import StubResolver
    from tests.fixtures import SPARK_01, WS_3090

    class GGUFResolver(StubResolver):
        def resolve(self, model_id, dtype=None):
            return _shape(model_id)

        def supported_by(self, shape, runtime, image=None):
            return True, "llama.cpp reads this GGUF"

    GIB = 1024**3
    body = {
        "model_id": GGUF_REF,
        "runtime": "llamacpp",
        "context": 4096,
        "concurrency": 1,
        "node_ids": [WS_3090.node_id],
    }
    gpu = {SPARK_01.node_id: 15 * GIB, WS_3090.node_id: 20 * GIB}
    host = {WS_3090.node_id: 40 * GIB}
    measured = dataclasses.replace(WS_3090, host_memory_bandwidth_gbps=46.3)
    with _pooled_client(gpu, host, GGUFResolver(), ws=measured) as client:
        plan = client.post("/api/plan", json=body)
        chart = client.post("/api/plan/concurrency", json={**body, "contexts": [4096]})
    with _pooled_client(gpu, host, GGUFResolver()) as client:
        blind = client.post("/api/plan", json=body)
        blind_chart = client.post("/api/plan/concurrency", json={**body, "contexts": [4096]})

    assert plan.status_code == 200, plan.text
    shape = _shape(GGUF_REF)
    expected = predict_decode_tps(shape, 46.3, kv_cache_bytes(shape, 4096, 1, "fp16"))
    assert plan.json()["fit_live"]["predicted_decode_tps"] == pytest.approx(expected)
    assert chart.status_code == 200, chart.text
    assert chart.json()["bandwidth_gbps"] == 46.3, "host RAM, never the card's"

    assert blind.status_code == 200, blind.text
    assert blind.json()["fit_live"]["predicted_decode_tps"] is None
    assert blind_chart.json()["bandwidth_gbps"] is None


def test_a_llamacpp_plan_on_a_discrete_box_is_budgeted_against_host_ram():
    """The acceptance for the narrowing: on the 3090 the gate compares a
    llamacpp launch against host RAM, never against the card -- and the Spark
    beside it is still refused, by name, for the unified pool."""
    from tests.fixtures import SPARK_01, WS_3090

    GIB = 1024**3
    gpu = {SPARK_01.node_id: 15 * GIB, WS_3090.node_id: 20 * GIB}
    host = {WS_3090.node_id: 90 * GIB}
    body = {
        "model_id": "Qwen/Qwen3-30B-A3B",
        "runtime": "llamacpp",
        "context": 4096,
        "concurrency": 1,
    }
    with _pooled_client(gpu, host) as client:
        named = client.post("/api/plan", json={**body, "node_ids": [WS_3090.node_id]})
        spark = client.post("/api/plan", json={**body, "node_ids": [SPARK_01.node_id]})
        picked = client.post("/api/plan", json=body)

    assert named.status_code == 200, named.text
    out = named.json()
    assert out["capacity"]["allocatable_per_node"] == 90 * GIB
    assert out["fit_live"]["usable_per_node"] == 90 * GIB, "the host budget, not the card's 20 GiB"
    assert out["fit_live"]["verdict"] != "wont_fit", out["fit_live"]["reason"]
    assert out["serve"]["basis"] == "live"
    # The card's ceiling is not a static budget for a server that never sees it.
    assert out["fit"]["usable_per_node"] == 0

    assert spark.status_code == 400, spark.text
    assert spark.json()["error"]["code"] == "node_has_no_memory"
    assert "unified memory" in spark.json()["error"]["message"]

    # Left to the planner, the Spark is left out and said so.
    assert picked.status_code == 200, picked.text
    assert picked.json()["plan"]["node_ids"] == [WS_3090.node_id]
    assert any(SPARK_01.node_id in w for w in picked.json()["placement"]["warnings"])


def test_a_discrete_box_with_only_a_card_reading_is_refused_as_unmeasured_host_ram():
    """The placement check asks the HOST pool's reading. A 3090 whose agent
    sent VRAM figures and no host RAM (an agent from before the discrete
    read) has nothing a llamacpp launch can be budgeted against; the card's
    20 GiB must not stand in for it."""
    from tests.fixtures import SPARK_01, WS_3090

    GIB = 1024**3
    gpu = {SPARK_01.node_id: 15 * GIB, WS_3090.node_id: 20 * GIB}
    body = {
        "model_id": "Qwen/Qwen3-30B-A3B",
        "runtime": "llamacpp",
        "context": 4096,
        "concurrency": 1,
        "node_ids": [WS_3090.node_id],
    }
    with _pooled_client(gpu, {}) as client:
        named = client.post("/api/plan", json=body)

    assert named.status_code == 400, named.text
    message = named.json()["error"]["message"]
    assert "Nothing has measured how much host memory" in message
    assert WS_3090.node_id in message


def test_a_llamacpp_launch_is_gated_on_the_host_budget_it_was_planned_against():
    """Found live on the RTX box: /api/plan said `fits` against 47 GiB of host
    RAM and the launch refused with "0.0 GiB usable", because the launch gate
    read the STATIC verdict, and a host-pool runtime's static budget is 0 by
    design. That refused every llamacpp launch on every machine. The live
    verdict is the gate; too little host RAM is a plain wont_fit, never the
    409 that offers to override against an idle-machine ceiling that does not
    exist for host memory."""
    from control_plane.resolver.stub import StubResolver
    from tests.fixtures import SPARK_01, WS_3090

    class GGUFResolver(StubResolver):
        def resolve(self, model_id, dtype=None):
            return _shape(model_id)

        def supported_by(self, shape, runtime, image=None):
            return True, "llama.cpp reads this GGUF"

    GIB = 1024**3
    body = {
        "model_id": GGUF_REF,
        "runtime": "llamacpp",
        "context": 4096,
        "concurrency": 1,
        "node_ids": [WS_3090.node_id],
    }
    gpu = {SPARK_01.node_id: 15 * GIB, WS_3090.node_id: 20 * GIB}
    with _pooled_client(gpu, {WS_3090.node_id: 90 * GIB}, GGUFResolver()) as client:
        roomy = client.post("/api/deployments", json=body)
    with _pooled_client(gpu, {WS_3090.node_id: GIB // 4}, GGUFResolver()) as client:
        tight = client.post("/api/deployments", json=body)

    assert roomy.status_code == 201, roomy.text
    assert tight.status_code == 400, tight.text
    assert tight.json()["error"]["code"] == "wont_fit"


def test_capacity_reports_what_llamacpp_could_serve_from_a_discrete_boxs_host_ram():
    """`/api/capacity` counted only the card on a discrete box, so "the largest
    model that runs here" never said what llamacpp could serve from its host
    RAM -- 90 GiB beside a 20 GiB card. Reported BESIDE the GPU sides, sized
    the way that launch's own gate sizes it (host budget, card taken out); a
    Spark's unified pool gets nothing, and a box nothing measured says why."""
    from tests.fixtures import SPARK_01, WS_3090

    GIB = 1024**3
    gpu = {SPARK_01.node_id: 15 * GIB, WS_3090.node_id: 20 * GIB}
    params = {"context": 4096, "concurrency": 1, "models": "Qwen/Qwen3-30B-A3B"}
    with _pooled_client(gpu, {WS_3090.node_id: 90 * GIB}) as client:
        box = client.get("/api/capacity", params={**params, "on": WS_3090.node_id})
        spark = client.get("/api/capacity", params={**params, "on": SPARK_01.node_id})
    with _pooled_client(gpu, {}) as client:
        unmeasured = client.get("/api/capacity", params={**params, "on": WS_3090.node_id})

    assert box.status_code == 200, box.text
    out = box.json()
    assert out["live"]["allocatable_per_node"] == 20 * GIB, "the GPU answer is unchanged"
    pool = out["host_pool"]
    assert pool is not None, out.get("host_pool_unavailable_reason")
    assert pool["runtimes"] == ["llamacpp"] and pool["node_id"] == WS_3090.node_id
    assert pool["allocatable_per_node"] == 90 * GIB, "the host budget, not the card's"
    (row,) = pool["rows"]
    assert row["fits"] and row["dtype"] == row["native_dtype"], row["reason"]
    assert row["predicted_decode_tps"] is None, "never priced at the card's bandwidth"
    (gpu_row,) = out["live"]["rows"]
    assert gpu_row["dtype"] != gpu_row["native_dtype"] or not gpu_row["fits"]

    assert spark.status_code == 200, spark.text
    assert spark.json()["host_pool"] is None
    assert spark.json()["host_pool_unavailable_reason"] is None

    assert unmeasured.status_code == 200, unmeasured.text
    assert unmeasured.json()["host_pool"] is None
    assert "measured" in unmeasured.json()["host_pool_unavailable_reason"]


def test_routing_strength_never_scores_a_llamacpp_deployment_by_the_card_it_cleared():
    """Before ~100 requests give a measured rate, a local target is scored by
    `predicted_decode_tps`, else by bandwidth. A host-pool launch has no
    prediction (it is planned on the box with its card taken out), and the
    bandwidth fallback read the RAW profile -- the 3090's ~936 GB/s for a
    server whose recipe cleared `--gpus`, which could hand it most of the
    traffic. It is judged on the same card-less profile, so it gets the
    neutral default; a GPU runtime on the same box still gets the card."""
    from control_plane.gateway.settings import GatewaySettings
    from control_plane.gateway.stats import StatsRegistry
    from control_plane.gateway.strength import (
        SOURCE_BANDWIDTH, SOURCE_DEFAULT, local_strength,
    )
    from tests.unit.test_gateway import make_deployment

    assert RTX.memory_bandwidth_gbps > 0
    base = make_deployment("d", "m", backend_url=None, node_ids=(RTX.node_id,), predicted_tps=None)
    profiles = {RTX.node_id: RTX}
    args = (StatsRegistry(), profiles, GatewaySettings())

    cpu = local_strength(dataclasses.replace(base, runtime="llamacpp"), *args)
    assert (cpu.raw, cpu.source) == (1.0, SOURCE_DEFAULT)
    gpu = local_strength(base, *args)
    assert gpu.source == SOURCE_BANDWIDTH and gpu.raw == RTX.memory_bandwidth_gbps * max(1, RTX.gpu_count)

    # Measured host RAM: the same fallback has a real number, and it is the
    # host's, never the card's.
    measured = {RTX.node_id: dataclasses.replace(RTX, host_memory_bandwidth_gbps=46.3)}
    host = local_strength(
        dataclasses.replace(base, runtime="llamacpp"), StatsRegistry(), measured, GatewaySettings()
    )
    assert (host.raw, host.source) == (46.3, SOURCE_BANDWIDTH)
    assert host.raw > cpu.raw
