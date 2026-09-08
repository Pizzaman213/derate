import { useKeyedResource } from './backend'
import type {
  EventHistory,
  HistoryEnvelope,
  LogHistory,
  NodeHistory,
  NodeHistorySample,
  RequestHistory,
  RequestHistoryRow,
  ShellStatus,
} from '../api/types'
import type { TelemetryPoint } from './useTelemetry'

// The client for `/api/history/*`. These routes shipped with the durable
// telemetry package and had no caller in the browser at all: the only history
// anywhere in the UI was sixty seconds of it accumulated in a tab, lost on
// reload. The archive keeps thirty days of raw node samples and four hundred
// of hourly rollups, and it survives a restart.
//
// Everything below the hooks is pure and free of React, because the awkward
// part of this file is not fetching -- it is that ONE route answers with two
// different row shapes depending on how wide the window is, and reading the
// wrong column yields a chart that is silently empty rather than wrong.

/** The windows the node page offers. `live` is not a fetch at all: it is the
 *  60-second ring `state/telemetry.tsx` accumulates from the SSE frame. */
export type HistoryWindow = 'live' | '5m' | '1h' | '24h'

export const HISTORY_WINDOWS: HistoryWindow[] = ['live', '5m', '1h', '24h']

interface WindowSpec {
  label: string
  /** Relative, so the server picks both boundaries off its own clock. */
  from: string
  /** Slow on purpose. The whole point of a durable window is that it does not
   *  need to be chased at 1 Hz; a 24-hour chart re-read every second would be
   *  the same picture and a scan of the archive each time. */
  pollMs: number
}

export const WINDOW_SPEC: Record<Exclude<HistoryWindow, 'live'>, WindowSpec> = {
  // Five minutes first, deliberately: TELEMETRY_RING_S is 300, so this is the
  // one archive window a box with telemetry switched off can still answer --
  // from the registry's in-RAM ring, labelled `durable: false`. On a
  // development machine (no DERATE_DATA_DIR, so no archive) it is the
  // difference between a page with charts and a page with an error.
  '5m': { label: '5m', from: '-5m', pollMs: 10_000 },
  '1h': { label: '1h', from: '-1h', pollMs: 30_000 },
  '24h': { label: '24h', from: '-24h', pollMs: 60_000 },
}

export function windowLabel(w: HistoryWindow): string {
  return w === 'live' ? 'live' : WINDOW_SPEC[w].label
}

/** An envelope for a window nothing was asked for. Not an error and not an
 *  empty archive -- the caller simply did not ask, and a chart fed from this
 *  draws nothing rather than a flat line at zero. */
const EMPTY: HistoryEnvelope = {
  from: 0,
  to: 0,
  resolution: 'raw',
  durable: false,
  gaps: [],
  truncated: false,
}

const NO_NODES: NodeHistory = { ...EMPTY, samples: [] }
const NO_REQUESTS: RequestHistory = { ...EMPTY, requests: [] }
const NO_EVENTS: EventHistory = { ...EMPTY, events: [] }
const NO_LOGS: LogHistory = { ...EMPTY, logs: [] }

// ── Hooks ────────────────────────────────────────────────────────────────────
//
// All keyed, because every one of them is parameterised and `useResource`
// would keep answering the previous node's question for a full interval.
// All no-ops on `live`, where the browser's own ring is the source.

export function useNodeHistory(nodeId: string, window: HistoryWindow) {
  const spec = window === 'live' ? null : WINDOW_SPEC[window]
  return useKeyedResource<NodeHistory>(
    `nodes:${nodeId}:${window}`,
    (b) =>
      spec && nodeId
        ? b.historyNodes({ nodeId, from: spec.from })
        : Promise.resolve(NO_NODES),
    spec?.pollMs ?? 60_000,
  )
}

/** An empty `servedName` is a real query, not a skipped one: unfiltered, so
 *  the caller can narrow by `node_id` instead. That is the only way to ask
 *  "what ran on this machine", since the route filters by served name and
 *  target and never by node. */
export function useRequestHistory(
  servedName: string,
  window: HistoryWindow,
  limit?: number,
) {
  const spec = window === 'live' ? null : WINDOW_SPEC[window]
  return useKeyedResource<RequestHistory>(
    `requests:${servedName}:${window}:${limit ?? ''}`,
    (b) =>
      spec
        ? b.historyRequests({ servedName, from: spec.from, limit })
        : Promise.resolve(NO_REQUESTS),
    spec?.pollMs ?? 60_000,
  )
}

export function useNodeEvents(nodeId: string, window: HistoryWindow) {
  const spec = window === 'live' ? null : WINDOW_SPEC[window]
  return useKeyedResource<EventHistory>(
    `events:${nodeId}:${window}`,
    (b) =>
      spec && nodeId
        ? b.historyEvents({ nodeId, from: spec.from, limit: 200 })
        : Promise.resolve(NO_EVENTS),
    spec?.pollMs ?? 60_000,
  )
}

