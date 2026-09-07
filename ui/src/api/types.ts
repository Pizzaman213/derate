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
  /** The operator's name for this machine, set in the node sheet. Null when
   *  nobody renamed it — never a copy of `node_id`, so "renamed to its own id"
   *  and "never renamed" stay distinguishable. Purely a caption: `node_id` is
   *  what deployments, links and routing are keyed by, and a rename does not
   *  move it. */
  label?: string | null
  hostname: string
  device_class: DeviceClass
  gpu_name: string
  state: NodeHealth
  role: NodeRole
  memory_used_pct: number
  power_w: number
  temp_c: number
  util_pct: number
  sample_ts?: number | null
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
  /** Optional because the gateway omits it for a pair it has never probed --
   *  `/api/topology` returns `{src, dst, measured: false, stale: false}` and
   *  nothing else. Declared required here until the live wire disagreed. */
  medium?: string
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

// ── POST /api/links/reach ────────────────────────────────────────────────────

/** One direction of one pair. Legs are directional and reported separately:
 *  asymmetry is the interesting case, not an inconsistency to be averaged into
 *  a single "connected" flag. */
export interface ReachLeg {
  /** node_id of the machine that dialled, or the literal `"coordinator"`. */
  source: string
  target: string
  /** Exactly what was dialled, so an operator can try it themselves. */
  url: string
  ok: boolean
  /** Round trip in ms. Null whenever `ok` is false — never 0, which beside
   *  "unreachable" would read as a fast link. */
  ms?: number | null
  error?: string | null
  /** The node_id the far side called itself. A mismatch is its own fault. */
  answered_as?: string | null
  /** Set only on a leg that was never dialled, saying why. Two kinds, told
   *  apart by `ok`: the coordinator's leg to itself (ok — nothing to dial) and
   *  a direction that could not be tested at all (not ok, and not a failure
   *  either). */
  note?: string | null
  /** True when this leg tests a direction BETWEEN the two nodes asked about.
   *  False for the coordinator's probe of an endpoint when the coordinator is
   *  neither of them — reaching two machines from a third says nothing about
   *  whether those two can reach each other. */
  pair?: boolean
}

