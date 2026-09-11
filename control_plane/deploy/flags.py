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

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping, Sequence

from control_plane.contracts.quant import BYTES_PER_PARAM

#: The launcher's name on PATH, and the fallback when nothing overrides it.
SPARKRUN_BIN = "sparkrun"
SPARKRUN_VERIFIED_VERSION = "0.2.40"

#: First sparkrun that can bring up a pure data-parallel cluster.
#:
#: Found by bisecting the released wheels for `native_rendezvous_port`, which is
#: the hook the fix is built on: 0.3.0 through 0.3.6 do not have it, 0.3.7 does.
#: Upstream calls the bug issue #292 and describes the symptom this project
#: measured independently -- "a working two-node ``--dp 2`` deployment left rank
#: 1 sitting on ``sleep infinity``".
SPARKRUN_DATA_PARALLEL_VERSION = "0.3.7"


def _version_tuple(raw: str) -> tuple[int, ...] | None:
    """``"0.3.7"`` -> ``(0, 3, 7)``, or None for anything not purely numeric.

    None rather than a best effort: a version this cannot read is a version
    this must not rank, and the caller treats it as "no opinion" rather than
    as either answer.
    """
    parts = raw.strip().split(".")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def launcher_does_data_parallel(version: str | None) -> bool | None:
    """Whether this sparkrun can start a pure data-parallel cluster.

    None means the question could not be answered -- no version string, or one
    that does not parse -- and is a third answer rather than a stand-in for
    either. `data_parallel_refusal` then REFUSES, which is the opposite of how
    `imageprobe.py` treats an unanswerable probe, and deliberately so: the
    version here defaults to None, so "could not ask" and "the caller forgot to
    pass it" arrive as the same value. Reading that as permission would let a
    forgotten argument disable the gate everywhere and silently -- the exact
    shape of a knob whose recipe template has no placeholder. Refusing makes
    the same mistake loud, and the sentence it prints names the upgrade, which
    is the useful instruction under either cause.
    """
    if not version:
        return None
    have = _version_tuple(version)
    if have is None:
        return None
    return have >= _version_tuple(SPARKRUN_DATA_PARALLEL_VERSION)


def sparkrun_binary(env: Mapping[str, str] | None = None) -> str:
    """The launcher to invoke: ``DERATE_SPARKRUN_BIN``, else ``sparkrun``.

    Read here rather than baked into a default argument because a default
    argument is evaluated at import and a variable read at import cannot be
    tested, set by a supervisor after start, or reported by `envspec`
    truthfully.

    It needed writing at all because the variable was declared and never read.
    `envspec.py` lists it, `docs/CONTRACTS.md` publishes it from there, and
    `NOT_INSTALLED` tells the operator to "point DERATE_SPARKRUN_BIN at it" --
    while `SparkrunAdapter.__init__` defaulted to the bare constant and nothing
    anywhere called `os.environ` for the name. Setting it did nothing, silently,
    which is the same shape as a recipe knob whose template has no placeholder.
    """
    env = os.environ if env is None else env
    return env.get("DERATE_SPARKRUN_BIN") or SPARKRUN_BIN

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


#: What `--kv-cache-dtype` accepts, read off the pinned image's own
#: `CacheConfig.cache_dtype` rather than copied from a changelog -- the same
#: stance `imageprobe.py` takes for the architecture table, and for the same
#: reason: a table that claims a value the image cannot load is a launch that
#: clears every gate and dies, and one that refuses a value the image accepts
#: is a 400 on a servable configuration.
#:
#: Re-read it with:
#:   docker run --rm --entrypoint python3 <image> -c \
#:     "from vllm.config import CacheConfig; import typing; \
#:      print(typing.get_args(CacheConfig.__annotations__['cache_dtype']))"
VLLM_KV_CACHE_DTYPES: frozenset[str] = frozenset({
    "auto", "float16", "bfloat16", "fp8", "fp8_e4m3", "fp8_e5m2", "fp8_inc",
    "fp8_ds_mla", "nvfp4_ds_mla", "nvfp4", "nvfp4_4over6",
    "turboquant_k8v4", "turboquant_4bit_nc", "turboquant_k3v4_nc",
    "turboquant_3bit_nc",
    "int4_per_token_head", "int8_per_token_head", "fp8_per_token_head",
})

#: derate's KV vocabulary (`fit/kv.py::KV_ELEM_BYTES`) is not vLLM's, because
#: one names a WIDTH the gate prices and the other names a KERNEL the engine
#: runs. These are the only ones where the two mean the same thing.
#:
#: Everything mapping to None is "the model's own dtype" -- the gate's default,
#: and the case where passing no flag is exactly right. `int8`/`fp32` are
#: deliberately ABSENT rather than mapped: the gate can price them and this
#: image cannot serve them, and that gap is a refusal, not a silent
#: substitution.
_KV_DTYPE_TO_VLLM: dict[str, str | None] = {
    "": None, "auto": None, "model": None, "none": None,
    "fp16": None, "float16": None, "half": None,
    "bf16": None, "bfloat16": None,
    "fp8": "fp8", "float8": "fp8",
    "fp8_e4m3": "fp8_e4m3",
    "fp8_e5m2": "fp8_e5m2",
}


#: Every environment variable the pinned image's NCCL honours, read off
#: `libnccl.so.2`'s own string table rather than off a changelog -- the same
#: evidence rule `VLLM_KV_CACHE_DTYPES` follows, and for the same reason: a
#: name this build accepts and the runtime ignores is a setting that vanishes
#: in silence, which is the one failure this module exists to prevent.
#:
#: NCCL **2.31.2**, as shipped in ghcr.io/pizzaman213/derate/vllm-audio --
#: which is the loaded `libnccl.so.2`, not what torch reports.
#: `torch.cuda.nccl.version()` says 2.29.7 there: that is the version torch
#: was COMPILED against, and the runtime error text names 2.31.2. Read the
#: library, not the binding. A different image is a different table.
#:
#: The ones that matter for derate's regime are the small-message knobs --
#: NCCL_PROTO, NCCL_ALGO, NCCL_MIN_NCHANNELS/NCCL_MAX_NCHANNELS, NCCL_NTHREADS,
#: NCCL_BUFFSIZE, NCCL_IB_QPS_PER_CONNECTION. A tensor-parallel decode step
#: moves ~5.6 KB per exchange and pays ~86 exchanges, so essentially all of the
#: wire cost is per-collective overhead rather than bytes. WHICH VALUES ARE
#: RIGHT IS NOT KNOWN: nothing has measured a collective on this fabric, and
#: `tests/nccl_sweep.py` is what would. Do not guess one in here.
#: A bare token: no quotes, no spaces, nothing a shell would reinterpret.
#: The rendered recipe is what reaches `bash -c`, same rule as
#: `_EXTRA_ARG_SAFE`.
_NCCL_VALUE_SAFE = re.compile(r"^[A-Za-z0-9,._:+/-]+$")

