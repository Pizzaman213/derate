// The only place the UI talks to the gateway. Live only: the coordinator is
// always there in the deployed shape (a Compose service, a systemd unit) and
// a UI that opens without one is not the shape this ships in. Day-0 fixture
// data lived here through bring-up; it is gone as of the derate port.

import { apiToken } from './apiToken'
import { apiUrl } from './origin'
import { scrub } from './redact'
import { createThinkSplitter } from './think'
import type { ThinkSplit } from './think'
import type {
  AlertsReport,
  Activity,
  AdminRow,
  AdminSession,
  AdoptedRuntime,
  ApiKeyRow,
  AuditPage,
  AuditQuery,
  AuthState,
  CacheClearResult,
  Candidate,
  CapacityReport,
  ProviderSignInFlow,
  ProviderSignInStart,
  ProviderSignInStatus,
  ChatMessage,
  ChatRequestOptions,
  ChatTurnMeta,
  Cluster,
  CuratedModel,
  DeploymentDTO,
  DeploymentLogs,
  Enrollment,
  EnrollmentRow,
  EnrollmentSpec,
  EventHistory,
  KeyCreate,
  KeyFilter,
  KeyPatch,
  RequestsPage,
  RequestsQuery,
  KillResult,
  LaunchRequest,
  LimitState,
  LinkMeasurement,
  LocalPrice,
  LogHistory,
  MemoryReportList,
  MetricsFrame,
  MintedKey,
  Modality,
  ModelDeleteResult,
  ModelDetail,
  ModelRegistryResponse,
  ModelSearchResponse,
  NodeHealth,
  NodeHistory,
  NodeLogTail,
  NodeProcessList,
  NodeProfile,
  NodeRuntime,
  NodeStateDTO,
  OrgRow,
  PlanRequest,
  PlanResponse,
  Provider,
  ProviderBackendOption,
  ProviderCatalogueModel,
  ProviderKindSpec,
  ProviderPatch,
  ProviderSpec,
  PullAccepted,
  QuantTable,
  ReachReport,
  RequestHistory,
  RouteLimits,
  RoutingConfig,
  RoutingPolicy,
  ServedModel,
  Settings,
  SettingsPatch,
  SetupAdminResult,
  SetupStatus,
  ShellStatus,
  SpeechRequest,
  SpeechResult,
  StorageReport,
  TargetKind,
  TelemetryEstate,
  Topology,
  TranscriptionRequest,
  TranscriptionResult,
  UsageQuery,
  UsageResult,
  VariantLadder,
  VoiceLibrary,
  SpeculativeHeads,
  WiringRequest,
  WiringResponse,
} from './types'

/** One turn's worth of arguments for `chatStream`.
 *
 *  `onDelta` is called per answer fragment as it arrives and `onReasoning` per
 *  reasoning fragment, whether the server separated it or the browser split it
 *  out of inline `<think>` tags; `onRewrite` replaces both wholesale when the
 *  browser's split turns out to have been wrong (see `api/think.ts`), and a
 *  caller without it keeps the text as first routed. `onOpen` fires once the
 *  response headers are in, which is the earliest the request id exists and is
 *  therefore the only way a caller learns it for a turn that goes on to fail.
 *  `options` is spread into the request body as extra top-level fields.
 *  `thinkOpen`: the chat template is expected to open the `<think>` block in
 *  the prompt, so the answer starts inside it. */
export interface ChatStreamRequest {
  model: string
  messages: ChatMessage[]
  onDelta: (text: string) => void
  onReasoning?: (text: string) => void
  onRewrite?: (reasoning: string, content: string) => void
  onOpen?: (requestId: string | null) => void
  options?: ChatRequestOptions
  thinkOpen?: boolean
  signal: AbortSignal
  temperature?: number
  max_tokens?: number
  stop?: string[]
}

