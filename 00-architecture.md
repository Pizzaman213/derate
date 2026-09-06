# Spark Control Plane: Architecture and Contracts

**Every agent reads this file. Nobody edits it.**

Contracts here are frozen before any agent starts. That is what makes 8-way parallelism work: agents depend on type signatures, not on each other's progress. If a contract is wrong, one person changes it here and announces it. No agent changes a contract unilaterally.

---

## 1. What we are building

A planning and orchestration layer for DGX Spark clusters. It does not run inference. vLLM and SGLang run inference; sparkrun handles fabric setup and process launch. We own the four things nobody else does:

1. **Measure** the real interconnect bandwidth instead of trusting the spec sheet.
2. **Plan** the parallelism degrees from that measurement plus the model's shape.
3. **Refuse** launches that will run out of memory, and say what to change.
4. **Front** everything behind one OpenAI-compatible endpoint listing every model in the cluster.

### Non-goals

Do not build, in any workstream:

- Fabric setup. sparkrun owns it.
- Inference kernels, or a training path.
- User accounts, multi-tenancy.
- A model catalog browser. The picker is a short curated list plus one free-text HuggingFace ID field.
- A WAN endpoint. The gateway binds to the LAN. Nothing here is exposed to the internet.
- A chat interface, or chat history. This is a control plane; clients bring their own.
- A log browser.
- A deep-dive metrics page. The main view's readouts are the whole metrics surface.

The last four are scope boundaries for Agent H specifically, and they are the ones most likely to be rebuilt by accident because each looks like a small addition to a screen that already exists. None of them survive four days alongside a working multi-node path.

### Why this is defensible

sparkrun takes `--tp N` from the user and never chooses it. Dynamo's AIConfigurator has no GB10 profile at all. vLLM checks whether peer-to-peer works, not how fast the link is. Nothing in the ecosystem measures the link and derives a plan from it.

The demo this enables: NVIDIA's own playbook says TP=2 for two Sparks. Measured all-reduce on that link is roughly 10 GB/s rather than the 25 GB/s nameplate, because GPUDirect RDMA is off. At that bandwidth PP=2 beats TP=2 substantially on batched serving. Our planner probes the link, contradicts the playbook, and is right.

---

## 2. Deployment model

**One image. Every node runs the same container. Role is decided at runtime.**

```
   node 1                              node 2
   ┌────────────────────────┐          ┌────────────────────────┐
   │ docker run ... sparkplane          │ docker run ... sparkplane
   │                        │          │                        │
   │  node agent            │◀── mDNS ─┤  node agent            │
   │  - probes local HW     │   join   │  - probes local HW     │
   │  - serves /agent/*     │          │  - serves /agent/*     │
   │                        │          │                        │
   │  COORDINATOR           │          │  (worker)              │
   │  - registry, planner   │          │                        │
   │  - fit, deploy         │          │                        │
   │  - gateway :8080       │          │                        │
   │  - UI                  │          │                        │
   └────────────────────────┘          └────────────────────────┘
```

The install story, and the demo:

```bash
# node 1
docker run --network host -v sparkplane:/data sparkplane/node
#   -> no coordinator found, becomes coordinator
#   -> prints cluster token, opens UI on :8080

# node 2, same command, no configuration
docker run --network host -v sparkplane:/data sparkplane/node
#   -> finds coordinator via mDNS, joins as worker
#   -> appears in the UI roster within seconds
```

### Roles

Every container starts a **node agent**: probes local hardware, advertises itself over mDNS as `_sparkplane._tcp.local.`, and serves a small local API (`/agent/profile`, `/agent/telemetry`, `/agent/health`). That is all a worker does.

Exactly one container additionally runs the **coordinator**: registry, links, resolver, fit, planner, deployment manager, gateway, and UI. Role resolution on startup, controlled by `SPARKPLANE_ROLE` with default `auto`:

