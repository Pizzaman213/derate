// TypeScript mirrors of the frozen contracts in 00-architecture.md section 4.
// These describe Agent G's HTTP surface only. Nothing here is invented: every
// field appears in the architecture doc or in one of its example payloads.

export type DeviceClass = 'gb10' | 'discrete' | 'apple' | 'cpu' | 'unknown'
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
  /** 0 on a machine the probe found no GPU on, whose `util_pct` is then host
   *  CPU from /proc/stat. Optional only because the stubs predate it. */
  gpu_count?: number
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

/** One model served by a provider rather than by a deployment.
 *
 *  `node_id` is the interesting field and the reason this exists: a provider
 *  can BE a machine on the roster -- the Pi enrols as a GPU-less node and is
 *  also registered as an Ollama provider -- and in that case a model served
 *  "remotely" is running on a box already drawn on the cluster screen. `null`
 *  is the ordinary case, somebody else's hardware, and is matched exactly
 *  server-side: never inferred from a subnet and never reverse-resolved. */
export interface TopologyRemote {
  target_id: string
  provider_id: string
  served_name: string
  upstream_id: string
  /** The roster node hosting this provider, or null for somebody else's. */
  node_id: string | null
  state: 'healthy' | 'unhealthy'
  admitting: boolean | null
  tokens_per_sec: number
  /** Which endpoint family this row answers on. Absent from a coordinator
   *  older than the key; read it as `text`. It decides which entry plate the
   *  band hangs off on the cluster floor -- a provider's `tts-1` drawn behind
   *  `POST /v1/chat/completions` is a picture of a request that 400s. */
  modality?: Modality
}

export interface Topology {
  cluster_id: string
  coordinator: string
  nodes: TopologyNode[]
  edges: TopologyEdge[]
  deployments: TopologyDeployment[]
  /** Absent on a coordinator older than this key; treat as []. */
  remotes?: TopologyRemote[]
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

/** What one provider kind needs before anyone configures it. `GET
 *  /api/providers/kinds`, straight from the server's own KindSpec table, so
 *  the add form cannot drift from what the server will accept. */
export interface ProviderKindSpec {
  kind: ProviderKind
  display_name: string
  /** The default the server applies when base_url is left blank. Shown rather
   *  than implied: Ollama's is `http://localhost:11434/v1`, which resolves on
   *  the coordinator and is almost never the box the operator meant. */
  base_url: string
  requires_key: boolean
  requires_base_url: boolean
  /** Whether this kind hosts its own weights and can be told to fetch one.
   *  False for a hosted API, which already has every model it will ever have. */
  supports_pull: boolean
  publishes_pricing: boolean
  forwardable: boolean
  /** Whether this kind aggregates several backend hosts per model and can be
   *  asked which ones (`GET /api/providers/{id}/backends`), and told to pin
   *  one (`PATCH ... {backend_pins}`). True only for OpenRouter. */
  supports_backend_routing: boolean
  /** Set when this build cannot talk to the kind at all. The server's own
   *  sentence; render it verbatim and do not offer the kind. */
  unsupported_reason: string | null
}

/** `POST /api/providers/{id}/pull`. 202 once the download size is known and
 *  accepted; the transfer continues on the server. */
export interface PullAccepted {
  provider_id: string
  model: string
  /** 0 when the provider already had it and nothing was downloaded. */
  download_bytes: number
  /** The node the size was judged against, or the bare host when no node in
   *  the roster claims that address and the pull went unjudged. */
  checked_against: string
  free_bytes: number
  budget_bytes: number
  /** Whether a size was actually weighed against a measurement. The two byte
   *  counts above cannot say: a machine that never joined the cluster and one
   *  that was measured with nothing free both report 0, and reporting the
   *  first as "0 GiB free" states a measurement that never happened. */
  gated: boolean
  state: 'pulling' | 'present'
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
  /** What `memory_used` is a fraction of, measured rather than probed. On a GPU
   *  node prefer `profile.addressable_memory`; this is what a machine with no
   *  GPU has instead, and 0 when nothing has been sampled. */
  memory_total: number
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
export interface ServeRequirement {
  /** The `LaunchRequest` field this unlocks, sent as `true` only once a person
   *  has read the sentence and ticked the box. */
  param: string
  /** The server's own sentence for why the permission is needed. Rendered
   *  verbatim -- it is the claim the checkbox sits under. */
  reason: string
}

export interface ServeDecision {
  allowed: boolean
  verdict: Verdict | null
  basis: 'live' | 'static'
  reason: string
  override_required: boolean
  override_param: string | null
  unavailable_reason: string | null
  /** Every permission this launch needs, each with its own sentence. When
   *  present it is the COMPLETE list and supersedes the two legacy fields --
   *  the live-memory gate appears here too, as `allow_over_live_memory`.
   *
   *  It exists because `allowed: false` can express only one gate and only a
   *  memory one: pooling unlike hardware is a permission an operator grants
   *  while the fit itself passes. `allowed` keeps its old meaning exactly (the
   *  fit gate passed) and is no longer sufficient on its own.
   *
   *  Absent -- not empty -- on a gateway that predates it. */
  overrides?: ServeRequirement[]
}

/** What became of degrees the operator set by hand, and whose choice the
 *  effective ones were. */
export interface PlanDegrees {
  source: 'planner' | 'operator'
  tensor_parallel: number
  pipeline_parallel: number
  expert_parallel: number
  data_parallel: number
  /** The recommendation's own rejection line for the shape that was chosen
   *  instead, whole. null when the planner never rejected it -- a shape can
   *  rank second without being argued against, which is not a warning.
   *
   *  Matched server-side, because the labels it matches on ("TP=2",
   *  "TP=2/PP=2", "single node") are the planner's vocabulary. Prefix-matching
   *  `plan.rejected` here would be a second copy of that vocabulary, and it
   *  would break silently the first time a label changed. */
  rejection: string | null
}

/** Which machines were asked for, which are used, and whether they are alike. */
export interface PlacementBlock {
  mode: 'planner' | 'operator'
  /** null when the planner chose. Never `[]` -- an empty selection is a 400. */
  requested_node_ids: string[] | null
  node_ids: string[]
  /** Named but carrying no rank. Only reachable when the degrees were left to
   *  the planner; naming both and under-filling is refused outright. */
  unused_node_ids: string[]
  mixed_hardware: boolean
  warnings: string[]
}

/** One legal shape on this node set: degrees and hosts, no prose.
 *
 *  Deliberately carries neither `reason` nor `rejected` -- a rejection list per
 *  shape is kilobytes of text to populate a hint, and choosing one triggers a
 *  re-plan that returns its full planner prose anyway. */
export interface PlanAlternative {
  kind: ParallelismKind
  world_size: number
  tensor_parallel: number
  pipeline_parallel: number
  expert_parallel: number
  data_parallel: number
  node_ids: string[]
}

export interface PlanResponse {
  plan: ParallelismPlan
  /** What this verdict was taken at, which is not necessarily what was sent:
   *  a request with no context asks the coordinator to choose one out of what
   *  actually fits. Optional because a gateway that predates the derivation
   *  never echoed them. */
  context?: number
  concurrency?: number
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