export interface Backend {
  cluster(): Promise<Cluster>
  topology(): Promise<Topology>
  deployments(): Promise<DeploymentDTO[]>
  candidates(): Promise<Candidate[]>
  routing(): Promise<RoutingConfig[]>
  providers(): Promise<Provider[]>
  /** `GET /api/setup`: has anyone set this cluster up, and what machine is it
   *  running on. The first call a fresh install makes. */
  setup(): Promise<SetupStatus>
  /** Records that the wizard was finished, so it is not offered again --
   *  including when the person chose to add nothing, which is still an answer. */
  completeSetup(): Promise<void>
  /** The models this session can chat with: `GET /api/chat/models`, the
   *  session-gated mirror of `/v1/models` (which answers only to an API key).
   *  Local deployments and remote providers alike. */
  models(): Promise<ServedModel[]>
  /** `POST /api/chat/completions` with `stream: true` -- the session-gated
   *  twin of `/v1/chat/completions`, attributed to this admin's own key.
   *  Resolves when the stream ends -- including when it ended because the
   *  caller aborted, which is an outcome rather than an error and leaves the
   *  partial text standing. */
  chatStream(req: ChatStreamRequest): Promise<ChatTurnMeta>
  /** `POST /api/chat/speech` (= `/v1/audio/speech`). One request, one audio
   *  file, no stream. */
  speech(req: SpeechRequest, signal?: AbortSignal): Promise<SpeechResult>
  /** `GET /api/chat/voices?model=` (= `/v1/audio/voices`). The reference clips installed beside this
   *  deployment, and the ones it declined to offer. */
  voices(model: string): Promise<VoiceLibrary>
  /** `POST /api/chat/transcriptions` (= `/v1/audio/transcriptions`). An audio file in, its text out. */
  transcribe(req: TranscriptionRequest, signal?: AbortSignal): Promise<TranscriptionResult>
  plan(req: PlanRequest): Promise<PlanResponse>
  /** Refused by the fit gate when the verdict is WONT_FIT. Nothing starts. */
  launch(req: LaunchRequest): Promise<DeploymentDTO>
  /** `PUT /api/routing/{name}`. `limits` replaces the name's stored limits
   *  (`{}` deletes them); omitted, they are left as they are. */
  setPolicy(
    servedName: string,
    policy: RoutingPolicy,
    opts?: { limits?: RouteLimits },
  ): Promise<RoutingConfig>
  /** `DELETE /api/routing/{name}`: back to what auto_policy picks. */
  clearPolicy(servedName: string): Promise<RoutingConfig>
  /** `POST /api/wiring`: a batch of provider and routing changes, validated
   *  whole. A refused batch is a 400 `wiring_invalid` thrown as `ApiError`
   *  whose `body` carries the per-op `results`. */
  wiring(body: WiringRequest): Promise<WiringResponse>
  admit(nodeId: string): Promise<void>
  /** What model runtime is listening on this node, if any. Cheap enough to
   *  call when a node sheet opens; the probe is bounded and a miss is normal. */
  nodeRuntime(nodeId: string): Promise<NodeRuntime>
  /** Adopt that runtime as a provider. Takes no body: the coordinator already
   *  knows the address it probed, and accepting one from the browser would let
   *  a click register a target pointing somewhere else. Idempotent. */
  adoptNodeRuntime(nodeId: string): Promise<AdoptedRuntime>
  /** Load a model into memory on that node's runtime, or evict it. Not a
   *  cluster launch: nothing is placed and no rank is assigned. */
  setRuntimeModel(
    nodeId: string,
    model: string,
    resident: boolean,
  ): Promise<{ done_reason: string | null }>
  /** Mint a short-lived token for one install. The response is the only
   *  time the secret is returned; it lives in `command` and nowhere else. */
  mintEnrollment(spec?: EnrollmentSpec): Promise<Enrollment>
  /** Live tokens, without their secrets. Expired ones are already gone. */
  enrollments(): Promise<EnrollmentRow[]>
  revokeEnrollment(tokenId: string): Promise<void>
  removeNode(nodeId: string): Promise<void>
  /** Replace the cluster token. Needs the signed-in admin's password; 409
   *  names any member that still depends on the old token. The new token is
   *  never returned: members hold their own, new nodes join on enrollment. */
  rotateClusterToken(currentPassword: string): Promise<{ rotated: boolean }>
  measureLink(a: string, b: string): Promise<LinkMeasurement | void>
  /** Can these two nodes reach each other, and from which side? Four health
   *  checks, seconds not a minute, and safe to run against a cluster that is
   *  serving — deliberately not a mode of `measureLink`, which saturates the
   *  interconnect. */
  checkReach(a: string, b: string): Promise<ReachReport>
  /** Start a calibration for a pair and RETURN -- the work outlives the
   *  request. It runs one two-rank collective per candidate setting, so it is
   *  minutes; holding a fetch open that long would tie up a worker, trip any
   *  proxy in front of this, and say nothing while it waited. Watch
   *  `measuring` on the links poll instead, which also keeps the state right
   *  across a reload and across two people looking at once. */
  tuneLink(a: string, b: string): Promise<{ a: string; b: string; measuring: boolean }>
  /** Give a node a display name, or clear it with an empty string. Changes
   *  the caption and nothing else: `node_id` stays what it was, so every
   *  deployment, link and routing target keyed by it still resolves. */
  renameNode(nodeId: string, label: string): Promise<{ node_id: string; label: string | null }>
  /** `DELETE /api/deployments/{id}`. Drains first: the deployment stops
   *  admitting immediately and in-flight requests finish. */
  stopDeployment(deploymentId: string): Promise<void>
  /** `PATCH /api/deployments/{id}`. Offers the deployment on the API, or
   *  stops offering it. The container keeps running and keeps holding its
   *  GPU memory either way — `stopDeployment` is what frees it. */
  setDeploymentServing(deploymentId: string, serving: boolean): Promise<DeploymentDTO>
  /** What the launcher and the backend have said, for the sheet that shows
   *  it. Cheap while a launch is in flight — those lines are already in the
   *  coordinator's memory — and one bounded `sparkrun logs` afterwards, which
   *  is why the reply says which of the two answered. Poll only the first. */
  deploymentLogs(deploymentId: string, tail?: number): Promise<DeploymentLogs>
  /** What is holding GPU memory on a node right now. Read on demand — this
   *  costs an nvidia-smi call on the node, so it is only polled while a node
   *  sheet is open. */
  nodeProcesses(nodeId: string): Promise<NodeProcessList>
  /** `GET /api/nodes/{id}/logs`. A tail of that node's own `node.log` or
   *  `proxy.log` -- the control plane's process log, not a deployment's
   *  serving log. Read on demand; there is no "is this actively streaming"
   *  signal to poll against, so a caller refreshes by hand. */
  nodeLogTail(nodeId: string, which: 'node' | 'proxy', tail?: number): Promise<NodeLogTail>
  /** `DELETE /api/nodes/{id}/processes/{pid}`. SIGTERM, then SIGKILL after a
   *  grace period, and the result reports which one it took and how much
   *  memory actually came back. Refused with 409 for a process the control
   *  plane launched — stopping that deployment is the correct verb, and it
   *  drains first. */
  killProcess(nodeId: string, pid: number): Promise<KillResult>
  /** Disk across the cluster, and what this product is spending it on. Read
   *  on demand — disk is deliberately not sampled anywhere, so there is no
   *  history of it and every number is as of `measured_at`. Fans out to every
   *  node agent, so it is polled slowly. */
  storage(): Promise<StorageReport>
  /** `DELETE /api/storage/cache/resolver`. Drops every cached model
   *  resolution; the next resolve pays one hub round trip. */
  clearResolverCache(): Promise<CacheClearResult>
  /** Delete one downloaded repository from one node. Refused with 409 while a
   *  non-terminal deployment is serving it — stopping that deployment is the
   *  correct verb, and it drains first. */
  deleteCachedModel(nodeId: string, folder: string): Promise<ModelDeleteResult>
  /** Every node's live memory picture. One poll for the whole app, so the
   *  planner, the node rails and Headroom cannot disagree about a number
   *  they all show. */
  memory(): Promise<MemoryReportList>
  /** The largest model that runs right now, and the same question against
   *  the idle-hardware ceiling. The fit gate answers; nothing is computed
   *  in the browser.
   *
   *  Every argument is nullable and null is not "use the default" -- it is a
   *  question. A null context asks the coordinator to choose one per model
   *  from what actually fits, clamped to each model's own window; that is the
   *  default path, and it is why this screen bands every row on a fresh
   *  install with nothing configured. Null machines means the coordinator's
   *  own host. Sending 8192 instead would answer a narrower question and print
   *  it as though somebody had asked it. */
  capacity(
    context: number | null,
    concurrency: number | null,
    nodeIds?: string[] | null,
  ): Promise<CapacityReport>
  /** The same walk, for a named set instead of the curated shortlist.
   *
   *  One request rather than one per model: ten single-model calls are ten hub
   *  resolves the server can neither batch nor memoise together, and the whole
   *  point is to answer "can I run the things I just found" without a round
   *  trip per row. The report comes back in exactly `capacity()`'s shape, so
   *  the two fold into one `capacityIndex`. */
  capacityFor(
    modelIds: string[],
    context: number | null,
    concurrency: number | null,
    nodeIds?: string[] | null,
  ): Promise<CapacityReport>
  /** The contract's own quantization table. Fetched, never re-typed here: a
   *  second copy of these byte figures is a second answer, and the one that
   *  disagrees with the fit gate is the one that gets somebody an OOM. */
  quantTable(): Promise<QuantTable>
  /** The curated shortlist, server side, so the picker and the capacity answer
   *  cannot drift apart. */
  catalog(): Promise<CuratedModel[]>

  /** Every model this cluster knows about, from one place: running, on disk,
   *  curated, served by a provider, published by one.
   *
   *  Deliberately carries NO fit answer -- that is a function of (model,
   *  context, concurrency, nodes) and stays on `/api/capacity` -- and no hub
   *  search hits, which stay on `/api/models/search` because that endpoint
   *  resolves nothing and its answers are a query's, not a fact about this
   *  cluster. `models()` is `/v1/models`, which is a different question
   *  again: what the gateway will accept as a `model` right now. */
  modelRegistry(): Promise<ModelRegistryResponse>
  /** Three sources at once, none resolved. Degrades to the local two when
   *  the hub is unreachable rather than answering empty. */
  searchModels(q: string, limit?: number): Promise<ModelSearchResponse>
  /** Draft heads published for one model, priced and ranked. Launches nothing.
   *  Called on demand — never on the plan path, which fires per keystroke. */
  /** Every way this model can speculate, ranked, plus the one to offer.
   *
   *  `refresh` goes past the stored scan and asks the hub again. Without it
   *  a model is scanned once ever and the answer is instant afterwards. */
  speculativeHeads(
    modelId: string,
    opts?: { limit?: number; refresh?: boolean },
  ): Promise<SpeculativeHeads>
  modelDetail(modelId: string): Promise<ModelDetail>
  /** Every obtainable quantization, with this cluster's verdict on each.
   *
   *  `nodeIds` is the machines the board above the ladder has ticked, and it
   *  is load-bearing rather than decorative: without it the server sized every
   *  row on one machine while the Serve button launched onto several, and the
   *  pane apologised for the gap in prose. The response's `sized_on` says what
   *  the rows were actually sized against, which is what the caption states. */
  modelVariants(
    modelId: string,
    opts?: {
      context?: number | null
      concurrency?: number | null
      nodeIds?: string[] | null
    },
  ): Promise<VariantLadder>
  /** Node samples over a window: 1 Hz raw rows, or 1-minute/1-hour rollups
   *  when the window is too wide for raw. Answers from the registry's
   *  five-minute in-RAM ring when no archive exists, and says so with
   *  `resolution: 'ring'` and `durable: false`; 503s with a sentence when
   *  neither is available. */
  historyNodes(opts?: HistoryQuery & { nodeId?: string; step?: string }): Promise<NodeHistory>
  /** Request attempts over a window, raw or rolled. Rolled buckets carry the
   *  only real percentiles in the product -- everything on the live frame is
   *  an exponential moving average. Filters by served name or target, never
   *  by node: a raw row carries `node_id` and the caller filters. */
  historyRequests(
    opts?: HistoryQuery & { servedName?: string; targetId?: string; step?: string },
  ): Promise<RequestHistory>
  /** Lifecycle events the bus emitted, as recorded rather than as broadcast. */
  historyEvents(
    opts?: HistoryQuery & { nodeId?: string; deploymentId?: string; type?: string; source?: string },
  ): Promise<EventHistory>
  /** Log records at a level and worse, already redacted by the handler that
   *  shipped them. */
  /** `exclude` is a comma-separated list of logger prefixes to drop. Omitted,
   *  the server applies its own -- the same list the journal handler declines
   *  to record -- so a window from before that change reads like one from
   *  after it. Pass an empty string to see everything the archive still holds. */
  historyLogs(
    opts?: HistoryQuery & {
      nodeId?: string
      level?: string
      logger?: string
      q?: string
      exclude?: string
    },
  ): Promise<LogHistory>
  /** Whether anything is being kept at all, and how much. Same shape as
   *  `StorageReport.telemetry`. */
  historyStatus(): Promise<TelemetryEstate>
  /** Whether a terminal can be opened, and if not, the sentence saying why.
   *  Always answers, including when the shell is off -- that is the whole
   *  point, so the page can explain rather than offer a button that fails.
   *  It never reports whether a key is configured: that is information about
   *  a secret, offered to an unauthenticated caller, for no benefit to a
   *  legitimate one. */
  shellStatus(): Promise<ShellStatus>
  getSettings(): Promise<Settings>
  /** 501, with a message naming why, when the patch includes a daily spend
   *  cap and no provider port can be measured against. */
  patchSettings(patch: SettingsPatch): Promise<Settings>
  providerKinds(): Promise<ProviderKindSpec[]>
  /** Reference *names* already in secrets.json. Names only -- the values stay
   *  on the coordinator -- so the reference field can offer what exists. */
  providerSecretRefs(): Promise<string[]>
  pullToProvider(
    providerId: string,
    body: { model: string; allow_over_memory?: boolean },
  ): Promise<PullAccepted>
  /** Transfers in flight and models still starting. Polled fast, because it
   *  reads a process-local dict and the in-memory deployment list -- no
   *  fan-out to the node agents, unlike `storage()`. */
  activity(): Promise<Activity>
  alerts(): Promise<AlertsReport>
  addProvider(spec: ProviderSpec): Promise<Provider>
  /** Whether remote authorization was revoked. Claude returns false (local
   *  removal only); key-based providers return null. */
  removeProvider(providerId: string): Promise<{ removed: string; revoked: boolean | null }>
  /** Start or reconnect using the selected flow; ordinary redacted responses. */
  startProviderSignIn(flow: ProviderSignInFlow, providerId?: string): Promise<ProviderSignInStart>
  /** Finish with the displayed code or callback URL. */
  completeProviderSignIn(flow: ProviderSignInFlow, attemptId: string, callbackUrl: string): Promise<Provider>
  /** A 404 means the attempt expired or was never started. */
  providerSignInStatus(flow: ProviderSignInFlow, attemptId: string): Promise<ProviderSignInStatus>
  patchProvider(providerId: string, patch: ProviderPatch): Promise<Provider>
  /** The provider's whole catalogue, each row saying whether it is switched
   *  on. Every other provider read in this client is already filtered to the
   *  enabled models; this is the one that is not. */
  providerModels(providerId: string): Promise<ProviderCatalogueModel[]>
  /** One model's backend hosts, live from OpenRouter's own endpoints call --
   *  not cached, unlike everything else this client reads. 400 for a kind
   *  without `supports_backend_routing`. */
  providerBackends(providerId: string, upstreamId: string): Promise<ProviderBackendOption[]>
  refreshProvider(providerId: string): Promise<Provider>
  /** Ask the upstream whether the provider's key works right now. */
  checkProviderKey(providerId: string): Promise<Provider>
  /** Returns an unsubscribe. `onState` reports stream health so the UI can grey
   *  live values during a gap instead of freezing or zeroing them. */
  subscribe(
    onFrame: (f: MetricsFrame) => void,
    onState: (s: StreamState) => void,
  ): () => void