export interface ReachReport {
  a: string
  b: string
  ok: boolean
  /** The gateway's own sentence, rendered verbatim. */
  summary: string
  checked_at: number
  legs: ReachLeg[]
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
  /** See `TopologyNode.label`. Deliberately not on `profile`: that is the
   *  hardware as probed, and a re-probe would overwrite a name a human chose. */
  label?: string | null
  healthy: boolean
  state: NodeHealth
  role: NodeRole
  last_seen: number
  /** When the live readings were measured, as opposed to when the agent last
   *  answered a health check. A node whose nvidia-smi is gone keeps a fresh
   *  `last_seen` and a frozen `sample_ts`. */
  sample_ts?: number | null
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

/** One compute context holding GPU memory on a node, as `nvidia-smi
 *  --query-compute-apps` reports it, annotated by the coordinator with the
 *  deployment it belongs to. `deployment_id: null` means nothing we launched
 *  matched it — a leftover llama-server, an orphan from a killed load run —
 *  and that is exactly the case there is no other way to clear. */
export interface GpuProcessDTO {
  pid: number
  name: string
  command: string | null
  user: string | null
  gpu_memory: number
  deployment_id: string | null
  served_name: string | null
  deployment_state: DeploymentState | null
  killable: boolean
  /** Set only when `killable` is false. The server's sentence, rendered
   *  verbatim rather than re-worded in the component. */
  not_killable_reason: string | null
}

/** `available: false` and an empty `processes` are different answers: the
 *  first means nvidia-smi could not be read, the second that nothing is
 *  resident. `reason` carries which, so the UI never has to guess. */
export interface NodeProcessList {
  node_id: string
  processes: GpuProcessDTO[]
  available: boolean
  reason: string | null
}

export interface KillResult {
  pid: number
  name: string
  signal_sent: string
  exited: boolean
  gpu_memory_before: number
  gpu_memory_reclaimed: number
  detail: string
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

// ── Enrollment: the installer's credential ───────────────────────────────────

/** One live enrollment token, as `GET /api/enroll` lists them.
 *
 *  Note what is absent: the secret. The coordinator returns it exactly once,
 *  on the mint, and only inside `Enrollment.command` -- there is no endpoint
 *  that hands back a token you already minted, by design. Losing it means
 *  minting another, which costs nothing.
 */
export interface EnrollmentRow {
  token_id: string
  created_at: number
  expires_at: number
  expires_in_s: number
  /** null means unlimited uses within the TTL. */
  uses_remaining: number | null
  auto_admit: boolean
}

/** What `POST /api/enroll` returns: the row, plus the one line to paste. */
export interface Enrollment extends EnrollmentRow {
  /** The whole `curl ... | sh -s -- --join ... --token ...` line, composed by
   *  the coordinator so the address is its own and not the browser's. This is
   *  the only field that carries the token. */
  command: string
  join_url: string
  install_url: string
  /** Where the FIRST node gets the script, since there is no coordinator to
   *  ask yet. */
  public_install_url: string
}

export interface EnrollmentSpec {
  ttl_s?: number
  uses?: number | null
  auto_admit?: boolean
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
  /** "static" = the addressable ceiling under the guardrail, i.e. what this
   *  hardware could spend with nothing else running. "live" = what the node
   *  could actually hand out when the check ran. */
  budget_basis?: 'static' | 'live'
}

/** `serialize.shape_payload`. The backend emits this; it never echoed back a
 *  `model_id`/`context_length`/`concurrency` triple, which the old type here
 *  claimed and nothing ever read. */
export interface PlanShape {
  model_id: string
  num_layers: number
  hidden_size: number
  num_attention_heads: number
  num_kv_heads: number
  vocab_size: number
  total_params: number
  dtype: string
  head_dim: number | null
  num_experts: number
  num_experts_per_token: number
  active_params: number | null
  sliding_window: number | null
  layers_with_full_attention: number | null
  mla_latent_dim: number | null
  mla_rope_dim: number | null
  vision_params: number
}

/** One node's share of the memory picture the verdict was taken against. */
export interface MemoryReport {
  node_id: string
  binding_limit?: 'gpu' | 'host' | null
  guardrail?: number
  unified_memory?: boolean
  device_class?: string
  swap_total?: number | null
  memory_used_pct?: number | null
  memory_pressure_pct?: number | null
  memory_warn_pct?: number
  memory_critical_pct?: number
  memory_severity?: 'ok' | 'warning' | 'critical' | null
  sampled_at?: number
  allocatable?: number | null
  static_ceiling?: number | null
  addressable?: number | null
  pool_used?: number | null
  gpu_used?: number | null
  host_available?: number | null
  host_reserve?: number | null
  swap_used?: number | null
  stale?: boolean
}

/** What the machine looked like at the moment the gate ran. Every figure is
 *  the one the gate actually used -- the UI must not recompute any of it. */
export interface CapacityBlock {
  basis: string | null
  measured_at: number
  allocatable_per_node: number | null
  static_per_node: number | null
  binding_node: string | null
  nodes: MemoryReport[]
  excluded: { node_id: string; reason: string }[]
}

/** The single field the UI reads to decide what Serve may do. Which verdict
 *  governs is the backend's decision, not the client's -- handing the client
 *  two verdicts to choose between is how the two answers drift apart. */
export interface ServeDecision {
  allowed: boolean
  verdict: Verdict | null
  basis: 'live' | 'static'
  reason: string
  override_required: boolean
  override_param: string | null
  unavailable_reason: string | null
}

export interface PlanResponse {
  plan: ParallelismPlan
  /** null when the fit port is unwired -- `internal_api.py` returns
   *  `"fit": null` on that path, and this type used to claim otherwise, so
   *  every render dereferenced null and blanked the box. */
  fit: FitResult | null
  /** The same verdict taken against what the nodes can hand out right now.
   *  null when nothing could read them; never a stand-in for `fit`. */
  fit_live?: FitResult | null
  /** Absent on a gateway that predates the live-memory work. */
  capacity?: CapacityBlock
  serve?: ServeDecision
  shape?: PlanShape
  /** From the resolver, not the fit gate -- e.g. "least reliable source:
   *  config estimate". Qualifies how trustworthy the plan/fit above are;
   *  render verbatim alongside fit.warnings, never folded into it. */
  resolver_warnings: string[]
}

export interface LaunchRequest {
  model_id: string
  context: number
  concurrency: number
  target: string
  runtime: string
  /** Named for exactly what it overrides. Sent only when a person has read
   *  the sentence naming the measured figure and chosen to proceed. */
  allow_over_live_memory?: boolean
}

export interface PlanRequest {
  model_id: string
  context: number
  concurrency: number
  target: string
  /** SIZING ONLY, and deliberately absent from `LaunchRequest`.
   *
   *  Overrides bytes-per-parameter in the fit arithmetic so "what would this
   *  cost at q4_k_m" can be asked. It can never change what launches: neither
   *  serve command template carries `--quantization`, and the only model
   *  identifier they interpolate is the repository id. `POST /api/deployments`
   *  refuses this field with 400 `dtype_not_launchable` for that reason.
   *
   *  To launch a quantized model, send that variant repository's own id as
   *  `model_id` — a quantization is a different repository, not a flag. */
  dtype?: string
}

/** Which endpoint family a served model answers on. Mirrors
 *  `control_plane/contracts/modality.py`.
 *
 *  `text` and `embedding` are one family in practice -- a single vLLM server
 *  answers `/v1/chat/completions` and `/v1/embeddings` from the same weights --
 *  so only the two audio values mean "a different endpoint". Optional on the
 *  wire: a gateway that predates the field says nothing, and absent reads as
 *  `text`, which is what everything was before audio existed. */
export type Modality = 'text' | 'embedding' | 'speech' | 'transcription'

/** True when this model does not answer the chat endpoint. The one question
 *  the UI actually asks, so it is asked in one place. */
export function isAudio(m: Modality | undefined): boolean {
  return m === 'speech' || m === 'transcription'
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
  /** Absent from a gateway that predates the field; read it as `text`. */
  modality?: Modality
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
  /** Unix seconds when the four readings above were measured; null when the
   *  node has never been sampled. Compare against the frame's own `ts` — both
   *  are the coordinator's clock, so the comparison is immune to client skew.
   *  Not the same as `last_seen`, which only says the agent answered. */
  sample_ts?: number | null
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

// ── OpenAI surface: GET /v1/models, POST /v1/chat/completions ────────────────
//
// The first `/v1` types in this file. Everything above mirrors `/api/*`, which
// is the control plane's own surface; these two endpoints are the product's
// public one, and the Chat tab is the only thing in the UI that speaks them.

/** One entry of `GET /v1/models`. `id` is the `served_name` a client passes as
 *  `model`. The three fields after it are gateway extras (openai_api.py's
 *  "Clients ignore what they do not know; the UI uses them") -- they are what
 *  lets the model list say local or remote, and how many targets back it. */
export interface ServedModel {
  id: string
  /** `null` when the gateway advertises none for this name. Never 0. */
  context_length: number | null
  target_count: number
  target_kinds: TargetKind[]
  /** Which endpoint accepts this name. Absent reads as `text`. Without it the
   *  picker cannot tell a TTS model from a chat model, which is exactly how a
   *  provider's `whisper-1` ended up offered as a chat target. */
  modality?: Modality
}

export type ChatRole = 'user' | 'assistant'

export interface ChatMessage {
  role: ChatRole
  content: string
}

/** What one completed turn observed about itself.
 *
 *  Every figure here is measured in the browser or read off the wire; none is
 *  derived from a constant. `completionTokens` is the one that needs a
 *  provenance flag beside it, because there are two ways to arrive at it and
 *  they are not the same number -- see `tokensEstimated`. */
export interface ChatTurnMeta {
  model: string
  /** `X-Request-Id`, minted by the gateway before the first thing that can
   *  refuse, so a bad answer can be traced to the row that recorded it.
   *  `null` only if the header was absent. */
  requestId: string | null
  /** Milliseconds to the first delta carrying text. Not to the first byte:
   *  the gateway deliberately holds the status line until the first upstream
   *  chunk, so the first token is the honest mark. `null` if none arrived. */
  ttftMs: number | null
  /** Milliseconds from send to the end of the stream. */
  elapsedMs: number | null
  completionTokens: number | null
  /** True when `completionTokens` counts delta frames rather than reading an
   *  upstream `usage` block. The backend draws exactly this distinction on its
   *  own request records (`tokens_estimated`); presenting a counted frame as a
   *  measured token would be the same fabrication with a nicer font. */
  tokensEstimated: boolean
  /** The reader was cancelled from the Stop button. The text is a real partial
   *  answer, not a failure. */
  stopped: boolean
}


// ── Capacity: GET /api/memory, GET /api/capacity ─────────────────────────────

/** One model's answer under one budget. Every field is the fit gate's; the
 *  UI renders this and computes none of it. */
export interface CapacityRow {
  model_id: string
  label: string
  total_params: number
  native_dtype: string
  /** The quantization that fits, or null when nothing on the ladder does. */
  dtype: string | null
  /** True when the ladder had to step down from the model's native dtype. */
  requantized: boolean
  verdict: Verdict | 'error'
  fits: boolean
  total: number | null
  headroom: number | null
  predicted_decode_tps: number | null
  reason: string
  warnings: string[]
}

export interface CapacitySide {
  allocatable_per_node?: number
  usable_per_node?: number
  rows: CapacityRow[]
  /** The largest that fits, by parameter count. null when nothing does. */
  best: CapacityRow | null
}

export interface CapacityReport {
  probed_node: string
  context: number
  concurrency: number
  kv_dtype: string
  nodes: string[]
  measured_at: number
  excluded: { node_id: string; reason: string }[]
  unresolved: { model_id: string; reason: string }[]
  unavailable_reason: string | null
  live: CapacitySide | null
  static?: CapacitySide
}

export interface MemoryReportList {
  nodes: MemoryReport[]
  measured_at: number
}


// ── Models browser ───────────────────────────────────────────────────────────
//
// Every number below is computed by the gateway. Nothing here is a figure the
// browser worked out for itself: a second implementation of a byte count or a
// head-count ratio is a second answer, and the one that disagrees with the fit
// gate is the one that gets somebody an out-of-memory kill.

/** One row of `GET /api/models/quant-table` — the contract's own table. */
export interface QuantScheme {
  key: string
  bytes_per_param: number
  bits_per_weight: number
  family: 'float' | 'int' | 'gguf' | 'block-float' | string
  /** Minimum CUDA compute capability for a native kernel; null when any will do. */
  native_compute_capability: number | null
  emulated_below_native: boolean
  note: string
  runtimes: Record<string, 'supported' | 'unverified' | 'unsupported'>
}

export interface QuantTable {
  default_dtype: string
  suggestion_order: string[]
  /** Cheapest first. An array, not a record: `noUncheckedIndexedAccess` makes
   *  every keyed lookup `T | undefined` and every call site branch on a case
   *  that cannot happen. */
  schemes: QuantScheme[]
}

/** `GET /api/catalog` — the curated shortlist, server side so the capacity
 *  answer and the picker cannot drift apart. */
export interface CuratedModel {
  model_id: string
  label: string
  detail: string
  default_context: number
  default_concurrency: number
}

/** Present/absent capability facts. When `present` is false every sibling is
 *  null rather than 0 — `window: 0` would claim every layer is windowed. */
export interface MoeCapability {
  present: boolean
  num_experts: number | null
  num_experts_per_token: number | null
  active_params: number | null
  active_fraction: number | null
}

export interface MlaCapability {
  present: boolean
  latent_dim: number | null
  rope_dim: number | null
  /** latent + rope. The latent alone is not the cached width, and reading it
   *  as if it were understates the KV cache on every DeepSeek checkpoint. */
  cached_width_per_layer: number | null
}

export interface GqaCapability {
  present: boolean
  num_attention_heads: number | null
  num_kv_heads: number | null
  ratio: number | null
}

export interface WindowCapability {
  present: boolean
  window: number | null
  /** null means every layer caches the full context; 0 means every layer is
   *  windowed; anything between is a real interleave. */
  layers_with_full_attention: number | null
  num_layers: number
}

/** Multi-token prediction. The checkpoint carries the module and the hub's
 *  weight index counts it; no runtime loads it unless speculative decoding is
 *  on, so `total_params` excludes it. Both figures are here so the screen can
 *  state the gap rather than leave it to be discovered. */
export interface MtpCapability {
  present: boolean
  params: number | null
  counted_in_total_params: boolean | null
  total_params_with_mtp: number | null
  note: string | null
}

export interface Capabilities {
  moe: MoeCapability
  mla: MlaCapability
  gqa: GqaCapability
  sliding_window: WindowCapability
  vision: { present: boolean; vision_params: number | null }
  context: { max_position_embeddings: number | null }
  mtp: MtpCapability
}

/** Whether this can be served here at all. Governs whether a Serve control
 *  exists; the fit verdict governs whether it is enabled. */
export interface Launchable {
  ok: boolean
  reason: string
}

export interface RuntimeSupport {
  runtime: string
  level: 'supported' | 'unverified' | 'unsupported'
  reason: string
}

/** `GET /api/models/detail`. */
export interface ModelDetail {
  model_id: string
  revision: string | null
  model_type: string
  architectures: string[]
  max_position_embeddings: number | null
  shape: PlanShape
  is_moe: boolean
  active_params_effective: number
  gqa_ratio: number | null
  bytes_per_param: number
  weight_bytes: number | null
  weight_bytes_effective: number | null
  param_breakdown: Record<string, number>
  /** Where each number came from, best first. Shown, not hidden: it is the
   *  difference between a measured figure and an analytic guess. */
  param_source: string | null
  quant_source: string | null
  capabilities: Capabilities
  launchable: Launchable
  support: {
    architectures: string[]
    runtimes: RuntimeSupport[]
    quant: {
      dtype: string
      native_compute_capability: number | null
      emulated_below_native: boolean
      note: string
    }
  } | null
  /** Can the nodes we actually have run this scheme? */
  nodes: { ok: boolean; problems: string[]; checked: number }
  warnings: string[]
  from_cache: boolean
  resolved_at: number
  elapsed_ms: number
}

/** One obtainable set of weights. `repo_id` is what a launch would use. */
export interface QuantVariant {
  /** Canonical key this codebase prices with. */
  dtype: string
  /** What the publisher called it — "UD-Q4_K_XL". Rendered verbatim; showing
   *  the canonical key instead would paraphrase somebody else's name. */
  label: string
  repo_id: string
  source: 'self' | 'repo_name' | 'tags' | 'gguf_file' | string
  gguf_file: string | null
  /** Measured, or null. Never a table estimate dressed as a size. */
  file_bytes: number | null
  downloads: number | null
  launchable: boolean
  note: string
  /** Files in this quantization. `file_bytes` is their sum and `gguf_file`
   *  names only the first, so without this a size and a filename disagree
   *  about how much is being described. */
  shard_count: number
  /** Every file in the set, in shard order. What the summed size is the sum
   *  of, so the count is checkable rather than merely asserted. */
  shard_files: string[]
  /** Position in the gateway's ordering: fit first, then the best quality
   *  inside that tier. Sent as a number so the browser sorts by an integer
   *  instead of reimplementing the judgement and disagreeing with the fit
   *  gate. Row 0 is the recommendation. */
  rank: number
  bits_per_weight: number | null
  family: string | null
  verdict: string | null
  fits: boolean | null
  headroom: number | null
  reason: string
  predicted_decode_tps: number | null
}

export interface VariantLadder {
  model_id: string
  variants: QuantVariant[]
  /** The largest variant that both fits and can be served here, or null when
   *  nothing does. */
  recommended: { repo_id: string; label: string; dtype: string; reason: string } | null
  /** Always true, and shown: names are the only signal most quantizers leave. */
  heuristic: boolean
  note: string
  from_cache: boolean
}


/** One hit from `GET /api/models/search`. Deliberately shapeless: the search
 *  path never resolves, so `resolved` is always false and no field here claims
 *  to describe the model's architecture. */
export interface ModelHit {
  model_id: string
  origin: 'deployment' | 'provider' | 'hub' | string
  served_name?: string | null
  provider_id?: string | null
  state?: string | null
  downloads?: number | null
  likes?: number | null
  pipeline_tag?: string | null
  /** Unix seconds from the hub listing. On the wire since the search endpoint
   *  shipped (`resolver.py` selects it); it was simply missing from this type. */
  last_modified?: number | null
  gated?: boolean | string | null
  tags?: string[]
  /** Guessed from the name and offered as a guess. */
  quant_hint?: string | null
  resolved: boolean
}

export interface ModelSearchResponse {
  query: string
  /** Per source, so the hub being down greys one section instead of emptying
   *  the screen. */
  sources: Record<string, { ok: boolean; note: string | null }>
  notes: string[]
  results: ModelHit[]
}

// ── Storage ──────────────────────────────────────────────────────────────────
// `GET /api/storage`. Read on demand, never sampled: disk is not in the metrics
// frame and not in the archive, deliberately, so nothing here has a history and
// every number is "as of measured_at".

/** One filesystem, counted once however many of our paths live on it.
 *
 *  `used + free` is smaller than `total` by `reserved` — the blocks a
 *  filesystem holds back for root. `used_pct` is computed against
 *  `used + free`, which is what `df` reports and what we can actually
 *  allocate into. */
export interface Filesystem {
  device: number
  mount_paths: string[]
  total: number
  used: number
  free: number
  reserved: number
  used_pct: number
  warn_pct: number
  critical_pct: number
  severity: 'ok' | 'warn' | 'critical'
}

/** One component of the data root. `bytes` is null when the component does not
 *  exist or could not be read — never 0, which would read as "present and
 *  empty". */
export interface EstateEntry {
  key: string
  label: string
  path: string
  kind: 'file' | 'dir'
  exists: boolean
  bytes: number | null
}

/** A path we tried to measure and could not, with the reason to render. */
export interface UnreadablePath {
  path: string
  reason: string
}

/** One cached repository on a node. `bytes` is what `blobs/` holds, which is
 *  what the disk actually gives back if it is deleted — snapshots are symlink
 *  trees into those same blobs and would double-count every revision. */
export interface CachedModel {
  folder: string
  repo_id: string
  bytes: number
  blob_count: number
  revisions: string[]
  last_modified: number | null
}

/** The downloaded weights on one node. `available: false` means no cache was
 *  found — usually a container without the mount — which is a different answer
 *  from a cache holding nothing. */
export interface ModelCache {
  available: boolean
  path: string | null
  repos: CachedModel[]
  total_bytes: number | null
  reason: string | null
  measured_at?: number
}

/** What `DELETE /api/storage/nodes/{id}/models/{folder}` gives back. */
export interface ModelDeleteResult {
  deleted: boolean
  folder: string
  repo_id: string
  bytes_freed: number
}

/** One node's disk picture. `available: false` is not an empty disk — the
 *  reason says which, and the UI must never render it as free space. */
export interface NodeStorage {
  node_id: string
  root?: string
  filesystems: Filesystem[]
  estate: EstateEntry[]
  unreadable: UnreadablePath[]
  measured_at?: number
  available: boolean
  reason: string | null
  /** The downloaded weights on this node. Fetched in the same fan-out, so it
   *  can never be a poll behind the filesystem it is filling. */
  models?: ModelCache
}

/** The coordinator's view of one node's collection: how far behind it is, and
 *  whether anything was dropped rather than shipped. */
export interface CollectorCursor {
  node_id: string
  last_seq: number
  last_ship_ts: number | null
  dropped: number
  head: number
  last_error: string | null
  behind: number
  age_s: number | null
}

export interface JournalStats {
  node_id: string
  path: string
  rows: number
  head: number
  shipped_hwm: number
  queued: number
  dropped: number
  written: number
  bytes: number
}

export interface ArchiveStats {
  path: string
  bytes: number
  rows: { samples: number; requests: number; events: number; logs: number }
  nodes: CollectorCursor[]
  oldest_sample_ts: number | null
  gaps?: unknown[]
  schema_version?: number
}

/** Telemetry's own account of itself. `enabled: false` carries a `reason`;
 *  telemetry is off when its data root does not exist, which is how a
 *  development machine differs from the container. */
export interface TelemetryEstate {
  enabled: boolean
  reason?: string
  journal?: JournalStats
  archive?: ArchiveStats
}

/** Retention horizons and ceilings, from the module that enforces them. Never
 *  re-typed in the browser: a horizon a deployment moved must not be reported
 *  by its default. */
export interface RetentionPolicy {
  samples_raw_s: number
  requests_raw_s: number
  events_s: number
  logs_s: number
  rollup_1m_s: number
  rollup_1h_s: number
  archive_max_bytes: number
  journal_max_bytes: number
  journal_retention_s: number
}

export interface StorageReport {
  nodes: NodeStorage[]
  telemetry: TelemetryEstate
  retention: RetentionPolicy
  measured_at: number
}

/** What `DELETE /api/storage/cache/resolver` gives back. */
export interface CacheClearResult {
  cleared: boolean
  bytes_freed: number
}

// ── History ──────────────────────────────────────────────────────────────────
//
// `GET /api/history/{nodes,requests,events,logs}`. These routes shipped with
// the durable-telemetry package and had no client at all until the node sheet
// grew charts; `control_plane/telemetry/query.py` is the shape below.
//
// Two things about them drive every type here. First, the SAME route answers
// with raw columns or rolled-up columns depending on how wide the window is
// (`step=auto`: raw under 6 hours, 1-minute buckets under 7 days, hourly
// beyond), so every column that only one of the two branches selects is
// optional and a reader must check rather than assume. Second, every answer
// carries its own provenance -- which resolution it used, whether it survives
// a restart, and which parts of the window are known-missing -- because a flat
// line has two causes and the product already refuses to blur that
// distinction for a link it has never measured.

/** A stretch the archive knows it does not have. Rendered as the sentence it
 *  is, never smoothed over: trimmed and quiet are different answers. */
export interface HistoryGap {
  node_id: string
  from_ts: number
  to_ts: number
  reason: string
}

export interface HistoryEnvelope {
  from: number
  to: number
  /** `'raw' | '1m' | '1h'` from the archive, or `'ring'` when there is no
   *  archive and the answer came from the registry's 300-sample in-RAM
   *  buffer instead. */
  resolution: 'raw' | '1m' | '1h' | 'ring'
  /** False for `'ring'`: it does not survive a restart. A durable and a
   *  non-durable series must never be charted as if they were the same
   *  claim about the world. */
  durable: boolean
  gaps: HistoryGap[]
  /** The window was wider than the row budget, so the FAR end was dropped.
   *  Say so; a truncated window that reads as a complete one is a lie about
   *  when something started. */
  truncated: boolean
}

/** One node sample. Raw rows carry the instantaneous columns; rolled rows
 *  carry `_avg`/`_max` over the bucket and an `n`. Never both. */
export interface NodeHistorySample {
  node_id: string
  /** The sample time, or the bucket's left edge when rolled. */
  ts: number

  // raw
  memory_used?: number
  memory_total?: number
  power_w?: number | null
  temp_c?: number | null
  util_pct?: number | null
  gpu_memory_used?: number | null
  gpu_process_count?: number | null
  host_memory_total?: number | null
  host_memory_available?: number | null
  swap_used?: number | null

  // rolled
  n?: number
  power_w_avg?: number | null
  power_w_max?: number | null
  temp_c_avg?: number | null
  temp_c_max?: number | null
  util_pct_avg?: number | null
  util_pct_max?: number | null
  memory_used_avg?: number | null
  memory_used_max?: number | null
  gpu_memory_used_avg?: number | null
  gpu_memory_used_max?: number | null
  host_memory_available_min?: number | null
  swap_used_max?: number | null
}

export interface NodeHistory extends HistoryEnvelope {
  samples: NodeHistorySample[]
}

/** A merged log-spaced histogram's summary. These are REAL percentiles: the
 *  buckets merge exactly, so an hourly p99 is the p99 of that hour and not a
 *  mean of sixty percentiles. Nothing else in the product has them -- the live
 *  frame's TTFT and mean duration are exponential moving averages. */
export interface HistSummary {
  n: number
  mean_ms: number | null
  p50_ms: number | null
  p90_ms: number | null
  p99_ms: number | null
  p999_ms: number | null
  max_ms: number | null
}

/** One request attempt (raw) or one bucket of them (rolled). A retried
 *  request is several raw rows sharing a `request_id` and differing in
 *  `attempt_no`. */
export interface RequestHistoryRow {
  ts: number

  // raw
  request_id?: string
  attempt_no?: number
  node_id?: string
  served_name?: string
  target_id?: string
  target_kind?: string
  provider_id?: string
  deployment_id?: string
  policy?: string
  strength_source?: string
  attempts?: number
  retry_reason?: string
  status?: number | null
  error_code?: string
  error_class?: string
  prompt_tokens?: number
  completion_tokens?: number
  tokens?: number
  /** 1 when the token count was counted from SSE frames rather than read
   *  from an upstream `usage` block. A counted frame presented as a measured
   *  token is the same fabrication in a nicer font. */
  tokens_estimated?: number
  ttft_ms?: number | null
  decode_ms?: number | null
  duration_ms?: number | null
  parked_ms?: number | null
  streaming?: number
  cost_usd?: number | null

  // rolled
  bucket?: number
  n?: number
  ok?: number
  failed?: number
  ttft?: HistSummary
  duration?: HistSummary
}

export interface RequestHistory extends HistoryEnvelope {
  requests: RequestHistoryRow[]
}

/** A lifecycle event as the bus emitted it. `body`'s keys are merged up into
 *  the row by the query layer, so an event carries whatever its emitter put
 *  there beyond the typed columns. */
export interface HistoryEvent {
  node_id: string
  ts: number
  source: string
  type: string
  deployment_id?: string
  served_name?: string
  [key: string]: unknown
}

export interface EventHistory extends HistoryEnvelope {
  events: HistoryEvent[]
}

/** One log record, already redacted at the handler that shipped it. */
export interface HistoryLog {
  node_id: string
  ts: number
  level: string
  logger: string
  message: string
  [key: string]: unknown
}

export interface LogHistory extends HistoryEnvelope {
  logs: HistoryLog[]
}