  /** Whose choice the machines were, and which of them carry a rank. Absent on
   *  a gateway that predates manual placement. */
  placement?: PlacementBlock
  /** Whose choice the degrees were. `source` is the explicit "you overruled
   *  me" signal -- read it rather than comparing plans. */
  degrees?: PlanDegrees
  /** The planner's own pick over the SAME node set, reason and rejected list
   *  intact, so an overruled recommendation stays on screen. Always sent by a
   *  gateway that has it, whether or not it differs from `plan`. */
  recommended_plan?: ParallelismPlan | null
  /** Every legal shape on this node set, for the degree hints. */
  alternatives?: PlanAlternative[]
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
  /** The second override, same rule. Independent of the one above: neither
   *  implies the other, and both must be sent when both gates apply. */
  allow_mixed_hardware?: boolean
  node_ids?: string[]
  parallelism?: ParallelismRequest
  /** Raw CLI text, e.g. `--quantization modelopt_fp4 --kv-cache-dtype fp8`,
   *  for a model the standard recipe doesn't cover. Launch-only, like this
   *  whole request: it never reaches sizing or planning, only the generated
   *  serve command, appended after every knob the plan already emits. The
   *  gateway splits it into tokens and checks each against an allowlist —
   *  a rejected token comes back as a 400 naming which one and why. Mutually
   *  exclusive with custom_command. */
  extra_args?: string
  /** Raw CLI text that REPLACES the generated serve command instead of
   *  appending to it: none of the plan's own TP/PP/context/concurrency/
   *  gpu-memory-utilization flags reach the launched process, only this text
   *  plus --host/--port/--served-model-name, which the gateway still pins
   *  after it. Same tokenizing and allowlist as extra_args. Mutually
   *  exclusive with it — sending both is a 400. */
  custom_command?: string
}

/** Degrees the operator set by hand.
 *
 *  A key omitted from this object means **1**, not "whatever the planner would
 *  have picked": defaulting to the recommendation would make the launched shape
 *  depend on a recommendation nobody saw, and one that can change between the
 *  preview round trip and the launch round trip. Degrees are owned as a set --
 *  send the object or omit it. */
export interface ParallelismRequest {
  tensor_parallel?: number
  pipeline_parallel?: number
  expert_parallel?: number
  data_parallel?: number
}

export interface PlanRequest {
  model_id: string
  /** Omitted on the default path, and omission is the request: the
   *  coordinator derives a context from what actually fits, clamped to the
   *  model's own window, and echoes what it chose. Sent only when somebody
   *  has overridden it in the Serve panel's advanced disclosure. */
  context?: number
  concurrency?: number
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

