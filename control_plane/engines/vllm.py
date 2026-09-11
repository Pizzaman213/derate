"""vLLM. The default engine, and the only one whose speculative decoding
the fit gate knows how to price.
"""

from __future__ import annotations

from .spec import RUNTIME_CACHE_DIR, EngineSpec


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


# The binary-and-model half of the command above, with none of the
# plan-derived flags -- what recipes.py::_custom_serve_command builds on when
# the operator supplies the rest themselves. It appends --host and --port
# generically and the served name through `served_name_arg`, which is a field
# rather than a literal because llama-server spells that one `--alias`.
_VLLM_CUSTOM_PREFIX = "vllm serve \\\n    {model}"


# `runtime: vllm` in a recipe resolves to sparkrun's "vllm-distributed"
# plugin (sparkrun/core/recipe.py::_resolve_vllm_variant). We write the
# bare name and let sparkrun pick the variant.
SPEC = EngineSpec(
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
    kv_cache_dtype_arg="--kv-cache-dtype {kv_cache_dtype}",
    quantization_arg="--quantization {quantization}",
    speculative_config_arg="--speculative-config '{speculative_config}'",
    # Verified against the live image (vllm serve --help=enforce-eager /
    # --help=CompilationConfig, version 0.28.1rc1.dev462+g9ca97b28b):
    # --enforce-eager is a plain boolean (no value, also spelled
    # --no-enforce-eager) that "disables both torch.compile and CUDA
    # graphs", and --cudagraph-capture-sizes is its own top-level CLI
    # flag taking a space-separated list of ints -- not JSON, and no
    # --compilation-config blob needed.
    enforce_eager_arg="--enforce-eager",
    cudagraph_capture_sizes_arg="--cudagraph-capture-sizes {cudagraph_capture_sizes}",
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
    cache_env=(("VLLM_CACHE_ROOT", RUNTIME_CACHE_DIR + "/vllm"),),
)


#: How to ask THIS image which flags it accepts. See
#: ``engines/flagcatalog.py`` for why it is asked rather than written down.
#:
#: Two things this has already taught us, both of which a hand-kept table would
#: have got wrong. The module moved -- ``vllm.entrypoints.openai.cli_args``
#: became ``vllm.entrypoints.launchers.cli_args`` -- so the import is a ladder
#: rather than a path. And ``nargs == 0`` is load-bearing next to the action
#: class name: vLLM spells its switches with a custom paired action
#: (``--enforce-eager`` / ``--no-enforce-eager``) whose class is in none of the
#: obvious sets, so reading only the class name records a switch as
#: value-taking and lets ``--enforce-eager=yes`` through.
#:
#: Needs a visible GPU even though it loads no model: vLLM builds this parser
#: through device-aware config and raises `Failed to infer device type`
#: without one.
FLAG_PROBE_NEEDS_GPU = True
FLAG_PROBE = r"""
import json, importlib
import vllm
mod = None
for _name in ("vllm.entrypoints.launchers.cli_args",
              "vllm.entrypoints.openai.cli_args"):
    try:
        mod = importlib.import_module(_name)
        break
    except Exception:
        continue
try:
    from vllm.utils import FlexibleArgumentParser
except Exception:
    from vllm.utils.argparse_utils import FlexibleArgumentParser
p = mod.make_arg_parser(FlexibleArgumentParser())
NOVAL = {"_StoreTrueAction", "_StoreFalseAction", "_CountAction",
         "_HelpAction", "_VersionAction"}
APPEND = {"_AppendAction", "_AppendConstAction", "_ExtendAction"}
flags = []
for a in p._actions:
    if not a.option_strings:
        continue
    kind = type(a).__name__
    nargs = a.nargs if isinstance(a.nargs, str) else (
        None if a.nargs is None else int(a.nargs))
    flags.append({
        "names": list(a.option_strings),
        "takes_value": kind not in NOVAL and nargs != 0,
        "nargs": nargs,
        "choices": sorted(map(str, a.choices)) if a.choices else None,
        "repeatable": kind in APPEND,
    })
flags.sort(key=lambda f: f["names"][0])
print("derate-flagprobe:" + json.dumps(
    {"engine": "vllm", "version": vllm.__version__, "flags": flags}))
"""
