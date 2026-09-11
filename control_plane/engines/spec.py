"""What one inference engine declares about itself.

Standard library only, by rule. See this package's ``__init__`` for why:
anything imported from ``control_plane.deploy`` drags the launcher in.
"""

from __future__ import annotations

from dataclasses import dataclass



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
class EngineSpec:
    """How one inference engine differs from the others.

    Was ``deploy/flags.py::RuntimeSpec``. The wire still says "runtime" --
    ``Deployment.runtime``, the API and the UI are unchanged, because
    ``contracts/`` is frozen and a cosmetic rename would cost a record
    migration for nothing. "Engine" is the word for the thing itself.
    """

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
    #: How this runtime spells "call yourself this on the wire".
    #:
    #: `_custom_serve_command` appends host, port and the served name to an
    #: operator's own command, and its docstring used to justify writing all
    #: three literally: every runtime here took them "under the same three
    #: names (verified against each one's own argparse table)". llama-server
    #: is the fourth and it does not -- it spells this one `--alias`, and it
    #: EXITS on an argument it does not recognise rather than ignoring it, so
    #: the generic spelling would turn every custom command on this runtime
    #: into a launch that dies at argv parsing.
    #:
    #: Defaulted, so the three runtimes the old sentence was true of are
    #: unchanged and the field only has to be set where the claim breaks.
    served_name_arg: str = "--served-model-name"
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
    #: Appended, with a JSON object this module builds, when the operator asked
    #: for speculative decoding and this runtime can be told about it.
    #:
    #: The value is single-quoted in the template because it is JSON and the
    #: rendered command runs under `bash -c`. That is safe here and would not be
    #: safe for anything a request supplies: `render_speculative_config` below
    #: builds the blob from an enum member and an int, so it cannot contain a
    #: quote to close the quoting with, and it asserts that rather than assuming
    #: it. The generic `extra_args` channel deliberately cannot carry this flag
    #: at all -- `_EXTRA_ARG_SAFE` in recipes.py rejects `{`, `"` and spaces,
    #: which is exactly right for operator text and exactly why the control
    #: plane has to build this one itself.
    speculative_config_arg: str | None = None
    #: Appended, with no value, when the operator asked to skip CUDA graph
    #: capture and torch.compile entirely. A plain literal token rather than a
    #: format string -- unlike the two flags above, there is no caller value
    #: to substitute, so there is nothing here for a render/validate step to
    #: check.
    enforce_eager_arg: str | None = None
    #: Appended, with a rendered space-separated list of ints the fit gate
    #: never sees, when the operator asked to trim which batch sizes get a
    #: captured graph instead of accepting the runtime's own default list.
    #: The value is built by `render_cudagraph_capture_sizes` below, which
    #: only ever emits digits and single spaces -- every element has already
    #: round-tripped through `int()` by the time it is a string, so unlike
    #: `speculative_config_arg` this carries no quoting and needs no
    #: whole-string grammar to protect the `bash -c` this reaches.
    cudagraph_capture_sizes_arg: str | None = None
    #: Appended, with a value from `render_kv_cache_dtype`, when the operator
    #: asked for a KV cache dtype other than the model's own. Distinct from
    #: `kv_cache_bytes_arg` above: that one says how many BYTES the cache may
    #: use, this one says how WIDE each entry is. The fit gate has always
    #: priced the second and until now could only send the first, so a
    #: request for fp8 bought a halved byte budget filled with fp16 entries.
    kv_cache_dtype_arg: str | None = None
    #: Appended, with a value from `render_quantization`, when the operator
    #: forces a weight quantization scheme rather than letting the runtime
    #: read it off the checkpoint. Same class as `kv_cache_dtype_arg` and the
    #: same danger: this is a FIT-GATE INPUT, not a launch-only choice, so a
    #: runtime with no such flag has to refuse rather than drop.
    quantization_arg: str | None = None
    #: Container-engine settings for this runtime, emitted as the recipe's
    #: top-level `executor_config:` block.
    #:
    #: sparkrun merges CLI > recipe > runtime-plugin defaults >
    #: EXECUTOR_DEFAULTS (core/launcher.py, the `_chain` built around line
    #: 475), and its defaults are written for a DGX Spark GPU workload:
    #: `gpus: "all"` among them (orchestration/executor.py). On a machine with
    #: no NVIDIA runtime that is not a wasted flag, it is a `docker run` that
    #: fails outright -- which is the whole reason this field exists rather
    #: than the alternative of shipping our own image for every CPU runtime.
    #:
    #: Empty string is the documented "clear it" value for both settings this
    #: is currently used for, and they are different mechanisms: the executor
    #: emits `--gpus` only `if cfg.gpus`, while `entrypoint` is checked
    #: `is not None` so that `""` renders `--entrypoint ''` and clears the
    #: image's own ENTRYPOINT. sparkrun's own Atlas plugin does the second of
    #: those for the same reason (runtimes/atlas.py::
    #: get_executor_config_defaults) -- a container launched with
    #: `sleep infinity` cannot also run the image's entrypoint.
    #:
    #: `()` is a decision rather than an omission on the three CUDA runtimes:
    #: sparkrun's defaults were written for exactly the hardware they run on.
    executor_config: tuple[tuple[str, str], ...] = ()
    #: Whether this runtime's context flag sizes a SHARED pool rather than one
    #: sequence.
    #:
    #: Measured, not read: `--ctx-size 2048 --parallel 2` on the pinned image
    #: prints `n_slots = 2, n_ctx_slot = 1024`, and `--ctx-size 8192
    #: --parallel 4` prints `n_ctx_slot = 2048`. llama.cpp divides the number
    #: it is given by the number of server slots.
    #:
    #: vLLM and SGLang do the opposite -- `--max-model-len` is per sequence and
    #: concurrency is its own flag -- and derate's `context_length` means the
    #: per-sequence number, because that is what the fit gate approves and what
    #: the Verdict card puts on screen. So for a runtime with this set, the
    #: recipe multiplies before it renders.
    #:
    #: Getting this wrong is the quietest failure in the project: nothing
    #: errors, the launch comes up READY, and every request gets
    #: `context / concurrency` of the window the gate approved. Same shape as
    #: the `--kv-cache-dtype` bug this module's header describes, and caught
    #: the same way -- by reading what the server actually printed.
    context_is_shared_pool: bool = False
    #: Which pool of memory a model launched on this runtime is placed in.
    #:
    #: `"gpu"` for the three CUDA runtimes: the budget is
    #: `NodeProfile.addressable_memory`, and a machine reporting none of it
    #: cannot carry a rank. `"host"` for llamacpp, whose budget is what the
    #: node says is free RAM right now.
    #:
    #: It is a claim about what `registry.allocatable_bytes` can ANSWER for,
    #: not about what the runtime prefers. That function branches on device
    #: class -- host memory for `DeviceClass.CPU`, a GPU figure for everything
    #: else -- so a host-pool runtime is placeable exactly where a host answer
    #: exists. Everywhere else the gate would size it against the wrong pool.
    #:
    #: This is the field that used to be implicit in `addressable_memory <= 0`.
    #: While every runtime needed a GPU, "has no GPU memory" and "cannot serve"
    #: were one question asked once; they are two questions now, and the six
    #: places that asked the old one each have to ask the new one. Naming it
    #: here rather than testing device classes at each site is what keeps the
    #: launch path and the node board from drifting apart.
    memory_pool: str = "gpu"
