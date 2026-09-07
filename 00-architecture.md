# Derate: Architecture and Contracts

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
   │ docker run ... derate          │ docker run ... derate
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
docker run --network host --gpus all --pid=host -v derate:/data ghcr.io/pizzaman213/derate/node
#   -> no coordinator found, becomes coordinator
#   -> prints cluster token, opens UI on :8080

# node 2, same command, no configuration
docker run --network host --gpus all --pid=host -v derate:/data ghcr.io/pizzaman213/derate/node
#   -> finds coordinator via mDNS, joins as worker
#   -> appears in the UI roster within seconds
```

The two GPU flags are as load-bearing as `--network host`, and fail more
quietly. The probe is `nvidia-smi`; without the device the container has
none, and `probe_local` returns `device_class=UNKNOWN` with zeroed memory
rather than raising — so the node joins, reports healthy, and reads `0W /
0°C / 0% / —%` in the roster with `ineligible_reason: "device class is not
recognized"`. `--pid=host` is the second half of the same requirement: GB10
reports `[N/A]` for every aggregate FB memory field, leaving
`--query-compute-apps` as the only way to tell a resident model from the
desktop, and nvidia-smi only counts processes inside its own PID namespace.

### Roles

Every container starts a **node agent**: probes local hardware, advertises itself over mDNS as `_derate._tcp.local.`, and serves a small local API (`/agent/profile`, `/agent/telemetry`, `/agent/health`). That is all a worker does.

Exactly one container additionally runs the **coordinator**: registry, links, resolver, fit, planner, deployment manager, gateway, and UI. Role resolution on startup, controlled by `DERATE_ROLE` with default `auto`:

1. Browse mDNS for 3 seconds.
2. If a coordinator responds and the cluster token matches, start as worker and join it.
3. If none responds, become coordinator and start advertising as one.
4. `DERATE_ROLE=coordinator` or `=worker` forces it. `DERATE_JOIN=<addr>` skips discovery for another subnet.

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

A shared `DERATE_TOKEN` gates joining. The coordinator generates one on first run, persists it, and prints it. A join with a wrong or missing token is rejected. Without this, anything on the subnet can enlist itself.

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

## Appendix: section 4 amendments (integration, 2026-09-06)

Section 4 above is frozen and unmodified; the following are additive deviations adopted during integration, transcribed into the contracts and pinned by tests.

- **`FitRequest.weight_bytes: int | None = None`** (4.3, additive field) — carries the resolver's measured on-disk safetensors/GGUF byte count so the fit calculator can prefer real bytes over `total_params * bytes_per_param` when both are available; `None` preserves every existing call site.
- **`ModelShape.mla_rope_dim: int | None = None`** (4.2, additive field) — the decoupled RoPE width cached per token alongside the MLA latent (config key `qk_rope_head_dim`); true cached width per layer per token is `mla_latent_dim + mla_rope_dim`. `None` for non-MLA models or when the config key is absent.
- **`LinkPort.measure` returns `LinkMeasurement | None`** (4.7, was `LinkMeasurement`) — every measurement rung can fail (no NCCL, no SSH, no loopback estimate); `None` is an honest absence rather than a fabricated bandwidth figure that the planner would otherwise act on.
- **`DEGRADED -> FAILED` transition** (4.6, deviation) — the frozen diagram's branch out of `DEGRADED` goes to `-> STOPPING`, alongside the `DEGRADED -> READY` recovery edge on the top line; `control_plane/deploy/fsm.py`'s `LEGAL` table additionally allows `DEGRADED -> FAILED` because a degraded deployment can die outright (e.g. the surviving node also goes unhealthy) and forcing a `STOPPING` detour first would misrecord a crash as an operator-requested stop.
- **Request failover on the proxy path** (4.5, additive behaviour) — 4.5 defines routing as a *selection-time* decision: `FAILOVER` and `LOCAL_FIRST` pick a different target for the **next** request. A request already in flight to a node that dies is now re-offered to another target serving the same `served_name`, bounded by `failover_max_attempts` (2) and `failover_deadline_s` (30). Retryable: any transport failure, and any 5xx. Not retryable: every 4xx, and any attempt past its first yielded body chunk. When the chain is exhausted the client receives the **last upstream 5xx verbatim** — status, headers and body, with no `Retry-After` of ours added — or 502 `upstream_unreachable` when no target was ever reached. The seven policies in 4.5 are unchanged; the retry loop excludes already-tried targets before the policy runs, so each policy simply sees a smaller field.
- **Response headers held until the first upstream chunk** (4.5, deviation) — the status line is now sent when the first body byte arrives rather than when the upstream connection is established, bounded by `upstream_header_hold_s` (10). This is what makes a node dying during prefill recoverable: past the first yielded chunk the status is committed to the client and the attempt can no longer be moved. One chunk is held, never the stream, so the no-buffering rule is intact and the client's first byte arrives when it always would have.
- **Gateway-side circuit breaker** (4.5, additive behaviour) — `RouteTarget.healthy` is now additionally false while the gateway's own breaker holds a target open: three consecutive transport failures bench it for 30s, then one half-open probe decides. It only ever subtracts from health, so Agent F's deployment state stays authoritative for putting a target back. It exists because `deploy/manager.py` needs two 5s health polls to notice a dead backend, and every request arriving in that window pays the full connect timeout. **5xx never trips it** — a backend answering 500 is reachable, and benching it for saying so would let one malformed request take every replica of a model out of rotation at once.
- **`circuit` on each routing target** (4.8, additive field) — `GET /api/routing` and `PUT /api/routing/{served_name}` now emit `"circuit": "closed" | "open" | "half_open"` per target, alongside the existing fields. Without it a target the breaker has benched appears as `healthy: false` for a deployment `/api/deployments` reports READY, with nothing to explain the difference.
- **Bounded parking on the proxy path** (4.5, deviation) — 4.5's rule is 503 rather than queueing *indefinitely*. A request whose model has no live target is now held for up to `park_grace_s` (10, and 0 disables it) and dispatched if one returns, bounded further by queue depth and body size. Two conditions must both hold: no target for that model carries an admission block, and the model had a live target within the last 60s. So a request refused by admission control — rate limited, draining, memory critical — keeps its instant 503, as does an unknown or first-launching model; only "it was serving a moment ago and its node just went" is held.
- **`fp4` alias re-pointed to `nvfp4`** (quant table alias, was `mxfp4`) — a bare `fp4` tag names no packer; NVFP4 (4.5 bpw) is the more plausible read than MXFP4 (4.25 bpw) on Blackwell-class hardware, so it now wins the ambiguous spelling.
- **Six additive fields, `/api/routing` and `/api/nodes` / `/api/cluster` / `/api/nodes/candidates`** (4.8, additive) — `flow: "local" | "spilled" | null` on a routing config, non-null only while its resolved policy is `LOCAL_FIRST`, set from which kind of target the most recent real selection actually landed on (a small router-side tracker written only at dispatch; the `/api/routing` read path only ever reads it, never recomputes it from current saturation). `auto_selected: bool` and `auto_reason: string | null` on a routing config — whether nothing overrode the policy, and the reason `auto_policy` already computed for its verdict, surfaced instead of discarded; both come back `false`/`null` under an explicit `PUT /api/routing/{served_name}` override, since that reason was never auto_policy's to begin with. `zero_weight_reason: string | null` on each routing target — why the 15% weak-target floor benched a local replica (`null` for a remote, which is never floored, or a local target above the floor). `node_ids: string[]` on each routing target — the deployment's `plan.node_ids` for a local target, `[]` for a remote. `eligible: bool` and `ineligible_reason: string | null` on a node or candidate payload — conservative and honest: `false` only when the node is unhealthy (a candidate has no health signal yet, so this check is skipped for one) or its device class is `DeviceClass.UNKNOWN`, in which case the reason says the class could not be confirmed rather than asserting an exclusion the planner does not itself enforce (nothing downstream currently filters placement on device class).
- **Seventeen more additive fields, link payloads, `/api/providers`, and `/api/routing`** (4.8, additive) — five annotation keys on a link payload (`active_ports`, `total_ports`, `ports_inspected_on`, `gdr_detected_by`, `duration_s`), present only when the measurement carries a `LinkAnnotation`, alongside the existing `estimated`/`raw_gbps`/`scale_factor`/`notes` four and absent under the same rule (a bare `LinkMeasurement` says nothing about how the figure was obtained). Nine spend keys on each provider payload (`admitting`, `admission_block`, `daily_budget_usd`, `spend_today_usd`, `tokens_today`, `requests_today`, `unpriced_requests_today`, `retry_in_s`, `model_count`), computed once per request from the provider port and sliced per provider — all `null` when the port does not account, honest zeros passed through untouched when it does, so "no requests today" stays distinguishable from "nobody is counting". And on each routing target: `counters` (`{completed, failed, total_tokens, decode_tps, mean_duration_s}`, with the same never-fabricated-zero rule — counters are real integers starting at 0, the two EWMAs stay `null` until observed, and every field of the object is `null` rather than zeroed for a target nothing has ever been dispatched to — `counters` itself is always an object, never a bare `null` — so a fresh replica reads differently from one nothing routes to); `strength_raw` (the un-normalized score behind `strength`, present exactly where `strength_source` is, since its unit — tokens/sec, GB/s × gpu_count, or a bare 1.0 — changes with that source and is only meaningful beside it); and `admission_blocks` (sorted reasons the target is not admitting, `null` when nothing blocks it).