1. Browse mDNS for 3 seconds.
2. If a coordinator responds and the cluster token matches, start as worker and join it.
3. If none responds, become coordinator and start advertising as one.
4. `SPARKPLANE_ROLE=coordinator` or `=worker` forces it. `SPARKPLANE_JOIN=<addr>` skips discovery for another subnet.

**No leader election.** First one wins, and the role is sticky for the process lifetime. If the coordinator dies, workers keep serving inference (sparkrun owns those processes, not us) and the UI goes dark until a coordinator is restarted. Raft is not a four-day feature and the failure mode here is acceptable.

### Join protocol

```
worker                              coordinator
  │  mDNS browse                         │
  │─────────────────────────────────────▶│
  │  POST /api/nodes/join                │
  │    {token, profile, agent_url}       │
  │─────────────────────────────────────▶│
  │                    probe back /agent/profile
  │◀─────────────────────────────────────│
  │  200 {node_id, cluster_id}           │
  │◀─────────────────────────────────────│
```

A shared `SPARKPLANE_TOKEN` gates joining. The coordinator generates one on first run, persists it, and prints it. A join with a wrong or missing token is rejected. Without this, anything on the subnet can enlist itself.

A joining node appears as a **candidate** in the UI, not a member. Admission is one click. Discovery proposes, a human accepts.

Workers heartbeat to the coordinator every 5 seconds. Three misses marks the node unhealthy; the record and its last telemetry are kept, never deleted.

### Networking

Host networking is required. mDNS does not cross a bridge, and the agent needs to see the real interfaces to report the ConnectX-7 topology. `--network host` is not optional and the container must fail loudly with an explanatory message if it detects bridge networking.

---

## 3. Architecture

```
                        ┌──────────────────────────┐
   client ─── HTTP ────▶│   Gateway (G)            │
                        │   /v1/models             │
                        │   /v1/chat/completions   │
                        │   routing policy         │
                        │   admission control      │
                        └────────┬─────────────────┘
                                 │ routes model → backend
                        ┌────────▼─────────────────┐
                        │   Deployment Manager (F) │
                        │   lifecycle FSM          │
                        │   sparkrun adapter       │
                        └────────┬─────────────────┘
                                 │ asks: is this plan legal and safe?
              ┌──────────────────┼──────────────────┐
              │                  │                  │
     ┌────────▼──────┐  ┌────────▼──────┐  ┌────────▼──────┐
     │  Planner (E)  │  │   Fit (D)     │  │ Resolver (C)  │
     │  TP/PP/EP     │◀─│  OOM gate     │◀─│  ModelShape   │
     └────────┬──────┘  └────────┬──────┘  └───────────────┘
              │                  │
     ┌────────▼──────────────────▼──────┐
     │   Registry (A)      Links (B)    │
     │   nodes, health,    measured     │
     │   live telemetry    bandwidth    │
     └──────────────────────────────────┘
                        │
                        ▼
                   UI (H) reads everything over the Gateway's API
```

Dataflow for a launch request:

1. Resolver turns a HuggingFace model ID into a `ModelShape`.
2. Registry supplies node profiles; Links supplies the worst measured pairwise bandwidth.
3. Planner produces a `ParallelismPlan` with a human-readable reason.
4. Fit checks that plan against per-node usable memory and returns a `FitResult`.
5. If the verdict is `WONT_FIT`, the launch is refused with the reason. Nothing is started.
6. Otherwise the Deployment Manager renders a sparkrun invocation and tracks the process.
7. The Gateway picks up the new backend and adds its model to `/v1/models`.

---

## 4. Frozen contracts

Language is Python 3.12. Types live in `control_plane/contracts/`. Agents import from there and never redefine.

### 4.1 Hardware and topology