/** Whether a terminal can be opened. Effectively static for the life of the
 *  coordinator process -- the flag is read once at startup, by design, so that
 *  nothing arriving over the network can switch the route on -- so this is
 *  polled at the lazy end purely to notice a restart. */
export function useShellStatus() {
  return useKeyedResource<ShellStatus>('shell:status', (b) => b.shellStatus(), 60_000)
}

export function useNodeLogs(nodeId: string, level: string, window: HistoryWindow) {
  const spec = window === 'live' ? null : WINDOW_SPEC[window]
  return useKeyedResource<LogHistory>(
    `logs:${nodeId}:${level}:${window}`,
    (b) =>
      spec && nodeId
        ? b.historyLogs({ nodeId, level, from: spec.from, limit: 200 })
        : Promise.resolve(NO_LOGS),
    spec?.pollMs ?? 60_000,
  )
}

// ── Adapters ─────────────────────────────────────────────────────────────────

/** Seconds per bucket at a given resolution. `raw` and `ring` are per-sample
 *  rows rather than buckets, so they have none. */
export function bucketSeconds(resolution: HistoryEnvelope['resolution']): number | null {
  if (resolution === '1m') return 60
  if (resolution === '1h') return 3600
  return null
}

/** How the chart's own axis should describe the window it is drawing. The
 *  count is of points actually present, not of the window asked for: a series
 *  that has been running for ten seconds says so. */
export function resolutionNote(env: HistoryEnvelope | null, points: number): string {
  if (!env) return `${points}`
  const bucket = bucketSeconds(env.resolution)
  if (bucket === 60) return `${points} × 1m`
  if (bucket === 3600) return `${points} × 1h`
  return `${points} samples`
}

export type NodeField = 'power' | 'temp' | 'util' | 'mem'

/** One node metric as a drawable series.
 *
 *  Reads the raw column when the row has one and the bucket average when it
 *  does not, because the same route returns either. A row that carries neither
 *  yields a `null` POINT rather than being dropped: a null lifts the pen in
 *  `Chart`, so a sampler that went quiet mid-window draws as the hole it is
 *  instead of a straight line across it.
 *
 *  `memoryTotal` is the denominator for `mem`, used only when the sample does
 *  not carry its own `memory_total` -- rollup rows do not. It is the node
 *  profile's figure, which is exactly what `serialize.node_payload` divides
 *  by, so the chart and the quad above it cannot disagree. */
export function nodeSeries(
  history: NodeHistory | null,
  field: NodeField,
  memoryTotal?: number,
): TelemetryPoint[] {
  if (!history) return []
  const out: TelemetryPoint[] = []
  for (const s of history.samples) {
    out.push({ t: s.ts, v: nodeValue(s, field, memoryTotal) })
  }
  return out
}

function nodeValue(
  s: NodeHistorySample,
  field: NodeField,
  memoryTotal?: number,
): number | null {
  switch (field) {
    case 'power':
      return num(s.power_w ?? s.power_w_avg)
    case 'temp':
      return num(s.temp_c ?? s.temp_c_avg)
    case 'util':
      return num(s.util_pct ?? s.util_pct_avg)
    case 'mem': {
      const used = num(s.memory_used ?? s.memory_used_avg)
      const total = num(s.memory_total) ?? num(memoryTotal)
      if (used == null || total == null || total <= 0) return null
      return (used / total) * 100
    }
  }
}

/** The bucket MAXIMUM for one node metric, as the upper edge of a band whose
 *  lower edge is `nodeSeries`.
 *
 *  This column was being thrown away. `nodeValue` reads the average, so on the
 *  1h and 24h windows a node that held 140 W for forty seconds inside a
 *  one-minute bucket drew as whatever the minute averaged to -- the spike was
 *  not smoothed, it was absent, and the chart said nothing about the fact.
 *  Drawn as a band rather than a second line because an average and the
 *  maximum it was rolled from are ONE quantity in two aggregations: two hues
 *  would claim they are two things.
 *
 *  Null, not an empty array, for a window that has no maximum to give:
 *  `raw` and `ring` rows are single samples, where every point already IS its
 *  own maximum and a band would be a flat restatement of the line. Null also
 *  for a rolled window whose maxima are entirely missing, so a band never
 *  renders as a line pinned to the floor. */
export function nodeBand(
  history: NodeHistory | null,
  field: NodeField,
  memoryTotal?: number,
): TelemetryPoint[] | null {
  if (!history || bucketSeconds(history.resolution) == null) return null
  const out: TelemetryPoint[] = []
  let real = 0
  for (const s of history.samples) {
    const v = nodeMax(s, field, memoryTotal)
    if (v != null) real += 1
    out.push({ t: s.ts, v })
  }
  return real > 0 ? out : null
}