## Appendix: manual placement (2026-09-07)

Section 4 is frozen and unmodified. The following are additive request and response fields on the two planning routes, adopted so an operator can choose the machines and the parallelism degrees rather than only accepting the planner's.

**Half of this was already specified.** `agents/E-planner.md` says of `alternatives`: *"returns every legal plan ranked, so the UI can offer an override. The user can always override; you are a recommendation with reasoning, not a lock."* Agent E built `Planner.alternatives`; nothing ever called it. Manual *degrees* finish that feature. Manual *placement* — naming the machines — is the part that was genuinely out of scope, recorded as reversed in the Settings "Scope changed" card and in `ui/README.md`.

- **`POST /api/plan` and `POST /api/deployments` take an optional placement** (4.8, additive request fields). Both bodies accept `node_ids: list[str]` (the machines to plan across, in order — the first is the pipeline head sparkrun connects to first) and `parallelism: {tensor_parallel, pipeline_parallel, expert_parallel, data_parallel}`. `POST /api/deployments` additionally accepts `allow_mixed_hardware: bool`. All absent is exactly the previous behaviour: the planner chooses the node set and the degrees, and unlike hardware is excluded with `topology.exclusion_note`. **A key omitted inside `parallelism` means 1, never the planner's value** — defaulting to the recommendation would make the launched shape depend on a recommendation the operator never saw, and one that can change between the preview round trip and the launch round trip. `node_ids: []` is a 400: an explicit empty selection is not the same request as an absent field.

