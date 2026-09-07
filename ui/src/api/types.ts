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
  /** `/api/topology`'s edges are the same `link_payload` shape as
   *  `/api/links` and `Cluster.links` -- see `LinkMeasurement` for what each
   *  of these means and when it is present. */
  estimated?: boolean
  raw_gbps?: number | null
  scale_factor?: number | null
  notes?: string[]
  active_ports?: number | null
  total_ports?: number | null
  ports_inspected_on?: string | null
  gdr_detected_by?: string | null
  duration_s?: number | null
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
  /** null when the node has never reported telemetry. A live-looking zero is
   *  worse than an honest blank, so this is never defaulted on the way in. */
  power_watts: number | null
  temperature_c: number | null
  utilization_pct: number | null
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
  /** Below here: present only when the measurement carries a
   *  `links.record.LinkAnnotation` (serialize.link_payload) -- a bare
   *  `LinkMeasurement` (a hand-built fixture, the stub) says nothing about
   *  estimation rather than implying "measured, not estimated". */
  estimated?: boolean
  /** Only meaningful alongside `scale_factor`: the rung below `all_reduce_gbps`
   *  on the measurement ladder (e.g. raw RDMA bandwidth), not NCCL bandwidth,
   *  and never rendered as though it were. */
  raw_gbps?: number | null
  scale_factor?: number | null
  /** One line each, verbatim -- how the figure above was derived. */
  notes?: string[]
  active_ports?: number | null
  total_ports?: number | null
  ports_inspected_on?: string | null
  gdr_detected_by?: string | null
  duration_s?: number | null
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
  addressable_memory: number
  /** false when this candidate is reachable but would not join the current pool */
  eligible?: boolean
  ineligible_reason?: string | null
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

export type CircuitState = 'closed' | 'open' | 'half_open'

/** Where `strength` was actually computed from. Its raw form
 *  (`strength_raw`) changes UNIT with this: tokens/sec when measured or
 *  predicted, GB/s × gpu_count when bandwidth, a dimensionless 1.0 when
 *  default -- so `strength_raw` is only meaningful beside it and the two are
 *  always present or absent together. */
export type StrengthSource = 'measured' | 'predicted' | 'bandwidth' | 'default'

export interface TargetCounters {
  /** Counters: `0` is a real answer ("never failed"), so these are `null`
   *  only when the target has never been selected at all. */
  completed: number | null
  failed: number | null
  total_tokens: number | null
  /** EWMAs: `null` until observed. Never `0` through the normal path, so a
   *  literal `0` here would be a rate nobody measured. */
  decode_tps: number | null
  mean_duration_s: number | null
}

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
  strength_source?: StrengthSource | null
  /** the un-normalized score behind `strength`. Present exactly where
   *  `strength_source` is. */
  strength_raw?: number | null
  cost_per_mtok: number | null
  /** set by G when a local target is held at zero weight */
  zero_weight_reason?: string | null
  node_ids?: string[]
  /** the failover breaker's view of this target. Absent targets read as
   *  closed; G always emits it, this is just belt-and-suspenders. */
  circuit?: CircuitState
  /** per-target request accounting. Absent only if the caller never asked
   *  for this target at all -- present-with-all-nulls means "known, never
   *  selected". */
  counters?: TargetCounters | null
  /** why this target is not admitting, sorted; null (or absent) when
   *  nothing blocks it. A READY deployment showing `admitting: false` with
   *  nothing here is the bug, not the normal case. */
  admission_blocks?: string[] | null
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

  // The nine spend keys (gateway/ui_detail.py `_SPEND_KEYS`): copied one at a
  // time from the provider port's own accounting, all `null` -- never `0` --
  // when the port does no accounting at all, so "never priced" and "priced at
  // zero" stay distinguishable on the wire and in the Spend/Settings screens.
  /** Whether this provider is currently accepting new requests -- rate limit
   *  and budget included, distinct from `healthy` above. */
  admitting: boolean | null
  /** One sentence naming why `admitting` is false; null while it is true. */
  admission_block: string | null
  daily_budget_usd: number | null
  spend_today_usd: number | null
  tokens_today: number | null
  requests_today: number | null
  /** Requests served by a model this provider never published a price for --
   *  spend that is real but not accounted in `spend_today_usd`. */
  unpriced_requests_today: number | null
  /** Seconds until a rate-limit backoff clears; null when not backed off. */
  retry_in_s: number | null
  model_count: number | null
}

/** `POST /api/providers` body (providers/service.py `_register`). Only `kind`
 *  is required; known kinds prefill everything else server-side. */
export interface ProviderSpec {
  kind: ProviderKind
  provider_id?: string
  base_url?: string
  /** the NAME of an environment variable or secrets.json key -- never a key */
  api_key_ref?: string
  display_name?: string
  aliases?: Record<string, string>
  daily_budget_usd?: number | null
  enabled?: boolean
  priority?: number
}

/** `PATCH /api/providers/{id}` body (providers/service.py `update`). */
export type ProviderPatch = Partial<
  Pick<
    ProviderSpec,
    'enabled' | 'priority' | 'display_name' | 'daily_budget_usd' | 'base_url' | 'api_key_ref' | 'aliases'
  >
>

// ── Settings: GET/PATCH /api/settings ────────────────────────────────────────

export type SettingSource = 'file' | 'env' | 'default'

/** The three settings a human can change at runtime (gateway/settings_store.py
 *  `MUTABLE_FIELDS`) -- everything else in `GatewaySettings` is a code-level
 *  tunable, not a preference, and has no UI control. */
export interface Settings {
  electricity_rate_usd_per_kwh: number
  local_only: boolean
  /** null means unset -- never rendered as "no cap", which would claim
   *  containment that is not configured. */
  daily_spend_cap_usd: number | null
  /** Where each value above came from, so "0.00 because nobody set it" and
   *  "0.00 because someone set it to zero" can render differently. */
  sources: Record<'electricity_rate_usd_per_kwh' | 'local_only' | 'daily_spend_cap_usd', SettingSource>
  writable: string[]
  /** The honesty gate on the cap: false when no provider port reports spend,
   *  so there is nothing to enforce the cap against. The UI renders the cap
   *  control disabled, with this as the reason, rather than offering a
   *  control that silently fails open. */
  daily_spend_cap_enforceable: boolean
}

export type SettingsPatch = Partial<
  Pick<Settings, 'electricity_rate_usd_per_kwh' | 'local_only' | 'daily_spend_cap_usd'>
>

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
  /** null on the gateway's degraded path (registry/deployment list
   *  unavailable) rather than an empty list, so the wire keeps that
   *  distinction. useMetrics coalesces to `[]` in one place; nothing past it
   *  should see the null. */
  nodes: MetricsNodeFrame[] | null
  deployments: MetricsDeploymentFrame[] | null
}
