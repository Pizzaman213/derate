"""SGLang. Reads its memory fraction against device FREE rather than device
TOTAL, which is the one thing about it that is not vLLM-shaped.
"""

from __future__ import annotations

from .spec import RUNTIME_CACHE_DIR, EngineSpec


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


# The binary-and-model half of the command above, with none of the
# plan-derived flags -- what recipes.py::_custom_serve_command builds on when
# the operator supplies the rest themselves. It appends --host and --port
# generically and the served name through `served_name_arg`, which is a field
# rather than a literal because llama-server spells that one `--alias`.
_SGLANG_CUSTOM_PREFIX = "python3 -m sglang.launch_server \\\n    --model-path {model}"


SPEC = EngineSpec(
    name="sglang",
    sparkrun_runtime="sglang",
    default_image_env="DERATE_SGLANG_IMAGE",
    default_image="scitrera/dgx-spark-sglang:0.5.9-t5",
    max_seqs_key="max_running_requests",
    command_template=_SGLANG_COMMAND,
    custom_command_prefix=_SGLANG_CUSTOM_PREFIX,
    expert_parallel_arg="--enable-ep-moe",
    # SGLang spells this the same way vLLM does -- one --quantization
    # flag taking a scheme name. Unlike --speculative-algorithm above,
    # there is no shape difference to reverse-engineer.
    quantization_arg="--quantization {quantization}",
    # Absent on purpose, and not because SGLang cannot do this. It spells
    # the same concept as a set of separate flags
    # (--speculative-algorithm/--speculative-num-steps/...) rather than one
    # JSON object, so the template would be a different shape -- and the
    # pinned SGLang image is not on the box this was written on, exactly as
    # the cache_env note below says. Writing one from the documentation
    # would be a claim about somebody else's argparse table that nothing
    # here has run. A None means the fit gate still prices speculative
    # decoding for this runtime and the launch path refuses it, which is the
    # honest pair.
    speculative_config_arg=None,
    # Absent for the same reason speculative_config_arg is: SGLang has
    # its own graph-capture controls (--disable-cuda-graph, plus
    # batch-size-keyed capture rather than an arbitrary size list), a
    # different shape from the pair in `vllm.py`, and this exact pairing has
    # not been checked against the image the way the cache_env below now
    # has been. A None means the launch path falls back to this
    # runtime's own default graph behavior rather than silently ignoring
    # what was asked for -- see graph_capture_refusal.
    enforce_eager_arg=None,
    cudagraph_capture_sizes_arg=None,
    health_path="/health",
    # torch's own cache variables rather than an SGLang one. Verified
    # 2026-09-10 against `scitrera/dgx-spark-sglang:0.5.12` (the pinned
    # default is 0.5.9-t5; that exact tag was not launched) by running a
    # real derate launch, sending it a chat completion, and reading the
    # host side of the one bind mount afterward. TRITON_CACHE_DIR holds
    # real compiled kernels (`.cubin`/`.ptx`/`.so`, e.g.
    # `create_flashinfer_kv_indices_triton.*`) that survive the
    # container being removed -- confirmed, not assumed.
    # TORCHINDUCTOR_CACHE_DIR is correctly pointed but stays empty: this
    # recipe never passes `--enable-torch-compile`, so SGLang's default
    # path never calls Inductor at all, only Triton JIT. Both env vars
    # are still worth setting -- Triton compiles unconditionally and
    # would otherwise recompile from cold under `HOME=/tmp` on every
    # launch, exactly as vLLM's did before `VLLM_CACHE_ROOT` was set.
    cache_env=(
        ("TORCHINDUCTOR_CACHE_DIR", RUNTIME_CACHE_DIR + "/sglang/inductor"),
        ("TRITON_CACHE_DIR", RUNTIME_CACHE_DIR + "/sglang/triton"),
    ),
)