  // ── Auth: admins and the session ──────────────────────────────────────────
  /** Open: whether an admin exists and whether this browser has a session. */
  authState(): Promise<AuthState>
  login(username: string, password: string): Promise<AuthState>
  logout(): Promise<void>
  me(): Promise<AdminRow>
  changePassword(oldPassword: string, newPassword: string): Promise<void>
  admins(): Promise<AdminRow[]>
  /** `currentPassword` is the signed-in admin's own: the server re-checks it
   *  for every admin-level change (401 `invalid_credentials` otherwise). */
  addAdmin(username: string, email: string, password: string, currentPassword: string): Promise<AdminRow>
  removeAdmin(username: string, currentPassword: string): Promise<void>
  /** Every live session of every admin: where it signed in and was last seen. */
  sessions(): Promise<AdminSession[]>
  /** Sign `username` out everywhere. Any admin may do this to any admin. */
  revokeAdminSessions(username: string): Promise<{ username: string; revoked: number }>
  /** The first admin. Answers 410 once one exists. Reveals the default key. */
  setupAdmin(username: string, email: string, password: string): Promise<SetupAdminResult>

  // ── API keys ──────────────────────────────────────────────────────────────
  keys(filter?: KeyFilter): Promise<ApiKeyRow[]>
  key(keyId: string): Promise<ApiKeyRow>
  /** Reveals the new key's secret, once. */
  createKey(spec: KeyCreate): Promise<MintedKey>
  patchKey(keyId: string, patch: KeyPatch): Promise<ApiKeyRow>
  deleteKey(keyId: string): Promise<void>
  disableKey(keyId: string, reason?: string): Promise<ApiKeyRow>
  orgs(): Promise<OrgRow[]>
  /** Disable every active key of the org, admin keys included. */
  disableOrgKeys(orgId: string, reason?: string): Promise<{ disabled: string[] }>
  enableKey(keyId: string): Promise<ApiKeyRow>
  /** Replaces the secret and reveals the new one, once. The old one stops
   *  working immediately. An admin key also needs the signed-in admin's
   *  `currentPassword`; other keys ignore it. */
  rotateKey(keyId: string, currentPassword?: string): Promise<MintedKey>
  /** This cycle only: the override is gone when the cycle ends. */
  raiseLimit(keyId: string, limitType: string, value: number): Promise<LimitState>
  resetCycle(keyId: string): Promise<void>
  keyRequests(keyId: string, q?: RequestsQuery): Promise<RequestsPage>
  /** The CSV export of the same query, as a URL a link can point at. */
  keyRequestsCsvUrl(keyId: string, q?: RequestsQuery): string
  /** One row per client request, cluster-wide: the final attempt, with
   *  `attempts` the true count. `retried` keeps only the ones that failed over. */
  requests(q?: RequestsQuery): Promise<RequestsPage>
  requestsCsvUrl(q?: RequestsQuery): string
  usage(q: UsageQuery): Promise<UsageResult>
  usageCsvUrl(q: UsageQuery): string
  audit(q?: AuditQuery): Promise<AuditPage>
  pricing(): Promise<LocalPrice[]>
  setPricing(model: string, inputUsdPerMtok: number | null, outputUsdPerMtok: number | null): Promise<LocalPrice>
  clearPricing(model: string): Promise<void>
}

/** The window every history route shares.
 *
 *  `from`/`to` are strings, not numbers, because `telemetry/query.py`'s
 *  `resolve_window` accepts a relative form (`-1h`, `-7d`) as well as an
 *  epoch. Sending `-1h` lets the server pick both boundaries off its own
 *  clock, which removes an argument about skew that a browser cannot win. */
export interface HistoryQuery {
  from?: string
  to?: string
  limit?: number
}

/** Builds a history URL, omitting every parameter the caller did not set.
 *
 *  Omitting matters rather than being tidy: each of these routes treats an
 *  empty string as "no filter" and an absent `from` as "the last hour", so a
 *  `?node_id=&step=` sent for an unset option is the same request -- but a
 *  `?limit=` is not, and neither is a `from` some caller stringified from
 *  `undefined`. One builder, and none of them can drift. */
function historyPath(
  base: string,
  window: HistoryQuery | undefined,
  extra: Record<string, string | number | undefined>,
  /** Keys whose empty string is a VALUE, not an omission. Only `exclude` so
   *  far: absent means "apply the server's own quiet-logger list", and present
   *  but empty means "apply none of it, show me everything the archive still
   *  holds". Dropping it with the other blanks would silently turn the second
   *  into the first, which is the one answer it must never give. */
  meaningfulWhenEmpty: readonly string[] = [],
): string {
  const params = new URLSearchParams()
  // `from` is the query key; the handler's parameter is `from_` with an alias.
  for (const [key, value] of Object.entries({
    from: window?.from,
    to: window?.to,
    limit: window?.limit,
    ...extra,
  })) {
    if (value == null) continue
    if (value === '' && !meaningfulWhenEmpty.includes(key)) continue
    params.set(key, String(value))
  }
  const qs = params.toString()
  return qs ? `${base}?${qs}` : base
}

export type StreamState =
  | { status: 'connecting' }
  | { status: 'open' }
  | { status: 'stale'; since: number; retryInMs: number; attempt: number }

// ── HTTP backend ─────────────────────────────────────────────────────────────

