"""Agent A: registry, discovery and telemetry.

The cluster's picture of itself. Which machines exist, what they are, whether
they are alive, and what they are doing right now.
"""

from .agent import NodeAgent, create_agent_app
from .bootstrap import (
    RoleDecision,
    post_join,
    rejoin_until_admitted,
    resolve_role,
    resolve_role_sync,
)
from .client import AgentClient, HttpAgentClient
from .config import RegistryConfig
from .discovery import Advertiser, DiscoveredPeer, browse, browse_async
from .errors import (
    BridgeNetworkError,
    JoinRejected,
    NodeNotFound,
    ProbeFailed,
    RegistryError,
)
from .identity import ClusterIdentity, banner, load_or_create_identity
from .net import detect_bridge_networking, primary_address, require_host_networking
from .probe import bandwidth_for, probe_local, unknown_profile
from .registry import Registry
from .startup import NodeRuntime, start_node
from .stub import StubRegistry
from .telemetry import (
    HostMemory,
    RingBuffer,
    TelemetrySample,
    TelemetryStore,
    allocatable_bytes,
    read_compute_apps,
    read_host_memory,
    read_telemetry,
)

__all__ = [
    "Advertiser",
    "AgentClient",
    "BridgeNetworkError",
    "ClusterIdentity",
    "DiscoveredPeer",
    "HostMemory",
    "HttpAgentClient",
    "JoinRejected",
    "NodeAgent",
    "NodeNotFound",
    "NodeRuntime",
    "ProbeFailed",
    "Registry",
    "RegistryConfig",
    "RegistryError",
    "RingBuffer",
    "RoleDecision",
    "StubRegistry",
    "TelemetrySample",
    "TelemetryStore",
    "allocatable_bytes",
    "bandwidth_for",
    "banner",
    "browse",
    "browse_async",
    "create_agent_app",
    "detect_bridge_networking",
    "load_or_create_identity",
    "post_join",
    "primary_address",
    "probe_local",
    "read_compute_apps",
    "read_host_memory",
    "read_telemetry",
    "rejoin_until_admitted",
    "require_host_networking",
    "resolve_role",
    "resolve_role_sync",
    "start_node",
    "unknown_profile",
]
