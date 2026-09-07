// The only place the UI talks to Agent G. Live only: the coordinator is
// always there in the deployed shape (a Compose service, a systemd unit) and
// a UI that opens without one is not the shape this ships in. Day-0 fixture
// data lived here through bring-up; it is gone as of the derate port.

import { scrub } from './redact'
import type {
  Candidate,
  ChatMessage,
  ChatTurnMeta,
  Cluster,
  Modality,
  NodeHealth,
  NodeProfile,
  NodeStateDTO,
  DeploymentDTO,
  Enrollment,
  EnrollmentRow,
  EnrollmentSpec,
  LaunchRequest,
  LinkMeasurement,
  MetricsFrame,
  PlanRequest,
  PlanResponse,
  Provider,
  ProviderPatch,
  ProviderSpec,
  RoutingConfig,
  RoutingPolicy,
  ServedModel,
  Settings,
  SettingsPatch,
  TargetKind,
  Topology,
  CuratedModel,
  MemoryReportList,
  ModelDetail,
  ModelSearchResponse,
  QuantTable,
  VariantLadder,
  CapacityReport,
  NodeProcessList,
  KillResult,
  StorageReport,
  CacheClearResult,
  ModelDeleteResult,
} from './types'

/** One turn's worth of arguments for `chatStream`.
 *
 *  `onDelta` is called per text fragment as it arrives; `onOpen` fires once the
 *  response headers are in, which is the earliest the request id exists and is
 *  therefore the only way a caller learns it for a turn that goes on to fail. */
export interface ChatStreamRequest {
  model: string
  messages: ChatMessage[]
  onDelta: (text: string) => void
  onOpen?: (requestId: string | null) => void
  signal: AbortSignal
}

export interface Backend {
  cluster(): Promise<Cluster>
  topology(): Promise<Topology>
  deployments(): Promise<DeploymentDTO[]>
  candidates(): Promise<Candidate[]>
  routing(): Promise<RoutingConfig[]>
  providers(): Promise<Provider[]>
  /** `GET /v1/models`: every model the gateway will accept as `model`, local
   *  deployments and remote providers alike. */
  models(): Promise<ServedModel[]>
  /** `POST /v1/chat/completions` with `stream: true`. Resolves when the stream
   *  ends -- including when it ended because the caller aborted, which is an
   *  outcome rather than an error and leaves the partial text standing. */
  chatStream(req: ChatStreamRequest): Promise<ChatTurnMeta>
  plan(req: PlanRequest): Promise<PlanResponse>
  /** Refused by the fit gate when the verdict is WONT_FIT. Nothing starts. */
  launch(req: LaunchRequest): Promise<DeploymentDTO>
  setPolicy(servedName: string, policy: RoutingPolicy): Promise<RoutingConfig>
  admit(nodeId: string): Promise<void>
  /** Mint a short-lived token for one install. The response is the only
   *  time the secret is returned; it lives in `command` and nowhere else. */
  mintEnrollment(spec?: EnrollmentSpec): Promise<Enrollment>
  /** Live tokens, without their secrets. Expired ones are already gone. */
  enrollments(): Promise<EnrollmentRow[]>
  revokeEnrollment(tokenId: string): Promise<void>
  removeNode(nodeId: string): Promise<void>
  measureLink(a: string, b: string): Promise<LinkMeasurement | void>
  /** `DELETE /api/deployments/{id}`. Drains first: the deployment stops
   *  admitting immediately and in-flight requests finish. */
  stopDeployment(deploymentId: string): Promise<void>
  /** What is holding GPU memory on a node right now. Read on demand — this
   *  costs an nvidia-smi call on the node, so it is only polled while a node
   *  sheet is open. */
  nodeProcesses(nodeId: string): Promise<NodeProcessList>
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
   *  in the browser. */
  capacity(context: number, concurrency: number): Promise<CapacityReport>
  /** The contract's own quantization table. Fetched, never re-typed here: a
   *  second copy of these byte figures is a second answer, and the one that
   *  disagrees with the fit gate is the one that gets somebody an OOM. */
  quantTable(): Promise<QuantTable>
  /** The curated shortlist, server side, so the picker and the capacity answer
   *  cannot drift apart. */
  catalog(): Promise<CuratedModel[]>
  /** Three sources at once, none resolved. Degrades to the local two when
   *  the hub is unreachable rather than answering empty. */
  searchModels(q: string, limit?: number): Promise<ModelSearchResponse>
  modelDetail(modelId: string): Promise<ModelDetail>
  /** Every obtainable quantization, with this cluster's verdict on each. */
  modelVariants(
    modelId: string,
    opts?: { context?: number; concurrency?: number },
  ): Promise<VariantLadder>
  getSettings(): Promise<Settings>
  /** 501, with a message naming why, when the patch includes a daily spend
   *  cap and no provider port can be measured against. */
  patchSettings(patch: SettingsPatch): Promise<Settings>
  addProvider(spec: ProviderSpec): Promise<Provider>
  removeProvider(providerId: string): Promise<void>
  patchProvider(providerId: string, patch: ProviderPatch): Promise<Provider>
  refreshProvider(providerId: string): Promise<Provider>
  /** Returns an unsubscribe. `onState` reports stream health so the UI can grey
   *  live values during a gap instead of freezing or zeroing them. */
  subscribe(
    onFrame: (f: MetricsFrame) => void,
    onState: (s: StreamState) => void,
  ): () => void
}