```python
class DeviceClass(str, Enum):
    GB10 = "gb10"            # DGX Spark, unified memory
    DISCRETE = "discrete"    # RTX 3090 and similar
    UNKNOWN = "unknown"

@dataclass(frozen=True)
class NodeProfile:
    node_id: str
    hostname: str
    address: str                  # management IP
    device_class: DeviceClass
    gpu_name: str
    gpu_count: int
    total_memory: int             # bytes
    addressable_memory: int       # bytes reachable by the GPU
    memory_bandwidth_gbps: float
    compute_capability: str
    driver_version: str

    def usable_memory(self, guardrail: float = 0.90) -> int: ...

@dataclass
class NodeState:
    profile: NodeProfile
    healthy: bool
    last_seen: float              # unix ts
    memory_used: int              # bytes, live
    power_watts: float
    temperature_c: float
    utilization_pct: float

@dataclass(frozen=True)
class LinkMeasurement:
    src: str                      # node_id
    dst: str                      # node_id
    all_reduce_gbps: float        # what governs tensor parallel
    sendrecv_gbps: float          # what governs pipeline handoff and KV transfer
    latency_us: float
    gpudirect_rdma: bool
    measured_at: float
    method: str                   # "nccl-tests" | "ib_write_bw" | "manual"
```

Constants, in `contracts/constants.py`:

```python
GB10_TOTAL_MEMORY      = 128 * 1024**3
GB10_ADDRESSABLE       = int(119.7 * 1024**3)   # GPU-reachable slice, not nameplate
GB10_MEM_BANDWIDTH     = 273.0                  # GB/s, intra-node

TP_VIABLE_THRESHOLD    = 40.0   # GB/s all-reduce below which TP loses to PP when batched
EP_VIABLE_THRESHOLD    = 40.0   # GB/s below which cross-node expert parallel is refused
DEGRADED_TPS_THRESHOLD = 10.0   # decode tok/s below which we call a fit degraded

DEFAULT_GUARDRAIL      = 0.90   # fraction of addressable memory we will spend
COMM_BUFFER_BYTES      = int(1.5 * 1024**3)
EP_EXTRA_BUFFER_BYTES  = int(2.0 * 1024**3)
FRAMEWORK_OVERHEAD      = int(1.0 * 1024**3)
```

### 4.2 Model shape

```python
@dataclass(frozen=True)
class ModelShape:
    model_id: str
    num_layers: int
    hidden_size: int
    num_attention_heads: int
    num_kv_heads: int             # NOT num_attention_heads. GQA ratio matters.
    vocab_size: int
    total_params: int
    dtype: str                    # key into BYTES_PER_PARAM

    head_dim: int | None = None

    # Mixture of experts
    num_experts: int = 0
    num_experts_per_token: int = 0
    active_params: int | None = None

    # Sliding window attention
    sliding_window: int | None = None
    layers_with_full_attention: int | None = None

    # Multi-head latent attention (DeepSeek family)
    mla_latent_dim: int | None = None

    # Replicated per node when sharding, never split
    vision_params: int = 0

    @property
    def is_moe(self) -> bool: ...
    @property
    def effective_head_dim(self) -> int: ...
    @property
    def effective_active_params(self) -> int: ...
    def bytes_per_param(self) -> float: ...
```

`BYTES_PER_PARAM` lives in `contracts/quant.py` and is owned by Agent C. Real bytes per parameter, not nominal bit width. Q4_K_M is 0.6125 bytes per param (4.90 bpw), not 0.5. MXFP4 is 0.53125 (4.25 bpw). NVFP4 is 0.5625 (4.5 bpw).

### 4.3 Plan and fit

```python
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
    reason: str                   # one sentence, shown verbatim in the UI
    measured_link_gbps: float     # what the decision was based on
    rejected: list[str]           # e.g. ["TP=2: link 10.2 GB/s below 40 GB/s threshold"]

    @property
    def world_size(self) -> int: ...

class Verdict(str, Enum):
    FITS = "fits"
    FITS_DEGRADED = "fits_degraded"   # loads, but predicted decode is unusably slow
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
    def total(self) -> int: ...

@dataclass
class FitResult:
    verdict: Verdict
    breakdown: MemoryBreakdown
    usable_per_node: int
    headroom: int
    reason: str
    limiting_term: str                        # "weights"|"kv_cache"|"bandwidth"|"combined"
    max_context_that_fits: int | None
    predicted_decode_tps: float | None
    warnings: list[str]
    @property
    def ok(self) -> bool: ...

@dataclass(frozen=True)
class FitRequest:
    shape: ModelShape
    context_length: int
    max_concurrent_seqs: int
    kv_dtype: str
    plan: ParallelismPlan
```

