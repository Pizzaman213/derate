"""Routing policy contracts. 00-architecture.md section 4.5.

Day-0 file. Transcribed from the architecture doc, not designed here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class RoutingPolicy(str, Enum):
    LEAST_OUTSTANDING = "least_outstanding"  # default, local only
    ROUND_ROBIN = "round_robin"
    WEIGHTED_CAPACITY = "weighted_capacity"  # for unequal nodes
    CACHE_AFFINITY = "cache_affinity"
    FAILOVER = "failover"
    LOCAL_FIRST = "local_first"  # spill to remote when saturated
    COST_AWARE = "cost_aware"


class TargetKind(str, Enum):
    LOCAL = "local"
    REMOTE = "remote"


@dataclass
class RouteTarget:
    target_id: str  # deployment_id or f"{provider_id}:{upstream_id}"
    kind: TargetKind
    backend_url: str
    weight: float  # normalized 0..1, weighted_capacity only
    outstanding: int  # live in-flight count
    healthy: bool
    admitting: bool  # false when memory critical, draining, or rate limited
    strength: float  # normalized capability score
    cost_per_mtok: float | None


@dataclass
class RoutingConfig:
    served_name: str
    policy: RoutingPolicy
    targets: list[RouteTarget] = field(default_factory=list)
    sticky_ttl_s: int = 0  # cache_affinity only, 0 disables stickiness
