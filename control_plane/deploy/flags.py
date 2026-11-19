"""The one place that knows sparkrun's command line.

Verified against sparkrun 0.2.40 by running `sparkrun run ... --dry-run` and
reading `sparkrun/cli/_common.py`. When sparkrun's flags change, this file is
the only edit.

Two things worth knowing before touching this, both learned the hard way from
sparkrun's own dry-run output:

1. sparkrun's CLI flags do not reach the inference runtime directly. They set
   *recipe override keys*, which are then substituted into the recipe's
   ``command`` template. Passing ``--pp 2`` to a recipe whose command template
   never mentions ``{pipeline_parallel}`` is a silent no-op: the flag is
   accepted, the exit code is zero, and pipeline parallelism does not happen.
   That is why we synthesize our own recipes (see recipes.py) instead of
   reusing registry ones. The planner's PP decision is the whole product; it
   must not be silently dropped.

2. There is no ``--max-num-seqs`` flag. Concurrency goes through the generic
   ``-o key=value`` override, and the key is runtime-specific.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal

SPARKRUN_BIN = "sparkrun"
SPARKRUN_VERIFIED_VERSION = "0.2.40"

Style = Literal["cli", "override"]


@dataclass(frozen=True)
class Knob:
    """One control-plane value and how sparkrun receives it.

    name        our name for it, used in errors and in the rendered plan
    style       "cli"      -> emitted as ``--flag value``
                "override" -> emitted as ``-o recipe_key=value``
    cli_flag    the sparkrun flag, when style is "cli"
    recipe_key  the recipe override key this ultimately writes. Every knob
                has one, including the "cli" ones, because the synthesized
                recipe's command template must reference ``{recipe_key}``
                or the value is dropped.
    render      value -> str, for the argv
    """

    name: str
    style: Style
    recipe_key: str
    cli_flag: str | None = None
    render: Callable[[Any], str] = str

    def emit(self, value: Any, recipe_key: str | None = None) -> list[str]:
        """Render this knob as argv fragments.

        *recipe_key* overrides :attr:`recipe_key` for runtimes that spell the
        same concept differently (sglang calls max_num_seqs
        ``max_running_requests``).
        """
        if value is None:
            return []
        rendered = self.render(value)
        if self.style == "cli":
            assert self.cli_flag is not None
            return [self.cli_flag, rendered]
        return ["-o", "%s=%s" % (recipe_key or self.recipe_key, rendered)]


def _pct(value: float) -> str:
    return "%.2f" % float(value)


# The table. Order here is the order in the rendered command, which is the
# order the UI shows the user, so keep the interesting knobs first.
KNOBS: tuple[Knob, ...] = (
    Knob("hosts", "cli", "hosts", "--hosts", lambda v: ",".join(v)),
    Knob("tensor_parallel", "cli", "tensor_parallel", "--tp"),
    Knob("pipeline_parallel", "cli", "pipeline_parallel", "--pp"),
    Knob("context_length", "cli", "max_model_len", "--max-model-len"),
    Knob("served_name", "cli", "served_model_name", "--served-model-name"),
    Knob("port", "cli", "port", "--port"),
    Knob("gpu_memory_utilization", "cli", "gpu_memory_utilization", "--gpu-mem", _pct),
    # No CLI flag exists for these; they ride the generic -o channel.
    Knob("max_concurrent_seqs", "override", "max_num_seqs"),
    Knob("expert_parallel", "override", "expert_parallel"),
    Knob("data_parallel", "override", "data_parallel"),
)

KNOBS_BY_NAME: dict[str, Knob] = {k.name: k for k in KNOBS}


#: The one host directory a launched container can write to that outlives it.
#:
#: `docker inspect` on a running sparkrun container: the only bind mount is the
#: host's HuggingFace cache at `/cache/huggingface`, the user is `1000:1000`,
#: and `HOME=/tmp`. sparkrun's recipe format has no `volumes:` key
#: (core/recipe.py::_KNOWN_KEYS) and its executor takes extra volumes only from
#: the runtime plugin, so this mount is not a preference -- it is the whole set
#: of persistent, writable paths available to us. A sibling of `hub/` rather
#: than anything inside it: registry/modelcache.py measures and deletes by
#: walking `hub/models--*`, so a directory beside it is invisible to the
#: storage screen's repository accounting rather than counted as weights.
RUNTIME_CACHE_DIR = "/cache/huggingface/derate-runtime-cache"


@dataclass(frozen=True)
class RuntimeSpec:
    """How one inference runtime differs. Contract runtime name -> sparkrun."""

    name: str  # our name, from Deployment.runtime
    sparkrun_runtime: str  # what goes in the recipe's `runtime:` field
    default_image_env: str  # env var that overrides the container image
    default_image: str
    max_seqs_key: str  # recipe key that carries max_concurrent_seqs
    command_template: str  # the serve command, with {recipe_key} placeholders
    expert_parallel_arg: str | None  # appended when plan.expert_parallel > 1
    health_path: str
    #: The binary invocation and the model flag ONLY, with none of the
    #: plan-derived flags `command_template` carries -- used instead of it
    #: when the operator supplies a full custom command (recipes.py::
    #: _custom_serve_command). `{model}` is still the fit gate's own
    #: resolved id, never operator text, so a custom launch cannot start a
    #: different model than the one the Verdict card judged.
    custom_command_prefix: str
    #: Environment that points this runtime's persistent state at somewhere
    #: that survives the container. Emitted as the recipe's `env:` block, which
    #: sparkrun merges ahead of its own tuning vars (runtimes/base.py, the
    #: merge_env call in _run_solo) and the docker executor turns into `-e`.
    #:
    #: "Persistent state" rather than "compiled artifacts", which is what this
    #: said when vLLM's compile cache was the only user. The direction does not
    #: matter and the mechanism does: `RUNTIME_CACHE_DIR` is the only writable
    #: path a launched container has, so anything that must outlive one launch
    #: is pointed there whether the runtime writes it (a compile cache) or only
    #: reads it (the tts voice library).
    #:
    #: Empty is still a decision rather than an omission: pointing an unread
    #: variable at a directory would suggest a saving that is not there.
    cache_env: tuple[tuple[str, str], ...] = ()
    #: Whether this runtime can split one model across ranks at all. False is
    #: not "prefers not to": the tts server is a single process holding a
    #: single checkpoint, and a plan with TP=2 against it would commit two
    #: machines to a launch that starts one server and wastes the other. The
    #: refusal is in `sharding_refusal` below so the launch path and the
    #: runtime itself cannot disagree about which degrees are legal.
    shards: bool = True
    #: Appended, with the byte count the fit gate budgeted, when this runtime
    #: can be told the KV cache size outright instead of deriving one.
    #:
    #: vLLM's default sizing is `device total x gpu_memory_utilization` minus
    #: the DEVICE's free-memory drop across its own profiling -- not this
    #: process's allocation. Its own note above that subtraction: "we assume
    #: that the other processes using the same GPU did not change their memory
    #: usage during the profiling." On a box with neighbours that assumption is
    #: false in both directions, and both are fatal: one allocating during the
    #: window is billed to whoever is profiling until the budget goes negative
    #: (`No available memory for the cache blocks`), and one *releasing* trips
    #: an assert outright (`Error in memory profiling ... other processes ...
    #: release GPU memory while vLLM is profiling`).
    #:
    #: Given this flag, `determine_available_memory` returns the figure and
    #: never reaches either -- no derived budget, so nothing a neighbour does
    #: can corrupt it. The number is `fit.breakdown.kv_cache`, which is what
    #: the gate approved the launch against, so this is also the first time the
    #: runtime is asked for the cache the plan actually costed rather than
    #: whatever a fraction of the machine happened to leave over.
    #:
    #: The trade, said out loud: vLLM will now allocate this exactly, and OOM
    #: if the plan was wrong, where before it would quietly settle for less.
    #: The fit gate sizes against live memory precisely so the plan is not
    #: wrong, and a launch that silently gets a smaller KV cache than the plan
    #: promised is a worse failure -- it is the one nobody notices.
    kv_cache_bytes_arg: str | None = None
    #: Where the per-deployment runtime API key goes (H3). The engine listens
    #: on 0.0.0.0 on the host network, so without a key anyone on the LAN --
    #: or any other model container, which shares the host netns -- can call
    #: it with no limits and no record. `api_key_env` is the variable the
    #: runtime reads it from (vLLM `VLLM_API_KEY`, SGLang `SGLANG_API_KEY`,
    #: derate's tts `DERATE_TTS_API_KEY`); `api_key_arg` is appended to the
    #: command for a runtime that also wants it as a flag.
    api_key_env: str = ""
    api_key_arg: str | None = None
    #: Appended with the sha the resolver read (M7), so remote code, when the
    #: operator opts in to it, runs the code that was approved, not whatever
    #: `main` is at launch.
    revision_arg: str | None = None
    #: Appended only when the deployment sets `trust_remote_code` (R2). Never
    #: in a command template: a repo's own Python runs inside the container
    #: (uid 1000, `--pid=host`, the HF cache mounted rw) only when the operator
    #: ticked the box beside the resolver's "ships Python" warning.
    trust_remote_code_arg: str | None = "--trust-remote-code"
    #: Whether `cache_env` is a compile cache to be split per model (M8): one
    #: shared, writable Inductor/Triton tree is a place one model container
    #: can plant `.py`/`.so` the next trusted launch loads.
    cache_per_model: bool = False


def cache_env_for(spec: RuntimeSpec, model_id: str) -> tuple[tuple[str, str], ...]:
    """`spec.cache_env`, scoped to one model when the runtime compiles."""
    if not spec.cache_per_model:
        return spec.cache_env
    out = []
    for c in model_id.lower():
        out.append(c if c.isalnum() else "-")
    slug = "".join(out).strip("-")[:64] or "model"
    return tuple((name, "%s/%s" % (path, slug)) for name, path in spec.cache_env)


# `--enable-prompt-tokens-details` makes vLLM report
# `usage.prompt_tokens_details.cached_tokens`, which is the only per-request
# figure for how much of a prompt the prefix cache served -- per-key telemetry
# reads it into `cache_read_tokens` / `prefix_cache_hit_rate`, and without it
# every local row says `cache_error: not_reported`. Read off the pinned image
# (vllm/entrypoints/launchers/cli_args.py, `enable_prompt_tokens_details:
# bool = False`); it changes the usage block and nothing about generation.
_VLLM_COMMAND = """\
vllm serve \\
    {model} \\
    --served-model-name {served_model_name} \\
    --host {host} \\
    --port {port} \\
    --tensor-parallel-size {tensor_parallel} \\
    --pipeline-parallel-size {pipeline_parallel} \\
    --max-model-len {max_model_len} \\
    --max-num-seqs {max_num_seqs} \\
    --gpu-memory-utilization {gpu_memory_utilization} \\
    --enable-prompt-tokens-details"""

# derate's own server, and the only command template here that does not name
# somebody else's binary. The flags deliberately mirror `vllm serve` where the
# concept exists, because render_command emits every knob for every runtime:
# a knob whose recipe_key is absent from the template is dropped in silence
# (see the header), so the choice is between a template that names all of them
# and a runtime that quietly ignores what the planner decided. Every flag
# below is read by control_plane/runtimes/tts.py, and the two parallelism ones
# are read in order to REFUSE anything but 1 -- consumed, not ignored.
_TTS_COMMAND = """\
python3 -m control_plane.runtimes.tts \\
    --model {model} \\
    --served-model-name {served_model_name} \\
    --host {host} \\
    --port {port} \\
    --tensor-parallel-size {tensor_parallel} \\
    --pipeline-parallel-size {pipeline_parallel} \\
    --max-model-len {max_model_len} \\
    --max-num-seqs {max_num_seqs} \\
    --gpu-memory-utilization {gpu_memory_utilization}"""

_SGLANG_COMMAND = """\
python3 -m sglang.launch_server \\
    --model-path {model} \\
    --served-model-name {served_model_name} \\
    --host {host} \\
    --port {port} \\
    --tp-size {tensor_parallel} \\
    --pp-size {pipeline_parallel} \\
    --context-length {max_model_len} \\
    --max-running-requests {max_running_requests} \\
    --mem-fraction-static {gpu_memory_utilization}"""

# The binary-and-model half of each command above, with none of the
# plan-derived flags -- what recipes.py::_custom_serve_command builds on when
# the operator supplies the rest themselves. Every runtime here takes
# --host/--port/--served-model-name under the same three names (verified
# against each one's own argparse table), which is what lets
# _custom_serve_command append them generically rather than per runtime.
_VLLM_CUSTOM_PREFIX = "vllm serve \\\n    {model}"
_TTS_CUSTOM_PREFIX = "python3 -m control_plane.runtimes.tts \\\n    --model {model}"
_SGLANG_CUSTOM_PREFIX = "python3 -m sglang.launch_server \\\n    --model-path {model}"


RUNTIMES: dict[str, RuntimeSpec] = {
    # `runtime: vllm` in a recipe resolves to sparkrun's "vllm-distributed"
    # plugin (sparkrun/core/recipe.py::_resolve_vllm_variant). We write the
    # bare name and let sparkrun pick the variant.
    "vllm": RuntimeSpec(
        name="vllm",
        sparkrun_runtime="vllm",
        default_image_env="DERATE_VLLM_IMAGE",
        # Not the upstream image, and the difference is one pip layer over
        # exactly it (docker/audio.Dockerfile). The upstream image cannot read
        # an audio file at all -- no torchcodec, no soundfile, no PyAV, no
        # system ffmpeg -- so a Whisper deployment launched from it comes up
        # READY, passes the health identity check, is offered on
        # /v1/audio/transcriptions, and refuses every upload with "Invalid or
        # unsupported audio file." Every gate in this project says that launch
        # is fine, because every one of them is asking a question the missing
        # decoder does not answer.
        #
        # A text deployment cannot tell the two images apart. Set
        # DERATE_VLLM_IMAGE to go back to upstream exactly.
        default_image="ghcr.io/pizzaman213/derate/vllm-audio:latest",
        max_seqs_key="max_num_seqs",
        command_template=_VLLM_COMMAND,
        custom_command_prefix=_VLLM_CUSTOM_PREFIX,
        expert_parallel_arg="--enable-expert-parallel",
        kv_cache_bytes_arg="--kv-cache-memory-bytes {kv_cache_memory_bytes}",
        health_path="/health",
        # vLLM resolves VLLM_CACHE_ROOT to `~/.cache/vllm` (envs.py, read out
        # of the shipped image), and sparkrun runs the container with
        # `HOME=/tmp` and `--rm`. So torch.compile output, the Inductor cache
        # and the FlashInfer autotune cache were being written into the
        # container's own filesystem and destroyed with it -- every launch of
        # every model recompiled from cold, and that compile is the part of
        # "launching" that happens after the download, when a person watching
        # has already been told the slow part is over. One variable moves the
        # whole tree onto the one mount that survives.
        # One subtree per model (M8): a shared writable compile cache lets
        # code running in one model's container poison the next launch's.
        cache_env=(("VLLM_CACHE_ROOT", RUNTIME_CACHE_DIR + "/vllm"),),
        cache_per_model=True,
        api_key_env="VLLM_API_KEY",
        revision_arg=(
            "--revision {revision} --code-revision {revision} "
            "--tokenizer-revision {revision}"
        ),
    ),
    "sglang": RuntimeSpec(
        name="sglang",
        sparkrun_runtime="sglang",
        default_image_env="DERATE_SGLANG_IMAGE",
        # Digest-pinned 2026-11-18 (`docker buildx imagetools inspect`): a tag
        # on Docker Hub can be re-pushed, a digest cannot. Bump both together.
        default_image=(
            "scitrera/dgx-spark-sglang:0.5.9-t5"
            "@sha256:a055011be7feb2e43ad8dbc365e3903f869c63b992864691f7edf83015bb941d"
        ),
        max_seqs_key="max_running_requests",
        command_template=_SGLANG_COMMAND,
        custom_command_prefix=_SGLANG_CUSTOM_PREFIX,
        expert_parallel_arg="--enable-ep-moe",
        health_path="/health",
        # torch's own cache variables rather than an SGLang one, and said out
        # loud because the difference matters: the SGLang image is not on the
        # box this was written on, so unlike the vLLM line above this is not
        # verified against the thing it configures. Both names are PyTorch's
        # (Inductor's compiled kernels, Triton's), read by any process that
        # compiles regardless of what launched it, and unset they default under
        # `HOME=/tmp` exactly as vLLM's did. Worst case the runtime compiles
        # nothing and nothing is written.
        cache_env=(
            ("TORCHINDUCTOR_CACHE_DIR", RUNTIME_CACHE_DIR + "/sglang/inductor"),
            ("TRITON_CACHE_DIR", RUNTIME_CACHE_DIR + "/sglang/triton"),
        ),
        cache_per_model=True,
        api_key_env="SGLANG_API_KEY",
        api_key_arg="--api-key {api_key}",
        revision_arg="--revision {revision}",
    ),
    # Text-to-speech. `sparkrun_runtime` is "vllm" and the command is not
    # vLLM's, which needs saying out loud: sparkrun's runtime field selects
    # its ORCHESTRATION plugin -- pull the image, run the container, watch it
    # come up -- and every one of those plugins renders an explicit `command:`
    # from the recipe verbatim (runtimes/vllm_distributed.py::generate_command
    # returns `recipe.render_command(config)` before it ever builds a vllm
    # line of its own, verified against 0.2.40). sparkrun has no plugin for a
    # runtime it has never heard of and we cannot add one to somebody else's
    # binary, so this borrows the plugin whose solo path is exactly "run this
    # container with this command" and brings its own everything else. What
    # comes with it is HF_HUB_OFFLINE=1 and the vLLM tuning mounts, both of
    # which are correct or inert here: the model is distributed to the node
    # before launch, exactly as it is for vLLM.
    "tts": RuntimeSpec(
        name="tts",
        sparkrun_runtime="vllm",
        default_image_env="DERATE_TTS_IMAGE",
        default_image="ghcr.io/pizzaman213/derate/tts:latest",
        max_seqs_key="max_num_seqs",
        command_template=_TTS_COMMAND,
        custom_command_prefix=_TTS_CUSTOM_PREFIX,
        # No expert parallelism: there are no experts, and appending a flag
        # the server does not take would fail the launch at argv parsing.
        expert_parallel_arg=None,
        health_path="/health",
        # Not a compile cache -- this server runs the checkpoint's own eager
        # PyTorch, nothing calls torch.compile, and there is no compiled
        # artifact to keep. It is the voice library, and it is here because
        # `env:` is the only channel that reaches a launched container: the
        # recipe format has no `volumes:` key, and the HuggingFace cache is
        # the one writable mount. The image defaults DERATE_TTS_VOICE_DIR to
        # /voices, which nothing mounts, so without this line the library is
        # empty on every launch and zero-shot cloning is a feature nobody can
        # reach. Beside `hub/` and never inside it, for the reason
        # RUNTIME_CACHE_DIR states: modelcache.py walks `hub/models--*` and
        # would bill a reference clip as weights.
        cache_env=(("DERATE_TTS_VOICE_DIR", RUNTIME_CACHE_DIR + "/voices"),),
        shards=False,
        api_key_env="DERATE_TTS_API_KEY",
        revision_arg="--revision {revision}",
    ),
}

SUPPORTED_RUNTIMES = tuple(RUNTIMES)


def runtime_spec(runtime: str) -> RuntimeSpec:
    try:
        return RUNTIMES[runtime]
    except KeyError:
        raise ValueError(
            "unknown runtime %r; supported: %s" % (runtime, ", ".join(SUPPORTED_RUNTIMES))
        ) from None


def sharding_refusal(
    runtime: str,
    tensor_parallel: int,
    pipeline_parallel: int,
    expert_parallel: int = 1,
    data_parallel: int = 1,
) -> str | None:
    """Why these degrees cannot run on this runtime, or None.

    Asked before a launch rather than discovered by one. A single-process
    runtime handed TP=2 would otherwise pass the fit gate (the arithmetic is
    per-rank and a sharded model fits more easily, not less), commit two
    machines, start one server, and leave the second rank permanently absent
    -- and the deployment would sit in LAUNCHING until the health timeout,
    with nothing on screen naming the cause.
    """
    spec = runtime_spec(runtime)
    if spec.shards:
        return None
    degrees = [
        ("tensor parallel", int(tensor_parallel)),
        ("pipeline parallel", int(pipeline_parallel)),
        # Neither can reach this runtime's command template at all -- there is
        # no expert_parallel_arg and no data-parallel flag -- so a degree above
        # 1 here is a decision that would evaporate between the plan on screen
        # and the process on the machine.
        ("expert parallel", int(expert_parallel)),
        ("data parallel", int(data_parallel)),
    ]
    named = [f"{label} {value}" for label, value in degrees if value > 1]
    if not named:
        return None
    return (
        "the %s runtime is one process holding one checkpoint and cannot "
        "shard a model, so %s is not a plan it can run. Serve this on a "
        "single node, or on a runtime that shards -- and note that the "
        "architectures %s loads are the ones no other runtime here can load "
        "at all." % (spec.name, " and ".join(named), spec.name)
    )