- **Four additive response fields on `POST /api/plan`** (4.8, additive) — `placement: {mode, requested_node_ids, node_ids, unused_node_ids, mixed_hardware, warnings}`, `degrees: {source, tensor_parallel, pipeline_parallel, expert_parallel, data_parallel}`, `recommended_plan` (the planner's own pick over the same node set, a full plan payload with its `reason` and `rejected` intact, so an overruled recommendation stays on screen), and `alternatives` (every legal shape on that node set, degrees and hosts only — no prose, because shipping a rejection list per shape is kilobytes of text to populate a hint).

- **`serve.overrides: [{param, reason}]`** (4.8, additive field) — the complete list of permissions a launch needs, the live-memory gate included. It exists because `allowed: false` can only express one gate and only a memory one: pooling unlike hardware is a permission the operator grants while the fit itself passes. `override_required`/`override_param` remain as a mirror of the first entry, so a client that predates `overrides` still reads a correct single gate. `allowed` keeps its exact old meaning — the fit gate passed — and the extra gates never touch it.

- **Pooling unlike hardware is refused at launch, not at plan** (4.8, behaviour). `POST /api/plan` plans the named set as given, pools it, and reports the objection as a serve gate; `POST /api/deployments` refuses with 400 `mixed_hardware_not_allowed` naming `allow_mixed_hardware`. A dry run starts nothing, so it owes an honest answer about the set it was handed — and refusing there would make this a permission nobody could see the consequence of before granting it. The two overrides are independent and neither implies the other. The check fires **only when `node_ids` was named**: with no selection the whole cluster is in play and mixed hardware is the normal case the planner narrows and narrates.

- **Illegal degrees are refused with 400 `illegal_parallelism`** carrying the planner's own sentence. `planner/legality.py` now owns `tp_rejection`/`pp_rejection`/`ep_rejection`, and `Planner._structural_rejections` calls into them, so a degree refused by hand is refused in bytes identical to the ranking's own rejection line. Legality here means "this shape cannot load", not "this shape does not fit": `fit.calculator.check` already refuses a world size wider than the supplied ranks with a better sentence, and two authorities on one fact is worse than one.

- **A named machine that would carry no rank is refused** with 400 `placement_underfilled`, but only when `node_ids` and `parallelism` were both given. Naming the machines alone leaves the rank count as the planner's advice, and the shortfall is reported in `placement.unused_node_ids` instead. Verified against sparkrun 0.2.40 with `--dry-run`: it launches the degrees it is given against the hosts it is given without complaint, so nothing downstream catches this.

- **`PlannerPort` is unchanged** (4.7). `allow_mixed_hardware` is a keyword-only extra on `plan`/`alternatives` with the same standing as the existing `context_length`/`kv_dtype`, and `plan_for` joins `alternatives`/`valid_tp_degrees`/`explain` on the extra-API surface `agents/E-planner.md` already describes. A planner exposing only the frozen port degrades with **501 `manual_degrees_unsupported`** rather than letting the gateway author a `reason`: reasons are rendered verbatim and persisted on the deployment, so a gateway-written one is a fabrication with a long half-life.

- **`NodeGroup.exemplar` is the weakest member** (`planner/topology.py`), not the first. A no-op for a homogeneous group — `_shape_key` already includes addressable memory and rounded bandwidth, so every member ties — and correct for a pooled group, where the capacity floor, the step-time estimate and the prose must all be charged against the node that will actually bind.

- **Who chose the shape is recorded on the persisted fit.** `plan.reason` stays verbatim planner prose even for an operator-forced shape, so the attribution rides in `FitResult.warnings` — already the channel for the live-memory override, already persisted — as `placed and shaped at the operator's instruction (node_ids, parallelism): ...; the planner ranked ... first for this node set`. Without it a stored deployment shows a human's choice wearing a planner-voiced sentence.

## Appendix: section 1 amendment (integration, 2026-09-07)

Section 1 is unmodified. What follows is a non-goal reversed deliberately, recorded here rather than edited into the list, in the same form as the durable-telemetry appendix below.

- **"A model catalog browser" is no longer a non-goal (§1, reversal).** §1 reads: *"A model catalog browser. The picker is a short curated list plus one free-text HuggingFace ID field."* That held while the resolver was the only thing that knew a model's shape and nothing exposed it over HTTP, which made a browser a subsystem. It is now a router and a tab, because the parts already existed and were being discarded: `resolve_full` returns architecture, parameter split, provenance, a per-runtime support verdict and measured on-disk bytes; `HubClient.search` searches the hub; `available_quants` enumerates a ladder; `fit/capacity.py`'s `largest_runnable` already answers "which of these fits" against the live budget. `POST /api/plan` reached the first of those and threw the rest away.

  It is also not the catalog §1 was refusing. A catalog lists what exists; this answers what runs *here*, with each quantization's verdict coming from the same `_plan_and_fit` path a launch goes through, so the screen cannot promise something the launch would refuse. Note the feature was *planned* before it was cut: `ui/mockups/derate.html` lists "Model catalog sync — browse Hugging Face in-app" under **Planned · not yet built**, and a later revision moved that one row into "will not build". The four other §1 boundaries — WAN endpoint, chat history, log browser, deep-dive metrics page — are unchanged and remain non-goals. *(Superseded in part: the chat-history boundary was narrowed the same day by the chat test console appendix, and the last two were reversed by the node page appendix at the end of this file. WAN endpoint stands.)*

- **A quantization variant is a different repository, not a flag (4.6, clarification and behaviour change).** `deploy/flags.py`'s `_VLLM_COMMAND` and `_SGLANG_COMMAND` pass no `--quantization`; the only model identifier either interpolates is `{model}`, filled from `shape.model_id` (`deploy/recipes.py`). The `dtype` field `_plan_and_fit` accepts therefore changes the **sizing arithmetic only**. `POST /api/deployments` now refuses a `dtype` with 400 `dtype_not_launchable`, because honouring it would budget for 4-bit weights and then start the repository's real 16-bit ones — the out-of-memory kill the fit gate exists to prevent. `POST /api/plan` still accepts it, since asking what a model would cost at another precision starts nothing.

  **`kv_dtype` has the identical gap and is not fixed.** `_plan_and_fit` feeds it to the planner and to `FitRequest`, and neither serve template carries `--kv-cache-dtype`. Nothing offers a control for it, and nothing should until the flag exists.

- **Four additive read endpoints (4.8, additive).** All on `gateway/capacity_api.py`, which is already the read-only, model-resolving, memoised router; all take a model id as a **query parameter**, never a path segment, because ids contain `/` and the ASGI server, any proxy and the Vite dev proxy disagree about when `%2F` is decoded.

  ```
  GET /api/models/quant-table                 the contract's own BYTES_PER_PARAM and QUANT_INFO,
                                              plus each runtime's verdict per scheme. Static.
  GET /api/models/detail?model_id=            resolve_full as JSON: capability facts as numbers,
                                              provenance, support matrix, whether this cluster's
                                              silicon can run the scheme, and launchability.
  GET /api/models/variants?model_id=          every obtainable quantization, each with the
                                              repository that carries it, its measured size, and a
                                              fit verdict from largest_runnable against the live
                                              budget; plus the largest that both fits and is servable.
  GET /api/catalog                            (already shipped) the curated shortlist, server side.
  ```

  `GET /api/models/quant-table` is the single source of `BYTES_PER_PARAM` for any client, so no client re-types it. A second copy of those figures in a browser is a second answer, and the one that disagrees with the fit gate is the one that costs somebody a failed load.

- **`QuantVariant` (4.2, additive type).** `dtype` (canonical key), `label` (as published — "UD-Q4_K_XL" is rendered verbatim, never replaced by its canonical key), `repo_id`, `source`, `gguf_file`, `file_bytes` (measured or `None`, never an estimate), `downloads`, `launchable`, `note`.

- **The quantization table gained the importance-matrix family and four plain formats (4.2, additive; one open question).** `iq1_s` through `iq4_nl`, plus `q4_1`, `q5_0`, `q5_1`, `q2_k_s`. Figures are llama.cpp's measured whole-model bits-per-weight, which run above the pure block arithmetic and therefore err high — the safe direction. Three `LLAMA_FTYPE` entries that mapped to `q4_0` (Q4_1, Q5_0, Q5_1, really 5.0/5.5/6.0 bpw) were corrected, and ftypes 21–31 and 39 were added; a missing ftype had been falling through to the bf16 default. Before this, `quant_detect.from_name` recognised 13 of the 27 `.gguf` files in `unsloth/Qwen3-30B-A3B-GGUF` and `unsloth/DeepSeek-R1-GGUF` planned as 671B bf16 and was refused outright.

  **Open:** `q2_k` (2.63 bpw) and `q3_k_m` (3.65) are the pure-block figures. llama.cpp's measured table says 3.1593 and 3.9960, and the real files measure 2.96 and 3.85 — so both guess **low**, which `contracts/quant.py`'s own docstring forbids. They are unchanged here because that docstring calls them frozen in section 4.2 and the Agent C brief. `tests/test_resolver.py::TestQuantLadderCoverage` carries a strict `xfail` recording it; correcting the two values makes that test pass and the marker should come off.

## Appendix: durable telemetry (integration, 2026-09-06)

Section 1's non-goals list "a log browser" and "a deep-dive metrics page". Both stand as UI scope decisions. *(Both were reversed on 2026-09-07 — see the node page appendix at the end of this file. The paragraph is left as written, because what it decided is why the storage layer below was built the way it is.)* What follows is the storage layer beneath them, adopted deliberately and recorded here rather than edited into section 1, because the cluster currently forgets everything: node metrics live in a 300-sample RAM ring (five minutes), the SSE hub holds exactly one frame, per-request numbers are folded into an EWMA and destroyed at `proxy.py`'s `settle()`, and the only throughput history in the product is sixty seconds of it in the browser. A page refresh loses the graph, and a restart loses the rest.

- **New package `control_plane/telemetry/**`, one owner** (5, additive) — a new top-level path in the ownership table rather than a shared extension of `registry/` or `gateway/`. Producers get a call-site line each; the substrate has a single owner, so the no-two-agents-write-the-same-file rule holds.
- **Every node keeps `/data/telemetry/journal.db`** (2, additive) — append-only SQLite in WAL mode: node samples at 1 Hz, one row per request attempt, every event either bus emits, and log records at INFO and above. Writes go through a bounded in-memory buffer and one writer thread, and drop the oldest on overflow with a counter, exactly as `EventBus` does. Nothing on the request path ever waits on a disk. Retained 72 hours or 512 MiB, whichever comes first. **A worker journals whether or not the coordinator is up**, which is the point: there is no coordinator failover, so a worker that recorded nothing would lose everything the coordinator was down for.
- **`GET /agent/journal?since=&limit=`** (4.8, additive endpoint, both roles) — `{node_id, rows[], next, head, dropped}`. The coordinator polls each member through the same `AgentClient` seam it already uses for health and telemetry. Asking for rows after `N` **is** the acknowledgement that everything through `N` is safe on the coordinator, so there is no second endpoint and no ack message; a replayed request returns the same rows and the archive's primary keys absorb them. Rows and cursor advance in one transaction, so collection is idempotent and crash-safe.
- **The coordinator additionally keeps `/data/telemetry/archive.db`** (2, additive) — journal rows become typed rows here: `samples`, `requests`, `events`, `logs`, plus `cursors`, `gaps`, and 1-minute and 1-hour rollups. The coordinator drains its own journal through the same collector, via a local reader rather than HTTP, so the single-node case exercises the identical ingest path. Defaults: 30 days of raw samples (~250 MB per node), 7 days of raw requests and logs, 90 days of 1-minute rollups, 400 days of hourly ones.
- **Latency rolls up through a log-spaced histogram** — the one from `tests/load/driver.py`, moved to `telemetry/hist.py` and imported back by the harness. Its buckets merge exactly, so an hourly p99 is the real p99 of that hour rather than a mean of sixty percentiles.
- **`GET /api/history/{nodes,requests,events,logs,status}`** (4.8, additive endpoints) — **the SSE frame in 4.8 is unchanged.** A history surface belongs beside it, not inside it. `step=auto` answers from raw rows under 6 hours, 1-minute rollups under 7 days, and hourly ones beyond, so no caller can ask for 2.6 million rows. Every response carries `resolution` and any `gaps` overlapping the window, because a flat line has two causes — the cluster was idle, or the rows were trimmed — and the product already refuses to blur that distinction for a link it has never measured.
- **`X-Request-Id` on every `/v1/*` response** (4.8, additive header) — `r-<12 hex>`, minted before the first thing that can refuse and shared by every attempt the request makes. There was no request id anywhere in the repo, so a retry could not be tied to the client request that caused it.
- **Token counts now come from the upstream's `usage` block** (deviation) — `proxy.py` counted `data:` SSE frames as a token proxy and said so in its own comment. It still does when nothing better is available, and `tokens_estimated` on the record says which of the two you are reading; otherwise `providers/usage.py`'s `UsageSniffer`, already trusted for provider spend, supplies a real prompt/completion split.
- **`EventBus.add_tap(fn)`** (additive) — a synchronous hook called inside `emit()`. A subscriber is right for a UI, which can miss an event and redraw; it is wrong for a durable record, because its queue drops the oldest under burst and the burst is when the events matter. Taps must not block; one that raises is logged and ignored, because recording an event may never break delivering it.
- **Gateway events that previously only logged** (additive) — breaker trips and recoveries, retry-budget refusals, admission block transitions, routing-source failures, degraded startup steps, and **park enter and exit with the wait and the outcome**, which closes the "queue with no gauge on it" the gateway README records.
- **Log records are redacted at a handler, never at a logger** — `providers/service.py` documents why at length: a `logging.Filter` on a parent logger is never consulted for a child's records, which is audit finding M-18 and shipped inert once already. A root handler has no such hole, and it is the only thing that redacts the `gateway.*` loggers, which sit outside the `control_plane.` hierarchy and have nothing today. `main.py` now passes `log_config=None` to uvicorn, which otherwise installs its own configuration after `basicConfig` and stops its access logger propagating.
- **What it costs**, measured with `tests/load` at 400 rps against two fake runtimes, three arms on the same box: telemetry off, journal only (collection disabled), and the whole thing. Every arm served 400 of 400 rps with no errors and no drops, and p50 was 2.0 ms throughout. Request-path p99 went 6-8 ms off, 8-10 ms journalling, 17-19 ms with collection running; event-loop lag p99 went 0.9-1.7 ms, 1.2-1.9 ms, 2.7 ms, against the harness's 250 ms bar. CPU +2 points, RSS +4 MB. Two findings came out of running it, and both are fixed: waking the journal's writer on every append made it commit a transaction per request and cost three times the CPU it should (it now wakes on a full batch, and a timer covers a quiet node), and the collector ingesting a whole backlog in one uninterrupted stretch put ~15 ms on the gateway's p99 (it now pages and paces). **The collector, not the journal, is what the tail pays for** -- worth knowing before anyone shortens `SHIP_INTERVAL_S`.
- **`DERATE_TELEMETRY`, `_RETENTION_DAYS`, `_LOG_LEVEL`, `_MAX_BYTES`, `_SHIP_INTERVAL_S`** (additive, all defaulted) — and `DERATE_LOG_LEVEL`, read by `main.py` since day 0 and declared in no Dockerfile, compose file or entrypoint until now. Telemetry is off when its data root does not exist, which is how the container records and a development machine does not, with neither having to say so.

## Appendix: chat test console (integration, 2026-09-07)

Section 1's non-goals list "a chat interface, or chat history", and name it among the four boundaries most likely to be rebuilt by accident. Recorded here rather than edited into section 1, in the same spirit as the durable-telemetry appendix above.

- **The history half stands, and is enforced by construction** (1, unchanged) — the transcript is React state in `ui/src/tabs/ChatTab.tsx` and nothing else. No `localStorage`, no server-side store, no new persistence anywhere. A reload loses it. `tabs/settings/ScopeCards.tsx` still lists "Chat history" as a non-goal, and `ui/README.md` still lists it; both were narrowed rather than deleted so the product does not deny a feature it ships.
- **The interface half is reversed** (1, deviation) — a **Chat** destination, the fifth in `AppShell`'s `Dest` union. It exists because nothing in the product could confirm that a deployment answers, or show the model list the gateway actually serves; both meant leaving for `curl`. The model catalog-browser non-goal is untouched: this lists what the gateway already serves, it does not browse HuggingFace for something to launch.
- **No backend surface was added** — the tab is a client for `GET /v1/models` and `POST /v1/chat/completions` exactly as 4.8 already defines them. It is the first thing in the UI to call `/v1/*` at all; every one of `Backend`'s other methods speaks `/api/*`.
- **`Backend.chatStream`, the first non-`EventSource` stream in the UI** (additive) — `EventSource` is GET-only and cannot carry a body, so the composer reads `fetch` + `ReadableStream` directly: buffer across chunk boundaries, split on the blank line, drop an unparseable frame rather than failing the turn (the rule `subscribe()` already applies to the metrics feed), stop on `[DONE]`. An `AbortError` is an outcome, not an error — Stop keeps the partial answer and says it was stopped.
- **Token counts carry their provenance** — a turn prefers an upstream `usage` block and falls back to counting delta frames, labelled `(est.)` when it did. This is the same distinction `tokens_estimated` already draws on a request record; presenting a counted frame as a measured token would be the same fabrication in a nicer font. `stream_options: {include_usage: true}` is deliberately **not** sent — not every `ProviderKind` upstream accepts it, and a request refused for an unknown field is worse than an honest estimate.
- **`X-Request-Id` is read from the response headers and shown under every answer** — including under a refusal, since the header arrives before the body and is the only handle on the row that recorded it. That readout, not the tokens, is why a chat surface belongs in a control plane.

## Appendix: enrollment tokens and the curl installer (integration, 2026-09-07)

Section 2 says "a joining node appears as a **candidate** in the UI, not a member. Admission is one click. Discovery proposes, a human accepts." That rule stands for every credential it was written about. What follows adds one more credential, recorded here rather than edited into section 2.

- **A third credential: the enrollment token** (2, additive) — short-lived (one hour by default), spent after a fixed number of uses (one by default), revocable, and never persisted anywhere but the coordinator's own `/data/enrollments.json` at 0600. `control_plane/registry/enrollment.py`, minted through `POST /api/enroll`. The permanent cluster token is unchanged and still buys candidacy only; a wrong, expired or spent token is rejected with the same sentence as a garbage one, so none of this is a token oracle.
- **A node holding a live enrollment token is admitted on arrival** (2, deviation) — the one case where nobody clicks Admit. The click did not disappear, it moved: minting a token in the UI and carrying it to a specific machine *is* the acceptance, made a few minutes earlier. `Registry.add_node` already takes exactly this position for a human typing an address ("A human typing an address is the admission decision, so this skips the candidate step"); this is the same act with the typing done on the other end. Discovery still proposes and mDNS sightings still wait, because discovery never involved a human deciding anything.
- **The join response carries `cluster_token`, once** (4.8, additive field) — only on an admission granted by an enrollment token, and never on any other response. `handle_join` checks the token *before* it checks membership, so a worker that kept presenting a spent enrollment token would eventually 403 itself out of a cluster it is already a member of. The permanent token is therefore handed over with the admission and persisted by `bootstrap.adopt_cluster_token`. No endpoint returns it to a browser.
- **`GET /install.sh`** (4.8, additive route) — the installer script, served verbatim by the coordinator so every node after the first installs from the cluster it is joining. It is a static asset and contains no credential; the token is only ever an argv value in the command the UI composes. That is what makes it safe on a surface with no authentication, and it is a property to preserve rather than a coincidence. Registration order matters more than usual: this is a root-level path, and below the `StaticFiles` mount `curl | sh` would be piped `index.html`.
- **`POST /api/enroll` composes the whole command server-side** (4.8, additive) — including the coordinator's address, taken from the registry's own probed profile and falling back to the request Host only when that is not loopback. `ui/src/tabs/settings/ClusterCard.tsx` deleted its gateway-address row for this exact reason: `window.location.host` is correct in production and a lie in dev, and a wrong address in an install command fails on a machine nobody is looking at.
- **`install.sh` at the repo root** (5, new path) — POSIX `sh`, Docker only, idempotent, `--dry-run` and `--uninstall`. It does what the `docker run` line in section 2 always did and nothing more: preflight, pull, run with host networking, the GPU and the data volume, wait on `/agent/health`, then say what happened. Missing Docker is a refusal that names the command to fix it, not a silent daemon install. `--gpus all --pid=host` are passed whenever the host has an NVIDIA driver, and if Docker then refuses them — driver present, container toolkit absent — the run is retried without and the script says what was lost, because a node that probes as unidentified hardware is invisible in precisely the way that reads as a bug somewhere else. `--no-gpu` withholds them deliberately.
- **`Settings -> Add a node`** (UI) — `ui/src/tabs/settings/AddNodeCard.tsx`. This is the card `NodesCard`'s header comment says could not be built ("join is worker-to-coordinator and token-gated, so there is no endpoint an address field could call"). Still true; the card does not call the coordinator on the new node's behalf, it hands the operator the line that makes the new node call it. `ScopeCards`' "Add a node by address" non-goal is removed, since the page would otherwise contradict the card above it.
- **One credential is now rendered, deliberately** — `ui/src/api/redact.ts` blanks any field named `token` and says it has no inverse and no reveal control. That stands: the enrollment token arrives inside `Enrollment.command`, a composed shell line rather than a credential field, and the permanent cluster token is returned by no endpoint at all. Before this, the documented way to add a machine was to copy the permanent token by hand.

## Appendix: killing a resident GPU process (integration, 2026-09-07)

Section 4.6's lifecycle stops deployments. It has nothing to say about a process that is holding GPU memory and is not a deployment — and on GB10 that process is indistinguishable, in the only memory number the driver will give us, from one that is. Recorded here rather than edited into section 4, in the same spirit as the appendices above.

- **The node agent has a mutating endpoint** (2, deviation) — `POST /agent/processes/{pid}/kill`, alongside a read at `GET /agent/processes`. Every other `/agent/*` route is an open read; this one requires the cluster token in `X-Derate-Token`, checked before the PID is even looked at so an uncredentialled caller cannot use the choice of refusal to learn which PIDs exist. It exists because `sparkrun` can only address workloads it launched, and the whole point of this surface is the ones it did not: a leftover `llama-server`, an orphan from a killed `tests/load` run, a backend the coordinator lost across a restart. `control_plane/registry/procs.py` bounds what it may touch — only PIDs `--query-compute-apps` currently reports as holding GPU memory, never PID 1, the agent itself, or any ancestor of it. That bound is in the kill primitive, not in the route, so no future endpoint can skip it.
- **A kill is SIGTERM, then SIGKILL, and success means the memory came back** — a ten-second grace, then force, and both are confirmed against the compute-apps list rather than against the signal returning. A process can exit while the driver still holds its context; reporting that as a reclaim would hand an operator a number they then plan against. `_alive()` treats a zombie as dead for the same reason: whether the parent has got round to `wait()` is not a fact about the GPU.
- **`GET`/`DELETE /api/nodes/{id}/processes[/{pid}]`** (4.8, additive routes) — the coordinator proxies the agent and annotates each process with the deployment it belongs to, matched on the sparkrun cluster id or the manager's allocated port appearing in the command line (`control_plane/procmatch.py`, shared with the manager so the two cannot drift). **A process attributed to a non-terminal deployment is refused with 409**, naming the deployment: killing a backend the router is still dispatching to would leave the record claiming READY for two health polls while the process is gone, and the operator would see 502s with nothing connecting them to their own click. Stopping the deployment is the correct verb and it drains first. A process under a STOPPED or FAILED record is killable — that is exactly the orphan this exists to clear. Like `DELETE /api/deployments/{id}` and `DELETE /api/nodes/{id}`, these sit on the unauthenticated `/api` surface (`AUDIT-2026-09-06.md`'s open finding); the agent hop is token-gated regardless, because that one is reachable on every node.
- **`DeploymentPort` is unchanged** (4.7) — the gateway reaches launch handles through an optional `handles()` accessor found with `getattr`, the same shape as the live-memory kwarg. A port without it (both stubs) reports every process unattributed, which degrades toward "a Kill button behind a confirm dialog" rather than toward refusing to answer.
- **`stop()` has a second tier** (4.6, behaviour change) — when `sparkrun stop` does not confirm inside its budget, `DeploymentManager` now asks each of `plan.node_ids`' agents to kill the processes matching that deployment, then re-checks. Previously it gave up here: the record went to STOPPED, `last_error` named the orphan, and the workload held the pool indefinitely. A stop that needed the force tier still records that it happened — an abrupt teardown is where a wedged driver comes from, and an operator about to relaunch needs to know. `registry` is None in unit wiring, which skips the tier entirely. **`manager.py`'s rule that memory pressure never kills is untouched**: that is about the watch loop, and this fires only on a stop a human asked for. The FSM is untouched too — all of it happens inside the existing `STOPPING -> STOPPED` edge.
- **`TelemetrySample` did not gain a process list** — the payload is read on demand from `GET /agent/processes`, not folded into the 5s poll, so the durable journal does not carry a per-second process inventory nobody reads until a node sheet is open.
- **A kill ends a process, not a supervisor** (known limitation) — verified on the live cluster: killing a 72.5 GiB `llama-server` released every byte within four seconds, and a cron-driven guard script had it loading again before the next memory poll. The control plane has no visibility into whatever started a process it did not launch, and inventing one (scanning for units, walking the parent chain to guess at intent) would be guessing at an operator's own supervision. The reclaim is reported honestly and the restart is the operator's to stop.
- **`resident on this GPU`, in the node sheet** (UI) — `ui/src/inspectors/NodeInspector.tsx`. Unmanaged rows get a Kill button behind the `window.confirm` pattern `NodesCard` established, naming the process, its PID and how much it holds; managed rows get a link to their deployment instead. An unreadable nvidia-smi and an idle GPU render differently, because "nothing is holding the GPU" is the one wrong answer here — it is the basis on which someone decides the memory is free.

