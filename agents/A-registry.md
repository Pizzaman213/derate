# Agent A: Registry, Discovery, and Telemetry

Read `00-architecture.md` first. Contracts there are frozen.

**You own:** `control_plane/registry/**`, `tests/test_registry.py`
**You depend on:** contracts only
**Downstream of you:** D (usable memory), E (node list), F, G, H

---

## What you build

The cluster's picture of itself. Which machines exist, what they are, whether they are alive, and what they are doing right now.

### 1. Hardware probe

Given a reachable machine, produce a `NodeProfile`. Run `nvidia-smi --query-gpu=name,memory.total,driver_version,compute_cap --format=csv,noheader,nounits`, parse it, and classify.

If the GPU name contains `GB10`, set `device_class=GB10` and use the constants: total 128 GiB, addressable 119.7 GiB, bandwidth 273 GB/s. Do not read addressable memory from `nvidia-smi` on GB10; unified memory reporting is unreliable there and the constant is the measured GPU-reachable slice.

For discrete cards, take `memory.total` and subtract 1 GiB for display and driver context. Memory bandwidth comes from a small lookup table keyed on the model name (3090: 936, 4090: 1008, A6000: 768, H100: 3350, A100: 2039); return 0.0 when unknown rather than guessing.

If `nvidia-smi` is absent or fails, return a profile with `device_class=UNKNOWN` and zeroed memory. Never raise. An unprobeable node is a node the planner will skip, not a crash.

### 2. The node agent

Every container runs one, coordinator or worker. It is the part of you that exists on every machine.

Serves three endpoints on the agent port:

```
GET /agent/profile      -> NodeProfile from the local probe
GET /agent/telemetry    -> current NodeState metrics
GET /agent/health       -> 200 with uptime and role
```

Advertises over mDNS as `_derate._tcp.local.` with TXT records carrying `role`, `cluster_id`, and `node_id`. Use `zeroconf`. Advertise on startup, withdraw cleanly on shutdown.

### 3. Role resolution and join

On startup, read `DERATE_ROLE` (default `auto`):

1. Browse mDNS for 3 seconds.
2. A coordinator responds and `DERATE_TOKEN` matches: start as worker, `POST /api/nodes/join` to it with our profile and agent URL.
3. Nothing responds: become coordinator, generate and persist a cluster token if none exists, print it, start advertising `role=coordinator`.
4. `DERATE_ROLE=coordinator` or `=worker` forces the outcome. `DERATE_JOIN=<addr>` skips discovery for a node on another subnet.

Role is sticky for the process lifetime. There is no election and no failover. If the coordinator dies, workers keep running and the UI goes dark until it comes back. This is a deliberate scope decision.

As coordinator, handle `/api/nodes/join`: reject a wrong or missing token with 403, probe the joiner back at its `/agent/profile` to confirm it is real, then store it as a **candidate**. Candidates are not members. `admit(node_id)` promotes one, and that is a click in the UI. Discovery proposes, a human accepts.

Keep candidates and members in separate collections and expose them separately, so the UI can show "found on your network" without implying membership.

Host networking is required for mDNS. Detect bridge networking on startup and fail with a message saying to add `--network host`, rather than silently never discovering anything.

Do not build fabric setup. sparkrun and NVIDIA Sync own ConnectX-7 configuration, subnets, and the SSH mesh. Discovery here is over the management LAN only.

### 4. Manual add

`add_node(address)` for anything mDNS cannot reach. Discovery will fail for someone and a dead end is worse than a form.

### 5. Health

Poll each member every 5 seconds. Healthy means the probe endpoint answers within 2 seconds. Three consecutive failures marks the node unhealthy; one success clears it. Keep `last_seen` current on every successful poll.

A node going unhealthy must not delete it or its last known telemetry. The UI greys the stale values and says what happened. Losing the numbers on failure is worse than showing old ones with a fault marker.

### 6. Telemetry

Poll every second: `nvidia-smi --query-gpu=memory.used,memory.total,power.draw,temperature.gpu,utilization.gpu`. Populate `NodeState`. Keep a 300-sample ring buffer per node so the UI can draw a 60-second graph without its own history.

Expose an async generator yielding the current snapshot once per second. Agent G wraps it in SSE. You do not do HTTP.

---

## Interface you must satisfy

```python
class RegistryPort(Protocol):
    def list_nodes(self) -> list[NodeState]: ...
    def get_node(self, node_id: str) -> NodeState | None: ...
    def healthy_nodes(self) -> list[NodeState]: ...
```

Plus, beyond the shared port:

```python
def add_node(self, address: str) -> NodeState        # manual, probe then admit
def admit(self, node_id: str) -> NodeState            # promote a candidate
def remove_node(self, node_id: str) -> None
def candidates(self) -> list[dict]                    # discovered, not yet admitted
def handle_join(self, token: str, profile: NodeProfile, agent_url: str) -> dict
def role(self) -> str                                 # "coordinator" | "worker"
def cluster_token(self) -> str
def history(self, node_id: str, seconds: int = 60) -> list[dict]
async def snapshots(self) -> AsyncIterator[dict]      # one per second
```

---

## Day 0 stub

Return the two GB10 fixtures and one 3090 fixture from `tests/fixtures/`, all healthy, with plausible static telemetry. Downstream agents must be able to import and call you within the first hour.

---

## Acceptance

- Probing a real Spark yields `device_class=GB10` and 119.7 GiB addressable.
- Probing a machine with no `nvidia-smi` returns UNKNOWN and does not raise.
- Two containers started with the same command on the same subnet form a cluster with no configuration: the first becomes coordinator, the second appears as a candidate within 10 seconds.
- A join with a wrong token is rejected with 403 and the node does not appear anywhere.
- A container started with bridge networking fails at startup with a message naming `--network host`.
- Killing a node's network moves it to unhealthy within 15 seconds, keeps its last telemetry, and does not remove it.
- `usable_memory(0.90)` on a GB10 profile returns roughly 107.7 GiB.
- Telemetry generator sustains 1 Hz for 5 minutes without leaking the ring buffer.

## Traps

Unified memory reports differently from discrete VRAM; the addressable constant exists because the nameplate is wrong for planning. Do not let a slow `nvidia-smi` call block the poll loop, run it with a timeout. Do not let discovery add nodes automatically, a found machine is a suggestion.