function nodeMax(
  s: NodeHistorySample,
  field: NodeField,
  memoryTotal?: number,
): number | null {
  switch (field) {
    case 'power':
      return num(s.power_w_max)
    case 'temp':
      return num(s.temp_c_max)
    case 'util':
      return num(s.util_pct_max)
    case 'mem': {
      const used = num(s.memory_used_max)
      const total = num(s.memory_total) ?? num(memoryTotal)
      if (used == null || total == null || total <= 0) return null
      return (used / total) * 100
    }
  }
}

export type DepField = 'tps' | 'ttft' | 'ttft_p99' | 'failed'

/** One deployment metric as a drawable series, from request rows.
 *
 *  Only rolled buckets can answer this: a raw row is one request, not a rate,
 *  and drawing a point per request would be a scatter of arrival times rather
 *  than throughput. A raw window returns nothing here and the caller says so,
 *  which is honest -- the numbers it wants were never computed at that
 *  resolution.
 *
 *  Buckets for OTHER targets under the same served name are summed in for
 *  `tps` and `failed` and ignored for latency, because a rate is additive
 *  across targets and a percentile is not. */
export function depSeries(
  history: RequestHistory | null,
  field: DepField,
): TelemetryPoint[] {
  if (!history) return []
  const per = bucketSeconds(history.resolution)
  if (per == null) return []

  const byBucket = new Map<number, RequestHistoryRow[]>()
  for (const r of history.requests) {
    const key = r.ts
    const rows = byBucket.get(key)
    if (rows) rows.push(r)
    else byBucket.set(key, [r])
  }

  const out: TelemetryPoint[] = []
  for (const t of [...byBucket.keys()].sort((a, b) => a - b)) {
    const rows = byBucket.get(t)!
    out.push({ t, v: depValue(rows, field, per) })
  }
  return out
}

function depValue(rows: RequestHistoryRow[], field: DepField, per: number): number | null {
  if (field === 'tps') {
    let tokens = 0
    let seen = false
    for (const r of rows) {
      if (r.tokens == null) continue
      tokens += r.tokens
      seen = true
    }
    return seen ? tokens / per : null
  }
  if (field === 'failed') {
    let failed = 0
    let seen = false
    for (const r of rows) {
      if (r.failed == null) continue
      failed += r.failed
      seen = true
    }
    return seen ? failed : null
  }
  // Latency: the widest bucket in this slice wins rather than an average of
  // percentiles, which is the thing hist.py exists so nobody computes.
  const key = field === 'ttft' ? 'p50_ms' : 'p99_ms'
  let best: number | null = null
  let bestN = -1
  for (const r of rows) {
    const summary = r.ttft
    if (!summary) continue
    const v = num(summary[key])
    if (v == null) continue
    if (summary.n > bestN) {
      bestN = summary.n
      best = v
    }
  }
  return best
}

/** Sums the rolled counters across a whole window. Returns null for a total
 *  no row carried, so "nothing was recorded" never renders as a measured 0. */
export function requestTotals(history: RequestHistory | null): {
  n: number | null
  ok: number | null
  failed: number | null
  tokens: number | null
  cost: number | null
  ttft: { p50: number | null; p90: number | null; p99: number | null; n: number }
  duration: { p50: number | null; p90: number | null; p99: number | null; n: number }
} | null {
  if (!history || bucketSeconds(history.resolution) == null) return null

  let n: number | null = null
  let ok: number | null = null
  let failed: number | null = null
  let tokens: number | null = null
  let cost: number | null = null

  const add = (acc: number | null, v: number | null | undefined) =>
    v == null ? acc : (acc ?? 0) + v

  // Percentiles cannot be summed. The busiest bucket's own summary is
  // reported, labelled with the count behind it, rather than a mean of
  // percentiles -- which would be a number nothing measured.
  let ttft = { p50: null as number | null, p90: null as number | null, p99: null as number | null, n: 0 }
  let duration = { ...ttft }

  for (const r of history.requests) {
    n = add(n, r.n)
    ok = add(ok, r.ok)
    failed = add(failed, r.failed)
    tokens = add(tokens, r.tokens)
    cost = add(cost, r.cost_usd)
    if (r.ttft && r.ttft.n > ttft.n) {
      ttft = { p50: num(r.ttft.p50_ms), p90: num(r.ttft.p90_ms), p99: num(r.ttft.p99_ms), n: r.ttft.n }
    }
    if (r.duration && r.duration.n > duration.n) {
      duration = {
        p50: num(r.duration.p50_ms),
        p90: num(r.duration.p90_ms),
        p99: num(r.duration.p99_ms),
        n: r.duration.n,
      }
    }
  }

  return { n, ok, failed, tokens, cost, ttft, duration }
}

function num(v: number | null | undefined): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}