## Appendix: the storage surface (integration, 2026-09-07)

Nothing in this document mentioned disk. `NodeProfile` and `TelemetrySample` carry memory, power, temperature and utilisation, and there was no `shutil.disk_usage`, `statvfs` or `df` anywhere in the tree — so a launch could fail on a full filesystem with no warning on any screen, and the ~150 MB/day this product's own telemetry writes was invisible to the person whose disk it was. Recorded here rather than edited into section 1, in the same spirit as the appendices above.

**This is not a reversal.** Section 1's four standing non-goals — WAN endpoint, chat history, log browser, deep-dive metrics page — are untouched, and storage was never among them. `ScopeCards.tsx`'s `SCOPE_CHANGED` card is deliberately *not* extended: that card records named non-goals built anyway, and putting a surface there that was never refused would misreport the one register the product keeps of its own scope decisions.

- **Read on demand, never sampled** (4.1, deviation avoided) — the probe is a request-time syscall, not a telemetry field, so **no frozen contract changed and the archive schema did not move**. `TelemetrySample`, `NodeProfile`, `SCHEMA_VERSION`, the journal, the collector and both rollup tables are all untouched. This is the call `GpuProcess` already made and its reasoning transfers verbatim: disk changes over hours, and two more `INTEGER` columns per node per second would cost the durable record roughly 17% for a number nobody reads until they open the screen. The precedent matters more than the saving — a second sampled-versus-on-demand answer in the same product is how the two drift.
- **`GET /agent/storage`** (4.8, additive route, both roles) — `{node_id, root, filesystems[], estate[], unreadable[], measured_at, available, reason}`. Uncredentialed, like `/agent/profile` and `/agent/telemetry`; the kill route remains the only mutating endpoint on that surface. The directory walk runs in a thread, as `/agent/journal` already does, so `/agent/health` cannot queue behind it.
- **Filesystems are grouped by `st_dev`** — the data root, the resolver cache and the sparkrun cache normally share one disk, and on the development box `/tmp/derate-live`, `/home` and `/` are all device 66306. Reporting them separately would have described a 3.7 TB device as 11 TB used. Each filesystem appears once and names every one of our paths that landed on it, which is also the only way an operator learns that two things they believe are separate are not.
- **`used_pct` is measured against `used + free`, not against `total`** — a filesystem reserves blocks for root, 190 GiB of them on the NVMe this was written against. Counting them as capacity put the figure at 67.2% where `df` said 71%. `df` uses this denominator for the same reason, and a storage screen that disagrees with `df` by four points is one an operator stops believing. The gap is reported as `reserved` rather than hidden, so `used + free + reserved = total` is checkable on screen.
- **A path that cannot be read reports a reason, never a zero** (consistent with the dash rule) — 0 bytes free reads as an emergency and 0 bytes used reads as an empty disk. Unreadable paths come back in `unreadable[]` with a sentence, and a node whose agent does not answer comes back `available: false` with a different one. Neither renders as a number.
- **`GET /api/storage`** (4.8, additive route) — the coordinator fans out to every member's agent concurrently and **never fails as a whole**: one unreachable worker becomes one degraded row, because the node that cannot be read is exactly the node somebody opened this tab to look at. The response also carries `telemetry` (the same `status()` `GET /api/history/status` returns) and `retention` — every horizon and ceiling read from `telemetry/config.py`, so no client re-types a figure a deployment can move. This is the first UI consumer of any of that: the durable-telemetry appendix shipped `GET /api/history/{nodes,requests,events,logs,status}` and nothing in `ui/` had called one of them until now.
- **`DELETE /api/storage/cache/resolver`** (4.8, additive route) — the one mutation, and the one deletion under the data root that loses nothing: `ShapeCache.clear()` existed with no route, a miss costs a single hub round trip, and entries are keyed by model, revision, dtype and a schema version already. Reached with `getattr(resolver, "cache", None)` in the duck-typed style the optional ports use, so a `StubResolver` answers 503 with a sentence rather than a traceback. Nothing else on this screen deletes: a deployment record, a recipe or a link measurement is a record the product needs, and retention already collects the two streams that grow.
- **Model weights were first recorded here as unreachable. That was wrong, and the correction is the next appendix.** The claim was that the weights sit in the runtime container's own cache where this codebase never sees them. `resolver/hf.py` being metadata-only is true and unchanged, but it does not follow: `docker inspect` on a running `sparkrun` container shows `/home/<user>/.cache/huggingface` bind-mounted to `/cache/huggingface` with `HF_HOME` pointing at it, so the runtime downloads into the **host's** cache, on the same machine as the node agent. The weights were always readable; nothing had looked.
- **`Storage`, a seventh destination** (UI) — `ui/src/tabs/StorageTab.tsx`, composing three cards in the `SettingsTab` shape. Filesystems, what derate stores (with the retention horizons that bound it), and Collection. The last exists for one number: `dropped`. A node dropping journal rows still serves, still streams live telemetry and still draws a full graph — it has simply stopped keeping the history, and nothing else in the product would ever say so.

