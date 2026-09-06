// TypeScript mirrors of the frozen contracts in 00-architecture.md section 4.
// These describe Agent G's HTTP surface only. Nothing here is invented: every
// field appears in the architecture doc or in one of its example payloads.

export type DeviceClass = 'gb10' | 'discrete' | 'unknown'
export type NodeRole = 'coordinator' | 'worker'
export type NodeHealth = 'healthy' | 'degraded' | 'unreachable'

export type DeploymentState =
  | 'planned'
  | 'launching'
  | 'ready'
  | 'degraded'
  | 'failed'
  | 'stopping'
  | 'stopped'

export type ParallelismKind =
  | 'single_node'
  | 'tensor'
  | 'pipeline'
  | 'expert'
  | 'hybrid'

export type Verdict = 'fits' | 'fits_degraded' | 'wont_fit'

export type RoutingPolicy =
  | 'least_outstanding'
  | 'round_robin'
  | 'weighted_capacity'
  | 'cache_affinity'
  | 'failover'
  | 'local_first'
  | 'cost_aware'

export type TargetKind = 'local' | 'remote'

export type ProviderKind =
  | 'openrouter'
  | 'openai'
  | 'anthropic'
  | 'together'
  | 'groq'
  | 'ollama'
  | 'custom'

// ── GET /api/topology ────────────────────────────────────────────────────────

export interface TopologyNode {
  node_id: string
  hostname: string
  device_class: DeviceClass
  gpu_name: string
  state: NodeHealth
  role: NodeRole
  memory_used_pct: number
  power_w: number
  temp_c: number
  util_pct: number
  strength: number
  deployments: string[]
  /** bytes; present on /api/nodes, echoed here by the stub for the roster line */
  total_memory?: number
  address?: string
  last_error?: string | null
}

export interface TopologyEdge {
  src: string
  dst: string
  /** absent when measured === false. Never render a number that was not measured. */
  all_reduce_gbps?: number
  sendrecv_gbps?: number
  latency_us?: number
  gpudirect_rdma?: boolean
  medium: string
  stale: boolean
  measured: boolean
}

export interface TopologyDeployment {
  deployment_id: string
  served_name: string
  node_ids: string[]
  state: DeploymentState
  /** short human form, e.g. "PP 2" */
  plan: string
  tokens_per_sec: number
}

export interface Topology {
  cluster_id: string
  coordinator: string
  nodes: TopologyNode[]
  edges: TopologyEdge[]
  deployments: TopologyDeployment[]
}

// ── GET /api/cluster ─────────────────────────────────────────────────────────

export interface NodeProfile {
  node_id: string
  hostname: string
  address: string
  device_class: DeviceClass
  gpu_name: string
  gpu_count: number
  total_memory: number
  addressable_memory: number
  memory_bandwidth_gbps: number
  compute_capability: string
  driver_version: string
}

export interface NodeStateDTO {
  profile: NodeProfile
  healthy: boolean
  state: NodeHealth
  role: NodeRole
  last_seen: number
  memory_used: number
  power_watts: number
  temperature_c: number
  utilization_pct: number
  last_error?: string | null
  /** false when this node cannot join the current serving pool */
  eligible?: boolean
  ineligible_reason?: string | null
}

export interface LinkMeasurement {
  src: string
  dst: string
  all_reduce_gbps: number
  sendrecv_gbps: number
  latency_us: number
  gpudirect_rdma: boolean
  measured_at: number
  method: string
}

export interface ClusterSummary {
  cluster_id: string
  coordinator: string
  node_count: number
  healthy_count: number
  total_addressable_memory: number
}

export interface Cluster {
  summary: ClusterSummary
  nodes: NodeStateDTO[]
  links: LinkMeasurement[]
  deployments: DeploymentDTO[]
}

// ── Candidates: GET /api/nodes/candidates ────────────────────────────────────

export interface Candidate {
  node_id: string
  hostname: string
  address: string
  device_class: DeviceClass
  gpu_name: string
  total_memory: number
  discovered_at: number
  /** set when the node is reachable but would not join the current pool */
  note?: string | null
}

// ── Plan and fit: POST /api/plan ─────────────────────────────────────────────

