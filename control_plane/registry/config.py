"""Runtime configuration and the operational constants Agent A runs on.

The nine values in architecture section 4.1 are frozen and imported from
``contracts``. The timing constants below are *operational*: the day-0 contracts
file carries them as additive values, and this module prefers that copy when it
is present. The fallbacks are the literals from the Agent A brief, so the
registry keeps working if the additive block is reorganised. Nothing here
redefines a frozen contract.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

try:  # the additive day-0 block, when it exists
    from control_plane.contracts import constants as _k
except Exception:  # pragma: no cover - contracts must import in a real tree
    _k = None  # type: ignore[assignment]


def _const(name: str, default: Any) -> Any:
    return getattr(_k, name, default) if _k is not None else default


# Discovery
MDNS_SERVICE_TYPE = _const("MDNS_SERVICE_TYPE", "_derate._tcp.local.")
MDNS_BROWSE_SECONDS = _const("MDNS_BROWSE_SECONDS", 3.0)

# Health: 5 s cadence, 2 s timeout, 3 consecutive misses. Worst case an
# unreachable node is marked unhealthy at ~12 s, inside the 15 s acceptance.
HEARTBEAT_INTERVAL_S = _const("HEARTBEAT_INTERVAL_S", 5.0)
HEARTBEAT_TIMEOUT_S = _const("HEARTBEAT_TIMEOUT_S", 2.0)
HEARTBEAT_MISSES_UNHEALTHY = _const("HEARTBEAT_MISSES_UNHEALTHY", 3)

# Telemetry: 1 Hz, 300 samples, so the UI gets 60 s of graph from a 300 s ring.
TELEMETRY_INTERVAL_S = _const("TELEMETRY_INTERVAL_S", 1.0)
TELEMETRY_RING_SAMPLES = _const("TELEMETRY_RING_SAMPLES", 300)

# Display and driver context on a discrete card. Not charged on GB10, where the
# addressable constant already accounts for what the GPU can actually reach.
DISCRETE_MEMORY_RESERVE = _const("DISCRETE_MEMORY_RESERVE", 1 * 1024**3)

# Unified memory: the floor we refuse to spend on a GB10.
#
# On a Spark the GPU allocates out of the same pool as the operating system, so
# a model that fills the pool does not fail to allocate, it pushes the OS into
# swap and takes the inference process down with it. MemAvailable is the
# kernel's own estimate of what can be handed out without swapping; this is the
# margin we keep on top of it, for the page cache and whatever else starts
# between the fit check and the load. 8 GiB is ~6% of the pool.
HOST_MEMORY_RESERVE = _const("HOST_MEMORY_RESERVE", 8 * 1024**3)

DEFAULT_AGENT_PORT = 8081
DEFAULT_COORDINATOR_PORT = 8080

ROLE_AUTO = "auto"
ROLE_COORDINATOR = "coordinator"
ROLE_WORKER = "worker"
VALID_ROLES = (ROLE_AUTO, ROLE_COORDINATOR, ROLE_WORKER)


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class RegistryConfig:
    """Everything the registry reads from the environment, resolved once."""

    role: str = ROLE_AUTO
    token: str | None = None
    join_address: str | None = None
    agent_port: int = DEFAULT_AGENT_PORT
    coordinator_port: int = DEFAULT_COORDINATOR_PORT
    data_dir: Path = Path("/data")
    node_id: str | None = None
    cluster_id: str | None = None
    allow_bridge: bool = False
    host_memory_reserve: int = HOST_MEMORY_RESERVE

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "RegistryConfig":
        env = os.environ if env is None else env
        role = (env.get("DERATE_ROLE") or ROLE_AUTO).strip().lower()
        if role not in VALID_ROLES:
            raise ValueError(
                f"DERATE_ROLE={role!r} is not one of {', '.join(VALID_ROLES)}"
            )
        return cls(
            role=role,
            token=env.get("DERATE_TOKEN") or None,
            join_address=env.get("DERATE_JOIN") or None,
            agent_port=_int(env, "DERATE_AGENT_PORT", DEFAULT_AGENT_PORT),
            coordinator_port=_int(
                env, "DERATE_PORT", DEFAULT_COORDINATOR_PORT
            ),
            data_dir=Path(env.get("DERATE_DATA_DIR") or "/data"),
            node_id=env.get("DERATE_NODE_ID") or None,
            cluster_id=env.get("DERATE_CLUSTER_ID") or None,
            allow_bridge=(env.get("DERATE_ALLOW_BRIDGE") or "").lower()
            in ("1", "true", "yes"),
            host_memory_reserve=_int(
                env, "DERATE_HOST_RESERVE_MIB", HOST_MEMORY_RESERVE // 1024**2
            )
            * 1024**2,
        )
