"""Frozen contracts from 00-architecture.md section 4.

Nobody edits these to unblock themselves. One person changes a contract,
in the architecture doc first, and announces it.
"""

from __future__ import annotations

from .constants import (
    COMM_BUFFER_BYTES,
    DEFAULT_GUARDRAIL,
    DEGRADED_TPS_THRESHOLD,
    EP_EXTRA_BUFFER_BYTES,
    EP_VIABLE_THRESHOLD,
    FRAMEWORK_OVERHEAD,
    GB10_ADDRESSABLE,
    GB10_MEM_BANDWIDTH,
    GB10_TOTAL_MEMORY,
    TP_VIABLE_THRESHOLD,
    UNMEASURED_COLLECTIVE_LATENCY_US,
)
from .deployment import Deployment, DeploymentOrigin, DeploymentState
from .hardware import (
    DeviceClass,
    GpuProcess,
    LinkMeasurement,
    NodeProfile,
    NodeState,
)
from .modality import ENDPOINT_FOR_MODALITY, Modality
from .model import ModelShape
from .plan import (
    FitRequest,
    FitResult,
    MemoryBreakdown,
    ParallelismKind,
    ParallelismPlan,
    SpeculativeMethod,
    SpeculativeSpec,
    Verdict,
)
from .ports import (
    DeploymentPort,
    FitPort,
    LinkPort,
    PlannerPort,
    ProviderPort,
    RegistryPort,
    ResolverPort,
)
from .providers import Provider, ProviderKind, ProviderModel
from .quant import BYTES_PER_PARAM
from .routing import RouteTarget, RoutingConfig, RoutingPolicy, TargetKind

__all__ = [
    "BYTES_PER_PARAM",
    "COMM_BUFFER_BYTES",
    "DEFAULT_GUARDRAIL",
    "DEGRADED_TPS_THRESHOLD",
    "EP_EXTRA_BUFFER_BYTES",
    "EP_VIABLE_THRESHOLD",
    "FRAMEWORK_OVERHEAD",
    "GB10_ADDRESSABLE",
    "GB10_MEM_BANDWIDTH",
    "GB10_TOTAL_MEMORY",
    "TP_VIABLE_THRESHOLD",
    "UNMEASURED_COLLECTIVE_LATENCY_US",
    "Deployment",
    "DeploymentOrigin",
    "DeploymentPort",
    "DeploymentState",
    "DeviceClass",
    "ENDPOINT_FOR_MODALITY",
    "FitPort",
    "FitRequest",
    "FitResult",
    "GpuProcess",
    "LinkMeasurement",
    "LinkPort",
    "MemoryBreakdown",
    "Modality",
    "ModelShape",
    "NodeProfile",
    "NodeState",
    "ParallelismKind",
    "ParallelismPlan",
    "SpeculativeMethod",
    "SpeculativeSpec",
    "PlannerPort",
    "Provider",
    "ProviderKind",
    "ProviderModel",
    "ProviderPort",
    "RegistryPort",
    "ResolverPort",
    "RouteTarget",
    "RoutingConfig",
    "RoutingPolicy",
    "TargetKind",
    "Verdict",
]