export type StreamState =
  | { status: 'connecting' }
  | { status: 'open' }
  | { status: 'stale'; since: number; retryInMs: number; attempt: number }

// ── HTTP backend ─────────────────────────────────────────────────────────────

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { 'content-type': 'application/json', ...(init?.headers ?? {}) },
  })
  if (!res.ok) {
    const body = await res.text().catch(() => '')
    throw new ApiError(res.status, body || res.statusText, path)
  }
  if (res.status === 204) return undefined as T
  return scrub((await res.json()) as T)
}

/** OpenAI-shaped error bodies (gateway/errors.py): `{error:{message,...}}`.
 *  When the body parses that way, `.message` is the sentence the gateway
 *  actually wrote -- e.g. the 501 explaining why a daily spend cap cannot be
 *  enforced -- rather than the raw JSON blob. `.body` keeps the untouched text
 *  for a caller that wants more than the message. */
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
  ) {
    super(errorMessage(status, path, body))
  }
}

// ── Wire adaptation ──────────────────────────────────────────────────────────
//
// §4.8 is owned by Agent G and says the UI codes against exactly that surface,
// so where the wire and the view models differ the adaptation happens here.
// This file is the only place that knows the wire format; everything above
// `Backend` sees the shapes in `types.ts`.

/** `GET /api/cluster` as Agent G emits it: identity at the top level, a
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

function toNodeState(n: NodeWire, coordinator: string | null): NodeStateDTO {
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
    healthy: n.healthy,
    state: nodeHealth(undefined, n.healthy),
    role: n.node_id === coordinator ? 'coordinator' : 'worker',
    last_seen: n.last_seen,
    // When the readings below were measured, which is not when the node was
    // last reachable. Null means never sampled, and `nodeLive` greys the
    // readings rather than presenting a frozen one as current.
    sample_ts: n.sample_ts ?? null,
    memory_used: n.memory_used,
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
 *  Agent G's, and a stock client ignoring them is the point. */
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
 *  others. */
