"""Deployment record and lifecycle states. 00-architecture.md section 4.6.

Day-0 file. Transcribed from the architecture doc, not designed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .modality import Modality
from .model import ModelShape
from .plan import FitResult, ParallelismPlan, SpeculativeSpec


class DeploymentState(str, Enum):
    PLANNED = "planned"
    LAUNCHING = "launching"
    READY = "ready"
    DEGRADED = "degraded"  # up but a node is unhealthy or memory is critical
    FAILED = "failed"
    STOPPING = "stopping"
    STOPPED = "stopped"


class DeploymentOrigin(str, Enum):
    """How this record came to exist. Not a lifecycle state -- an adopted
    deployment still moves through the same DeploymentState machine as any
    other, this only says how it got its first one.

    LAUNCHED is derate's own decision: the fit gate judged ``plan``/``fit``
    against live evidence before anything started. ADOPTED means the
    container was already running and unrecorded -- store.py lost its file,
    or it was started outside the normal launch path entirely
    (``DeploymentManager.adopt``, deploy/autoadopt.py) -- so ``plan``/``fit``
    here are reconstructed from the running process's own flags rather than
    decided in advance, and must be shown as such: CLAUDE.md's rule that
    planner/fit strings are the product applies double to a figure that was
    never actually the basis for anything running.
    """

    LAUNCHED = "launched"
    ADOPTED = "adopted"


@dataclass
class Deployment:
    deployment_id: str
    served_name: str  # what clients pass as "model"
    shape: ModelShape
    plan: ParallelismPlan
    fit: FitResult
    # "vllm" | "sglang" | "tts". The third is derate's own, and the annotation
    # here named two long after it shipped -- deploy/flags.py::SUPPORTED_RUNTIMES
    # is the list that decides, and tests/unit/test_single_source.py holds
    # resolver/support.py's table to the same names.
    runtime: str
    state: DeploymentState
    backend_url: str | None  # OpenAI-compatible base URL of the runtime
    context_length: int
    max_concurrent_seqs: int
    started_at: float | None
    last_error: str | None
    # Which endpoint family this deployment answers on. Defaults to TEXT so
    # every record written before this field existed still decodes.
    modality: Modality = Modality.TEXT
    # Caller-supplied CLI tokens appended to the generated serve command,
    # each one already passed through deploy/recipes.py::check_extra_args_safe.
    # Empty for every deployment launched before this field existed.
    extra_args: tuple[str, ...] = ()
    # Caller-supplied CLI tokens that REPLACE the plan-derived flags in the
    # generated serve command, rather than appending to them -- mutually
    # exclusive with extra_args (deploy/recipes.py::synthesize refuses both
    # at once). Same M-22 safety check as extra_args. Empty for every
    # deployment launched before this field existed.
    custom_command: tuple[str, ...] = ()
    # Speculative decoding, as the fit gate charged it and the recipe rendered
    # it. Stored rather than re-derived, for the reason `fit` is stored: this
    # is what the launch was judged against, and a later resolve of the same
    # model can offer a different figure once the checkpoint or this build's
    # arithmetic moves. None means one token per step, which is what every
    # deployment launched before this field existed did.
    speculative: SpeculativeSpec | None = None
    # See DeploymentOrigin. Defaults to LAUNCHED so every record written
    # before this field existed decodes as what it always was: something
    # derate itself launched.
    origin: DeploymentOrigin = DeploymentOrigin.LAUNCHED
    # Whether this launch disabled CUDA graph capture and torch.compile
    # outright, trading decode throughput for a startup that skips the
    # single slowest phase (deploy/progress.py's own timing: 40s+). A launch
    # option, not a fit-gate input -- FitResult.breakdown.framework_overhead
    # is a flat constant regardless of graph settings, so this changes
    # nothing the fit gate priced. False for every deployment launched
    # before this field existed, which is what they all did.
    enforce_eager: bool = False
    # A trimmed set of batch sizes to capture CUDA graphs for, instead of the
    # runtime's own default list -- fewer sizes, less capture time, at the
    # cost of falling back to eager execution for any batch size not in the
    # list. Mutually exclusive with enforce_eager (which disables graph
    # capture entirely, leaving nothing to trim) and with custom_command
    # (which replaces every plan-derived flag). None for every deployment
    # launched before this field existed, which is the runtime's own default
    # sizing -- exactly what it still means today.
    cudagraph_capture_sizes: tuple[int, ...] | None = None
    # The KV cache element width this launch was GATED AT. Unlike the two
    # above it this is a fit-gate input, not a launch-only choice:
    # `fit/kv.py::KV_ELEM_BYTES` halves bytes-per-token for fp8, so the gate
    # approves a context on the narrow width and passes the narrow BYTE
    # budget. Stored for the same reason `fit` is -- the engine has to be
    # told the same width the gate assumed, or it fills that halved budget
    # with full-width entries and serves half the approved context without
    # erroring anywhere. None is the runtime's own default, which is what
    # every deployment launched before this field existed got.
    kv_dtype: str | None = None
    # The weight quantization scheme this launch was GATED AT and told to
    # load, when the operator forced one rather than letting the runtime read
    # it off the checkpoint. Exactly the same contract as `kv_dtype` above,
    # one term heavier: `BYTES_PER_PARAM` differs by 3.5x between bf16 and
    # nvfp4, so the gate priced the weights at this scheme and the engine has
    # to be told the same thing or it loads the checkpoint's own packing into
    # a budget sized for something else. None means the runtime decides,
    # which is what every deployment launched before this field existed got.
    quantization: str | None = None
    # Whether this deployment is offered on the API. False takes it out of
    # `/v1/models`, out of routing, off the chat picker and off the topology
    # graph together -- the single seam is `gateway/targets.py::build_index`,
    # which is exactly how a provider's `enabled_models` allowlist already
    # works, and for the same reason: one edit, everything downstream follows.
    #
    # The container KEEPS RUNNING and keeps holding its GPU memory. That is
    # the whole difference between this and DELETE, and it is why every
    # surface that draws this has to say so -- an idle 30 GiB that nothing can
    # reach is the most expensive thing on a box like this one, and a switch
    # that hid it would be worse than no switch.
    #
    # True for every deployment written before this field existed, which is
    # what they all were.
    serving: bool = True
    # The container image this deployment was launched with, exactly as the
    # operator chose it. `None` means the runtime's own pinned default, which
    # is resolved at synthesis time by `deploy/recipes.py::container_image`
    # (`DERATE_<RUNTIME>_IMAGE`, else `RuntimeSpec.default_image`) -- and is
    # what every deployment launched before this field existed got.
    #
    # Stored rather than re-derived, for the reason `fit` and `kv_dtype` are
    # stored: an environment variable is one restart away from being lost, and
    # when `DERATE_TTS_IMAGE` was lost at 22:58 on 2026-09-07 the next launch
    # went straight back to `manifest unknown`. A record that names its own
    # image cannot be moved by somebody else's restart.
    #
    # An ADOPTED record leaves this `None` on purpose. `deploy/adopt.py`
    # reconstructs from the running command line and the image is not in it,
    # so a value here would be a guess -- and the guess most likely to be
    # wrong is exactly the one above.
    image: str | None = None