## Appendix: downloaded weights, and deleting them (integration, 2026-09-07)

The storage appendix above recorded model weights as something this product could not see. That was wrong on the facts, and the correction is worth more than the feature: **`docker inspect` on a running `sparkrun` container shows `/home/<user>/.cache/huggingface` bind-mounted to `/cache/huggingface`, with `HF_HOME` set to it and `HF_HUB_OFFLINE=1`.** The runtime downloads into the *host's* cache, on the same machine the node agent runs on. Nothing had ever looked. On the development box that cache held **894.5 GiB across 51 repositories**, against 2.5 TB used on the disk — so the largest single consumer of storage in the entire system was the one thing the storage screen had just declared out of reach.

- **New module `control_plane/registry/modelcache.py`** (5, additive) — locate the cache, scan it, delete one repository. No new dependency: `huggingface_hub` is not in `requirements.txt` and `resolver/hf.py` already avoids the hub libraries on purpose, so the cache layout is walked directly. The layout is stable and the walk is trivial — **3 ms for 51 repositories holding 894 GiB** — because only `blobs/` is stat'd and it is flat.
- **Only `blobs/` is counted** — the cache stores each file once under `blobs/` and builds `snapshots/<sha>/` from symlinks into it. Counting snapshots would report the same weights once per cached revision, so a model with two revisions cached would read at double its size, and the number an operator uses to choose what to delete would be the wrong one.
- **A repository is matched by its encoded folder name, never by a decoded id** — the cache encodes `org/name` as `models--org--name`, and the inverse is ambiguous whenever a name contains a double hyphen (`models--a--b--c` is either `a/b--c` or `a--b/c`). Decoding happens once, for display, and nothing decides on it. Every decision that matters — is this in use, is this the one to delete — compares the encoded form, which is exact.
- **`GET /agent/models/cache` and `DELETE /agent/models/cache/{folder}`** (4.8, additive routes, both roles) — the read is open like the rest of the agent surface; **the delete is token-gated, and the token is checked before the folder is looked at**, so an uncredentialled caller cannot learn what is cached by reading which refusal comes back. That is the same rule, for the same reason, as `POST /agent/processes/{pid}/kill`.
- **Three independent guards bound the delete**, all in `resolve_target` rather than at the route, so no future caller can reach the removal without them: the folder must be a single path segment (it arrives as a URL segment), it must carry the cache's own `models--` prefix, and its *resolved* path must sit directly inside the *resolved* cache directory. The third is the one that matters: without it a symlink named `models--x--y` planted in the cache would redirect an `rmtree` at whatever it pointed to.
- **`DELETE /api/storage/nodes/{node_id}/models/{folder}`** (4.8, additive route) — **refused with 409 `model_in_use` while any non-terminal deployment is serving that repository**, naming the deployment. Deleting them would leave the record claiming READY with the files gone, and the model would keep serving until it next needed a shard — failing later, somewhere with no visible connection to the click that caused it. Stopping the deployment is the correct verb and it drains first. This is deliberately the coordinator's check and not the agent's, because the agent has no idea what a deployment is; it is the identical split, and the identical status code, that `process_is_managed` already uses. `deploy/fsm.TERMINAL` is reused rather than re-listing states. Verified against the live cluster: `openai/gpt-oss-120b` was READY, the delete was refused, and all 183 GB were still on disk afterwards.
- **`_DELETE_TIMEOUT_S = 120.0`** — removing a 182 GiB repository is a great many `unlink` calls, and timing out on a delete that is actually succeeding is worse than waiting.
- **The HF cache is now mounted into the container, read-write** (2, deviation) — `compose.yaml` and `install.sh`. The only read-write bind mount besides `/data`, and unavoidable: reclaiming the weights is the point. Mounted only when the path already exists, matching how the sparkrun and SSH mounts guard themselves, because a bind mount of a missing path silently creates a root-owned directory in the operator's home. **Without the mount nothing breaks** — the node answers "no model cache" with a reason and the card renders that, which is a different answer from a cache holding nothing.
- **`DERATE_HF_CACHE`** (additive, defaulted) — checked before `HUGGINGFACE_HUB_CACHE` and `HF_HOME`, for a deployment that mounts the cache somewhere of its own choosing.
- **`Downloaded models`, in the Storage tab** (UI) — `ui/src/tabs/storage/ModelCacheCard.tsx`. Per node, largest first, with the size bar drawn against the largest repository rather than the total: against 894 GiB every bar but the first would otherwise be invisible, and this is a ranking of what is worth deleting. A repository being served is marked `serving` and has no button, which is the browser agreeing with a refusal the server makes regardless — the marking is computed from the same encoded folder name the 409 compares.