### 4.4 Remote providers

The gateway fronts remote OpenAI-compatible upstreams alongside local deployments. One endpoint, every model, local or not.

```python
class ProviderKind(str, Enum):
    OPENROUTER = "openrouter"
    OPENAI     = "openai"
    ANTHROPIC  = "anthropic"
    TOGETHER   = "together"
    GROQ       = "groq"
    OLLAMA     = "ollama"       # another box on the LAN, not ours to orchestrate
    CUSTOM     = "custom"       # any OpenAI-compatible base_url

@dataclass
class ProviderModel:
    served_name: str                    # what clients call it through us
    upstream_id: str                    # what the provider calls it
    context_length: int
    supports_streaming: bool
    supports_tools: bool
    input_cost_per_mtok: float | None   # USD, None when unknown
    output_cost_per_mtok: float | None

@dataclass
class Provider:
    provider_id: str
    kind: ProviderKind
    display_name: str
    base_url: str
    api_key_ref: str            # env var name or secret key. NEVER the key itself.
    enabled: bool
    priority: int               # lower is preferred among remotes
    models: list[ProviderModel]
    healthy: bool
    last_error: str | None
    last_refreshed: float
```

**`api_key_ref` holds a reference, never a value.** Keys live in environment variables or `/data/secrets.json` at mode 0600 and are resolved only at request time. They never appear in an API response, a log line, the topology payload, a persisted record, or an error message. Any response carrying a provider renders the key as `"***"` and nothing else.

A remote model can share a `served_name` with a local deployment. That is the useful case, not a collision: run locally until the cluster saturates, then spill to OpenRouter. The routing policy decides.

### 4.5 Routing policy

Several targets can serve the same `served_name`: replicas of a local deployment, unequal machines, remote providers, or a mix. The policy decides which answers.

```python
class RoutingPolicy(str, Enum):
    LEAST_OUTSTANDING = "least_outstanding"   # default, local only
    ROUND_ROBIN       = "round_robin"
    WEIGHTED_CAPACITY = "weighted_capacity"   # for unequal nodes
    CACHE_AFFINITY    = "cache_affinity"
    FAILOVER          = "failover"
    LOCAL_FIRST       = "local_first"         # spill to remote when saturated
    COST_AWARE        = "cost_aware"

class TargetKind(str, Enum):
    LOCAL  = "local"
    REMOTE = "remote"

@dataclass
class RouteTarget:
    target_id: str             # deployment_id or f"{provider_id}:{upstream_id}"
    kind: TargetKind
    backend_url: str
    weight: float              # normalized 0..1, weighted_capacity only
    outstanding: int           # live in-flight count
    healthy: bool
    admitting: bool            # false when memory critical, draining, or rate limited
    strength: float            # normalized capability score
    cost_per_mtok: float | None

@dataclass
class RoutingConfig:
    served_name: str
    policy: RoutingPolicy
    targets: list[RouteTarget]
    sticky_ttl_s: int = 0      # cache_affinity only, 0 disables stickiness
```

**LEAST_OUTSTANDING** sends to the target with the fewest in-flight requests. Correct default: it accounts for one replica being mid-prefill on a long prompt, which round robin ignores.

**ROUND_ROBIN** rotates. Cheap, predictable, and the right answer when replicas are identical and requests uniform. Offered because it is what people expect and because it makes an unfair split obvious in the UI.

**WEIGHTED_CAPACITY** is the answer to unequal nodes. Each target receives traffic in proportion to a strength score. This is what keeps a 3090 desktop from being handed the same share as a Spark and becoming the tail latency for the whole cluster.

Strength score, computed by Agent G, in this order of preference:

1. Measured sustained decode tok/s for this target, once at least 100 requests have completed. Always prefer measurement over estimate.
2. Agent D's `predicted_decode_tps` for this shape on this node's profile.
3. `memory_bandwidth_gbps * gpu_count` as a last resort.

Normalize across targets so weights sum to 1. Recompute every 60 seconds so a thermally throttling node loses share on its own. A local target below 15 percent of the strongest gets zero weight and is held as failover only, because routing a small share to a very slow node still produces a bad tail.

**CACHE_AFFINITY** hashes the prompt prefix to a target so repeat prefixes land on the runtime that already has them cached. Falls back to least-outstanding when the chosen target is not admitting. Worth real throughput on agent workloads with shared system prompts. Remote targets are excluded; we cannot reason about their cache.

**FAILOVER** sends everything to the primary and switches only when it stops admitting.

**LOCAL_FIRST** is the reason remote providers exist here. Prefer local targets by least-outstanding. Spill to remote only when every local target is saturated, unhealthy, or not admitting. Return to local as soon as capacity frees. This makes the cluster the default and the paid API the overflow valve, which is the right shape for someone who owns the hardware.

**COST_AWARE** ranks by `cost_per_mtok` ascending and picks the cheapest admitting target. Local targets carry a cost derived from measured power draw and a configurable electricity rate, defaulting to zero when unset. Ties break on least-outstanding.

Policy is per `served_name`, defaults to `LEAST_OUTSTANDING`, or automatically to `WEIGHTED_CAPACITY` when local targets differ in strength by more than 25 percent, or to `LOCAL_FIRST` when both local and remote targets exist. Overridable from the UI at any time without a restart.

### 4.6 Deployment

```python
class DeploymentState(str, Enum):
    PLANNED = "planned"
    LAUNCHING = "launching"
    READY = "ready"
    DEGRADED = "degraded"      # up but a node is unhealthy or memory is critical
    FAILED = "failed"
    STOPPING = "stopping"
    STOPPED = "stopped"

@dataclass
class Deployment:
    deployment_id: str
    served_name: str           # what clients pass as "model"
    shape: ModelShape
    plan: ParallelismPlan
    fit: FitResult
    runtime: str               # "vllm" | "sglang"
    state: DeploymentState
    backend_url: str | None    # OpenAI-compatible base URL of the runtime
    context_length: int
    max_concurrent_seqs: int
    started_at: float | None
    last_error: str | None
```

Legal transitions. Anything else is a bug:

```
PLANNED  -> LAUNCHING -> READY -> DEGRADED -> READY
                      \         \          \
                       -> FAILED  -> FAILED  -> STOPPING -> STOPPED
READY    -> STOPPING -> STOPPED
```

### 4.7 Internal service interfaces

Each agent implements one of these. Everyone else codes against the protocol, not the implementation.

```python
class RegistryPort(Protocol):
    def list_nodes(self) -> list[NodeState]: ...
    def get_node(self, node_id: str) -> NodeState | None: ...
    def healthy_nodes(self) -> list[NodeState]: ...

class LinkPort(Protocol):
    def get(self, a: str, b: str) -> LinkMeasurement | None: ...
    def worst_all_reduce(self, node_ids: list[str]) -> LinkMeasurement | None: ...
    def measure(self, a: str, b: str) -> LinkMeasurement: ...

class ResolverPort(Protocol):
    def resolve(self, model_id: str, dtype: str | None = None) -> ModelShape: ...

class FitPort(Protocol):
    def check(self, req: FitRequest, nodes: list[NodeProfile]) -> FitResult: ...
    def max_context(self, shape, plan, nodes, max_seqs, kv_dtype) -> int: ...

class PlannerPort(Protocol):
    def plan(
        self, shape: ModelShape, nodes: list[NodeProfile],
        link: LinkMeasurement | None, target: str, concurrency: int,
    ) -> ParallelismPlan: ...

class ProviderPort(Protocol):
    def list(self) -> list[Provider]: ...                  # keys redacted
    def add(self, spec: dict) -> Provider: ...
    def refresh(self, provider_id: str) -> Provider: ...
    def models(self) -> list[tuple[str, ProviderModel]]: ...  # (provider_id, model)
    def resolve_key(self, provider_id: str) -> str: ...       # request time only
    def health(self, provider_id: str) -> tuple[bool, str | None]: ...

class DeploymentPort(Protocol):
    def launch(self, shape, plan, fit, runtime, ctx, max_seqs) -> Deployment: ...
    def stop(self, deployment_id: str) -> None: ...
    def list(self) -> list[Deployment]: ...
    def get(self, deployment_id: str) -> Deployment | None: ...
```

