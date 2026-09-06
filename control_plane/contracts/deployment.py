"""Deployment record and lifecycle states. 00-architecture.md section 4.6.

Day-0 file. Transcribed from the architecture doc, not designed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

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
    runtime: str  # "vllm" | "sglang"
    state: DeploymentState
    backend_url: str | None  # OpenAI-compatible base URL of the runtime
    context_length: int
    max_concurrent_seqs: int
    started_at: float | None
    last_error: str | None
