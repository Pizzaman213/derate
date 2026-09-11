"""llama.cpp. The only host-memory engine: it spends RAM rather than VRAM,
and its ``--ctx-size`` is a SHARED pool, not a per-sequence window.
"""

from __future__ import annotations

from .spec import RUNTIME_CACHE_DIR, EngineSpec


# The CPU runtime, and the only template here that names no parallelism flag
# at all. That is not an omission of the kind the note in `tts.py` warns
# about: `shards=False` means `sharding_refusal` has already refused any degree
# above 1 before a recipe is synthesized, so there is no decision being dropped
# in silence -- there is a decision that cannot have been made.
#
# `--n-gpu-layers 0` is a literal rather than a knob for the same reason. This
# runtime exists to serve a machine with no GPU, the fit gate budgets it
# against host RAM on that basis, and a layer count reaching the device would
# be spending memory nothing costed. A GPU-backed llama.cpp is a different
# runtime entry, not a flag on this one.
#
# Every flag below was read off `llama-server --help` in the pinned image
# (`ghcr.io/ggml-org/llama.cpp:server`, version 0.4.0-dev build 10902, aarch64)
# rather than from documentation, and the spellings are its own:
# `-hf <user>/<model>[:quant]`, `-a/--alias`, `-c/--ctx-size`, `-np/--parallel`,
# `-ngl/--n-gpu-layers`.
#
# `-hf` takes the same GGUF spelling sparkrun parses -- see
# `deploy/flags.py::llamacpp_model_spec` -- and sparkrun's llama-cpp plugin
# rewrites it to `-m <cache path>` once it has resolved the file
# (sparkrun's own runtimes/llama_cpp.py::generate_command).
#
# `--jinja` and `--no-webui` are both written out even though this build
# already defaults the first on, because they are the two places where a
# default is somebody else's decision about behaviour derate depends on.
# `--jinja` is what makes /v1/chat/completions apply the checkpoint's own chat
# template, and a build that flipped it back would not fail -- it would format
# every conversation wrongly, which reads as a bad model. `--no-webui` keeps
# the served port an API rather than also a UI on the same origin as the
# gateway's own.
#
# The `PATH=` prefix is about the IMAGE, not the flags: derate defaults to the
# upstream CPU build, where the binary is `/app/llama-server`, is the image
# ENTRYPOINT, and is NOT on PATH -- and this runtime then clears that
# entrypoint, so a bare `llama-server` dies with `llama-server: not found`.
# Prepending rather than hardcoding keeps sparkrun's own CUDA image (binary at
# /usr/local/bin) working under DERATE_LLAMACPP_IMAGE. Kept identical to
# `deploy/flags.py`, which `test_engine_registry.py` enforces field by field.
_LLAMACPP_COMMAND = """\
PATH="/app:$PATH" llama-server \\
    -hf {model} \\
    --alias {served_model_name} \\
    --host {host} \\
    --port {port} \\
    --ctx-size {ctx_size} \\
    --parallel {parallel} \\
    --n-gpu-layers 0 \\
    --jinja \\
    --no-webui"""


# The binary-and-model half of the command above, with none of the
# plan-derived flags -- what recipes.py::_custom_serve_command builds on when
# the operator supplies the rest themselves. It appends --host and --port
# generically and the served name through `served_name_arg`, which is a field
# rather than a literal because llama-server spells that one `--alias`.
_LLAMACPP_CUSTOM_PREFIX = 'PATH="/app:$PATH" llama-server \\\n    -hf {model}'


# The CPU runtime, and the first entry here whose point is the machine
# rather than the model. vLLM and SGLang need a CUDA device; a Raspberry
# Pi, a NAS or a spare x86 box has none, and until this existed such a
# machine could join the roster and never serve anything -- which is what
# `addressable_memory == 0` meant when it was written.
#
# Unlike `tts.py`, `sparkrun_runtime` is not a borrow: sparkrun ships a
# real llama-cpp plugin (runtimes/llama_cpp.py, `runtime_name =
# "llama-cpp"`), which renders this recipe's own `command:` verbatim and
# additionally resolves the GGUF into the container's cache before exec.
SPEC = EngineSpec(
    name="llamacpp",
    sparkrun_runtime="llama-cpp",
    default_image_env="DERATE_LLAMACPP_IMAGE",
    # A CPU build, deliberately not sparkrun's own default prefix
    # (`scitrera/dgx-spark-llama-cpp`), which is compiled against CUDA and
    # links libcuda -- present on a Spark and absent on every machine this
    # runtime exists for. Multi-arch, because GB10 and a Pi 5 are both
    # arm64 and the same tag has to serve an amd64 spare box too.
    default_image="ghcr.io/ggml-org/llama.cpp:server",
    max_seqs_key="parallel",
    command_template=_LLAMACPP_COMMAND,
    custom_command_prefix=_LLAMACPP_CUSTOM_PREFIX,
    # llama-server spells this one differently from the other three and
    # exits on an argument it does not recognise -- see `served_name_arg`.
    served_name_arg="--alias",
    # sparkrun's container defaults are written for a DGX Spark GPU
    # workload. `--gpus all` is a `docker run` that FAILS on a machine with
    # no NVIDIA runtime, not a flag that is merely wasted there, and the
    # image ships an ENTRYPOINT that a `sleep infinity` container cannot
    # also run. Both are cleared with the empty string, which is each
    # setting's own documented "clear it" value.
    executor_config=(("gpus", ""), ("entrypoint", "")),
    # No experts, no draft head, no captured graph, no weight-quantization
    # flag, and no way to be told a KV width or a KV byte budget. Every one
    # of these is a None so the matching `*_refusal` in this module speaks
    # -- llama.cpp has its own spellings for some of them (`--cache-type-k`
    # is the KV width) and this build has run none of them. A refusal names
    # what to change; a dropped flag is a launch that quietly does
    # something else.
    expert_parallel_arg=None,
    kv_cache_bytes_arg=None,
    kv_cache_dtype_arg=None,
    quantization_arg=None,
    speculative_config_arg=None,
    enforce_eager_arg=None,
    cudagraph_capture_sizes_arg=None,
    health_path="/health",
    # llama.cpp's own downloader writes here (`LLAMA_CACHE`), and without
    # it every launch re-downloads the GGUF into a container that is
    # removed on exit. Beside `hub/` rather than inside it, for the reason
    # RUNTIME_CACHE_DIR gives.
    cache_env=(("LLAMA_CACHE", RUNTIME_CACHE_DIR + "/llamacpp"),),
    context_is_shared_pool=True,
    memory_pool="host",
    # Not "prefers not to". llama.cpp does have an RPC mode that spans
    # machines, and nothing here has run it, so a plan above one rank would
    # commit machines to a launch this build cannot describe.
    shards=False,
)