### 4.8 HTTP API surface

Owned by Agent G. The UI codes against exactly this.

```
GET    /v1/models                      OpenAI-compatible, every ready deployment
POST   /v1/chat/completions            proxied, streaming passthrough
POST   /v1/completions
POST   /v1/embeddings

GET    /api/cluster                    nodes + links + summary
GET    /api/topology                   graph form: {nodes[], edges[], deployments[]}
GET    /api/nodes
GET    /api/nodes/{id}
GET    /api/nodes/candidates            discovered, not yet admitted
POST   /api/nodes/join                  worker -> coordinator, token-gated
POST   /api/nodes/{id}/admit            promote a candidate to member
DELETE /api/nodes/{id}
GET    /api/links
POST   /api/links/measure              body: {a, b} -> LinkMeasurement

GET    /api/routing                    all RoutingConfig
PUT    /api/routing/{served_name}      body: {policy} -> RoutingConfig

GET    /api/providers                  keys redacted, always
POST   /api/providers                  body: {kind, base_url, api_key_ref, ...}
PATCH  /api/providers/{id}             enable, disable, reprioritize
DELETE /api/providers/{id}
POST   /api/providers/{id}/refresh     re-pull the upstream model list
GET    /api/providers/{id}/models

POST   /api/plan                       body: {model_id, context, concurrency, target}
                                       -> {plan, fit}   (dry run, launches nothing)

GET    /api/deployments
POST   /api/deployments                body: {model_id, context, concurrency, target, runtime}
DELETE /api/deployments/{id}

GET    /api/metrics/stream             SSE, one event per second
```

`GET /api/topology` returns the graph the UI draws. Positions are the UI's problem, not ours.

```json
{
  "cluster_id": "c-8f21",
  "coordinator": "spark-01",
  "nodes": [
    {"node_id": "spark-01", "hostname": "spark-01", "device_class": "gb10",
     "gpu_name": "GB10", "state": "healthy", "role": "coordinator",
     "memory_used_pct": 78, "power_w": 71, "temp_c": 62, "util_pct": 94,
     "strength": 1.0, "deployments": ["d-1"]},
    {"node_id": "ws-3090", "hostname": "workstation", "device_class": "discrete",
     "gpu_name": "RTX 3090", "state": "healthy", "role": "worker",
     "memory_used_pct": 31, "power_w": 210, "temp_c": 68, "util_pct": 22,
     "strength": 0.34, "deployments": ["d-2"]}
  ],
  "edges": [
    {"src": "spark-01", "dst": "spark-02", "all_reduce_gbps": 10.2,
     "sendrecv_gbps": 9.0, "latency_us": 40, "gpudirect_rdma": false,
     "medium": "connectx-7", "stale": false, "measured": true},
    {"src": "spark-01", "dst": "ws-3090", "all_reduce_gbps": 1.1,
     "medium": "ethernet", "stale": false, "measured": true}
  ],
  "deployments": [
    {"deployment_id": "d-1", "served_name": "gpt-oss-120b",
     "node_ids": ["spark-01", "spark-02"], "state": "ready",
     "plan": "PP 2", "tokens_per_sec": 127.4}
  ]
}
```

`measured: false` on an edge means the link exists but has never been probed. The UI draws it dashed and offers to measure. Never emit a bandwidth figure that was not measured.

SSE event payload:

