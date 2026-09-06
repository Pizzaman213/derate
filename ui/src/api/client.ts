// The only place the UI talks to Agent G.
//
// Two backends behind one interface: the real HTTP surface, and the day-0
// fixture stub. Mode is `auto` by default — probe the coordinator once, fall
// back to fixtures if it is not there — so the UI opens and renders whether or
// not a cluster exists. When fixtures are in use the UI says so, because
// demo data that looks live is worse than demo data that is labelled.

import { fixtures, fixtureFrame } from './fixtures'
import { scrub } from './redact'
import type {
  Candidate,
  Cluster,
  DeploymentDTO,
  LaunchRequest,
  LinkMeasurement,
  MetricsFrame,
  PlanRequest,
  PlanResponse,
  Provider,
  RoutingConfig,
  RoutingPolicy,
  Topology,
} from './types'

export type Mode = 'live' | 'fixture'

export interface Backend {
  readonly mode: Mode
  cluster(): Promise<Cluster>
  topology(): Promise<Topology>
  deployments(): Promise<DeploymentDTO[]>
  candidates(): Promise<Candidate[]>
  routing(): Promise<RoutingConfig[]>
  providers(): Promise<Provider[]>
  plan(req: PlanRequest): Promise<PlanResponse>
  /** Refused by the fit gate when the verdict is WONT_FIT. Nothing starts. */
  launch(req: LaunchRequest): Promise<DeploymentDTO>
  setPolicy(servedName: string, policy: RoutingPolicy): Promise<RoutingConfig>
  admit(nodeId: string): Promise<void>
  measureLink(a: string, b: string): Promise<LinkMeasurement | void>
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

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly body: string,
    readonly path: string,
  ) {
    super(`${status} on ${path}: ${body}`)
  }
}

const httpBackend: Backend = {
  mode: 'live',
  cluster: () => req<Cluster>('/api/cluster'),
  topology: () => req<Topology>('/api/topology'),
  deployments: () => req<DeploymentDTO[]>('/api/deployments'),
  candidates: () => req<Candidate[]>('/api/nodes/candidates'),
  routing: () => req<RoutingConfig[]>('/api/routing'),
  providers: () => req<Provider[]>('/api/providers'),
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
  measureLink: (a, b) =>
    req<LinkMeasurement>('/api/links/measure', {
      method: 'POST',
      body: JSON.stringify({ a, b }),
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

// ── Fixture backend ──────────────────────────────────────────────────────────

const settle = <T,>(v: T): Promise<T> =>
  new Promise((resolve) => window.setTimeout(() => resolve(scrub(v)), 40))

const fixtureBackend: Backend = {
  mode: 'fixture',
  cluster: () => settle(fixtures.cluster()),
  topology: () => settle(fixtures.topology()),
  deployments: () => settle(fixtures.deployments()),
  candidates: () => settle(fixtures.candidates()),
  routing: () => settle(fixtures.routing()),
  providers: () => settle(fixtures.providers()),
  plan: (r) => settle(fixtures.plan(r)),
  launch: (r) => settle(fixtures.launch(r)),
  setPolicy: (name, policy) => settle(fixtures.setPolicy(name, policy)),
  admit: async (nodeId) => {
    fixtures.admit(nodeId)
  },
  measureLink: async (a, b) => {
    // A measurement is disruptive and takes real time. Pretending it is instant
    // would misrepresent the one action in this UI that costs something.
    await new Promise((r) => window.setTimeout(r, 1800))
    fixtures.measureLink(a, b)
  },

  subscribe(onFrame, onState) {
    onState({ status: 'open' })
    onFrame(fixtureFrame(Date.now() / 1000))
    const id = window.setInterval(
      () => onFrame(fixtureFrame(Date.now() / 1000)),
      1000,
    )
    return () => window.clearInterval(id)
  },
}

// ── Mode resolution ──────────────────────────────────────────────────────────

const forced = (import.meta.env.VITE_API_MODE ?? 'auto') as 'auto' | Mode

/** Resolved once at startup. `auto` probes the coordinator and falls back. */
export async function resolveBackend(): Promise<Backend> {
  if (forced === 'fixture') return fixtureBackend
  if (forced === 'live') return httpBackend
  try {
    const res = await fetch('/api/cluster', {
      method: 'GET',
      signal: AbortSignal.timeout(2500),
    })
    if (res.ok) return httpBackend
  } catch {
    // No coordinator on this origin. Fixtures, and the UI will say so.
  }
  return fixtureBackend
}