export interface ParallelismPlan {
  kind: ParallelismKind
  tensor_parallel: number
  pipeline_parallel: number
  expert_parallel: number
  data_parallel: number
  node_ids: string[]
  /** one sentence from the planner. Rendered verbatim, never paraphrased. */
  reason: string
  measured_link_gbps: number
  /** one line each, verbatim */
  rejected: string[]
}

export interface MemoryBreakdown {
  weights: number
  kv_cache: number
  activations: number
  comm_buffers: number
  replicated: number
  framework_overhead: number
  total: number
}

export interface FitResult {
  verdict: Verdict
  breakdown: MemoryBreakdown
  usable_per_node: number
  headroom: number
  /** from the fit gate. Rendered verbatim. */
  reason: string
  limiting_term: 'weights' | 'kv_cache' | 'bandwidth' | 'combined' | string
  max_context_that_fits: number | null
  predicted_decode_tps: number | null
  warnings: string[]
}

export interface PlanResponse {
  plan: ParallelismPlan
  fit: FitResult
  /** echoed request, so the panel can show what was asked */
  model_id: string
  context_length: number
  concurrency: number
}

export interface LaunchRequest {
  model_id: string
  context: number
  concurrency: number
  target: string
  runtime: string
}

export interface PlanRequest {
  model_id: string
  context: number
  concurrency: number
  target: string
}

// ── Deployments: GET /api/deployments ────────────────────────────────────────

export interface DeploymentDTO {
  deployment_id: string
  served_name: string
  model_id: string
  runtime: string
  state: DeploymentState
  context_length: number
  max_concurrent_seqs: number
  started_at: number | null
  last_error: string | null
  node_ids: string[]
  plan: ParallelismPlan
  fit: FitResult
}

// ── Routing: GET /api/routing, PUT /api/routing/{served_name} ────────────────

export interface RouteTarget {
  target_id: string
  kind: TargetKind
  /** may be a remote base_url; never carries credentials */
  backend_url: string
  weight: number
  outstanding: number
  healthy: boolean
  admitting: boolean
  strength: number
  cost_per_mtok: number | null
  /** set by G when a local target is held at zero weight */
  zero_weight_reason?: string | null
  node_ids?: string[]
  provider_id?: string
  display_name?: string
}

export interface RoutingConfig {
  served_name: string
  policy: RoutingPolicy
  targets: RouteTarget[]
  sticky_ttl_s: number
  /** set when G auto-selected the policy rather than a human choosing it */
  auto_selected?: boolean
  auto_reason?: string | null
  /** LOCAL_FIRST only: is traffic currently local or spilled to a remote */
  flow?: 'local' | 'spilled' | null
}

// ── Providers: GET /api/providers ────────────────────────────────────────────

export interface ProviderModel {
  served_name: string
  upstream_id: string
  context_length: number
  supports_streaming: boolean
  supports_tools: boolean
  input_cost_per_mtok: number | null
  output_cost_per_mtok: number | null
}

export interface Provider {
  provider_id: string
  kind: ProviderKind
  display_name: string
  base_url: string
  /** an env var NAME, never key material. Displayed as a label, value is always *** */
  api_key_ref: string
  enabled: boolean
  priority: number
  models: ProviderModel[]
  healthy: boolean
  last_error: string | null
  last_refreshed: number
  /** USD, today, if G tracks it */
  spend_today_usd?: number | null
}

// ── SSE: GET /api/metrics/stream ─────────────────────────────────────────────

export interface MetricsClusterFrame {
  tokens_per_sec: number | null
  total_power_w: number | null
  cache_hit_pct: number | null
}

export interface MetricsNodeFrame {
  node_id: string
  power_w: number | null
  temp_c: number | null
  memory_used_pct: number | null
  util_pct: number | null
}

export interface MetricsDeploymentFrame {
  deployment_id: string
  state: DeploymentState
  tokens_per_sec: number | null
  ttft_ms: number | null
  queue_depth: number | null
}

export interface MetricsFrame {
  ts: number
  cluster: MetricsClusterFrame
  nodes: MetricsNodeFrame[]
  deployments: MetricsDeploymentFrame[]
}