NCCL_TUNABLES: frozenset[str] = frozenset({
    "NCCL_ALGO", "NCCL_ALLGATHERV_ENABLE", "NCCL_ALLOC_P2P_NET_LL_BUFFERS",
    "NCCL_BUFFSIZE", "NCCL_CE_COLL_AG_MULTICAST_THRESHOLD",
    "NCCL_CFT_ENABLE", "NCCL_CGA_CLUSTER_SIZE", "NCCL_CHECK_MODE",
    "NCCL_CHECK_POINTERS", "NCCL_CHUNK_SIZE", "NCCL_COLLNET_ENABLE",
    "NCCL_COLLNET_NODE_THRESHOLD", "NCCL_COMM_BLOCKING", "NCCL_COMM_ID",
    "NCCL_COMM_SHRINK_SHARE_RESOURCES", "NCCL_COMM_SPLIT_SHARE_RESOURCES",
    "NCCL_CONF_FILE", "NCCL_CONNECT_ROUND_MAX_PEERS", "NCCL_CROSS_NIC",
    "NCCL_CTA_POLICY", "NCCL_CUMEM_ENABLE", "NCCL_CUMEM_HOST_ENABLE",
    "NCCL_DEBUG", "NCCL_DEBUG_FILE", "NCCL_DEBUG_SUBSYS",
    "NCCL_DEBUG_TIMESTAMP_FORMAT", "NCCL_DEBUG_TIMESTAMP_LEVELS",
    "NCCL_DEV_API_JIT", "NCCL_DIAGNOSTICS_ECC_THRESHOLD",
    "NCCL_DISABLE_MEM_MANAGER", "NCCL_DMABUF_ENABLE",
    "NCCL_ELASTIC_BUFFER_REGISTER", "NCCL_ENABLE_VERSION_CHECK",
    "NCCL_ENQUEUE_REARCH_ENABLE", "NCCL_ENV_PLUGIN",
    "NCCL_GDAKI_USE_RELIABLE_DB", "NCCL_GDRCOPY_ENABLE",
    "NCCL_GDRCOPY_FIFO_ENABLE", "NCCL_GDRCOPY_FLUSH_ENABLE",
    "NCCL_GDRCOPY_SYNC_ENABLE", "NCCL_GDRCOPY_USE_INTERNAL_DMABUF",
    "NCCL_GDR_FLUSH_DISABLE", "NCCL_GIN_ENABLE",
    "NCCL_GIN_ERROR_QUERY_SEC", "NCCL_GIN_GDAKI_MAX_DEST_RD_ATOMIC",
    "NCCL_GIN_GDAKI_MAX_QP_RD_ATOMIC", "NCCL_GIN_GDAKI_NIC_HANDLER",
    "NCCL_GIN_GDAKI_QP_DEPTH", "NCCL_GIN_IB_OOO_OPT", "NCCL_GIN_IB_TC",
    "NCCL_GIN_NCONNECTIONS", "NCCL_GIN_PLUGIN",
    "NCCL_GIN_PLUGIN_REF_COUNT", "NCCL_GIN_PROXY_NTHREADS",
    "NCCL_GIN_PROXY_POLL_BATCH", "NCCL_GIN_PROXY_QUEUE_SIZE",
    "NCCL_GIN_TYPE", "NCCL_GRAPH_DUMP_FILE", "NCCL_GRAPH_DUMP_FILE_RANK",
    "NCCL_GRAPH_FILE", "NCCL_GRAPH_HELPER_DISABLE",
    "NCCL_GRAPH_MIXING_SUPPORT", "NCCL_GRAPH_REGISTER",
    "NCCL_GRAPH_STREAM_ORDERING", "NCCL_GROUP_CUDA_STREAM",
    "NCCL_HIER_CE_COLL_NUM_CTX", "NCCL_HOSTID", "NCCL_IBVERBS_LIB",
    "NCCL_IB_ADAPTIVE_ROUTING", "NCCL_IB_ADDR_FAMILY",
    "NCCL_IB_ADDR_RANGE", "NCCL_IB_AR_THRESHOLD", "NCCL_IB_DATA_DIRECT",
    "NCCL_IB_DEVICE_PCI_ORDER", "NCCL_IB_DISABLE", "NCCL_IB_ECE_ENABLE",
    "NCCL_IB_EVENT_BASED_LB", "NCCL_IB_EVENT_BASED_LB_REMOTE",
    "NCCL_IB_FIFO_TC", "NCCL_IB_GID_INDEX", "NCCL_IB_MERGE_NICS",
    "NCCL_IB_MERGE_VFS", "NCCL_IB_MQP_RETRY_ALL", "NCCL_IB_MQP_RETRY_CNT",
    "NCCL_IB_MQP_RETRY_SLEEP_MSEC", "NCCL_IB_OOO_RQ",
    "NCCL_IB_PCI_RELAXED_ORDERING", "NCCL_IB_PKEY", "NCCL_IB_PKEY_VALUE",
    "NCCL_IB_PREPOST_RECEIVE_WORK_REQUESTS", "NCCL_IB_QPS_PER_CONNECTION",
    "NCCL_IB_QUERY_PORT_SPEED", "NCCL_IB_RECEIVER_SIDE_MATCHING_SCHEME",
    "NCCL_IB_RESILIENCY_PORT_FAILOVER",
    "NCCL_IB_RESILIENCY_PORT_FAILOVER_MAX_ATTEMPTS",
    "NCCL_IB_RESILIENCY_PORT_FAILOVER_PROBE_DELAY",
    "NCCL_IB_RESILIENCY_PORT_RECOVERY",
    "NCCL_IB_RESILIENCY_PORT_RECOVERY_ACK_TIMEOUT",
    "NCCL_IB_RESILIENCY_PORT_RECOVERY_ALIVE_MSG_BATCH_INTERVAL",
    "NCCL_IB_RESILIENCY_PORT_RECOVERY_ALIVE_MSG_BATCH_SIZE",
    "NCCL_IB_RESILIENCY_PORT_RECOVERY_ALIVE_MSG_SEQUENCE_SIZE",
    "NCCL_IB_RESILIENCY_PORT_RECOVERY_ALIVE_MSG_TIMEOUT",
    "NCCL_IB_RESILIENCY_PORT_RECOVERY_ATTEMPTS_MAX",
    "NCCL_IB_RESILIENCY_PORT_RECOVERY_START_DELAY", "NCCL_IB_RETRY_CNT",
    "NCCL_IB_RETURN_ASYNC_EVENTS", "NCCL_IB_ROCE_VERSION_NUM",
    "NCCL_IB_ROUTABLE_FLID_GID_INDEX", "NCCL_IB_SL",
    "NCCL_IB_SPLIT_DATA_ON_QPS", "NCCL_IB_SUBNET_AWARE_ROUTING",
    "NCCL_IB_SUBNET_PREFIX_LEN", "NCCL_IB_TC", "NCCL_IB_TIMEOUT",
    "NCCL_IB_USE_INLINE", "NCCL_IB_WARN_RAIL_LOCAL",
    "NCCL_IB_WQE_LATENCY_REPORT", "NCCL_IB_WQE_LATENCY_THRESHOLD_NS",
    "NCCL_IGNORE_COLLNET_MISMATCH", "NCCL_IGNORE_CPU_AFFINITY",
    "NCCL_IGNORE_DISABLED_P2P", "NCCL_IGNORE_NET_MISMATCH",
    "NCCL_IPC_USE_ABSTRACT_SOCKET", "NCCL_L1_SHARED_MEMORY_CARVEOUT",
    "NCCL_LAUNCH_MODE", "NCCL_LAUNCH_ORDER_IMPLICIT",
    "NCCL_LAUNCH_RACE_FATAL", "NCCL_LEGACY_CUDA_REGISTER",
    "NCCL_LL128_BUFFSIZE", "NCCL_LL128_C2C", "NCCL_LL128_NTHREADS",
    "NCCL_LL_BUFFSIZE", "NCCL_LOCAL_REGISTER", "NCCL_LSA_TEAM_SIZE",
    "NCCL_MAX_CTAS", "NCCL_MAX_NCHANNELS", "NCCL_MAX_NRINGS",
    "NCCL_MAX_P2P_NCHANNELS", "NCCL_MEM_SYNC_DOMAIN", "NCCL_MIN_CTAS",
    "NCCL_MIN_NCHANNELS", "NCCL_MIN_NRINGS", "NCCL_MIN_P2P_NCHANNELS",
    "NCCL_MLOPART_RDMA_ENABLE", "NCCL_MNNVL_CLIQUE_ID",
    "NCCL_MNNVL_CROSS_CLIQUE", "NCCL_MNNVL_ENABLE",
    "NCCL_MNNVL_RAIL_PER_HOST", "NCCL_MNNVL_SCATTER_NETS_ENABLE",
    "NCCL_MNNVL_UUID", "NCCL_MULTI_RANK_GPU_ENABLE",
    "NCCL_MULTI_SEGMENT_REGISTER", "NCCL_NCHANNELS_PER_NET_PEER",
    "NCCL_NET", "NCCL_NETDEVS_POLICY", "NCCL_NET_DISABLE_INTRA",
    "NCCL_NET_FORCE_FLUSH", "NCCL_NET_FORCE_MERGE", "NCCL_NET_GDR_C2C",
    "NCCL_NET_GDR_LEVEL", "NCCL_NET_GDR_MLOPART", "NCCL_NET_GDR_READ",
    "NCCL_NET_MERGE_LEVEL", "NCCL_NET_MERGE_POLICY",
    "NCCL_NET_OPTIONAL_RECV_COMPLETION", "NCCL_NET_OVERHEAD",
    "NCCL_NET_PLUGIN", "NCCL_NET_PLUGIN_REF_COUNT",
    "NCCL_NET_SHARED_BUFFERS", "NCCL_NET_SHARED_COMMS", "NCCL_NO_CACHE",
    "NCCL_NSOCKS_PERTHREAD", "NCCL_NTHREADS", "NCCL_NUM_RMA_CTX",
    "NCCL_NUM_RMA_INT_CTX", "NCCL_NVB_DISABLE", "NCCL_NVB_PRECONNECT",
    "NCCL_NVLINK_UTIL_CENTRIC_SCHED_ENABLE", "NCCL_NVLSTREE_MAX_CHUNKSIZE",
    "NCCL_NVLS_CHUNKSIZE", "NCCL_NVLS_ENABLE", "NCCL_NVLS_NCHANNELS",
    "NCCL_NVTX_DISABLE", "NCCL_OOB_NET_ENABLE", "NCCL_OOB_NET_IFNAME",
    "NCCL_P2P_DIRECT_DISABLE", "NCCL_P2P_DISABLE", "NCCL_P2P_EPOCH_ENABLE",
    "NCCL_P2P_LEVEL", "NCCL_P2P_LL_THRESHOLD", "NCCL_P2P_MAX_PEERS",
    "NCCL_P2P_NET_CHUNKSIZE", "NCCL_P2P_NVL_CHUNKSIZE",
    "NCCL_P2P_PCI_CHUNKSIZE", "NCCL_P2P_PER_CHANNEL_NET_BW",
    "NCCL_P2P_PER_CHANNEL_REG_NET_BW", "NCCL_P2P_PXN_LEVEL",
    "NCCL_P2P_READ_ENABLE", "NCCL_P2P_SCHEDULE_GROUP_SIZE",
    "NCCL_P2P_USE_CUDA_MEMCPY", "NCCL_PARAM_DUMP_ALL", "NCCL_PAT_ENABLE",
    "NCCL_PROFILER_PLUGIN", "NCCL_PROGRESS_APPENDOP_FREQ", "NCCL_PROTO",
    "NCCL_PROXY_APPEND_BATCH_SIZE", "NCCL_PROXY_CPUSET",
    "NCCL_PROXY_DUMP_SIGNAL", "NCCL_PXN_C2C", "NCCL_PXN_DISABLE",
    "NCCL_RAS_ADDR", "NCCL_RAS_ENABLE", "NCCL_RAS_TIMEOUT_FACTOR",
    "NCCL_REPORT_CONNECT_PROGRESS", "NCCL_RMA_DISABLE",
    "NCCL_RMA_EAGER_INIT", "NCCL_RMA_MULTI_CTX_THRESHOLD",
    "NCCL_RMA_PLUGIN", "NCCL_RMA_PLUGIN_REF_COUNT",
    "NCCL_RMA_PROXY_DUMP_SIGNAL", "NCCL_RMA_PROXY_QUEUE_SIZE",
    "NCCL_RUNTIME_CONNECT", "NCCL_RUN_DIAGNOSTICS",
    "NCCL_RUN_RAS_DIAGNOSTICS", "NCCL_SET_CPU_STACK_SIZE",
    "NCCL_SET_STACK_SIZE", "NCCL_SET_THREAD_NAME",
    "NCCL_SHADOW_MEMPOOL_MAX_SIZE", "NCCL_SHM_DISABLE",
    "NCCL_SHM_LOCALITY", "NCCL_SINGLE_PROC_MEM_REG_ENABLE",
    "NCCL_SOCKET_FAMILY", "NCCL_SOCKET_IFNAME", "NCCL_SOCKET_INLINE",
    "NCCL_SOCKET_MAGIC", "NCCL_SOCKET_MIN_TASKSIZE",
    "NCCL_SOCKET_NTHREADS", "NCCL_SOCKET_POLL_TIMEOUT_MSEC",
    "NCCL_SOCKET_RCVBUF", "NCCL_SOCKET_RETRY_CNT",
    "NCCL_SOCKET_RETRY_SLEEP_MSEC", "NCCL_SOCKET_SNDBUF",
    "NCCL_SYM_CE_THRESHOLD", "NCCL_SYM_CTAS",
    "NCCL_SYM_GIN_KERNELS_ENABLE", "NCCL_SYM_KERNEL",
    "NCCL_SYM_NOWIN_ENABLE", "NCCL_SYM_REUSE_SYSMEM_HANDLES",
    "NCCL_SYM_RS_GIN_CHUNK_SIZE", "NCCL_SYM_TMA_ENABLE",
    "NCCL_THREAD_THRESHOLDS", "NCCL_TOPO_DUMP_FILE",
    "NCCL_TOPO_DUMP_FILE_RANK", "NCCL_TOPO_FILE",
    "NCCL_TOPO_SCATTER_START_NET", "NCCL_TOPO_SPLIT_MLOPART",
    "NCCL_TUNER_PLUGIN", "NCCL_UID_STAGGER_RATE",
    "NCCL_UID_STAGGER_THRESHOLD", "NCCL_UNPACK_DOUBLE_NCHANNELS",
    "NCCL_WARN_ENABLE_DEBUG_INFO", "NCCL_WIN_ENABLE", "NCCL_WIN_STRIDE",
    "NCCL_WORK_ARGS_BYTES", "NCCL_WORK_FIFO_BYTES",
})


