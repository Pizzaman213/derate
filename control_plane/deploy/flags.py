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
# order Agent H shows the user, so keep the interesting knobs first.
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
    --trust-remote-code"""

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
    --mem-fraction-static {gpu_memory_utilization} \\
    --trust-remote-code"""


RUNTIMES: dict[str, RuntimeSpec] = {
    # `runtime: vllm` in a recipe resolves to sparkrun's "vllm-distributed"
    # plugin (sparkrun/core/recipe.py::_resolve_vllm_variant). We write the
    # bare name and let sparkrun pick the variant.
    "vllm": RuntimeSpec(
        name="vllm",
        sparkrun_runtime="vllm",
        default_image_env="DERATE_VLLM_IMAGE",
        default_image="ghcr.io/spark-arena/dgx-vllm-eugr-nightly:latest",
        max_seqs_key="max_num_seqs",
        command_template=_VLLM_COMMAND,
        expert_parallel_arg="--enable-expert-parallel",
        health_path="/health",
    ),
    "sglang": RuntimeSpec(
        name="sglang",
        sparkrun_runtime="sglang",
        default_image_env="DERATE_SGLANG_IMAGE",
        default_image="scitrera/dgx-spark-sglang:0.5.9-t5",
        max_seqs_key="max_running_requests",
        command_template=_SGLANG_COMMAND,
        expert_parallel_arg="--enable-ep-moe",
        health_path="/health",
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