/** A numeric response header, or null.
 *
 *  Null rather than 0 for an absent or unparseable one: a provider sends
 *  neither of the two this reads, and `0 Hz` on screen would be a claim about
 *  the audio rather than a gap in what came back. */
function numberHeader(res: Response, name: string): number | null {
  const raw = res.headers.get(name)
  if (raw === null) return null
  const n = Number(raw)
  return Number.isFinite(n) ? n : null
}

/** Fired on any 401 from the coordinator. AppShell listens and sends the
 *  browser to /login: a session that expired mid-visit is the common case, and
 *  a screen of failed panels is a worse answer to it than the login form. */
export const UNAUTHENTICATED_EVENT = 'derate:unauthenticated'

function noteUnauthenticated(res: Response): void {
  if (res.status === 401 && typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent(UNAUTHENTICATED_EVENT))
  }
}

async function fetchOk(path: string, init?: RequestInit): Promise<Response> {
  // Every path in this file is written as the contract spells it -- relative,
  // rooted at /api or /v1 -- and `apiUrl` is the one place a configured
  // coordinator base is prepended. Same origin (the deployed shape, and the
  // default) leaves the path untouched.
  const url = apiUrl(path)
  const token = apiToken()
  const res = await fetch(url, {
    ...init,
    headers: {
      'content-type': 'application/json',
      // A no-op header on every deployment that never set DERATE_API_TOKEN,
      // which is most of them -- see api/apiToken.ts.
      ...(token ? { authorization: `Bearer ${token}` } : {}),
      ...(init?.headers ?? {}),
    },
  })
  if (!res.ok) {
    noteUnauthenticated(res)
    const body = await res.text().catch(() => '')
    // The resolved URL, not the contract path: when a base is set, which
    // coordinator refused is half the message.
    throw new ApiError(res.status, body || res.statusText, url, res.headers)
  }
  return res
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetchOk(path, init)
  if (res.status === 204) return undefined as T
  return scrub((await res.json()) as T)
}

/** `req` without `scrub()`. Used by exactly three calls -- `setupAdmin`,
 *  `createKey`, `rotateKey` -- because their whole answer is a `drt_` secret
 *  shown once (redact.ts names the exception). Nothing else may use it. */
async function reqUnscrubbed<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetchOk(path, init)
  return (await res.json()) as T
}

function qs(
  params: Record<string, string | number | boolean | string[] | null | undefined>,
): string {
  const parts: string[] = []
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null) continue
    const value = Array.isArray(v) ? v.join(',') : String(v)
    if (value === '') continue
    parts.push(`${k}=${encodeURIComponent(value)}`)
  }
  return parts.length ? `?${parts.join('&')}` : ''
}

/** `retried` is a flag on the wire: present as 1, or absent. `false` must not
 *  serialize as the string "false", which the server would read as unset only
 *  by luck. */
function requestParams(q: RequestsQuery) {
  return { ...q, retried: q.retried ? 1 : undefined }
}

const post = (body?: unknown): RequestInit => ({
  method: 'POST',
  ...(body === undefined ? {} : { body: JSON.stringify(body) }),
})

const seg = encodeURIComponent

/** OpenAI-shaped error bodies (gateway/errors.py): `{error:{message,...}}`.
 *  When the body parses that way, `.message` is the sentence the gateway
 *  actually wrote -- e.g. the 501 explaining why a daily spend cap cannot be
 *  enforced -- rather than the raw JSON blob. `.body` keeps the untouched text
 *  for a caller that wants more than the message. */
/** A path plus however many query parts survived. Zero is a real answer here:
 *  `/api/capacity` with nothing named is the default request, and appending a
 *  bare "?" would be a second spelling of it. */
function query(path: string, parts: string[]): string {
  return parts.length ? `${path}?${parts.join('&')}` : path
}

/** The fit question as query parts: what context, how many sequences, which
 *  machines. Every one of them is OMITTED when it is null.
 *
 *  Omission is the load-bearing behaviour, not a saving. A missing `context=`
 *  asks the coordinator to choose one per model out of what actually fits,
 *  clamped to the model's own window; a missing `on=` means its own host. That
 *  is the default path for the whole models screen, and it is the reason there
 *  is no field anywhere asking for either number before a model is chosen.
 *  Sending `context=8192` when nobody asked for 8192 would answer a narrower
 *  question and then print the answer as if somebody had asked it. */
function fitQuestion(
  context?: number | null,
  concurrency?: number | null,
  nodeIds?: string[] | null,
): string[] {
  const parts: string[] = []
  if (context != null) parts.push(`context=${encodeURIComponent(context)}`)
  if (concurrency != null) parts.push(`concurrency=${encodeURIComponent(concurrency)}`)
  if (nodeIds && nodeIds.length) {
    parts.push(`on=${nodeIds.map(encodeURIComponent).join(',')}`)
  }
  return parts
}

function errorMessage(status: number, path: string, body: string): string {
  try {
    const parsed = JSON.parse(body) as { error?: { message?: unknown } }
    if (typeof parsed.error?.message === 'string') return parsed.error.message
  } catch {
    // Not a JSON error envelope. Fall through to the raw form below.
  }
  return `${status} on ${path}: ${body}`
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly body: string,
    readonly path: string,
    /** The refusal's headers -- `Retry-After` on a locked login, the
     *  `X-RateLimit-*` trio on a 429. Null where the caller had none. */
    readonly headers: Headers | null = null,
  ) {
    super(errorMessage(status, path, body))
  }
}

// ── Wire adaptation ──────────────────────────────────────────────────────────
//
// §4.8 is owned by the gateway and says the UI codes against exactly that surface,
// so where the wire and the view models differ the adaptation happens here.
// This file is the only place that knows the wire format; everything above
// `Backend` sees the shapes in `types.ts`.

/** `GET /api/cluster` as the gateway emits it: identity at the top level, a
 *  counters-only `summary`, flat node rows, and no deployments — those come
 *  from `/api/deployments`. */
interface ClusterWire {
  cluster_id: string
  coordinator: string | null
  nodes: NodeWire[]
  links: LinkMeasurement[]
  summary: { node_count: number; healthy_nodes: number }
}

interface NodeWire {
  node_id: string
  label?: string | null
  hostname: string
  address: string
  device_class: NodeProfile['device_class']
  gpu_name: string
  gpu_count: number
  total_memory: number
  addressable_memory: number
  memory_bandwidth_gbps: number
  compute_capability: string
  driver_version: string
  healthy: boolean
  last_seen: number
  sample_ts?: number | null
  memory_used: number
  /** Live denominator for `memory_used`, from the node's own sample. 0 until
   *  one arrives, and 0 forever on a node whose telemetry never reports one. */
  memory_total?: number
  power_w: number | null
  temp_c: number | null
  util_pct: number | null
  last_error?: string | null
  eligible?: boolean
  ineligible_reason?: string | null
}

/** The gateway reports node health as `healthy | unhealthy`; the UI's three-way
 *  `NodeHealth` reserves `degraded` for a node that is up but impaired, which
 *  the wire cannot express. An unhealthy node is one we cannot reach. */
function nodeHealth(state: string | undefined, healthy: boolean): NodeHealth {
  if (state === 'degraded' || state === 'unreachable' || state === 'healthy') return state
  return healthy ? 'healthy' : 'unreachable'
}

/** Exported for `tabs/models/rows.check.mjs`, which drives `board.ts` with real
 *  `/api/cluster` payloads and has to hand it the same shape the app does. A
 *  second copy of this mapping written inside the verifier would agree with any
 *  bug that came from the same reading of the schema. */
export function toNodeState(n: NodeWire, coordinator: string | null): NodeStateDTO {
  return {
    profile: {
      node_id: n.node_id,
      hostname: n.hostname,
      address: n.address,
      device_class: n.device_class,
      gpu_name: n.gpu_name,
      gpu_count: n.gpu_count,
      total_memory: n.total_memory,
      addressable_memory: n.addressable_memory,
      memory_bandwidth_gbps: n.memory_bandwidth_gbps,
      compute_capability: n.compute_capability,
      driver_version: n.driver_version,
    },
    label: n.label ?? null,
    healthy: n.healthy,
    state: nodeHealth(undefined, n.healthy),
    role: n.node_id === coordinator ? 'coordinator' : 'worker',
    last_seen: n.last_seen,
    // When the readings below were measured, which is not when the node was
    // last reachable. Null means never sampled, and `nodeLive` greys the
    // readings rather than presenting a frozen one as current.
    sample_ts: n.sample_ts ?? null,
    memory_used: n.memory_used,
    memory_total: n.memory_total ?? 0,
    // A missing reading stays missing. Defaulting to 0 would draw a live-
    // looking zero for a node that has simply never reported telemetry.
    power_watts: n.power_w,
    temperature_c: n.temp_c,
    utilization_pct: n.util_pct,
    last_error: n.last_error ?? null,
    eligible: n.eligible,
    ineligible_reason: n.ineligible_reason ?? null,
  }
}