def parse_nccl_env(text: str | None) -> dict[str, str]:
    """`NCCL_PROTO=LL,NCCL_MIN_NCHANNELS=2` -> a dict. Raises on anything else.

    Operator text, so it is parsed strictly rather than forgivingly: a typo
    that produced an empty dict would launch with no tuning at all and look
    exactly like a launch that was tuned.
    """
    out: dict[str, str] = {}
    for piece in (text or "").split(","):
        piece = piece.strip()
        if not piece:
            continue
        if "=" not in piece:
            raise ValueError(
                f"NCCL setting {piece!r} is not NAME=VALUE"
            )
        name, value = piece.split("=", 1)
        name, value = name.strip(), value.strip()
        if not name or not value:
            raise ValueError(f"NCCL setting {piece!r} has an empty name or value")
        if not _NCCL_VALUE_SAFE.match(value):
            # Same grammar rule as _EXTRA_ARG_SAFE: this ends up in a recipe
            # that sparkrun renders into a shell command, so a value carrying
            # a quote or a space is refused at the door rather than escaped.
            raise ValueError(
                f"NCCL value {value!r} for {name} is not a bare token; "
                f"letters, digits, and ,._:+-/ only"
            )
        out[name] = value
    return out


def nccl_env_refusal(
    runtime: str, nccl_env: Mapping[str, str] | None, *, world_size: int = 1
) -> str | None:
    """Why this runtime cannot be told these NCCL settings, or None.

    Asked before a launch rather than discovered by one, exactly like
    `speculative_refusal`, `graph_capture_refusal` and `kv_cache_dtype_refusal`.

    Three refusals, and the first is the one that matters. NCCL reads its
    environment once at communicator init and silently ignores a name it does
    not know -- there is no warning and no failure -- so a misspelled variable
    is a launch that reports as tuned and is not. `NCCL_TUNABLES` is the
    image's own table, so this catches it at the door.
    """
    if not nccl_env:
        return None
    spec = runtime_spec(runtime)
    if not spec.shards:
        return (
            f"the {runtime} runtime runs a single process and opens no NCCL "
            f"communicator, so {', '.join(sorted(nccl_env))} would be set in "
            f"its environment and read by nothing"
        )
    unknown = sorted(k for k in nccl_env if k not in NCCL_TUNABLES)
    if unknown:
        return (
            f"{', '.join(unknown)} is not read by the NCCL in this image "
            f"(2.31.2, {len(NCCL_TUNABLES)} variables). NCCL ignores an "
            f"unknown name without warning, so the launch would report as "
            f"tuned and run with the defaults"
        )
    if world_size <= 1:
        return (
            f"a single-rank launch runs no collective, so "
            f"{', '.join(sorted(nccl_env))} would change nothing. Plan across "
            f"more than one rank, or drop the setting"
        )
    return None