```json
{
  "ts": 1757193600.0,
  "cluster": {"tokens_per_sec": 127.4, "total_power_w": 139, "cache_hit_pct": 84},
  "nodes": [{"node_id": "spark-01", "power_w": 71, "temp_c": 62,
             "memory_used_pct": 78, "util_pct": 94}],
  "deployments": [{"deployment_id": "d-1", "state": "ready",
                   "tokens_per_sec": 127.4, "ttft_ms": 142, "queue_depth": 3}]
}
```

---

## 5. File ownership

No two agents write the same file. This is the hard rule that keeps merges clean.

| Path | Owner |
|---|---|
| `control_plane/contracts/**` | frozen, day 0 |
| `control_plane/registry/**` | A |
| `control_plane/links/**` | B |
| `control_plane/resolver/**` | C |
| `control_plane/fit/**` | D |
| `control_plane/planner/**` | E |
| `control_plane/deploy/**` | F |
| `control_plane/gateway/**` | G |
| `control_plane/providers/**` | I |
| `ui/**` | H |
| `tests/fixtures/**` | frozen, day 0 |
| `tests/test_<component>.py` | that component's owner |
| `docker/**`, `compose.yaml` | F |

Shared fixtures are written on day 0 alongside the contracts, so every agent tests against the same model shapes and node profiles. Minimum set: `llama-3.3-70b` (dense, GQA), `gpt-oss-120b` (MoE, MXFP4, sliding window), `qwen3-30b-a3b` (MoE, fits one node), `deepseek-v3` (MLA), plus two GB10 node profiles and one 3090 profile, and one `LinkMeasurement` at 10.2 GB/s with `gpudirect_rdma=False`.

---

## 6. Dependency graph

With contracts frozen, all nine start at once. Arrows are runtime dependencies, not scheduling ones.

```
A (registry) ──┬──▶ D (fit)
B (links) ─────┼──▶ E (planner)
C (resolver) ──┘

D, E ──▶ F (deploy) ──┬──▶ G (gateway) ──▶ H (ui)
                      │
I (providers) ────────┘
```

Integration checkpoints:

- **T+8h**: every agent's module imports cleanly and its unit tests pass against fixtures. No cross-wiring yet.
- **T+16h**: C, D, E wired. `POST /api/plan` returns a real plan and fit for a real HuggingFace ID.
- **T+24h**: A, B wired. Plan uses a measured link, not a default.
- **T+32h**: F wired. A deployment launches through sparkrun and reaches READY.
- **T+40h**: G and I wired. `/v1/chat/completions` returns tokens from a local deployment and spills to a remote provider under LOCAL_FIRST.
- **T+48h**: H wired against live data.

Anything not integrated by its checkpoint gets stubbed and cut, not rescued.

---

## 7. Stubs

Every agent ships a stub of their port on day 0 so downstream agents are never blocked. The stub returns fixture data and is deleted at integration. A stub that fails to return valid contract types is worse than no stub.

---

## 8. Risks

**The interconnect is a measurement, not a constant.** If a driver update enables GPUDirect RDMA, the all-reduce bandwidth jumps and the planner should start preferring tensor parallel. Never hardcode 10.2. Read it from the Links component every time.

**sparkrun's own estimator has known bugs.** Do not defer to it for fit. We compute fit ourselves and treat sparkrun purely as a launcher.

**MoE memory is the classic under-count.** Expert-parallel communication buffers are what other planners forget, and then the job OOMs at runtime. Agent D charges `EP_EXTRA_BUFFER_BYTES` and Agent E refuses cross-node EP below threshold.

**Provider API keys are the one unrecoverable mistake here.** This is a tool people screenshot. Keys are stored as references, resolved only at request time, and rendered as `***` everywhere. Agent I owns a test asserting no key material appears in any serialized output; if that test is missing, the feature is not done.

**Fits and usable are different questions.** A dense 70B loads on two Sparks and decodes at a few tokens per second. `FITS_DEGRADED` exists so the UI can say that out loud instead of letting someone discover it after a five-minute load.
