"""Internal service interfaces. 00-architecture.md section 4.7.

Day-0 file. Transcribed from the architecture doc, not designed here.
Everyone codes against the protocol, not the implementation.
"""

from __future__ import annotations

from collections.abc import Mapping

from typing import Protocol, runtime_checkable

from .deployment import Deployment
from .hardware import LinkMeasurement, NodeProfile, NodeState
from .model import ModelShape
from .plan import FitRequest, FitResult, ParallelismPlan
from .providers import Provider, ProviderModel


@runtime_checkable
class RegistryPort(Protocol):
    def list_nodes(self) -> list[NodeState]: ...
    def get_node(self, node_id: str) -> NodeState | None: ...
    def healthy_nodes(self) -> list[NodeState]: ...


@runtime_checkable
class LinkPort(Protocol):
    def get(self, a: str, b: str) -> LinkMeasurement | None: ...
    def worst_all_reduce(self, node_ids: list[str]) -> LinkMeasurement | None: ...
    # None = every measurement rung failed; an honest absence, never a fabricated
    # figure (adopted deviation, see the 00-architecture.md amendments appendix).
    def measure(self, a: str, b: str) -> LinkMeasurement | None: ...


@runtime_checkable
class ResolverPort(Protocol):
    def resolve(self, model_id: str, dtype: str | None = None) -> ModelShape: ...


@runtime_checkable
class FitPort(Protocol):
    def check(
        self,
        req: FitRequest,
        nodes: list[NodeProfile],
        *,
        allocatable: Mapping[str, int] | None = None,
    ) -> FitResult: ...
    def max_context(self, shape, plan, nodes, max_seqs, kv_dtype) -> int: ...


@runtime_checkable
class PlannerPort(Protocol):
    def plan(
        self,
        shape: ModelShape,
        nodes: list[NodeProfile],
        link: LinkMeasurement | None,
        target: str,
        concurrency: int,
    ) -> ParallelismPlan: ...


@runtime_checkable
class ProviderPort(Protocol):
    def list(self) -> list[Provider]: ...  # keys redacted
    def add(self, spec: dict) -> Provider: ...
    def refresh(self, provider_id: str) -> Provider: ...
    def models(self) -> list[tuple[str, ProviderModel]]: ...  # (provider_id, model)
    def resolve_key(self, provider_id: str) -> str: ...  # request time only
    def health(self, provider_id: str) -> tuple[bool, str | None]: ...


@runtime_checkable
class DeploymentPort(Protocol):
    # `modality` is keyword-only and defaulted: an implementation that predates
    # audio keeps working, and one that accepts it records what the deployment
    # answers on so the gateway can route a request to the right endpoint.
    def launch(
        self, shape, plan, fit, runtime, ctx, max_seqs, *, modality=...
    ) -> Deployment: ...
    def stop(self, deployment_id: str) -> None: ...
    def list(self) -> list[Deployment]: ...
    def get(self, deployment_id: str) -> Deployment | None: ...