def render_kv_cache_dtype(kv_dtype: str | None) -> str | None:
    """The `--kv-cache-dtype` value for a derate kv_dtype, or None for "don't".

    None means "pass no flag" -- the model's own dtype, which is what every
    launch does today. A value this build cannot serve raises rather than
    returning None, because silently falling back to the model default is
    precisely the bug this function exists to end: the fit gate would have
    sized the cache at half the bytes and approved a context the engine then
    cannot hold.
    """
    key = (kv_dtype or "auto").strip().lower()
    if key not in _KV_DTYPE_TO_VLLM:
        raise ValueError(
            f"kv_dtype {kv_dtype!r} has no --kv-cache-dtype this build can "
            f"request; the fit gate would size for it and the engine would "
            f"not honour it"
        )
    rendered = _KV_DTYPE_TO_VLLM[key]
    if rendered is not None and rendered not in VLLM_KV_CACHE_DTYPES:
        raise ValueError(
            f"kv_dtype {kv_dtype!r} renders as {rendered!r}, which this "
            f"image's CacheConfig does not accept"
        )
    return rendered


#: What `--quantization` accepts, read off the pinned image's own registry
#: rather than copied from documentation -- the same evidence rule
#: `VLLM_KV_CACHE_DTYPES` and `VLLM_ARCHITECTURES` follow, and for the same
#: reason: a name the image cannot load clears every gate and dies at load.
#:
#: Re-read it with:
#:   docker run --rm --entrypoint python3 <image> -c \
#:     "from vllm.model_executor.layers.quantization import QUANTIZATION_METHODS; \
#:      print(sorted(QUANTIZATION_METHODS))"
VLLM_QUANTIZATION_METHODS: frozenset[str] = frozenset({
    "auto_awq", "auto_gptq", "awq", "awq_marlin", "compressed-tensors",
    "deepseek_v4_fp8", "experts_int8", "fbgemm_fp8", "fp8", "fp8_per_block",
    "fp8_per_channel", "fp8_per_tensor", "fp_quant", "gpt_oss_mxfp4", "gptq",
    "gptq_marlin", "humming", "inc", "int8_per_channel_weight_only",
    "modelopt", "modelopt_fp4", "modelopt_mixed", "modelopt_mxfp8",
    "moe_wna16", "mxfp4", "mxfp8", "nvfp4_per_token", "online", "quark",
    "torchao",
})

#: derate's dtype key -> the runtime's `--quantization` name, or None for the
#: unquantized widths, which take no flag at all.
#:
#: Deliberately NOT all 33 schemes `contracts/quant.py` prices. This table is
#: what the operator may FORCE, and forcing is only meaningful where the
#: runtime has a loader that reads that packing:
#:
#:   * the GGUF ladder (21 of the 33) is llama.cpp's format. `resolver.py`
#:     already marks every GGUF variant unlaunchable, so a forced GGUF
#:     quantization is a launch nothing could serve.
#:   * `nf4` is bitsandbytes, and **`bitsandbytes` is not in this image's
#:     registry** -- read above, not assumed. Absent rather than mapped, so it
#:     refuses by name instead of dying at load.
#:   * `int8` is carried by checkpoints as compressed-tensors metadata rather
#:     than requested by a flag; there is no value here that forces it without
#:     claiming a packing the weights may not have.
_QUANT_TO_VLLM: dict[str, str | None] = {
    "fp32": None,
    "fp16": None,
    "bf16": None,
    "fp8": "fp8",
    "awq_int4": "awq",
    "gptq_int4": "gptq",
    "mxfp4": "mxfp4",
    "nvfp4": "modelopt_fp4",
}