  /** Exactly the machines to plan across, in order -- the first is the
   *  pipeline head. Absent means the planner picks, which is what every
   *  request sent before this field existed meant. Never `[]`: an explicit
   *  empty selection is a 400, because silently reading it as "the planner
   *  picks" would substitute a placement nobody asked for. */
  node_ids?: string[]
  /** Absent means the planner picks the degrees. */
  parallelism?: ParallelismRequest
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

/** The route each family is served on. Mirrors `ENDPOINT_FOR_MODALITY` in
 *  `control_plane/contracts/modality.py`, and it is here rather than in the
 *  screen that draws it because two screens now need it: the cluster floor
 *  labels its entry plates from this, and the model pane says which route the
 *  model it is offering to serve will answer on. */
export const ENDPOINT_FOR_MODALITY: Record<Modality, string> = {
  text: '/v1/chat/completions',
  embedding: '/v1/embeddings',
  speech: '/v1/audio/speech',
  transcription: '/v1/audio/transcriptions',
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
  /** The extra CLI tokens this deployment was launched with, if any.
   *  Absent from a gateway that predates the field; read it as none. */
  extra_args?: string[]
  /** The custom-command tokens this deployment was launched with, if any --
   *  mutually exclusive with extra_args. Absent from a gateway that
   *  predates the field; read it as none. */
  custom_command?: string[]
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

/** One row of `GET /api/providers/{id}/models` -- the whole catalogue, which is
 *  the ONE provider surface the allowlist does not filter, because it is the
 *  one the allowlist is chosen from. Everywhere else in the app a provider's
 *  `models` are only the enabled ones, filtered server-side. */
export interface ProviderCatalogueModel extends ProviderModel {
  enabled: boolean
}

/** One row of `GET /api/providers/{id}/backends?upstream_id=...` -- the
 *  backend hosts OpenRouter itself can route this one model to. Live, not
 *  cached: unlike `models`, this is fetched fresh each time it is asked for,
 *  because the point is to see what is available right now to pin against. */
export interface ProviderBackendOption {
  /** The value `backend_pins` and OpenRouter's own `provider.only` expect. */
  tag: string
  provider_name: string
  context_length: number | null
  input_cost_per_mtok: number | null
  output_cost_per_mtok: number | null
  quantization: string | null
  /** Whether this is the backend `backend_pins` currently forces this model to. */
  pinned: boolean
}

export interface Provider {
  provider_id: string
  kind: ProviderKind
  display_name: string
  base_url: string
  /** an env var NAME, never key material. Displayed as a label, value is always *** */
  api_key_ref: string
  /** Whether `api_key_ref` resolves to anything, from the coordinator's own
   *  `key_status()`. `null` is "this port cannot say" -- a store with no
   *  key_status -- and must not be rendered as a missing key. `not_needed` is
   *  a kind that takes no credential, which is not the same as one whose
   *  credential is absent. */
  key_state: 'set' | 'missing' | 'not_needed' | null
  /** Which of the two places answered: an environment variable the process was
   *  started with, or secrets.json on the coordinator. A place, never a value,
   *  and there is no third field that could carry one. `null` whenever
   *  `key_state` is not `set` -- and for a port that knows the state without
   *  knowing the source. */
  key_source: 'environment' | 'secrets.json' | null
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
  /** Two counts, not one: the gateway sends `{input, output}` (providers/
   *  serialization.py). "Tokens generated" is the output half -- the same
   *  thing a local target's `counters.total_tokens` holds, which is completion
   *  tokens only (gateway/proxy.py `_apply_sniffed`). */
  tokens_today: { input: number; output: number } | null
  requests_today: number | null
  /** Requests served by a model this provider never published a price for --
   *  spend that is real but not accounted in `spend_today_usd`. */
  unpriced_requests_today: number | null
  /** Of `requests_today`, how many the provider priced itself, in its own
   *  response. The remainder was priced from our copy of its published rate
   *  card, which cannot see prompt caching or a long-context tier -- so the
   *  two are a charge and a forecast, and the Spend screen says which. */
  metered_requests_today: number | null
  /** Seconds until a rate-limit backoff clears; null when not backed off. */
  retry_in_s: number | null
  /** How many models this provider actually serves -- the length of `models`,
   *  which the allowlist has already filtered. */
  model_count: number | null
  /** How many it publishes. The gap between this and `model_count` is the
   *  allowlist, and one number cannot express "2 of 312". */
  catalogue_count: number | null
  /** Whether anybody ever chose which of them to serve.
   *
   *  Not derivable from the two counts above: they are equal both for a record
   *  written before the allowlist existed -- which keeps serving everything it
   *  publishes -- and for one whose operator switched everything on. Only one
   *  of those deserves to be told that nothing was ever chosen.
   *
   *  Three states, like `key_state`. `null` is "this port cannot say" -- the
   *  shipped stub does no accounting, so every field in this group comes back
   *  null -- and must render as no warning at all, never as "nobody chose". */
  models_chosen: boolean | null
}

/** `POST /api/providers` body (providers/service.py `_register`). Only `kind`
 *  is required; known kinds prefill everything else server-side. */
export interface ProviderSpec {
  kind: ProviderKind
  provider_id?: string
  base_url?: string
  /** the NAME of an environment variable or secrets.json key -- never a key */
  api_key_ref?: string
  /** the key itself. The one field in this API that carries key material, and
   *  it travels one way: the coordinator writes it to secrets.json at 0600 and
   *  persists only the reference. It is on no record and in no response. Sent
   *  alone it mints a reference; sent with `api_key_ref` it is stored under
   *  that name. */
  api_key?: string
  display_name?: string
  aliases?: Record<string, string>
  daily_budget_usd?: number | null
  enabled?: boolean
  priority?: number
  /** Upstream ids this provider is allowed to serve, sent as the complete set
   *  rather than a delta. PATCH only: on POST the catalogue has not been
   *  fetched yet, so there would be nothing to check an id against. Every id
   *  must be one the provider publishes or the whole patch is a 400. */
  enabled_models?: string[]
  /** Upstream id -> the backend tag OpenRouter should be forced to for it,
   *  sent as the complete map, like `aliases`: to clear one model's pin, send
   *  the map without that key rather than a null value. PATCH only, and only
   *  on a kind with `supports_backend_routing`. */
  backend_pins?: Record<string, string>
}

/** `PATCH /api/providers/{id}` body (providers/service.py `update`). */
export type ProviderPatch = Partial<
  Pick<
    ProviderSpec,
    | 'enabled'
    | 'priority'
    | 'display_name'
    | 'daily_budget_usd'
    | 'base_url'
    | 'api_key_ref'
    | 'api_key'
    | 'aliases'
    | 'enabled_models'
    | 'backend_pins'
  >
>

// ── Settings: GET/PATCH /api/settings ────────────────────────────────────────

export type SettingSource = 'file' | 'env' | 'default'

/** The four settings a human can change at runtime (gateway/settings_store.py
 *  `MUTABLE_FIELDS`) -- everything else in `GatewaySettings` is a code-level
 *  tunable, not a preference, and has no UI control. */
export interface Settings {
  electricity_rate_usd_per_kwh: number
  local_only: boolean
  /** null means unset -- never rendered as "no cap", which would claim
   *  containment that is not configured. */
  daily_spend_cap_usd: number | null
  /** Relaunch a deployment automatically after a crash (not an operator
   *  stop), up to a small bounded number of attempts. Defaults true. */
  auto_restart_crashed_deployments: boolean
  /** Where each value above came from, so "0.00 because nobody set it" and
   *  "0.00 because someone set it to zero" can render differently. */
  sources: Record<
    | 'electricity_rate_usd_per_kwh'
    | 'local_only'
    | 'daily_spend_cap_usd'
    | 'auto_restart_crashed_deployments',
    SettingSource
  >
  writable: string[]
  /** The honesty gate on the cap: false when no provider port reports spend,
   *  so there is nothing to enforce the cap against. The UI renders the cap
   *  control disabled, with this as the reason, rather than offering a
   *  control that silently fails open. */
  daily_spend_cap_enforceable: boolean
}

export type SettingsPatch = Partial<
  Pick<
    Settings,
    | 'electricity_rate_usd_per_kwh'
    | 'local_only'
    | 'daily_spend_cap_usd'
    | 'auto_restart_crashed_deployments'
  >
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

/** The same counters for a model a PROVIDER serves, off the same registry and
 *  the same window as a deployment's. Only targets the gateway has actually
 *  routed to appear: every model of an un-allowlisted key would be hundreds of
 *  rows a second, and a target with no counter has served nothing, which reads
 *  as the zero it is. */
export interface MetricsRemoteFrame {
  /** `<provider id>:<upstream id>`, the routing target id. */
  target_id: string
  provider_id: string
  served_name: string
  state: 'healthy' | 'unhealthy'
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
  /** null on the degraded path, absent on a coordinator older than this key.
   *  Both mean "no figure", which is why every reader goes through `?.`. */
  remotes?: MetricsRemoteFrame[] | null
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

/** The containers `POST /v1/audio/speech` will encode into.
 *
 *  libsndfile's list, which is the tts runtime's whole encoder
 *  (`control_plane/runtimes/tts.py::FORMATS`). `aac` is deliberately absent
 *  rather than offered and refused: libsndfile cannot write it, and a client
 *  that asked for AAC and got MP3 under `Content-Type: audio/aac` fails
 *  somewhere much further away than here. A provider that accepts `aac` will
 *  say so in its own words if somebody sends one by hand. */
export type SpeechFormat = 'mp3' | 'wav' | 'flac' | 'opus' | 'pcm'

export const SPEECH_FORMATS: readonly SpeechFormat[] = [
  'mp3',
  'wav',
  'flac',
  'opus',
  'pcm',
]

/** One `POST /v1/audio/speech`.
 *
 *  `voice` absent is a real request and the default one: the model speaks in
 *  its own voice. A named voice is a reference clip plus that clip's exact
 *  transcript, installed on the node, and an unknown name is refused with the
 *  list of what is there -- which is why `voices()` exists rather than a free
 *  text field. There is no `speed` and no `stream`: the server refuses the
 *  first (a resample moves the pitch) and has never had the second. */
export interface SpeechRequest {
  model: string
  input: string
  voice?: string
  response_format: SpeechFormat
}

/** What came back. The bytes, and the two things only the headers know.
 *
 *  `sampleRate` is not decoration: `pcm` is headerless, so nothing in the file
 *  itself says what rate to play it at, and `opus` was resampled from the
 *  codec's 44.1 kHz to 48 kHz on the way out. `null` when the upstream did not
 *  send the header -- a provider will not. */
export interface SpeechResult {
  blob: Blob
  contentType: string
  bytes: number
  durationS: number | null
  sampleRate: number | null
  /** The trace id the gateway minted, the same one the chat readout shows. */
  requestId: string | null
}

/** `GET /v1/audio/voices?model=`.
 *
 *  `skipped` is the server naming clips it declined and why -- a `.wav` with
 *  no `.txt` transcript beside it, or one over the reference limit. It is
 *  rendered verbatim, because it is the only thing that tells somebody how to
 *  make the voice they installed actually appear. */
export interface VoiceLibrary {
  voices: string[]
  skipped: string[]
}

/** One `POST /v1/audio/transcriptions`. Audio file in, text out.
 *
 *  The only request on the whole `/v1` surface that is not JSON: it is a
 *  multipart upload, and the file is forwarded byte for byte under the
 *  browser's own boundary.
 *
 *  `language` is an ISO-639-1 code and is not optional in practice on an
 *  English-only checkpoint. vLLM runs language auto-detection when the field
 *  is absent, and `whisper-*.en` has no language tokens to detect with, so it
 *  fails an assertion inside the model and returns 500. It is therefore a
 *  visible control defaulting to `en` rather than a parameter sent silently:
 *  a multilingual checkpoint is one field away, and an English-only one does
 *  not 500 on arrival. */
export interface TranscriptionRequest {
  model: string
  file: File
  language?: string
}

/** What came back. Text, and the id of the row that recorded it. */
export interface TranscriptionResult {
  text: string
  requestId: string | null
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
  /** What THIS row was judged at. Per row, not per report: on the default path
   *  nobody names a context, so the fit gate chooses one per model out of what
   *  fits, clamped to that model's own window. One number at the top of the
   *  report would describe whichever row happened to come first. */
  context: number
  max_seqs: number
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
  /** The context every row was taken at, or null when the gate chose one per
   *  model -- in which case `CapacityRow.context` is the number to read.
   *
   *  Null is the DEFAULT path, not an edge case, so reaching for this field
   *  first is the likely mistake rather than an unlikely one. Falling back to
   *  `report.context ?? 8192` would print a figure next to a row it was not
   *  computed for -- the same defect as a zero that reads as an observation
   *  of something nobody watched. Read the row. */
  context: number | null
  concurrency: number
  kv_dtype: string
  /** The machines the rows were sized on. One by default: the coordinator's
   *  own host. More only when `on=` named more. */
  nodes: string[]
  /** The tensor-parallel degrees actually used, ascending. `[1]` unless the
   *  answer was sized across several machines -- and the degree is what the
   *  MODEL admits, not how many boxes were ticked, so three machines can still
   *  come back as 2. */
  tensor_parallel: number[]
  /** Which memory the verdicts were budgeted against. `"host_memory"` means no
   *  enrolled machine has GPU-addressable memory and the figures come from the
   *  live host reading instead -- real numbers, about a machine that still
   *  cannot serve. Pair it with `local_serving`. */
  budget_basis: 'gpu' | 'host_memory'
  /** False when nothing here can be launched on these machines whatever the
   *  verdict says. A verdict you cannot act on must not grow a Serve button. */
  local_serving: boolean
  measured_at: number
  excluded: { node_id: string; reason: string }[]
  unresolved: { model_id: string; reason: string }[]
  unavailable_reason: string | null
  live: CapacitySide | null
  /** Null under `budget_basis: "host_memory"`: the static ceiling is derived
   *  from addressable memory, which is 0 on a machine with no GPU on purpose,
   *  so the whole side would read 0.0 GiB beside rows that say "fits". */
  static?: CapacitySide | null
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

/** One deployment of a model.
 *
 *  Plural on a row, because a repository can be served twice under two names
 *  and collapsing that to one scalar loses the second.
 *
 *  Declared here rather than in `tabs/models/rows.ts` because it is now a wire
 *  shape: `GET /api/models` emits exactly this. `rows.ts` re-exports it so
 *  every existing import keeps working. */
export interface RowDeployment {
  deployment_id: string
  served_name: string
  state: DeploymentState
  runtime: string
  node_ids: string[]
  last_error: string | null
}

/** One provider that serves this model. Facts read off the payload and
 *  nothing else: no key material, not `api_key_ref`, not anything derived
 *  from it. */
export interface RowProvider {
  provider_id: string
  display_name: string
  served_name: string
  context_length: number | null
  /** null is "never priced", which is a different fact from priced at zero.
   *  Never coerce it to 0 — the wire distinguishes them deliberately. */
  input_cost_per_mtok: number | null
  output_cost_per_mtok: number | null
  supports_tools: boolean
  supports_streaming: boolean
  healthy: boolean
  last_error: string | null
  admitting: boolean | null
  admission_block: string | null
  /** The id the allowlist is written in terms of. Optional because the
   *  legacy `/api/providers` join did not carry it and the registry does. */
  upstream_id?: string
  modality?: Modality
  /** Whether the PROVIDER is switched on, which is not the same question as
   *  whether this model is in its allowlist — the two are filtered in
   *  different places server side, so an allowlisted model on a disabled
   *  provider is listed while nothing routes to it. */
  provider_enabled?: boolean | null
}

/** A provider that publishes this model and is NOT serving it.
 *
 *  Deliberately its own type, and `ModelRow.offers` deliberately its own
 *  array, rather than an `enabled` flag on `RowProvider`. `providers` means
 *  "serves this today" and three things read it that way — the inspector's
 *  provider facts, `support.ts`, and `rows.check.mjs`. Widening it to mean
 *  "publishes this" would have changed what all three say without changing a
 *  type.
 *
 *  It carries no health and no admission: nothing is routing to it, so a
 *  health figure here would describe a path that does not exist. */
export interface RowOffer {
  provider_id: string
  display_name: string
  /** What it would answer to at `/v1` once switched on. Not in `servedNames`:
   *  the row does not answer to it yet, and a search that found it there would
   *  be claiming an endpoint that 404s. */
  served_name: string
  /** The id the allowlist is written in terms of. */
  upstream_id: string
  context_length: number | null
  input_cost_per_mtok: number | null
  output_cost_per_mtok: number | null
  supports_tools: boolean
  supports_streaming: boolean
  modality?: Modality
}

/** Where the registry found a model. The browser's `Facet` is this plus
 *  `'hub'`, which the server deliberately never emits: `/api/models/search`
 *  resolves nothing, so its hits are a query's answer rather than a fact
 *  about this cluster. */
export type ModelFacet = 'running' | 'ondisk' | 'catalog' | 'provider' | 'offered'

/** One row of `GET /api/models`.
 *
 *  Carries no fit answer. `verdict`, `reason`, `basis`, `predicted_decode_tps`,
 *  `headroom`, `total_params` and `dtype` are a function of (model, context,
 *  concurrency, node set) and stay on `/api/capacity`; a copy here would give
 *  the screen two sources of `verdict` that can disagree. */
export interface RegistryModel {
  model_id: string
  label: string
  detail: string
  default_context: number | null
  default_concurrency: number | null
  /** Deduplicated, in canonical order, never empty. */
  where: ModelFacet[]
  served_names: string[]
  deployments: RowDeployment[]
  providers: RowProvider[]
  offers: RowOffer[]
  cached_on: string[]
  /** The largest figure any node reports, never the sum: two node records can
   *  share one physical cache, and summing would double a download's size. */
  bytes_on_disk: number | null
}

/** How one contributing feed is doing.
 *
 *  Load-bearing rather than decorative. The Models tab's rule is that a feed
 *  failing greys nothing and empties nothing — it prints one line naming the
 *  feed and the server's own sentence. Folding five fetches into one leaves
 *  the browser with no failed request to notice, so the sentence travels
 *  here instead. */
export interface RegistrySource {
  ok: boolean
  reason: string | null
  observed_at?: number | null
  attempted_at?: number | null
  rows?: number | null
  /** Only on `cache`: which nodes could be read. A node that could not be
   *  read is not a node holding nothing. */
  nodes?: {
    node_id: string
    available: boolean
    reason: string | null
    observed_at: number | null
    attempted_at: number | null
  }[]
}

/** `GET /api/models` — every model this cluster knows about, from one place. */
export interface ModelRegistryResponse {
  models: RegistryModel[]
  sources: Record<string, RegistrySource>
  revision: number
  schema_version: number
  generated_at: number
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
  /** The runtime image's own version string, when a probe has run against
   *  it -- absent on a coordinator with no docker or no image pulled yet. */
  version: string | null
}

/** `GET /api/models/detail`. */
export interface ModelDetail {
  model_id: string
  revision: string | null
  model_type: string
  architectures: string[]
  /** Which endpoint family this model answers on, read off its architecture.
   *  Absent from a gateway that predates the field, and absent is `text` --
   *  what every model was before audio existed. It decides which runtime the
   *  Serve panel offers first and which route it says the model will answer
   *  on, because a speech model on `vllm` is refused, not merely slow. */
  modality?: Modality
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
  /** Can the nodes we actually have run this scheme?
   *
   *  `checked` counts only placement candidates. `skipped` names the machines
   *  that were not asked and why -- a node with no GPU answers every
   *  quantization question with a compute-capability complaint, which says
   *  nothing about the model. Not blockers: never render them as problems. */
  nodes: {
    ok: boolean
    problems: string[]
    checked: number
    skipped?: { node_id: string; reason: string }[]
  }
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
  /** What this row was judged at. Null when the fit gate could not judge it at
   *  all, which is the same condition that leaves `verdict` null. */
  context: number | null
  max_seqs: number | null
  /** The same row against the hardware's own ceiling instead of against what
   *  is free this second. Additive and NEVER governing: `verdict` and `fits`
   *  above are still the live answer, so nothing that reads them flips.
   *
   *  This pair exists because the table printed two different situations
   *  identically. "This machine cannot hold it" and "this machine is full
   *  right now" want different actions from whoever is reading -- buy a
   *  different box, or go and find what is resident -- and a single
   *  "will not fit" says neither.
   *
   *  Optional: an older coordinator omits them. Under `host_memory` basis
   *  there is no static side to report at all, because the static ceiling
   *  derives from addressable memory, which is 0 there on purpose. */
  static_verdict?: string | null
  static_fits?: boolean | null
  static_headroom?: number | null
  static_reason?: string
  static_predicted_decode_tps?: number | null
}

/** What a ladder's rows were sized against.
 *
 *  This exists because the answer used to be "one machine, and we are not
 *  going to tell you which" while the board above the ladder let several be
 *  ticked -- so the pane apologised in prose for numbers that were quietly
 *  about something else. The caption states this instead. */
export interface LadderBasis {
  /** The machines the rows were sized on -- the PLACEMENT, not everything that
   *  was ticked. A model that only splits two ways reports two of the three
   *  boxes somebody checked, because two is where the numbers came from. */
  nodes: string[]
  probed_node: string | null
  /** What the model legally admits over those machines, not how many were
   *  ticked. Tensor parallelism has to divide the KV heads. */
  tensor_parallel: number
  budget_basis: 'gpu' | 'host_memory'
  local_serving: boolean
  /** Whether the governing verdicts were taken against a live reading or
   *  against the static ceiling. False is not an error: `allocatable_map`
   *  legitimately answers with nothing when no node has a telemetry sample,
   *  and the fit gate then judges on the ceiling. The caption has to know,
   *  or it claims a measurement nobody took. */
  budget_is_live?: boolean
  /** The binding (smallest) figure across the placement, which is what the
   *  fit gate actually gates on. `allocatable_per_node` is null when no live
   *  reading governed; `usable_per_node` is null under `host_memory` basis,
   *  where a static ceiling would be 0 beside rows that say "fits". */
  allocatable_per_node?: number | null
  usable_per_node?: number | null
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
  sized_on: LadderBasis
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
/** `GET|POST /api/nodes/{id}/runtime`. A model runtime already listening on a
 *  node -- an Ollama the operator started there -- which the coordinator can
 *  adopt as a provider without anybody retyping its address.
 *
 *  `detected: null` is the normal answer and is NOT an error: most machines
 *  are not running a runtime, and a GPU node has no reason to. */
export interface NodeRuntime {
  node_id: string
  detected: DetectedRuntime | null
  /** Set only when this build could not look at all (a registry with no HTTP
   *  client). Distinct from `detected: null`, which means we looked and found
   *  nothing. */
  reason?: string | null
  /** The provider this runtime is already registered as, when it is. Non-null
   *  means there is nothing to adopt and the UI should say which provider it
   *  is rather than offer a button that would duplicate it. */
  provider_id?: string | null
}

export interface DetectedRuntime {
  kind: ProviderKind
  base_url: string
  /** How many models it is holding. `null` when it did not say. 0 is a real
   *  answer -- a fresh runtime with nothing pulled yet -- and reads
   *  differently from "it would not tell us". */
  model_count: number | null
  /** Every model on the runtime, with whether it is loaded right now. */
  models?: RuntimeModel[]
  /** Whether this build can load and unload here, as opposed to only observe.
   *  The buttons key off this, so a runtime we cannot drive never renders a
   *  control that would do nothing. */
  controllable?: boolean
}

export interface RuntimeModel {
  name: string
  /** Bytes on disk -- what the pull cost. */
  size: number | null
  /** Bytes in memory while loaded, `null` when it is not. On a CPU-only box
   *  this is the resident footprint rather than VRAM, which is honestly zero
   *  there. */
  resident_bytes: number | null
  /** `null` means the runtime would not say. Rendered as neither loaded nor
   *  unloaded, rather than guessing one. */
  resident: boolean | null
}

/** `POST /api/nodes/{id}/runtime`. `created: false` means it was already a
 *  provider, so clicking twice is one provider rather than two. */
export interface AdoptedRuntime {
  node_id: string
  provider_id: string
  created: boolean
}

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

/** `GET /api/shell/status`. Answers whether a terminal can be opened at all.
 *
 *  Deliberately says nothing about whether a key is configured. That is a fact
 *  about a secret, and this route is as unauthenticated as everything else on
 *  `/api` -- so reporting it would tell an anonymous caller how close they are
 *  without helping the operator, who can read the node's own log. */
export interface ShellStatus {
  enabled: boolean
  /** Present and non-empty when `enabled` is false: the sentence to render,
   *  naming the variable that turns it on. */
  reason: string
  /** Seconds of inactivity before a session closes itself. */
  idle_timeout_s: number
}

/** One transfer the coordinator is running, or has just finished.
 *
 *  These arrive from `GET /api/activity` while a pull is in flight. The
 *  provider streams `completed` alongside `total` the whole way down; before
 *  this existed the control plane read those frames and discarded them, so a
 *  download was one number reported once and then silence until the catalogue
 *  happened to refresh. */
export interface DownloadActivity {
  pull_id: string
  provider_id: string
  /** The provider's display name, already resolved server-side. */
  provider: string
  model: string
  completed: number
  /** null until the upstream reports one. Never 0 -- a download of unknown
   *  size and a download of no size are different answers, and only one of
   *  them can honestly draw a bar. */
  total: number | null
  /** The upstream's own word for what it is doing ("pulling manifest",
   *  "verifying sha256 digest"), passed through unchanged. */
  status: string
  error: string | null
  done: boolean
}

/** A model that is starting but not yet serving.
 *
 *  This used to say a launch carried no percentage and never would, because
 *  the runtime container is where the work happens and the control plane
 *  cannot see inside it. The second half was never true: `sparkrun logs`
 *  reaches that container, the manager reads it while the launch is in
 *  flight, and the runtime narrates itself in there -- including a checkpoint
 *  loader that counts its own shards. What survives from the old rule is the
 *  part that mattered: `fraction` is null unless something measured it, and
 *  the download, the compile and the graph capture all still report none. */
export interface LaunchActivity {
  deployment_id: string
  served_name: string
  model_id: string | null
  runtime: string
  state: string
  node_ids: string[]
  /** Which slow step it is on: 'preparing' | 'downloading' | 'loading' |
   *  'starting'. Null before anything has been read, and deliberately not
   *  narrowed to a union here -- the server owns this vocabulary
   *  (control_plane/deploy/progress.py) and a UI that fails to compile
   *  against a phase it has not heard of would be worse than one that shows
   *  the sentence and no tick. */
  phase: string | null
  /** The sentence sparkrun or the runtime printed about what it is doing
   *  ('Pulling image: ...', 'Loading safetensors checkpoint shards: 5/11'),
   *  passed through unchanged. '' when nothing has been said yet. */
  status: string
  /** 0..1 only while the runtime is counting its own checkpoint shards, null
   *  the rest of the time. Not interchangeable with 0. */
  fraction: number | null
  /** Seconds left, as the program doing the work estimated them about itself:
   *  the model downloader and the checkpoint loader are both tqdm bars and
   *  both print their own remaining time. Null for every step that counts
   *  nothing — the image pull, the compile, the graph capture — and never
   *  derived here from a rate. An estimate is planned around, so it may only
   *  come from the thing being estimated. */
  eta_s: number | null
  /** The runtime said it was dying, in `status`. The manager fails the launch
   *  on the same reading, so this is true only for the moment between the two
   *  — long enough that the row must not draw it as ordinary progress. */
  fatal: boolean
  /** When this coordinator first saw it launching -- not when the launch
   *  began. `Deployment.started_at` is null until the READY transition, so
   *  there is no launch timestamp to report and this does not claim to be one.
   *  After a restart it is when the coordinator came back. */
  since: number
  last_error: string | null
}

/** `GET /api/deployments/{id}/logs`. What the launcher and the backend said.
 *
 *  `source` is the cost of the answer, and the screen has to respect it:
 *  `buffer` is the coordinator's own memory of a launch it is streaming right
 *  now and may be polled; `read` is one bounded `sparkrun logs` for a
 *  deployment nothing is following any more, and must be asked for, not
 *  timed. `none` is a deployment with nothing to show, `unavailable` a
 *  control plane that cannot show one at all — different answers, because one
 *  of them is a backend that printed nothing. */
export interface DeploymentLogs {
  lines: string[]
  source: 'buffer' | 'read' | 'none' | 'unavailable'
  cluster_id?: string | null
}

export interface Activity {
  downloads: DownloadActivity[]
  launches: LaunchActivity[]
}

/** `GET /api/setup`. Whether this coordinator has been set up, and the one
 *  machine the first screen is about.
 *
 *  `completed` is derived on the server, not merely a stored flag: a cluster
 *  that is serving a model or has a provider configured is set up whatever the
 *  flag says, which is what stops a wiped data directory dropping a working
 *  cluster back into onboarding. `reason` is that derivation in words, and is
 *  the answer to "why am I not being offered setup".
 *
 *  `machine` is null on an ambiguous roster rather than a guess. The server
 *  will only name a machine it can identify -- an authoritative local node id,
 *  or a roster of exactly one -- because the first screen introduces this
 *  machine's hardware and naming the wrong one is a lie the reader cannot
 *  check. */
export interface SetupStatus {
  completed: boolean
  reason: string
  machine: NodeStateDTO | null
  cluster: { nodes: number; healthy: number }
  deployments: number
  providers: number
  /** False when no provider kind can be added and routed to. The cloud step is
   *  then not rendered at all -- not disabled, not explained. A step nobody can
   *  take is noise on the one screen where every word is read. */
  provider_routing: boolean
}