## Appendix: audio endpoints (2026-09-07)

Section 4 is frozen and unmodified; these are additive deviations, transcribed into the contracts and pinned by tests. `/v1` has not been extended since day 0, so this is the first amendment that widens the OpenAI surface itself rather than `/api`.

- **`Modality`** (4.2, new contract — `control_plane/contracts/modality.py`) — `text | embedding | speech | transcription`. What a served model answers on, deliberately *not* a claim about its internals: the endpoint family is the only distinction routing needs. Carried as an additive field with a `TEXT` default on **`Deployment.modality`** (4.6) and **`ProviderModel.modality`** (4.4), so every record written before it existed still decodes and every existing call site is unchanged.
- **`POST /v1/audio/speech`** (4.8, additive route) — text in, audio bytes out, proxied through the same dispatcher as the other three. No new response machinery was needed: the proxy has always streamed `aiter_raw()` and forwarded the upstream's own headers, so an MP3 survives it unaltered. The endpoint has no `stream` field, and `providers/service.py` no longer injects one into bodies bound for an endpoint that does not accept it — it did so unconditionally before, which would have made every audio request to a strict provider a 400.
- **`modality` on `GET /v1/models`** (4.8, additive field) — alongside `context_length`, `target_count` and `target_kinds`. **This closes a defect that predates the feature.** A provider catalog is ingested wholesale, and `providers/discovery.py` recognised `whisper` and `tts` only to clear `supports_streaming`; those models therefore appeared in `/v1/models`, and in the chat picker, as ordinary chat models. Nothing in the system could tell one from a chat model.
- **`wrong_modality`, 400** (4.8, additive refusal) — naming an audio model on a text endpoint, or the reverse, is refused before anything is sent, and the message names the endpoint that *would* have worked. "No such model" would be a lie — it exists and is serving — and a bare refusal reads as a typo in the model name. Same reasoning as `dtype_not_launchable`: name the mechanism, not just the refusal.
- **Text and embeddings are deliberately one family** — one vLLM server answers `/v1/chat/completions` and `/v1/embeddings` from the same weights and this gateway has always let it, so the guard only fires when one side is audio. Treating them as exclusive would refuse requests that work today.
- **Admission is skipped for a non-text deployment** (4.5, deviation) — `AdmissionController` is KV-cache arithmetic end to end, and a KV cache is not what a speech or transcription server has. Charging one against a budget that does not describe it would refuse requests for a reason that does not exist. A text deployment is unaffected.
- **`POST /v1/audio/transcriptions`** (4.8, additive route) — audio file in, text out, and the only request on this path that is not JSON. The upload is read once, bounded by `max_audio_upload_bytes` (25 MiB, matching what OpenAI accepts), and forwarded **byte for byte under the client's own content-type**, boundary included. `UpstreamProxy.forward` grew an optional raw-content path for it; it previously sent every request as `json=body` under a hardcoded `application/json`, which a multipart body cannot survive. Nothing downstream of the request build changed — the first-chunk hold, retry classification, verbatim 5xx replay and `settle()` were already byte-agnostic.
- **The model name is spliced, not rebuilt** (4.5, deviation) — a remote provider calls the model something else, and on the multipart path that name is a form field rather than a key in a dict that can be copied. Only the field's bytes are replaced; the audio part beside it is untouched. Re-encoding the form would mean parsing and rebuilding a 25 MiB upload to change nothing about it.
- **A multipart request takes the raw forward, not the managed provider path** (4.4, limitation) — `ProviderService.open_upstream` builds its payload with `dict(body)`, which an opaque upload has none of. Such a request still carries the provider's key and still fails over, but the provider service's own spend accounting and budget enforcement do not run on it. Acceptable because transcription is priced per audio-minute and nothing on this path can read that figure; **worth revisiting if audio spend ever needs to appear in the Spend tab.**
- **No token count is invented for an audio request** (4.5, deviation) — `_StreamAccounting` counts `data:` SSE frames as tokens and `_BodyAccounting` sniffs a bounded copy of the body for a `usage` block. Over binary audio the first is fiction and the second buffers a megabyte to learn nothing, so both are bypassed. Time-to-first-byte is still recorded, because that one is real whatever the bytes are; everything else stays null rather than zero.
- **Speech architectures in the vLLM support table** (4.2, additive) — `WhisperForConditionalGeneration`, `Qwen2AudioForConditionalGeneration`, `VoxtralForConditionalGeneration`. Deliberately **not** added to SGLang's: it has no transcription server, and a table that implied the two runtimes were equivalent here would send a launch to one that cannot serve it. `support.modality_for()` reports which endpoint family an architecture answers on; being loadable and being answerable on the chat route are different claims, and the architecture set only makes the first.
- **Encoder-decoder configs map to their decoder** (4.2, deviation) — `whisper-large-v3` publishes `decoder_attention_heads` and no `num_attention_heads`, so `map_config` raised `KeyError` on it before anything else ran. Consulted only when the ordinary keys are absent and only when `is_encoder_decoder` is true, so no decoder-only config can reach the fallback. The shape then describes the decoder stack alone, which is stated as a warning rather than hidden: the encoder's parameters arrive through the measured total instead.
- **`max_target_positions` is read as a position limit** (4.2, additive key) — Whisper states 448 there and nothing under any of the usual names. Absent, the planner would offer a context length vLLM refuses to start with — a launch that fails minutes later for a reason chosen here. **With it, the existing KV arithmetic needs no special case:** 448 tokens across 8 sequences is 560 MiB, and the calculator was never wrong, only badly fed.
- **No `--task`/`--runner` flag was added** (4.6, finding) — the pinned image (`dgx-vllm-eugr-nightly`) has no `--task` at all, and its `--runner` takes `auto|generate|pooling|draft`; `transcription` is a *task within* the generate runner, which vLLM detects from the model class (`WhisperForConditionalGeneration.supports_transcription_only`). A knob here would be one nothing consumes, which is the failure mode the `dtype_not_launchable` refusal exists to prevent.
- **`Deployment.modality` is taken from the resolution, not looked up later** (4.6, deviation) — `_PlanOutcome` carries it from the same resolution that produced the shape. `ModelResolver.modality_of` exists and reads the shape cache the way `supported_by` does, but the launch path deliberately does not use it: a cold or cleared cache reports no architecture, which would silently record a speech model as text, and the gateway would then refuse the very requests the deployment exists to serve with nothing on screen connecting the two.
- **`DeploymentPort.launch` gained a keyword-only `modality`** (4.7, additive) — defaulted, so an implementation that predates audio keeps working.
- **`GET /v1/realtime`, a WebSocket** (4.8, additive route) — the first non-HTTP route on this surface, and the first that does not go through the shared dispatcher. A realtime session cannot be re-offered to another target halfway through — the upstream holds conversation state we never saw — so failover, parking and the circuit breaker do not apply to it, and it deliberately shares nothing with the proxy path beyond the router's target lookup. Registered above the `/` StaticFiles mount like every other route. **`websockets` is now named in `requirements.txt`**: it already arrived via `uvicorn[standard]`, but this imports it directly as a *client*, and a future uvicorn dropping the extra would break realtime with an ImportError at connect time.
- **Realtime is a relay, not a composition** (4.8, limitation, stated on purpose) — frames are pumped both ways without being parsed, so the protocol stays the upstream's problem. A **local** model is refused, with a message saying the session is relayed to a provider that implements it rather than composed from local deployments. Building the local path means implementing the protocol here — roughly thirty event types, server-side voice activity detection for turn-taking, audio buffering and resampling, and barge-in — which is its own project. Refusing plainly beats opening a session that would never produce audio.
- **No token-denominated figure is reported for an audio deployment** (UI) — the deployments strip and the deployment inspector render `—` with the reason in place of tok/s, time-to-first-token and the prefill/decode split. The control plane measures no audio-side rate today, and a `0` would read as a stalled deployment. This is the dash rule applied to a new unit, not an exception to it.

