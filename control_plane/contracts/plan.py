"""ParallelismPlan and FitResult. 00-architecture.md section 4.3.

Day-0 file. Transcribed from the architecture doc, not designed here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .model import ModelShape


class ParallelismKind(str, Enum):
    SINGLE_NODE = "single_node"
    TENSOR = "tensor"
    PIPELINE = "pipeline"
    EXPERT = "expert"
    HYBRID = "hybrid"


@dataclass(frozen=True)
class ParallelismPlan:
    kind: ParallelismKind
    tensor_parallel: int
    pipeline_parallel: int
    expert_parallel: int
    data_parallel: int
    node_ids: list[str]
    reason: str  # one sentence, shown verbatim in the UI
    measured_link_gbps: float  # what the decision was based on
    rejected: list[str]  # e.g. ["TP=2: link 10.2 GB/s below 40 GB/s threshold"]

    @property
    def world_size(self) -> int:
        return self.tensor_parallel * self.pipeline_parallel * self.data_parallel


class Verdict(str, Enum):
    FITS = "fits"
    FITS_DEGRADED = "fits_degraded"  # loads, but predicted decode is unusably slow
    WONT_FIT = "wont_fit"


@dataclass
class MemoryBreakdown:
    weights: int
    kv_cache: int
    activations: int
    comm_buffers: int
    replicated: int
    framework_overhead: int

    @property
    def total(self) -> int:
        return (
            self.weights
            + self.kv_cache
            + self.activations
            + self.comm_buffers
            + self.replicated
            + self.framework_overhead
        )


@dataclass
class FitResult:
    verdict: Verdict
    breakdown: MemoryBreakdown
    usable_per_node: int
    headroom: int
    reason: str
    limiting_term: str  # "weights"|"kv_cache"|"bandwidth"|"combined"
    max_context_that_fits: int | None
    predicted_decode_tps: float | None
    warnings: list[str] = field(default_factory=list)
    # Which budget ``usable_per_node`` came from: "static" is the addressable
    # ceiling under the guardrail -- what this hardware could ever spend --
    # and "live" is what the node could actually hand out at the moment the
    # check ran. Trailing and defaulted, so every existing construction is
    # unchanged. It exists because Deployment.fit is persisted and
    # _emit_fit_miss reports usable_per_node from it: without this a stored
    # verdict cannot say which budget judged it.
    budget_basis: str = "static"

    @property
    def ok(self) -> bool:
        return self.verdict is not Verdict.WONT_FIT


@dataclass(frozen=True)
class FitRequest:
    shape: ModelShape
    context_length: int
    max_concurrent_seqs: int
    kv_dtype: str
    plan: ParallelismPlan
    # Measured on-disk weight bytes from the resolver's safetensors/GGUF
    # accounting; when present the fit calculator must prefer it over
    # total_params * bytes_per_param (that consumption lands in a later
    # package).
    weight_bytes: int | None = None