def forceable_quantizations() -> tuple[str, ...]:
    """The derate dtype keys an operator may force, best quality first.

    Ordered by bytes per parameter so a picker reads as a ladder rather than
    as a dictionary. The three unquantized widths are in it: choosing one is
    a real answer ("serve this checkpoint as it ships") and it renders no
    flag.
    """
    return tuple(
        sorted(_QUANT_TO_VLLM, key=lambda k: -BYTES_PER_PARAM[k])
    )


def render_quantization(dtype: str | None) -> str | None:
    """The `--quantization` value for a derate dtype, or None for "don't".

    None means "pass no flag" -- the runtime reads the packing off the
    checkpoint, which is what every launch does today. A scheme this build
    cannot request RAISES rather than returning None, for the reason
    `render_kv_cache_dtype` raises: forcing a scheme re-prices the WEIGHTS,
    so the fit gate has already answered at that width, and falling back to
    the checkpoint's own would be the `--kv-cache-dtype` bug again with a
    bigger term.
    """
    key = (dtype or "").strip().lower()
    if key in ("", "auto", "model", "none"):
        return None
    if key not in _QUANT_TO_VLLM:
        raise ValueError(
            f"quantization {dtype!r} cannot be forced by this build, so the "
            f"runtime would load this repository's own weights at their real "
            f"precision while the fit check budgeted for something smaller. "
            f"Forceable: {', '.join(forceable_quantizations())}. A GGUF "
            f"scheme is llama.cpp's format and is not launchable here at all; "
            f"nf4 needs a bitsandbytes loader this image does not carry. For "
            f"those, pass that quantization's own repository id as model_id "
            f"-- it is a different repository, not a flag"
        )
    rendered = _QUANT_TO_VLLM[key]
    if rendered is not None and rendered not in VLLM_QUANTIZATION_METHODS:
        raise ValueError(
            f"quantization {dtype!r} renders as {rendered!r}, which this "
            f"image's quantization registry does not hold"
        )
    return rendered


def quantization_refusal(runtime: str, dtype: str | None) -> str | None:
    """Why this runtime cannot be told to use *dtype*, or None.

    Asked before a launch rather than discovered by one, exactly like
    `speculative_refusal`, `graph_capture_refusal` and
    `kv_cache_dtype_refusal`.

    It REFUSES rather than dropping, and that is the whole point. This
    module's own header says a knob whose recipe_key is absent from the
    template is dropped in silence -- which is survivable for a launch-only
    option and is not survivable here: `BYTES_PER_PARAM` differs by 3.5x
    between bf16 and nvfp4, so the gate has already approved a plan at the
    forced width. Dropped, the engine loads the checkpoint's own packing and
    the approved budget is wrong by that factor.
    """
    key = (dtype or "").strip().lower()
    if key in ("", "auto", "model", "none"):
        return None
    try:
        rendered = render_quantization(key)
    except ValueError as exc:
        return str(exc)
    if rendered is None:
        return None
    spec = runtime_spec(runtime)
    if not spec.quantization_arg:
        return (
            f"the {runtime} runtime is not told its weight quantization by "
            f"this build, so quantization={dtype!r} would silently not reach "
            f"the launch command -- and the fit gate has already priced the "
            f"weights at that width, so the launch would be budgeted for a "
            f"checkpoint it is not going to load"
        )
    return None


def kv_cache_dtype_refusal(runtime: str, kv_dtype: str | None) -> str | None:
    """Why this runtime cannot be told to use *kv_dtype*, or None.

    Asked before a launch rather than discovered by one, exactly like
    `speculative_refusal` and `graph_capture_refusal`.

    This one is worse than those two if it goes unasked, because it is not
    merely ignored -- it is ignored AFTER the fit gate has already believed
    it. A request for fp8 halves `kv_bytes_per_token`, so the gate approves a
    context it would otherwise refuse, passes the halved budget through
    `--kv-cache-memory-bytes`, and the engine then fills that budget with
    fp16 entries. No OOM, no error: the operator silently gets half the
    context the gate promised.
    """
    key = (kv_dtype or "auto").strip().lower()
    if key in ("", "auto", "model", "none"):
        return None
    try:
        rendered = render_kv_cache_dtype(key)
    except ValueError as exc:
        return str(exc)
    if rendered is None:
        return None
    spec = runtime_spec(runtime)
    if not spec.kv_cache_dtype_arg:
        return (
            f"the {runtime} runtime is not told its KV cache dtype by this "
            f"build, so kv_dtype={kv_dtype!r} would silently not reach the "
            f"launch command -- and the fit gate has already sized the cache "
            f"as if it had, so the launch would get half the context it was "
            f"approved for"
        )
    return None


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

# The CPU runtime, and the only template here that names no parallelism flag
# at all. That is not an omission of the kind the _TTS_COMMAND note above warns
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
# `llamacpp_model_spec` below -- and sparkrun's llama-cpp plugin rewrites it to
# `-m <cache path>` once it has resolved the file (runtimes/llama_cpp.py::
# generate_command).
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
# The `PATH=` prefix is load-bearing and is about the IMAGE, not the flags.
# derate does not use sparkrun's own llama-cpp image
# (`scitrera/dgx-spark-llama-cpp`, which links libcuda and so cannot run on the
# machines this runtime exists for) -- it defaults to the upstream CPU build,
# where the binary is `/app/llama-server`, is the image's ENTRYPOINT, and is
# NOT on PATH. derate then clears that entrypoint (`executor_config` below,
# which it must, because a `sleep infinity` container cannot also run it), so a
# bare `llama-server` resolves against PATH alone and dies with
# `llama-server: not found` -- verified against the pinned image. Prepending
# `/app` rather than writing an absolute path keeps BOTH images working: the
# CUDA one has it at /usr/local/bin, which PATH still finds, so setting
# DERATE_LLAMACPP_IMAGE back to sparkrun's own does not break the command.
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


