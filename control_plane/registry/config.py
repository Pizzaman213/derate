"""Runtime configuration and the operational constants the registry runs on.

The nine values in architecture section 4.1 are frozen and imported from
``contracts``. The timing constants below are *operational*: the day-0 contracts
file carries them as additive values, and this module prefers that copy when it
is present. The fallbacks are this module's own literals, so the
registry keeps working if the additive block is reorganised. Nothing here
redefines a frozen contract.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from control_plane.paths import data_dir, default_data_dir

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

# Re-announcement: how a node keeps itself correctly registered.
#
# The coordinator polls every member every HEARTBEAT_INTERVAL_S, so silence is
# information: a member nobody has asked about in several intervals is a member
# whose coordinator has lost it -- restarted with an empty roster, replaced by
# another machine, or dialling an address this node no longer answers on. The
# window is one interval wider than the one the coordinator uses to mark a node
# unhealthy, so a node that is merely being marked down does not also start
# announcing itself in the same breath.
#
# There is deliberately no unconditional periodic re-announcement. The two real
# triggers -- our own address or hardware changed, and nobody is asking -- cover
# every case, and a timer on top of them would be constant traffic to say
# nothing. A healthy cluster runs this loop at zero network cost.
REANNOUNCE_TICK_S = _const("REANNOUNCE_TICK_S", 5.0)
REANNOUNCE_UNPOLLED_S = _const(
    "REANNOUNCE_UNPOLLED_S",
    HEARTBEAT_INTERVAL_S * (HEARTBEAT_MISSES_UNHEALTHY + 1),
)
# Floor between attempts, and the ceiling it backs off to while there is no
# coordinator to hear us. The trigger stays true for as long as we are
# forgotten, so without a floor this would be a hot loop against a dead address.
REANNOUNCE_RETRY_S = _const("REANNOUNCE_RETRY_S", 15.0)
REANNOUNCE_MAX_RETRY_S = _const("REANNOUNCE_MAX_RETRY_S", 120.0)

# Telemetry: 1 Hz, 300 samples, so the UI gets 60 s of graph from a 300 s ring.
TELEMETRY_INTERVAL_S = _const("TELEMETRY_INTERVAL_S", 1.0)
# Consecutive 1 Hz misses before a probe is called degraded, and the same
# count before a throttle is called real. Mirrors HEARTBEAT_MISSES_UNHEALTHY
# deliberately: the asymmetry (three in, one out) is what stops a flapping
# signal writing two rows a second into the events table, which is the one raw
# table retention never evicts.
PROBE_MISSES_DEGRADED = _const("PROBE_MISSES_DEGRADED", 3)
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

# The same margin, for a machine one or two orders of magnitude smaller.
#
# 8 GiB is ~6% of a Spark's pool and 100% of a Raspberry Pi 4's. Applying the
# GB10 figure to a CPU node does not make it conservative, it makes every
# model refuse: `allocatable_bytes` subtracts this from MemAvailable, and on
# an 8 GB board with a desktop running there is not 8 GiB of MemAvailable to
# subtract from. So the constant has to be sized for the hardware it guards.
#
# Deliberately a fraction with a floor rather than a flat number, because the
# machines this covers span 4 GB to 64 GB and neither end is served by the
# other's constant. The floor is what an idle Linux box plus derate's own node
# agent needs to stay responsive while a model loads; the fraction is what
# keeps the same rule sensible on a large box.
#
# NOTHING HAS MEASURED THIS. It is stated as a starting point rather than
# dressed up as a finding: no CPU launch has been watched to OOM here, and the
# number that matters -- how much a llama.cpp load transiently needs above its
# own weights -- is an experiment nobody has run. It errs high on purpose, for
# the reason `allocatable_bytes` already gives about the GB10 case: swapping a
# model is not slow, it is fatal.
CPU_HOST_MEMORY_RESERVE_FLOOR = _const("CPU_HOST_MEMORY_RESERVE_FLOOR", 1024**3)
CPU_HOST_MEMORY_RESERVE_SHARE = _const("CPU_HOST_MEMORY_RESERVE_SHARE", 0.10)


def cpu_host_reserve(available_bytes: int) -> int:
    """How much of a CPU node's RAM to leave alone, given what it says is free.

    Reads off MemAvailable rather than MemTotal because that is what the caller
    has and what the kernel is actually promising; the share is therefore of
    the spendable pool, not of the machine.
    """
    return max(
        int(CPU_HOST_MEMORY_RESERVE_FLOOR),
        int(max(0, available_bytes) * float(CPU_HOST_MEMORY_RESERVE_SHARE)),
    )

# How often a node re-reads its own hardware, and how often the coordinator
# re-reads a member's profile. Both deliberately slow: hardware changes across
# a driver install or a reboot, not between heartbeats, and the probe shells
# out to nvidia-smi. 60s is fast enough that an operator who just ran the
# installer sees the roster correct itself while still watching it.
#
# These exist because a profile used to be captured once, at join, and never
# again -- so a node that was upgraded, or had a driver installed, kept
# reporting whatever it happened to be when it first knocked.
PROFILE_REPROBE_INTERVAL_S = _const("PROFILE_REPROBE_INTERVAL_S", 60.0)
PROFILE_REFRESH_INTERVAL_S = _const("PROFILE_REFRESH_INTERVAL_S", 60.0)

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
    # A second credential this machine holds, tried only when the first is
    # rejected. It exists because a node can legitimately be holding two: the
    # enrollment token it was installed with, which install.sh bakes into the
    # container environment permanently, and the permanent cluster token the
    # coordinator handed it in exchange. The enrollment one is single-use and
    # expires within the hour, so on every restart after the first it is the
    # wrong one -- but it is also the right one when an operator is deliberately
    # re-homing this machine onto a different cluster. Which of those is
    # happening cannot be known from here; the coordinator settles it.
    fallback_token: str | None = None
    join_address: str | None = None
    agent_port: int = DEFAULT_AGENT_PORT
    coordinator_port: int = DEFAULT_COORDINATOR_PORT
    data_dir: Path = field(default_factory=default_data_dir)
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
            data_dir=data_dir(env),
            node_id=env.get("DERATE_NODE_ID") or None,
            cluster_id=env.get("DERATE_CLUSTER_ID") or None,
            allow_bridge=(env.get("DERATE_ALLOW_BRIDGE") or "").lower()
            in ("1", "true", "yes"),
            host_memory_reserve=_int(
                env, "DERATE_HOST_RESERVE_MIB", HOST_MEMORY_RESERVE // 1024**2
            )
            * 1024**2,
        )
