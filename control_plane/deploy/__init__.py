"""Deployment manager and sparkrun adapter (Agent F).

    from control_plane.deploy import DeploymentManager, StubDeploymentManager

Both satisfy DeploymentPort plus reconcile(), render_command() and events().
Swap the stub for the manager at integration; nothing downstream changes.
"""

from __future__ import annotations

from .events import (
    BACKEND_LOST,
    FIT_MISS,
    LAUNCH_FAILED,
    LAUNCH_REFUSED,
    MEMORY_CLEARED,
    MEMORY_CRITICAL,
    MEMORY_CRITICAL_FRACTION,
    MEMORY_WARN_FRACTION,
    MEMORY_WARNING,
    NODE_UNHEALTHY,
    RECONCILED,
    STATE_CHANGED,
    STOP_ESCALATED,
    EventBus,
)
from .fsm import LEGAL, SERVING, TERMINAL, IllegalTransition
from .health import probe
from .manager import DeploymentManager, DuplicateDeployment, LaunchRefused
from .recipes import RecipeSpec, synthesize
from .sparkrun import (
    LaunchError,
    LaunchResult,
    SparkrunAdapter,
    SparkrunNotInstalled,
    backend_origin,
    default_served_name,
)
from .store import DeploymentStore
from .stub import StubDeploymentManager

__all__ = [
    "BACKEND_LOST",
    "FIT_MISS",
    "LAUNCH_FAILED",
    "LAUNCH_REFUSED",
    "LEGAL",
    "MEMORY_CLEARED",
    "MEMORY_CRITICAL",
    "MEMORY_CRITICAL_FRACTION",
    "MEMORY_WARNING",
    "MEMORY_WARN_FRACTION",
    "NODE_UNHEALTHY",
    "RECONCILED",
    "SERVING",
    "STATE_CHANGED",
    "STOP_ESCALATED",
    "TERMINAL",
    "DeploymentManager",
    "DeploymentStore",
    "DuplicateDeployment",
    "EventBus",
    "IllegalTransition",
    "LaunchError",
    "LaunchRefused",
    "LaunchResult",
    "RecipeSpec",
    "SparkrunAdapter",
    "SparkrunNotInstalled",
    "StubDeploymentManager",
    "backend_origin",
    "default_served_name",
    "probe",
    "synthesize",
]