# The binary-and-model half of each command above, with none of the
# plan-derived flags -- what recipes.py::_custom_serve_command builds on when
# the operator supplies the rest themselves. Every runtime here takes
# --host/--port/--served-model-name under the same three names (verified
# against each one's own argparse table), which is what lets
# _custom_serve_command append them generically rather than per runtime.
_VLLM_CUSTOM_PREFIX = "vllm serve \\\n    {model}"
_TTS_CUSTOM_PREFIX = "python3 -m control_plane.runtimes.tts \\\n    --model {model}"
_SGLANG_CUSTOM_PREFIX = "python3 -m sglang.launch_server \\\n    --model-path {model}"
_LLAMACPP_CUSTOM_PREFIX = 'PATH="/app:$PATH" llama-server \\\n    -hf {model}'


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
    ),
    "sglang": RuntimeSpec(
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
        # different shape from vLLM's pair above, and this exact pairing has
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
    ),
    # The CPU runtime, and the first entry here whose point is the machine
    # rather than the model. vLLM and SGLang need a CUDA device; a Raspberry
    # Pi, a NAS or a spare x86 box has none, and until this existed such a
    # machine could join the roster and never serve anything -- which is what
    # `addressable_memory == 0` meant when it was written.
    #
    # Unlike `tts` above, `sparkrun_runtime` is not a borrow: sparkrun ships a
    # real llama-cpp plugin (runtimes/llama_cpp.py, `runtime_name =
    # "llama-cpp"`), which renders this recipe's own `command:` verbatim and
    # additionally resolves the GGUF into the container's cache before exec.
    "llamacpp": RuntimeSpec(
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


#: The JSON `--speculative-config` may contain, as a whole-string grammar.
#:
#: Not a blocklist of shell metacharacters -- an allowlist, for the same reason
#: `_COMMAND_SAFE` in recipes.py is one: the blocklist for `bash -c` is
#: unbounded. What this admits is a flat JSON object of bare keys mapping to
#: quoted words and integers, which is everything `render_speculative_config`
#: can produce and nothing that closes the single quote wrapped around it in
#: the command template.
#: A value is a bare integer, a lower-case word (the method), or a model id.
#: The model-id class is exactly `_COMMAND_SAFE`'s in recipes.py, and for the
#: identical reason -- it is a repository name that reaches `bash -c` -- but
#: without the quote characters that class never admitted either, so nothing
#: here can close the single quote the template wraps this in.
_SPECULATIVE_CONFIG_SAFE = re.compile(
    r'\A\{"[a-z_]+": (?:"[A-Za-z0-9/.~][A-Za-z0-9._:/~-]*"|\d+)'
    r'(?:, "[a-z_]+": (?:"[A-Za-z0-9/.~][A-Za-z0-9._:/~-]*"|\d+))*\}\Z'
)


def speculative_refusal(runtime: str, method: str) -> str | None:
    """Why this runtime cannot be told to speculate, or None.

    Asked before a launch rather than discovered by one, exactly like
    :func:`sharding_refusal`. A runtime with no flag for this would otherwise
    pass every gate -- the fit arithmetic charged the draft's memory and the
    memory is genuinely there -- and then serve at the ordinary rate while the
    screen said it was speculating, which is the failure nobody notices.
    """
    spec = runtime_spec(runtime)
    if spec.speculative_config_arg:
        return None
    return (
        f"the {runtime} runtime is not told about speculative decoding by this "
        f"build, so a launch with {method} would start and then decode one "
        f"token per step with the draft's memory still charged against it; "
        f"serve this on vllm, or launch without speculative decoding"
    )


def render_speculative_config(
    method: str, num_speculative_tokens: int, model: str | None = None
) -> str:
    """The JSON `vllm serve --speculative-config` takes, built from its parts.

    *method* must already be one of the contract's ``SpeculativeMethod``
    spellings and *num_speculative_tokens* an int; both are checked again here
    rather than trusted, because the string this returns is substituted into a
    command that runs under ``bash -c`` inside a privileged, host-networked
    container. The check is on the rendered result, not on the inputs: that is
    the string that actually reaches the shell, and validating anything else
    leaves a gap between what was approved and what runs.

    *model* is the head's own repository, for a method whose draft ships
    separately from the target. It is the one field here that carries operator
    text, so it goes through recipes.py's own model-id grammar first -- the
    same one `shape.model_id` passes -- and then through the whole-string check
    below like everything else.
    """
    from control_plane.contracts.plan import SpeculativeMethod

    # Raises on anything not in the enum. The enum is the allowlist -- there is
    # deliberately not a second copy of these names here.
    name = SpeculativeMethod(method).value
    body: dict[str, Any] = {"method": name}
    if model:
        from .recipes import _check_command_safe

        _check_command_safe(model, "speculative.model")
        body["model"] = model
    body["num_speculative_tokens"] = int(num_speculative_tokens)
    rendered = json.dumps(body, separators=(", ", ": "))
    if not _SPECULATIVE_CONFIG_SAFE.match(rendered):
        raise ValueError(
            "refusing to substitute %r into the launch command: it is not the "
            "flat JSON object this flag is allowed to carry" % rendered
        )
    return rendered


def render_cudagraph_capture_sizes(sizes: Sequence[int]) -> str:
    """The space-separated list `--cudagraph-capture-sizes` takes.

    Every element is re-validated here rather than trusted, same reasoning as
    `render_speculative_config`: a positive, strictly ascending int is what
    vLLM's own flag documents as sane (a captured graph is size-specific, and
    the runtime derives `max_cudagraph_capture_size` from the largest entry).
    Nothing here can carry a shell metacharacter once every element has gone
    through `int()` -- there is no whole-string grammar to check because
    there is no string surface for one to close over, unlike the JSON
    `render_speculative_config` builds.
    """
    if not sizes:
        raise ValueError("cudagraph_capture_sizes must not be empty")
    ints = [int(s) for s in sizes]
    if any(n <= 0 for n in ints):
        raise ValueError(
            "cudagraph_capture_sizes must be positive integers, got %r" % (ints,)
        )
    if ints != sorted(set(ints)):
        raise ValueError(
            "cudagraph_capture_sizes must be strictly ascending with no "
            "duplicates, got %r" % (ints,)
        )
    return " ".join(str(n) for n in ints)


def graph_capture_refusal(
    runtime: str, enforce_eager: bool, cudagraph_capture_sizes: Sequence[int] | None
) -> str | None:
    """Why this runtime cannot be told to change CUDA graph capture, or None.

    Asked before a launch rather than discovered by one, exactly like
    `speculative_refusal` and `sharding_refusal`. A runtime with no flag for
    this would otherwise pass every gate, start, and silently keep its own
    default graph behavior while the request said otherwise -- the failure
    nobody notices, same shape as speculating with no draft flag.
    """
    spec = runtime_spec(runtime)
    if enforce_eager and not spec.enforce_eager_arg:
        return (
            f"the {runtime} runtime is not told to force eager execution by "
            f"this build, so enforce_eager would silently not reach the "
            f"launch command and the runtime would keep its own default "
            f"graph-capture behavior; launch without it, or serve this on "
            f"a runtime that supports it"
        )
    if cudagraph_capture_sizes and not spec.cudagraph_capture_sizes_arg:
        return (
            f"the {runtime} runtime is not told a trimmed CUDA graph capture "
            f"list by this build, so cudagraph_capture_sizes would silently "
            f"not reach the launch command and the runtime would keep its "
            f"own default capture sizes; launch without it, or serve this "
            f"on a runtime that supports it"
        )
    return None


#: A model id this runtime cannot be handed, and why.
#:
#: Same shape and the same reason as every other refusal in this module: the
#: question is asked before a launch rather than discovered by one.
def placement_refusal(
    runtime: str,
    node_id: str,
    addressable_memory: int,
    live_budget: int | None,
) -> str | None:
    """Why this runtime cannot be placed on this machine, or None.

    One function because there are two directions and six call sites, and for
    most of this project's life there was only one direction: every runtime
    needed a GPU, so `addressable_memory <= 0` answered the whole question and
    the answer was spelled out by hand wherever it was needed. A CPU runtime
    splits that into a pair, and a pair spelled out by hand in six places is a
    pair that disagrees within a month.

    The three cases:

    * a GPU runtime on a machine with no GPU memory -- the original refusal,
      unchanged, still the common one;
    * a host runtime on a machine that HAS a GPU. The reason is an accounting
      one and not a preference, which matters because a preference is not
      grounds for a refusal in this project. llama.cpp would RUN on such a
      machine -- slowly, on its CPU, and nothing would break. What derate
      cannot do is BUDGET it: `registry.allocatable_bytes` returns host memory
      only for `DeviceClass.CPU`, and a GPU figure for everything else, so the
      fit gate would compare a host-RAM demand against a GPU number. That is
      wrong in both directions -- it can approve a launch that exhausts RAM
      and refuse one that would have fitted.

      Two things follow. The refusal is about THIS BUILD rather than about
      llama.cpp, so it names what would have to change. And it is wider than
      it strictly needs to be: on a discrete-GPU box VRAM and host RAM are
      separate pools and a host-pool runtime there is perfectly budgetable --
      it is refused anyway, because `allocatable_bytes` has no branch that
      would answer for it. Narrowing that is a change to that function, not to
      this one;
    * a host runtime on a CPU machine nothing has sampled. A GPU node with no
      sample falls back to its nameplate, because a nameplate describes a real
      device. A CPU node has no nameplate -- `addressable_memory` is 0 and
      always will be -- so there is nothing to fall back TO, and the honest
      answer is to say the measurement is missing rather than invent a budget.
      This is the one place in derate where a missing live reading refuses
      instead of degrading, and it does so because degrading would mean
      fabricating.
    """
    spec = RUNTIMES.get(runtime)
    if spec is None:
        return None
    if spec.memory_pool == "host":
        if addressable_memory > 0:
            others = ", ".join(
                name for name, other in RUNTIMES.items() if other.memory_pool == "gpu"
            )
            return (
                f"the {runtime} runtime serves from host RAM, and derate only "
                f"measures host RAM on a machine with no GPU. On '{node_id}' the "
                f"fit gate would size this launch against GPU memory the server "
                f"never touches -- a budget for the wrong pool, which can approve "
                f"a launch that exhausts RAM as easily as refuse one that would "
                f"have fitted. Serve it on a machine with no GPU, or use one of "
                f"the GPU runtimes here ({others})."
            )
        if live_budget is None:
            return (
                f"Nothing has measured how much memory '{node_id}' has free, "
                f"and {runtime} is budgeted against that reading rather than "
                f"against a specification -- a machine with no GPU has no "
                f"memory figure to fall back on. Wait for it to report, or "
                f"check that its agent is running."
            )
        if live_budget <= 0:
            return (
                f"'{node_id}' reports no free memory to serve from right now. "
                f"It is a cluster member; it cannot carry this launch until "
                f"something on it releases memory."
            )
        return None
    if addressable_memory <= 0:
        return (
            f"The machine '{node_id}' reports no addressable GPU memory, so "
            f"{runtime} cannot be placed on it. It can still be a cluster "
            f"member; it cannot be a serving node for this runtime."
        )
    return None


def is_hub_gguf_blob(model_id: str) -> bool:
    """Whether this is derate's own post-resolve spelling for a hub GGUF blob.

    ``hf://owner/repo/file.gguf`` is what an operator passes and what the
    resolver is documented to take. It is NOT what comes back:
    ``ModelResolver.resolve_gguf_full`` sets ``name = f"{repo}/{filename}"``
    and drops the scheme, so every consumer downstream of a resolve sees
    ``owner/repo/file.gguf``.

    That mattered because two functions below were written against the scheme
    and are both called with the resolved id -- `llamacpp_model_refusal` from
    `manager.launch` and `llamacpp_model_spec` from `recipes.synthesize`. One
    refused every legitimate hub GGUF as a local path; the other declined to
    translate it and left sparkrun an id it cannot parse. Same root cause, so
    one predicate rather than two copies of the same `startswith`.

    The test is deliberately narrow, because the thing it must not swallow is
    a genuine local path -- which also ends in ``.gguf`` and also has slashes.
    A hub reference has at least three segments (owner, repo, and a filename
    that may sit under a quant subdirectory) and does not begin with a path
    sigil. ``/home/me/x.gguf`` and ``./x.gguf`` are therefore not hub blobs
    and stay refusable; ``owner/repo-GGUF`` has two segments and no ``.gguf``
    suffix, so sparkrun's own spelling is untouched.
    """
    name = model_id.strip()
    if name.startswith(("/", "~", ".")):
        return False
    if not name.lower().endswith(".gguf"):
        return False
    return len(name.split("/")) >= 3


def llamacpp_model_refusal(runtime: str, model_id: str) -> str | None:
    """Refuse a model id that names a file on THIS machine, for a CPU launch.

    derate's resolver happily takes a local directory or a local ``.gguf``
    path, and for every question the resolver answers -- what shape is this,
    what does it weigh -- a local file is a perfectly good answer. A launch is
    a different question, and this is the one runtime where the difference
    bites: the container starts on the node the plan placed it on, which is
    the whole point of a CPU runtime (that node is a Pi, and this one is not).
    A path that resolves here names nothing there.

    The other three runtimes do not need this said. They launch onto machines
    that are the same kind of machine as the coordinator, and sparkrun's vLLM
    plugins distribute a model before they start -- but nothing distributes a
    loose file the operator happens to have in a home directory, and no gate
    in this project has ever checked for one. So this refuses rather than
    launching something that fails at open() several minutes later, inside a
    container, on another machine.
    """
    if runtime != "llamacpp":
        return None
    name = model_id.strip()
    if (
        name.lower().endswith(".gguf")
        and not name.startswith("hf://")
        # ...and is not the scheme-less form the resolver hands back for a hub
        # blob, which this used to read as a local path and refuse. See
        # `is_hub_gguf_blob`: the caller is `manager.launch`, and it passes
        # `shape.model_id`, so this arm saw a resolved id every time.
        and not is_hub_gguf_blob(name)
    ):
        return (
            "llamacpp cannot launch %r: that is a path to a file on this "
            "machine, and the server starts on the node the plan placed it "
            "on. Name the repository it came from instead "
            "(owner/repo-GGUF:Q4_K_M, or hf://owner/repo/file.gguf) and the "
            "node will fetch it itself." % model_id
        )
    return None


def llamacpp_model_spec(model_id: str) -> str:
    """derate's GGUF spelling -> the one sparkrun's llama-cpp plugin parses.

    Two names for one file. derate resolves ``hf://owner/repo/file.gguf`` --
    that form names the exact blob, which is what lets `resolver/gguf.py`
    measure it by summing its tensor directory rather than estimating from a
    parameter count. sparkrun takes ``owner/repo:QUANT``
    (models/download.py::parse_gguf_model_spec) and globs the cache for a
    ``.gguf`` whose name contains that token, case-insensitively.

    So the translation is: keep the repository, and carry the quantization as
    the token the PUBLISHER wrote, because that token is a substring of the
    filename by construction -- which is exactly what sparkrun matches on.
    `gguf_names.quant_token` is that string and is already the one this
    project shows on screen; deriving it a second way here is how the two
    would come to disagree.

    Both spellings of the blob are translated: the ``hf://`` reference an
    operator passes, and the scheme-less ``owner/repo/file.gguf`` the resolver
    hands back for it (`is_hub_gguf_blob`). The second is the one that
    actually arrives -- `recipes.synthesize` calls this with
    ``shape.model_id`` -- and testing only for the scheme meant the
    translation never fired on a real launch, leaving sparkrun a three-segment
    id whose `parse_gguf_model_spec` finds no colon and takes the whole string
    as a repository that does not exist.

    Anything that is neither is returned unchanged: a plain
    ``owner/repo-GGUF`` or ``owner/repo-GGUF:Q4_K_M`` is already sparkrun's
    own spelling, and this must not paraphrase an id an operator typed.

    The import is deferred on purpose. `deploy/` has never imported
    `resolver/`, and `from control_plane.resolver.gguf_names import ...` runs
    `resolver/__init__.py`, which pulls in `hf.py` and huggingface_hub. That
    is a real cost on a path that otherwise needs none of it, for one pure
    regex over a filename.
    """
    name = model_id.strip()
    if name.startswith("hf://"):
        ref = name[len("hf://") :]
    elif is_hub_gguf_blob(name):
        ref = name
    else:
        return name
    from control_plane.resolver.gguf_names import quant_token

    parts = ref.split("/")
    if len(parts) < 3:
        # Not owner/repo/file: nothing to translate, and inventing a shape for
        # it would be worse than handing it over as written.
        return name
    repo = "/".join(parts[:2])
    token = quant_token("/".join(parts[2:]))
    return "%s:%s" % (repo, token) if token else repo


def data_parallel_refusal(
    tensor_parallel: int,
    pipeline_parallel: int,
    data_parallel: int = 1,
    launcher_version: str | None = None,
) -> str | None:
    """Why a pure data-parallel plan cannot be launched, or None.

    Pure means ``tp * pp == 1`` with ``dp > 1``, which is exactly the shape
    cross-node expert parallel takes here -- `enumerate_candidates` emits
    ``tp=1, pp=1, ep=world, dp=world``, because vLLM's expert-parallel size is
    ``dp * tp``.

    vLLM itself is fine with it. Measured on this estate against
    0.28.1rc1.dev462+g9ca97b28b: the engine parsed every flag
    (``'data_parallel_size': 2, 'data_parallel_rank': 0, 'enable_expert_parallel':
    True``) and got as far as ``Started DP Coordinator process``. What cannot
    finish is the launch. sparkrun's cluster path
    (`runtimes/_cluster_ops.py::run_native_cluster`) execs the head's serve
    command, waits for the head to bind the torch-distributed **master port**
    -- `vllm_distributed.py` passes `init_port=25000, port_label="Master Port"`
    -- and only starts the workers in step 7, *after* that wait returns. A plan
    with ``tp * pp == 1`` never emits ``--master-port`` at all
    (`generate_node_command` adds the torch-distributed quartet only when
    ``replica_size > 1``), so nothing ever binds 25000, the wait burns its 60
    retries, and sparkrun gives up with "Head node failed to become ready"
    without ever starting rank 1 -- while the head sits in its DP Coordinator
    waiting for the rank that is never coming. Deterministic, not a race, and
    there is no flag to skip the wait.

    The single-node story is a different silence with the same ending: a solo
    launch goes through `generate_command`, which returns the rendered recipe
    command verbatim and injects no data-parallel flags at all, so the degree
    would evaporate between the plan on screen and the process on the machine.

    Refused rather than attempted because the failure takes 128 seconds and
    reports a head-node timeout -- a sentence that points at the network and
    the image, which are both fine. The shapes that DO work are named: expert
    parallel inside one multi-GPU box is ``tp=ep, dp=1``, which is ordinary
    tensor parallel as far as sparkrun is concerned and launches normally.

    **And it is a version gate, not a permanent one.** sparkrun 0.3.7 fixed
    exactly this (`SPARKRUN_DATA_PARALLEL_VERSION`), so a new enough launcher is
    not refused at all -- verified on 0.3.8 by launching `openai/gpt-oss-20b` at
    EP=2/DP=2 across two Sparks: "Step 7/7: Starting worker nodes", both ranks
    resident at 13007 MiB, a completion served, and vLLM's own
    `system_fingerprint` ending `-dp2-ep`. A version that cannot be read still
    refuses, and `launcher_does_data_parallel` says why that way round.
    """
    if int(data_parallel) <= 1:
        return None
    if int(tensor_parallel) * int(pipeline_parallel) > 1:
        return None
    if launcher_does_data_parallel(launcher_version):
        return None
    return (
        "data parallel %d with tensor parallel 1 and pipeline parallel 1 is a "
        "shape this launcher cannot bring up. vLLM accepts it -- the engine "
        "parses --data-parallel-size and --enable-expert-parallel and starts "
        "its DP coordinator -- but sparkrun's cluster path waits for the head "
        "node to bind the torch-distributed master port before it starts any "
        "worker, and a plan with tensor x pipeline = 1 never binds that port. "
        "The head then waits for a rank that is never launched. Serve this "
        "with a tensor- or pipeline-parallel degree above 1, or on one node "
        "with expert parallel equal to the tensor-parallel degree, which is "
        "what expert parallel inside a single box means to vLLM -- or upgrade "
        "the launcher, because sparkrun %s fixed this and the shape then "
        "launches unchanged (`uv tool upgrade sparkrun`)."
        % (int(data_parallel), SPARKRUN_DATA_PARALLEL_VERSION)
    )


def sharding_refusal(
    runtime: str,
    tensor_parallel: int,
    pipeline_parallel: int,
    expert_parallel: int = 1,
    data_parallel: int = 1,
    launcher_version: str | None = None,
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
        # This runtime can hold a sharded model, so the only question left is
        # whether the launcher can start the shape -- a different failure with
        # a different sentence. Asked here rather than above it because a
        # runtime that cannot shard at all deserves to say so first: for `tts`
        # every degree above 1 is refused, and "one process holding one
        # checkpoint" is the useful half of why.
        return data_parallel_refusal(
            tensor_parallel, pipeline_parallel, data_parallel, launcher_version
        )
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