## Appendix: the node page (2026-09-07)

Section 1 is unmodified. What follows is two non-goals reversed deliberately, recorded here rather than edited into the list, in the same form as the section 1 amendment and the durable-telemetry appendix above. Both reversals are also on screen, in Settings' "Scope changed" card, because that card is what stops these happening by accident and it only works if reversals are visible.

The two being reversed were reaffirmed as recently as the durable-telemetry appendix, which opens *"Section 1's non-goals list 'a log browser' and 'a deep-dive metrics page'. Both stand as UI scope decisions. What follows is the storage layer beneath them."* That was the right call at the time and it is being changed on purpose, not drifted past.

- **"A deep-dive metrics page" is no longer a non-goal (§1, reversal).** §1 reads: *"The main view's readouts are the whole metrics surface."* That was true of a product whose entire memory was five minutes of node samples in a RAM ring, one SSE frame, and sixty seconds of throughput accumulated in a browser tab and lost on reload — there was no depth to dive into. The archive changed the premise: thirty days of raw node samples, seven of raw requests, ninety of one-minute rollups and four hundred of hourly ones, with merged-histogram percentiles that are the only real ones in the system. Keeping all of that unreachable from the interface was no longer a scope decision, it was a dead subsystem. The page is **one machine's**, reached the way it always was — the node sheet, from a double-click on the graph or the roster — and there is still no cluster-wide metrics destination.

