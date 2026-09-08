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
    # is the list that decides, and tests/test_single_source.py holds
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
