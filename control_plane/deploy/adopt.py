"""Reconstruct a launch's shape from the command line it is still running.

The mirror image of ``recipes.py``'s command templates: where that module
turns a plan into flags, this turns flags back into the pieces a
``Deployment`` needs. Written against the *same* flag vocabulary
(``flags.py``'s ``_VLLM_COMMAND`` / ``_TTS_COMMAND`` / ``_SGLANG_COMMAND``) so
the two cannot silently drift apart -- a flag renamed there and not here is a
container this stops recognising, not one it misreads.

Every field comes from a flag the recipe actually renders, never inferred.
``None`` means the command did not look like one of the three runtimes this
project launches, or was missing a flag routing depends on -- both are a
refusal to adopt, never a guess filled in for it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Matches the binary by its last path segment, since the command line
#: carries whatever absolute path resolved it (``/usr/local/bin/vllm``) and a
#: bare word-start anchor would refuse to match "vllm" preceded by "/".
_VLLM_MODEL_RE = re.compile(r"(?:^|/)vllm\s+serve\s+(\S+)")
_SGLANG_MARKER = "sglang.launch_server"
_TTS_MARKER = "control_plane.runtimes.tts"
#: The binary, not a python module path like the two above. Anchored the way
#: `_VLLM_MODEL_RE` is, so a `--chat-template` value that happens to contain
#: the word cannot be mistaken for the invocation.
_LLAMACPP_RE = re.compile(r"(?:^|/|\s)llama-server(?:\s|$)")

#: sparkrun's own container-naming convention, confirmed live: a solo launch
#: is named ``sparkrun_<hex>_solo``; a multi-host one is presumably
#: ``_head``/``_target`` (sparkrun.py's launch() already parses those roles
#: out of `sparkrun run`'s stdout). Distinct from sparkrun.py's own
#: CLUSTER_ID_RE, which parses a labelled *log line* ("Cluster: sparkrun_xxx"),
#: not a bare container name -- a different string shape, not the same fact
#: read twice.
_CONTAINER_NAME_RE = re.compile(r"^(sparkrun_[0-9a-f]{6,})_[a-z0-9]+$")


def cluster_id_from_container_name(name: str | None) -> str | None:
    """The cluster id sparkrun embedded in a container's name, or None when
    *name* does not look like one sparkrun launched at all."""
    if not name:
        return None
    match = _CONTAINER_NAME_RE.match(name)
    return match.group(1) if match else None


@dataclass(frozen=True)
class AdoptedSpec:
    """What a running serve command says it is, in the vocabulary ``Deployment``
    itself uses -- not a raw flag dump."""

    runtime: str  # "vllm" | "sglang" | "tts"
    model_id: str
    served_name: str
    port: int
    tensor_parallel: int
    pipeline_parallel: int
    expert_parallel: bool
    context_length: int
    max_concurrent_seqs: int
    gpu_memory_utilization: float


def _flag(command: str, name: str) -> str | None:
    """The value of ``--name VALUE`` (or ``--name=VALUE``), or None if absent.

    Space and ``=`` both appear across these three runtimes' own argparse
    tables, so both are read. The lookbehind refuses a flag that is itself
    the tail of a longer one (``--max-num-seqs`` must not match a search for
    ``--num-seqs``, which does not exist here but is the class of mistake a
    plain substring search invites).
    """
    match = re.search(r"(?<![\w-])--%s[ =](\S+)" % re.escape(name), command)
    return match.group(1) if match else None


def _short_flag(command: str, name: str) -> str | None:
    """The value of a SINGLE-dash flag: ``-hf VALUE`` or ``-m VALUE``.

    Separate from `_flag` rather than a parameter on it, because the two
    grammars differ in more than the dash count. A single-dash flag must not
    match inside a double-dash one -- searching for ``-m`` has to miss
    ``--model``, ``--max-model-len`` and ``--no-webui`` alike -- so the
    lookbehind refuses a preceding dash as well as a word character, and the
    ``=`` spelling is deliberately not accepted: llama-server's short options
    take a following argument and nothing here should invent a form its
    parser does not read.
    """
    match = re.search(r"(?<![\w-])-%s[ ](\S+)" % re.escape(name), command)
    return match.group(1) if match else None


def _int_flag(command: str, name: str, default: int) -> int:
    value = _flag(command, name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _float_flag(command: str, name: str, default: float) -> float:
    value = _flag(command, name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def parse_serve_command(command: str | None) -> AdoptedSpec | None:
    """None when *command* is not a recognized serve invocation for one of the
    four runtimes, or is missing a flag routing needs (port, served name,
    context)."""
    if not command:
        return None

    if _SGLANG_MARKER in command:
        runtime = "sglang"
        model_id = _flag(command, "model-path")
        tensor_parallel = _int_flag(command, "tp-size", 1)
        pipeline_parallel = _int_flag(command, "pp-size", 1)
        context_length = _int_flag(command, "context-length", 0)
        max_concurrent_seqs = _int_flag(command, "max-running-requests", 1)
        gpu_memory_utilization = _float_flag(command, "mem-fraction-static", 0.0)
        expert_parallel = "--enable-ep-moe" in command
    elif _TTS_MARKER in command:
        runtime = "tts"
        model_id = _flag(command, "model")
        tensor_parallel = _int_flag(command, "tensor-parallel-size", 1)
        pipeline_parallel = _int_flag(command, "pipeline-parallel-size", 1)
        context_length = _int_flag(command, "max-model-len", 0)
        max_concurrent_seqs = _int_flag(command, "max-num-seqs", 1)
        gpu_memory_utilization = _float_flag(command, "gpu-memory-utilization", 0.0)
        expert_parallel = False  # _TTS_COMMAND has no expert_parallel_arg
    elif _LLAMACPP_RE.search(command):
        runtime = "llamacpp"
        # `-hf` here and not `--model`: llama-server's own spelling, and the
        # one derate's template writes. sparkrun rewrites it to `-m <path>`
        # once it has resolved the file, so a container adopted after that
        # rewrite carries a path rather than a repository -- `_flag` finds
        # whichever is there and the id is reported as the running process
        # actually has it, which is the point of adoption.
        model_id = _short_flag(command, "hf") or _short_flag(command, "m")
        # Not "unknown": this runtime cannot shard at all (`shards=False`), so
        # 1 is the only value either flag could have had.
        tensor_parallel = 1
        pipeline_parallel = 1
        # The inverse of what `recipes.synthesize` does on the way out, and it
        # has to be: `--ctx-size` is llama.cpp's SHARED pool, divided across
        # its slots (`--ctx-size 8192 --parallel 4` prints `n_ctx_slot =
        # 2048`). derate's `context_length` is the per-sequence number, so
        # reading the flag straight back would double-count -- a deployment
        # launched at 4096 and adopted after a restart would report 8192 and
        # the fit record rebuilt from it would price a window twice the size
        # of the one being served.
        max_concurrent_seqs = _int_flag(command, "parallel", 1)
        pool = _int_flag(command, "ctx-size", 0)
        context_length = pool // max(1, max_concurrent_seqs)
        # There is no such flag, and 0.0 is how this field already spells
        # "this runtime does not claim a share of a device".
        gpu_memory_utilization = 0.0
        expert_parallel = False
    else:
        match = _VLLM_MODEL_RE.search(command)
        if match is None:
            return None
        runtime = "vllm"
        model_id = match.group(1)
        tensor_parallel = _int_flag(command, "tensor-parallel-size", 1)
        pipeline_parallel = _int_flag(command, "pipeline-parallel-size", 1)
        context_length = _int_flag(command, "max-model-len", 0)
        max_concurrent_seqs = _int_flag(command, "max-num-seqs", 1)
        gpu_memory_utilization = _float_flag(command, "gpu-memory-utilization", 0.0)
        expert_parallel = "--enable-expert-parallel" in command

    # llama-server spells this `--alias` and exits on an unrecognised flag, so
    # it is the one of the three routing flags that is not shared -- see
    # `RuntimeSpec.served_name_arg`, which is the same fact on the way out.
    served_name = (
        _flag(command, "alias")
        if runtime == "llamacpp"
        else _flag(command, "served-model-name")
    )
    port_raw = _flag(command, "port")
    if not model_id or not served_name or not port_raw or not context_length:
        return None
    try:
        port = int(port_raw)
    except ValueError:
        return None

    return AdoptedSpec(
        runtime=runtime,
        model_id=model_id,
        served_name=served_name,
        port=port,
        tensor_parallel=max(1, tensor_parallel),
        pipeline_parallel=max(1, pipeline_parallel),
        expert_parallel=expert_parallel,
        context_length=context_length,
        max_concurrent_seqs=max(1, max_concurrent_seqs),
        gpu_memory_utilization=gpu_memory_utilization,
    )
