"""Deployment record and lifecycle states. 00-architecture.md section 4.6.

Day-0 file. Transcribed from the architecture doc, not designed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .modality import Modality
from .model import ModelShape
from .plan import FitResult, ParallelismPlan


class DeploymentState(str, Enum):
    PLANNED = "planned"
    LAUNCHING = "launching"
    READY = "ready"
    DEGRADED = "degraded"  # up but a node is unhealthy or memory is critical
    FAILED = "failed"
    STOPPING = "stopping"
    STOPPED = "stopped"


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
    # The 40-hex commit the resolver read the shape from and the launch was
    # pinned to (--revision/--code-revision). None when the resolver had no
    # sha (a local path, an inline config) or for a pre-pin record.
    revision: str | None = None
    # Draft tokens per step for MTP speculative decoding; 0 is off. Loads the
    # checkpoint's multi-token-prediction module, so the fit charges it.
    # Zero for every deployment launched before this field existed.
    speculative_tokens: int = 0
    # The operator opted in to running the repository's own Python
    # (--trust-remote-code). False for every deployment launched before the
    # flag became opt-in, which is what those records' commands did not say.
    trust_remote_code: bool = False
    # The exact container image reference launched (env override included),
    # and the local image id (`docker image inspect {{.Id}}`) it resolved to
    # on the coordinator; "" when that image is not present locally.
    image: str = ""
    image_id: str = ""


#: Upper bound on Deployment.speculative_tokens.
MAX_SPECULATIVE_TOKENS = 8


def check_speculative_tokens(value: object) -> int:
    """The launch's draft-token count, or ValueError naming the range."""
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= MAX_SPECULATIVE_TOKENS
    ):
        raise ValueError("speculative_tokens must be a whole number from 0 to 8.")
    return value