/** `GET /v1/models`. Standard OpenAI envelope; the three extras after `id` are
 *  the gateway's own, and a stock client ignoring them is the point. */
interface ModelsWire {
  object: string
  data: {
    id: string
    object: string
    created: number
    owned_by: string
    context_length?: number | null
    target_count?: number
    target_kinds?: TargetKind[]
    modality?: Modality
  }[]
}

/** One `data:` frame of a streamed chat completion, as much of it as this UI
 *  reads. Everything is optional: the first frame usually carries only a role,
 *  and `usage` appears on the last frame from some upstreams and never from
 *  others. The reasoning delta is `reasoning` from vLLM since it renamed the
 *  field and `reasoning_content` from SGLang and the providers, so both. */
interface ChatChunkWire {
  error?: { message?: string; type?: string; code?: string | null } | null
  choices?: {
    delta?: {
      content?: string | null
      reasoning?: string | null
      reasoning_content?: string | null
    }
    finish_reason?: string | null
  }[]
  usage?: {
    prompt_tokens?: number | null
    completion_tokens?: number | null
    completion_tokens_details?: { reasoning_tokens?: number | null } | null
  } | null
}

/** An aborted fetch or reader. Checked by name rather than by `instanceof
 *  DOMException`, which does not hold across every runtime this can run in. */
function isAbort(e: unknown): boolean {
  return (e as { name?: string } | null)?.name === 'AbortError'
}