interface ChatChunkWire {
  choices?: { delta?: { content?: string | null } }[]
  usage?: { completion_tokens?: number | null } | null
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
  async models(): Promise<ServedModel[]> {
    const wire = await req<ModelsWire>('/v1/models')
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
    onOpen,
    signal,
  }: ChatStreamRequest): Promise<ChatTurnMeta> {
    // The one thing in this file `req<T>` cannot carry: it awaits `res.json()`,
    // and the whole value here is in not waiting for the end of the body.
    // `EventSource` is no help either -- it is GET-only and cannot send one.
    const path = '/v1/chat/completions'
    const startedAt = performance.now()

    let requestId: string | null = null
    let ttftMs: number | null = null
    let textFrames = 0
    let usageCompletion: number | null = null

    const meta = (stopped: boolean): ChatTurnMeta => ({
      model,
      requestId,
      ttftMs,
      elapsedMs: performance.now() - startedAt,
      // `textFrames || null`: zero frames means nothing was ever observed, and
      // that reads as an em dash rather than as a model that emitted 0 tokens.
      completionTokens: usageCompletion ?? (textFrames || null),
      tokensEstimated: usageCompletion === null,
      stopped,
    })

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
        const text = frame.choices?.[0]?.delta?.content
        if (typeof text === 'string' && text !== '') {
          if (ttftMs === null) ttftMs = performance.now() - startedAt
          textFrames += 1
          onDelta(text)
        }
        // Some upstreams close with a usage block. A real count beats a counted
        // frame whenever one turns up, which is why this is checked every time
        // rather than only on the last frame.
        const completion = frame.usage?.completion_tokens
        if (typeof completion === 'number') usageCompletion = completion
      }
      return false
    }

    try {
      const res = await fetch(path, {
        method: 'POST',
        signal,
        headers: { 'content-type': 'application/json' },
        // No `stream_options: {include_usage: true}`. Not every upstream in
        // ProviderKind accepts it, and a request refused for an unknown field
        // is worse than a token count labelled as an estimate.
        body: JSON.stringify({ model, messages, stream: true }),
      })

      // Read before anything can throw on the body: a refusal carries the id
      // too, and it is the only handle on the row that recorded the refusal.
      requestId = res.headers.get('X-Request-Id')
      onOpen?.(requestId)

      if (!res.ok) {
        throw new ApiError(res.status, await res.text().catch(() => ''), path)
      }
      if (!res.body) {
        throw new ApiError(res.status, 'The response carried no body.', path)
      }

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      let done = false

      while (!done) {
        const chunk = await reader.read()
        if (chunk.done) break
        // `stream: true` so a multi-byte character split across two network
        // chunks is not decoded into two replacement characters.
        buffer += decoder.decode(chunk.value, { stream: true }).replace(/\r\n/g, '\n')
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

      // Nothing more will be read from it, whether the upstream closed or
      // `[DONE]` arrived first. Without this the connection stays open until
      // the tab does.
      await reader.cancel().catch(() => {})
    } catch (e) {
      // Stop was pressed. Deliberately not rethrown: the caller keeps the
      // partial text and the readout says it was stopped.
      if (isAbort(e)) return meta(true)
      throw e
    }

    return meta(false)
  },
  plan: (body) =>
    req<PlanResponse>('/api/plan', { method: 'POST', body: JSON.stringify(body) }),
  launch: (body) =>
    req<DeploymentDTO>('/api/deployments', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  setPolicy: (servedName, policy) =>
    req<RoutingConfig>(`/api/routing/${encodeURIComponent(servedName)}`, {
      method: 'PUT',
      body: JSON.stringify({ policy }),
    }),
  admit: (nodeId) =>
    req<void>(`/api/nodes/${encodeURIComponent(nodeId)}/admit`, { method: 'POST' }),
  mintEnrollment: (spec = {}) =>
    req<Enrollment>('/api/enroll', { method: 'POST', body: JSON.stringify(spec) }),
  enrollments: () => req<EnrollmentRow[]>('/api/enroll'),
  revokeEnrollment: (tokenId) =>
    req<void>(`/api/enroll/${encodeURIComponent(tokenId)}`, { method: 'DELETE' }),
  removeNode: (nodeId) =>
    req<void>(`/api/nodes/${encodeURIComponent(nodeId)}`, { method: 'DELETE' }),
  measureLink: (a, b) =>
    req<LinkMeasurement>('/api/links/measure', {
      method: 'POST',
      body: JSON.stringify({ a, b }),
    }),
  memory: () => req<MemoryReportList>('/api/memory'),
  quantTable: () => req<QuantTable>('/api/models/quant-table'),
  catalog: () => req<CuratedModel[]>('/api/catalog'),
  // A model id contains "/", and proxies and ASGI servers disagree about
  // whether %2F is decoded before routing -- there is a Vite dev proxy in the
  // chain too. So the id travels as a query parameter, encoded once.
  searchModels: (q, limit) =>
    req<ModelSearchResponse>(
      `/api/models/search?q=${encodeURIComponent(q)}&limit=${limit ?? 40}`,
    ),
  modelDetail: (modelId) =>
    req<ModelDetail>(`/api/models/detail?model_id=${encodeURIComponent(modelId)}`),
  modelVariants: (modelId, opts) =>
    req<VariantLadder>(
      `/api/models/variants?model_id=${encodeURIComponent(modelId)}` +
        `&context=${opts?.context ?? 8192}` +
        `&concurrency=${opts?.concurrency ?? 1}`,
    ),
  capacity: (context, concurrency) =>
    req<CapacityReport>(
      `/api/capacity?context=${encodeURIComponent(context)}` +
        `&concurrency=${encodeURIComponent(concurrency)}`,
    ),
  stopDeployment: (deploymentId) =>
    req<void>(`/api/deployments/${encodeURIComponent(deploymentId)}`, {
      method: 'DELETE',
    }),
  nodeProcesses: (nodeId) =>
    req<NodeProcessList>(`/api/nodes/${encodeURIComponent(nodeId)}/processes`),
  killProcess: (nodeId, pid) =>
    req<KillResult>(
      `/api/nodes/${encodeURIComponent(nodeId)}/processes/${encodeURIComponent(pid)}`,
      { method: 'DELETE' },
    ),
  storage: () => req<StorageReport>('/api/storage'),
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
  addProvider: (spec) =>
    req<Provider>('/api/providers', { method: 'POST', body: JSON.stringify(spec) }),
  removeProvider: (providerId) =>
    req<void>(`/api/providers/${encodeURIComponent(providerId)}`, { method: 'DELETE' }),
  patchProvider: (providerId, patch) =>
    req<Provider>(`/api/providers/${encodeURIComponent(providerId)}`, {
      method: 'PATCH',
      body: JSON.stringify(patch),
    }),
  refreshProvider: (providerId) =>
    req<Provider>(`/api/providers/${encodeURIComponent(providerId)}/refresh`, {
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
      es = new EventSource('/api/metrics/stream')

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
}
