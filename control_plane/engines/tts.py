"""derate's own speech server -- the one engine here that this repository
also implements (``control_plane/runtimes/tts.py``).
"""

from __future__ import annotations

from .spec import RUNTIME_CACHE_DIR, EngineSpec


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
    --gpu-memory-utilization {gpu_memory_utilization} \\
    --trust-remote-code"""


# The binary-and-model half of the command above, with none of the
# plan-derived flags -- what recipes.py::_custom_serve_command builds on when
# the operator supplies the rest themselves. It appends --host and --port
# generically and the served name through `served_name_arg`, which is a field
# rather than a literal because llama-server spells that one `--alias`.
_TTS_CUSTOM_PREFIX = "python3 -m control_plane.runtimes.tts \\\n    --model {model}"


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
SPEC = EngineSpec(
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
    # And no speculative decoding, for the same reason plus a stronger one:
    # control_plane/runtimes/tts.py calls `generate() -> codes` on the
    # checkpoint's own remote code. There is no draft head, no verify step
    # and no argument for one.
    speculative_config_arg=None,
    # Neither concept applies: this server runs the checkpoint's own
    # eager PyTorch, nothing calls torch.compile, and there is no
    # captured graph to disable or trim (see the cache_env comment
    # below, which says the same thing for the same reason).
    enforce_eager_arg=None,
    cudagraph_capture_sizes_arg=None,
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
)