export const httpBackend: Backend = {
  async cluster(): Promise<Cluster> {
    // Two calls because the wire splits what the instrument reads as one thing.
    const [wire, deployments] = await Promise.all([
      req<ClusterWire>('/api/cluster'),
      req<DeploymentDTO[]>('/api/deployments'),
    ])
    const nodes = wire.nodes.map((n) => toNodeState(n, wire.coordinator))
    return {
      summary: {
        cluster_id: wire.cluster_id,
        coordinator: wire.coordinator ?? '',
        node_count: wire.summary?.node_count ?? nodes.length,
        healthy_count:
          wire.summary?.healthy_nodes ?? nodes.filter((n) => n.healthy).length,
        // The wire's `total_memory` is physical. Every figure the UI puts next
        // to a fit is addressable, so it is summed from the nodes instead.
        total_addressable_memory: nodes.reduce(
          (a, n) => a + n.profile.addressable_memory,
          0,
        ),
      },
      nodes,
      links: wire.links ?? [],
      deployments,
    }
  },
  async topology(): Promise<Topology> {
    const t = await req<Topology>('/api/topology')
    // Same `unhealthy` spelling as above. Left unmapped, a node that is down
    // draws in the graph as if it were live.
    return {
      ...t,
      nodes: t.nodes.map((n) => ({
        ...n,
        state: nodeHealth(n.state, n.state === 'healthy'),
      })),
    }
  },
  deployments: () => req<DeploymentDTO[]>('/api/deployments'),
  candidates: () => req<Candidate[]>('/api/nodes/candidates'),
  routing: () => req<RoutingConfig[]>('/api/routing'),
  providers: () => req<Provider[]>('/api/providers'),
  async setup(): Promise<SetupStatus> {
    const wire = await req<Omit<SetupStatus, 'machine'> & { machine: NodeWire | null }>(
      '/api/setup',
    )
    return {
      ...wire,
      // Through `toNodeState` rather than used raw: the setup screen shows the
      // same hardware the cluster graph does, and a second mapping written here
      // would agree with any bug that came from the same reading of the schema.
      // The coordinator argument is the machine itself -- the server only names
      // one it could identify, so if there is a row here, this is that machine.
      machine: wire.machine ? toNodeState(wire.machine, wire.machine.node_id) : null,
    }
  },
  completeSetup: () => req<void>('/api/setup/complete', { method: 'POST' }),
  async models(): Promise<ServedModel[]> {
    const wire = await req<ModelsWire>('/api/chat/models')
    return (wire.data ?? []).map((m) => ({
      id: m.id,
      // `index.context_length` is a `.get()` on the gateway side and can come
      // back absent. Absent stays absent; a 0-token context window is not a
      // thing, and rendering one would be an invented fact.
      context_length: m.context_length ?? null,
      target_count: m.target_count ?? 0,
      target_kinds: m.target_kinds ?? [],
      // Absent from a gateway that predates the field. Left absent rather than
      // defaulted here so `isAudio` is the only place the fallback is decided.
      modality: m.modality,
    }))
  },

  async chatStream({
    model,
    messages,
    onDelta,
    onReasoning,
    onRewrite,
    onOpen,
    options,
    thinkOpen = false,
    signal,
    temperature,
    max_tokens,
    stop,
  }: ChatStreamRequest): Promise<ChatTurnMeta> {
    // The one thing in this file `req<T>` cannot carry: it awaits `res.json()`,
    // and the whole value here is in not waiting for the end of the body.
    // `EventSource` is no help either -- it is GET-only and cannot send one.
    const path = apiUrl('/api/chat/completions')
    const startedAt = performance.now()
    const splitter = createThinkSplitter({ startInside: thinkOpen })

    let requestId: string | null = null
    let ttftMs: number | null = null
    let firstContentMs: number | null = null
    // When the last reasoning fragment arrived, relative to send: how long the
    // model thought out loud. null for a turn with no reasoning at all.
    let reasoningMs: number | null = null
    let textFrames = 0
    let reasoningFrames = 0
    let reasoningSource: ChatTurnMeta['reasoningSource'] = null
    let finishReason: string | null = null
    let usagePrompt: number | null = null
    let usageCompletion: number | null = null
    let usageReasoning: number | null = null

    const meta = (stopped: boolean): ChatTurnMeta => ({
      model,
      requestId,
      ttftMs,
      firstContentMs,
      elapsedMs: performance.now() - startedAt,
      promptTokens: usagePrompt,
      // `|| null`: zero frames means nothing was ever observed, and that reads
      // as an em dash rather than as a model that emitted 0 tokens.
      completionTokens: usageCompletion ?? (textFrames + reasoningFrames || null),
      reasoningTokens: usageReasoning ?? (reasoningFrames || null),
      tokensEstimated: usageCompletion === null,
      reasoningMs,
      reasoningEstimated: usageReasoning === null,
      reasoningSource,
      finishReason,
      stopped,
    })

    const mark = (): number => {
      const now = performance.now() - startedAt
      if (ttftMs === null) ttftMs = now
      return now
    }

    /** Route what the splitter made of one content delta (`frame`) or of its
     *  end-of-stream flush (not a frame, so nothing is counted). A delta it
     *  swallowed whole into an open block -- the `<think>` tag itself, the
     *  closing tag -- is still a token the model emitted, so it counts as a
     *  reasoning frame; one it is holding undecided counts as text. */
    const route = (parts: ThinkSplit, frame: boolean): void => {
      if (parts.rewrite) {
        const now = mark()
        if (splitter.inline) {
          // A bare `</think>`: every frame so far was reasoning after all.
          reasoningSource ??= 'inline'
          reasoningFrames += textFrames
          textFrames = 0
          firstContentMs = parts.content !== '' ? now : null
          if (frame) {
            if (parts.content !== '') textFrames += 1
            else reasoningFrames += 1
          }
        } else {
          // Expected a block that never closed: it was all the answer.
          reasoningSource = null
          textFrames += reasoningFrames
          reasoningFrames = 0
          firstContentMs = ttftMs
        }
        onRewrite?.(parts.reasoning, parts.content)
        return
      }
      if (parts.content !== '') {
        const now = mark()
        if (firstContentMs === null) firstContentMs = now
        if (frame) textFrames += 1
        if (parts.reasoning !== '') {
          reasoningMs = now
          onReasoning?.(parts.reasoning)
        }
        onDelta(parts.content)
        return
      }
      if (!splitter.inline) {
        if (frame) textFrames += 1
        return
      }
      reasoningSource ??= 'inline'
      reasoningMs = mark()
      if (frame) reasoningFrames += 1
      if (parts.reasoning !== '') onReasoning?.(parts.reasoning)
    }

    /** Returns true when the frame was the stream's terminator. */
    const consume = (event: string): boolean => {
      for (const line of event.split('\n')) {
        // `event:`, `id:`, `retry:` and `:` comments are all legal SSE and all
        // uninteresting here.
        if (!line.startsWith('data:')) continue
        const payload = line.slice(5).trim()
        if (payload === '[DONE]') return true
        let frame: ChatChunkWire
        try {
          frame = JSON.parse(payload) as ChatChunkWire
        } catch {
          // A malformed frame is not a disconnect. Drop it and keep reading --
          // the same rule `subscribe()` applies to the metrics stream.
          continue
        }
        if (frame.error) {
          // HTTP has already succeeded, but this turn has not. Use the same
          // redacted error envelope as the JSON API, so Chat's existing catch
          // displays the upstream sentence and never receives successful meta.
          throw new ApiError(200, JSON.stringify(scrub(frame)), path)
        }
        const choice = frame.choices?.[0]
        const delta = choice?.delta
        // Reasoning first, so a frame carrying both keeps
        // `firstContentMs >= ttftMs`.
        const reasoning =
          typeof delta?.reasoning === 'string' && delta.reasoning !== ''
            ? delta.reasoning
            : typeof delta?.reasoning_content === 'string' && delta.reasoning_content !== ''
              ? delta.reasoning_content
              : null
        if (reasoning !== null) {
          reasoningSource = 'server'
          reasoningMs = mark()
          reasoningFrames += 1
          onReasoning?.(reasoning)
        }
        const text = delta?.content
        if (typeof text === 'string' && text !== '') {
          // A server that separates reasoning has already taken the tags out;
          // splitting again would eat an answer that quotes `<think>`.
          route(
            reasoningSource === 'server' ? { reasoning: '', content: text } : splitter.push(text),
            true,
          )
        }
        if (typeof choice?.finish_reason === 'string') finishReason = choice.finish_reason
        // Some upstreams close with a usage block. A real count beats a counted
        // frame whenever one turns up, which is why this is checked every time
        // rather than only on the last frame.
        const usage = frame.usage
        if (typeof usage?.prompt_tokens === 'number') usagePrompt = usage.prompt_tokens
        if (typeof usage?.completion_tokens === 'number') usageCompletion = usage.completion_tokens
        const reasoningTokens = usage?.completion_tokens_details?.reasoning_tokens
        if (typeof reasoningTokens === 'number') usageReasoning = reasoningTokens
      }
      return false
    }

    /** Whatever the splitter still holds, routed like any delta. */
    const flush = (): void => {
      // The server separated the reasoning, so the splitter never saw the
      // content and has nothing to hand back -- least of all a rewrite.
      if (reasoningSource === 'server') return
      const rest = splitter.flush()
      if (rest.rewrite || rest.content !== '' || rest.reasoning !== '') route(rest, false)
    }

    try {
      const body: Record<string, unknown> = { model, messages, stream: true }
      // Omitted rather than sent as `null`/absent-by-default -- the same
      // "unset means use the server's default" convention `speech()` uses for
      // an unpicked voice.
      if (temperature !== undefined) body.temperature = temperature
      if (max_tokens !== undefined) body.max_tokens = max_tokens
      if (stop !== undefined && stop.length > 0) body.stop = stop
      const res = await fetch(path, {
        method: 'POST',
        signal,
        headers: { 'content-type': 'application/json' },
        // No `stream_options: {include_usage: true}`. Not every upstream in
        // ProviderKind accepts it, and a request refused for an unknown field
        // is worse than a token count labelled as an estimate.
        body: JSON.stringify({ ...body, ...(options ?? {}) }),
      })

      // Read before anything can throw on the body: a refusal carries the id
      // too, and it is the only handle on the row that recorded the refusal.
      requestId = res.headers.get('X-Request-Id')
      onOpen?.(requestId)

      if (!res.ok) {
        noteUnauthenticated(res)
        throw new ApiError(res.status, await res.text().catch(() => ''), path)
      }
      if (!res.body) {
        throw new ApiError(res.status, 'The response carried no body.', path)
      }

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      let done = false

      try {
        while (!done) {
          const chunk = await reader.read()
          if (chunk.done) break
          // Decode incrementally and normalize after joining: either a UTF-8
          // character or a CRLF pair can straddle network chunks.
          buffer = (buffer + decoder.decode(chunk.value, { stream: true })).replace(/\r\n/g, '\n')
          // Events are separated by a blank line, and a chunk boundary can fall
          // anywhere -- mid-event, mid-token. Only whole events are parsed; the
          // tail stays buffered for the next read.
          for (;;) {
            const sep = buffer.indexOf('\n\n')
            if (sep === -1) break
            const event = buffer.slice(0, sep)
            buffer = buffer.slice(sep + 2)
            if (consume(event)) {
              done = true
              break
            }
          }
        }
        flush()
      } finally {
        // No more reads, including when an error frame throws. Cancel rather
        // than leave the upstream connection open until the tab closes.
        await reader.cancel().catch(() => {})
      }
    } catch (e) {
      // Stop was pressed. Deliberately not rethrown: the caller keeps the
      // partial text and the readout says it was stopped.
      if (isAbort(e)) {
        flush()
        return meta(true)
      }
      throw e
    }

    return meta(false)
  },

  async speech(
    { model, input, voice, response_format }: SpeechRequest,
    signal?: AbortSignal,
  ): Promise<SpeechResult> {
    // The second method in this file that cannot go through `req<T>`, and for
    // the mirror-image reason `chatStream` cannot: that one awaits
    // `res.json()` and the value is in not waiting, this one awaits it and the
    // body is not JSON. It must also stay clear of `scrub()` -- redact.ts
    // walks a decoded object graph, and there is nothing here to walk.
    const path = apiUrl('/api/chat/speech')
    const res = await fetch(path, {
      method: 'POST',
      signal,
      headers: { 'content-type': 'application/json' },
      // `voice` omitted rather than sent as null when nobody picked one:
      // naming no voice is a real request that means "your own", and an
      // explicit null is a value the server would have to have an opinion
      // about. No `speed` and no `stream` -- see SpeechRequest.
      body: JSON.stringify({
        model,
        input,
        response_format,
        ...(voice ? { voice } : {}),
      }),
    })

    const requestId = res.headers.get('X-Request-Id')

    if (!res.ok) {
      noteUnauthenticated(res)
      // A refusal is still JSON, and ApiError unwraps `{error:{message}}` into
      // `.message` -- which the speech screen renders through Verbatim. The
      // unknown-voice refusal lists what IS installed and the format refusals
      // name what would have worked; rewording either destroys the only part
      // a reader can act on.
      throw new ApiError(res.status, await res.text().catch(() => ''), path)
    }

    const blob = await res.blob()
    return {
      blob,
      contentType: res.headers.get('content-type') ?? blob.type ?? '',
      bytes: blob.size,
      // The runtime's own two headers. A provider sends neither, so both are
      // null rather than 0 -- the dash rule, applied to a figure nobody
      // reported rather than to one that came back zero.
      durationS: numberHeader(res, 'X-Audio-Duration-Seconds'),
      sampleRate: numberHeader(res, 'X-Audio-Sample-Rate'),
      requestId,
    }
  },

  async transcribe(
    { model, file, language }: TranscriptionRequest,
    signal?: AbortSignal,
  ): Promise<TranscriptionResult> {
    // The third method that cannot go through `req<T>`, and the one where
    // using it would fail most confusingly: `req` sets
    // `content-type: application/json`, and a multipart body carries its
    // boundary IN that header. Setting it by hand -- to anything, including
    // the right media type -- destroys the boundary and the server sees a
    // body with no parts. So no `headers` at all here: `fetch` derives the
    // full `multipart/form-data; boundary=...` from the FormData itself, and
    // the gateway forwards those bytes verbatim under the client's own
    // content-type (gateway/proxy.py's raw-content path).
    const path = apiUrl('/api/chat/transcriptions')
    const form = new FormData()
    form.append('model', model)
    form.append('file', file, file.name)
    if (language) form.append('language', language)

    const res = await fetch(path, { method: 'POST', signal, body: form })
    const requestId = res.headers.get('X-Request-Id')
    if (!res.ok) {
      noteUnauthenticated(res)
      throw new ApiError(res.status, await res.text().catch(() => ''), path)
    }
    // OpenAI's default `response_format` is json: `{"text": "..."}`. Not sent
    // as a field, because the default is the one we want and a strict
    // upstream is entitled to reject anything else. Scrubbed like any other
    // JSON body -- it is a decoded object graph, unlike a Blob.
    const wire = scrub((await res.json()) as { text?: unknown })
    return {
      text: typeof wire.text === 'string' ? wire.text : '',
      requestId,
    }
  },

  async voices(model: string): Promise<VoiceLibrary> {
    const wire = await req<{
      data?: { id?: unknown }[]
      skipped?: unknown[]
    }>(`/api/chat/voices?model=${encodeURIComponent(model)}`)
    return {
      voices: (wire.data ?? [])
        .map((v) => v.id)
        .filter((id): id is string => typeof id === 'string'),
      skipped: (wire.skipped ?? []).filter(
        (note): note is string => typeof note === 'string',
      ),
    }
  },
  plan: (body) =>
    req<PlanResponse>('/api/plan', { method: 'POST', body: JSON.stringify(body) }),
  launch: (body) =>
    req<DeploymentDTO>('/api/deployments', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  setPolicy: (servedName, policy, opts) =>
    req<RoutingConfig>(`/api/routing/${encodeURIComponent(servedName)}`, {
      method: 'PUT',
      body: JSON.stringify(opts?.limits ? { policy, limits: opts.limits } : { policy }),
    }),
  clearPolicy: (servedName) =>
    req<RoutingConfig>(`/api/routing/${encodeURIComponent(servedName)}`, { method: 'DELETE' }),
  wiring: (body) =>
    req<WiringResponse>('/api/wiring', { method: 'POST', body: JSON.stringify(body) }),
  admit: (nodeId) =>
    req<void>(`/api/nodes/${encodeURIComponent(nodeId)}/admit`, { method: 'POST' }),
  nodeRuntime: (nodeId) =>
    req<NodeRuntime>(`/api/nodes/${encodeURIComponent(nodeId)}/runtime`),
  adoptNodeRuntime: (nodeId) =>
    req<AdoptedRuntime>(`/api/nodes/${encodeURIComponent(nodeId)}/runtime`, {
      method: 'POST',
    }),
  setRuntimeModel: (nodeId, model, resident) =>
    req<{ done_reason: string | null }>(
      `/api/nodes/${encodeURIComponent(nodeId)}/runtime/model`,
      { method: 'POST', body: JSON.stringify({ model, resident }) },
    ),
  mintEnrollment: (spec = {}) =>
    req<Enrollment>('/api/enroll', { method: 'POST', body: JSON.stringify(spec) }),
  enrollments: () => req<EnrollmentRow[]>('/api/enroll'),
  revokeEnrollment: (tokenId) =>
    req<void>(`/api/enroll/${encodeURIComponent(tokenId)}`, { method: 'DELETE' }),
  removeNode: (nodeId) =>
    req<void>(`/api/nodes/${encodeURIComponent(nodeId)}`, { method: 'DELETE' }),
  rotateClusterToken: (currentPassword) =>
    req<{ rotated: boolean }>('/api/cluster/token/rotate', {
      method: 'POST',
      body: JSON.stringify({ current_password: currentPassword }),
    }),
  measureLink: (a, b) =>
    req<LinkMeasurement>('/api/links/measure', {
      method: 'POST',
      body: JSON.stringify({ a, b }),
    }),
  checkReach: (a, b) =>
    req<ReachReport>('/api/links/reach', {
      method: 'POST',
      body: JSON.stringify({ a, b }),
    }),
  tuneLink: (a, b) =>
    req<{ a: string; b: string; measuring: boolean }>('/api/links/tune', {
      method: 'POST',
      body: JSON.stringify({ a, b }),
    }),
  renameNode: (nodeId, label) =>
    req<{ node_id: string; label: string | null }>(
      `/api/nodes/${encodeURIComponent(nodeId)}/label`,
      { method: 'PUT', body: JSON.stringify({ label }) },
    ),
  memory: () => req<MemoryReportList>('/api/memory'),
  quantTable: () => req<QuantTable>('/api/models/quant-table'),
  catalog: () => req<CuratedModel[]>('/api/catalog'),
  modelRegistry: () => req<ModelRegistryResponse>('/api/models'),
  // A model id contains "/", and proxies and ASGI servers disagree about
  // whether %2F is decoded before routing -- there is a Vite dev proxy in the
  // chain too. So the id travels as a query parameter, encoded once.
  speculativeHeads: (modelId, opts) =>
    req<SpeculativeHeads>(
      `/api/models/speculative-heads?model_id=${encodeURIComponent(modelId)}` +
        `&limit=${opts?.limit ?? 12}` +
        (opts?.refresh ? '&refresh=1' : ''),
    ),
  searchModels: (q, limit) =>
    req<ModelSearchResponse>(
      `/api/models/search?q=${encodeURIComponent(q)}&limit=${limit ?? 40}`,
    ),
  modelDetail: (modelId) =>
    req<ModelDetail>(`/api/models/detail?model_id=${encodeURIComponent(modelId)}`),
  modelVariants: (modelId, opts) =>
    req<VariantLadder>(
      query(`/api/models/variants`, [
        `model_id=${encodeURIComponent(modelId)}`,
        ...fitQuestion(opts?.context, opts?.concurrency, opts?.nodeIds),
      ]),
    ),
  capacity: (context, concurrency, nodeIds) =>
    req<CapacityReport>(
      query('/api/capacity', fitQuestion(context, concurrency, nodeIds)),
    ),
  capacityFor: (modelIds, context, concurrency, nodeIds) =>
    req<CapacityReport>(
      query('/api/capacity', [
        ...fitQuestion(context, concurrency, nodeIds),
        // Each id encoded, joined on a literal comma. The %2F trap this file
        // documents above is about PATH segments and proxy routing; a query
        // value is safe, and `searchModels` already sends a "/" inside `q`.
        `models=${modelIds.map(encodeURIComponent).join(',')}`,
      ]),
    ),
  stopDeployment: (deploymentId) =>
    req<void>(`/api/deployments/${encodeURIComponent(deploymentId)}`, {
      method: 'DELETE',
    }),
  setDeploymentServing: (deploymentId, serving) =>
    req<DeploymentDTO>(`/api/deployments/${encodeURIComponent(deploymentId)}`, {
      method: 'PATCH',
      body: JSON.stringify({ serving }),
    }),
  deploymentLogs: (deploymentId, tail) =>
    req<DeploymentLogs>(
      `/api/deployments/${encodeURIComponent(deploymentId)}/logs` +
        (tail ? `?tail=${tail}` : ''),
    ),
  nodeProcesses: (nodeId) =>
    req<NodeProcessList>(`/api/nodes/${encodeURIComponent(nodeId)}/processes`),
  nodeLogTail: (nodeId, which, tail) =>
    req<NodeLogTail>(
      `/api/nodes/${encodeURIComponent(nodeId)}/logs?which=${which}` +
        (tail ? `&tail=${tail}` : ''),
    ),
  killProcess: (nodeId, pid) =>
    req<KillResult>(
      `/api/nodes/${encodeURIComponent(nodeId)}/processes/${encodeURIComponent(pid)}`,
      { method: 'DELETE' },
    ),
  storage: () => req<StorageReport>('/api/storage'),
  historyNodes: (opts) =>
    req<NodeHistory>(
      historyPath('/api/history/nodes', opts, { node_id: opts?.nodeId, step: opts?.step }),
    ),
  historyRequests: (opts) =>
    req<RequestHistory>(
      historyPath('/api/history/requests', opts, {
        served_name: opts?.servedName,
        target_id: opts?.targetId,
        step: opts?.step,
      }),
    ),
  historyEvents: (opts) =>
    req<EventHistory>(
      historyPath('/api/history/events', opts, {
        node_id: opts?.nodeId,
        deployment_id: opts?.deploymentId,
        type: opts?.type,
        source: opts?.source,
      }),
    ),
  historyLogs: (opts) =>
    req<LogHistory>(
      historyPath('/api/history/logs', opts, {
        node_id: opts?.nodeId,
        level: opts?.level,
        logger: opts?.logger,
        q: opts?.q,
        exclude: opts?.exclude,
      }, ['exclude']),
    ),
  historyStatus: () => req<TelemetryEstate>('/api/history/status'),
  shellStatus: () => req<ShellStatus>('/api/shell/status'),
  clearResolverCache: () =>
    req<CacheClearResult>('/api/storage/cache/resolver', { method: 'DELETE' }),
  deleteCachedModel: (nodeId, folder) =>
    req<ModelDeleteResult>(
      `/api/storage/nodes/${encodeURIComponent(nodeId)}/models/${encodeURIComponent(folder)}`,
      { method: 'DELETE' },
    ),
  getSettings: () => req<Settings>('/api/settings'),
  patchSettings: (patch) =>
    req<Settings>('/api/settings', { method: 'PATCH', body: JSON.stringify(patch) }),
  providerKinds: () => req<ProviderKindSpec[]>('/api/providers/kinds'),
  providerSecretRefs: async () =>
    (await req<{ refs?: string[] }>('/api/providers/secret-refs')).refs ?? [],
  pullToProvider: (providerId, body) =>
    req<PullAccepted>(`/api/providers/${encodeURIComponent(providerId)}/pull`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  activity: () => req<Activity>('/api/activity'),
  alerts: () => req<AlertsReport>('/api/alerts'),
  addProvider: (spec) =>
    req<Provider>('/api/providers', { method: 'POST', body: JSON.stringify(spec) }),
  removeProvider: (providerId) =>
    req<{ removed: string; revoked: boolean | null }>(
      `/api/providers/${encodeURIComponent(providerId)}`,
      { method: 'DELETE' },
    ),
  startProviderSignIn: (flow, providerId) =>
    req<ProviderSignInStart>(
      `/api/sign-in/${flow}`,
      post(providerId ? { provider_id: providerId } : {}),
    ),
  completeProviderSignIn: (flow, attemptId, callbackUrl) =>
    req<Provider>(
      `/api/sign-in/${flow}/${encodeURIComponent(attemptId)}/complete`,
      post({ callback_url: callbackUrl }),
    ),
  providerSignInStatus: (flow, attemptId) =>
    req<ProviderSignInStatus>(`/api/sign-in/${flow}/${encodeURIComponent(attemptId)}`),
  providerModels: (providerId) =>
    req<ProviderCatalogueModel[]>(
      `/api/providers/${encodeURIComponent(providerId)}/models`,
    ),
  providerBackends: (providerId, upstreamId) =>
    req<ProviderBackendOption[]>(
      `/api/providers/${encodeURIComponent(providerId)}/backends` +
        `?upstream_id=${encodeURIComponent(upstreamId)}`,
    ),
  patchProvider: (providerId, patch) =>
    req<Provider>(`/api/providers/${encodeURIComponent(providerId)}`, {
      method: 'PATCH',
      body: JSON.stringify(patch),
    }),
  refreshProvider: (providerId) =>
    req<Provider>(`/api/providers/${encodeURIComponent(providerId)}/refresh`, {
      method: 'POST',
    }),
  checkProviderKey: (providerId) =>
    req<Provider>(`/api/providers/${encodeURIComponent(providerId)}/key-check`, {
      method: 'POST',
    }),

  subscribe(onFrame, onState) {
    let es: EventSource | null = null
    let timer: number | undefined
    let attempt = 0
    let closed = false
    let staleSince = 0

    // 1s, 2s, 4s ... capped at 30s. A coordinator restart should be picked up
    // quickly; a coordinator that is gone should not be hammered.
    const backoffMs = () => Math.min(30_000, 1000 * 2 ** Math.min(attempt, 5))

    const connect = () => {
      if (closed) return
      if (attempt === 0) onState({ status: 'connecting' })
      es = new EventSource(apiUrl('/api/metrics/stream'))

      es.onopen = () => {
        attempt = 0
        staleSince = 0
        onState({ status: 'open' })
      }

      es.onmessage = (ev) => {
        try {
          onFrame(scrub(JSON.parse(ev.data) as MetricsFrame))
        } catch {
          // A malformed frame is not a disconnect. Drop it, keep the stream.
        }
      }

      es.onerror = () => {
        es?.close()
        es = null
        if (closed) return
        if (!staleSince) staleSince = Date.now()
        const wait = backoffMs()
        attempt += 1
        onState({ status: 'stale', since: staleSince, retryInMs: wait, attempt })
        timer = window.setTimeout(connect, wait)
      }
    }

    connect()
    return () => {
      closed = true
      es?.close()
      if (timer) window.clearTimeout(timer)
    }
  },

  authState: () => req<AuthState>('/api/auth/state'),
  login: (username, password) =>
    req<AuthState>('/api/auth/login', post({ username, password })),
  logout: () => req<void>('/api/auth/logout', post()),
  me: () => req<AdminRow>('/api/auth/me'),
  changePassword: (oldPassword, newPassword) =>
    req<void>('/api/auth/password', post({ old: oldPassword, new: newPassword })),
  admins: () => req<AdminRow[]>('/api/admins'),
  addAdmin: (username, email, password, currentPassword) =>
    req<AdminRow>('/api/admins', post({ username, email, password, current_password: currentPassword })),
  removeAdmin: (username, currentPassword) =>
    req<void>(`/api/admins/${seg(username)}`, {
      method: 'DELETE',
      body: JSON.stringify({ current_password: currentPassword }),
    }),
  sessions: () => req<AdminSession[]>('/api/auth/sessions'),
  revokeAdminSessions: (username) =>
    req<{ username: string; revoked: number }>(`/api/admins/${seg(username)}/sessions`, {
      method: 'DELETE',
    }),
  setupAdmin: (username, email, password) =>
    reqUnscrubbed<SetupAdminResult>('/api/setup/admin', post({ username, email, password })),

  keys: (filter = {}) =>
    req<ApiKeyRow[]>(
      `/api/keys${qs({
        status: filter.status,
        near_limit: filter.near_limit ? 'true' : undefined,
        expiring_days: filter.expiring_days,
        q: filter.q,
      })}`,
    ),
  key: (keyId) => req<ApiKeyRow>(`/api/keys/${seg(keyId)}`),
  createKey: (spec) => reqUnscrubbed<MintedKey>('/api/keys', post(spec)),
  patchKey: (keyId, patch) =>
    req<ApiKeyRow>(`/api/keys/${seg(keyId)}`, { method: 'PATCH', body: JSON.stringify(patch) }),
  deleteKey: (keyId) => req<void>(`/api/keys/${seg(keyId)}`, { method: 'DELETE' }),
  disableKey: (keyId, reason) =>
    req<ApiKeyRow>(`/api/keys/${seg(keyId)}/disable`, post(reason ? { reason } : {})),
  orgs: () => req<OrgRow[]>('/api/orgs'),
  disableOrgKeys: (orgId, reason) =>
    req<{ disabled: string[] }>(`/api/orgs/${seg(orgId)}/keys/disable`, post(reason ? { reason } : {})),
  enableKey: (keyId) => req<ApiKeyRow>(`/api/keys/${seg(keyId)}/enable`, post()),
  rotateKey: (keyId, currentPassword) =>
    reqUnscrubbed<MintedKey>(
      `/api/keys/${seg(keyId)}/rotate`,
      post(currentPassword ? { current_password: currentPassword } : undefined),
    ),
  raiseLimit: (keyId, limitType, value) =>
    req<LimitState>(`/api/keys/${seg(keyId)}/raise`, post({ limit_type: limitType, value })),
  resetCycle: (keyId) => req<void>(`/api/keys/${seg(keyId)}/reset-cycle`, post()),
  keyRequests: (keyId, q = {}) =>
    req<RequestsPage>(`/api/keys/${seg(keyId)}/requests${qs(requestParams(q))}`),
  keyRequestsCsvUrl: (keyId, q = {}) =>
    apiUrl(`/api/keys/${seg(keyId)}/requests${qs({ ...requestParams(q), format: 'csv' })}`),
  requests: (q = {}) => req<RequestsPage>(`/api/requests${qs(requestParams(q))}`),
  requestsCsvUrl: (q = {}) => apiUrl(`/api/requests${qs({ ...requestParams(q), format: 'csv' })}`),
  usage: (q) => req<UsageResult>(`/api/usage${qs({ ...q })}`),
  usageCsvUrl: (q) => apiUrl(`/api/usage${qs({ ...q, format: 'csv' })}`),
  audit: (q = {}) => req<AuditPage>(`/api/audit${qs({ ...q })}`),
  pricing: () => req<LocalPrice[]>('/api/pricing'),
  setPricing: (model, inputUsdPerMtok, outputUsdPerMtok) =>
    req<LocalPrice>(`/api/pricing/${model.split('/').map(seg).join('/')}`, {
      method: 'PUT',
      body: JSON.stringify({
        input_usd_per_mtok: inputUsdPerMtok,
        output_usd_per_mtok: outputUsdPerMtok,
      }),
    }),
  clearPricing: (model) =>
    req<void>(`/api/pricing/${model.split('/').map(seg).join('/')}`, { method: 'DELETE' }),
}