- **"A log browser" is reversed only in part (§1, narrowed).** What the node page carries is this node's recent events and log lines, over the same window as its charts, at warning-and-worse by default. The two controls that make a strip into a browser — a search box and a logger filter — are deliberately absent, and `/api/history/logs` supports both. `ScopeCards.tsx`'s existing gloss, "failure diagnostics only, on the deployment that failed", narrows to "on the machine or deployment you are looking at" rather than disappearing.

- **No new backend surface** (4.8, unchanged) — the page is a client for `/api/history/{nodes,requests,events,logs}` exactly as the durable-telemetry appendix defines them, plus `/api/memory`, `/api/routing` and `/api/topology`, all of which were already polled. The SSE frame is untouched.

- **One additive filter: `GET /api/history/events?node_id=`** (4.8, additive parameter) — `node_id` is stored on every event row and already selected; it simply had no `WHERE`. `logs` has filtered on it since it shipped. Without it, "what happened on this machine" means fetching the whole cluster's events and discarding most of them in the browser.

- **The window is a control, and it says which kind of answer it gave** — `live` is the sixty-second ring this browser accumulates from the 1 Hz frame; `5m`, `1h` and `24h` come from the coordinator. Every archived answer renders its own envelope: which resolution served it, whether it is durable, whether the far end was trimmed, and any `gaps` in the archive's own words. Five minutes is the first archived step on purpose, because `TELEMETRY_RING_S` is 300: on a machine with telemetry off — every development box, since telemetry disables itself when its data root does not exist — that one window still answers, from the registry's in-RAM ring, labelled `durable: false`. A wider window there returns the 503 and the page renders the server's sentence rather than an empty chart.

- **Percentiles are labelled as coming from a different instrument than everything beside them** — the four live readouts and the tok/s figures are exponential moving averages from the frame; p50/p90/p99 come from the archive's histograms. They are taken from the busiest bucket in the window rather than summed, because a percentile cannot be added, and the copy says so. The Telemetry sub-tab's note, which claimed no percentiles existed anywhere in the system, was corrected rather than left standing beside a screen that shows them.

- **`allocatable` is now read, not recomputed** (UI, deviation) — the node sheet derived it as `addressable - used` and guarded that to unified memory, so every discrete node showed an em dash for the one figure that decides whether anything can launch on it. `/api/memory` has carried a real per-node `allocatable` from the fit gate all along and is already polled at 2 s for the Serve button. Same rule as the quantization table: a second copy of a figure is a second answer, and the one that disagrees with the fit gate is the one that costs somebody an OOM.

- **Routing stays in the deployment inspector** — targets, shares, strength, circuits and cost are not duplicated onto the node page. Each served name on it is a button into the deployment sheet, which is the same one-detail-surface rule the cluster graph's rail already states.
