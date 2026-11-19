// The keys screen's arithmetic and wording, kept out of React so the two
// verifiers beside it can run it in node: `keys.check.mjs` (hermetic) and
// `table.check.mjs` (against the coordinator's own `/api/keys` rows).
//
// Nothing here invents a figure. Limit states, 30-day usage and every usage
// bucket come off the coordinator; this module only decides which of them is
// the tightest, what lamp a row wears, and how an absent value reads -- an em
// dash, never 0, because 0 is a measurement and absence is not.

import type {
  ApiKeyRow,
  FieldError,
  LimitState,
  Percentiles,
  RoutingPolicy,
  Usage30d,
  UsageBucket,
} from '../../api/types'
import { NEAR_LIMIT_FRACTION } from '../../api/types'
import type { TelemetryPoint } from '../../state/useTelemetry'

export const EM = '—'

const DAY_S = 86400
/** `CYCLE_DAYS` in contracts/auth.py: a limit cycle is 30 days from the key's
 *  (or org's) creation, repeating. */
export const CYCLE_DAYS = 30

/** The fourteen fixed names of `LIMIT_TYPES` in contracts/auth.py. The four
 *  `model:<id>:*` templates are built with `modelLimitType`. */
export const FIXED_LIMIT_TYPES = [
  'tokens_total_cycle',
  'tokens_input_cycle',
  'tokens_output_cycle',
  'requests_cycle',
  'budget_usd_cycle',
  'local_tokens_cycle',
  'local_usd_cycle',
  'cloud_tokens_cycle',
  'cloud_usd_cycle',
  'rpm',
  'tpm',
  'max_concurrent',
  'max_output_tokens_per_request',
  'max_prompt_tokens_per_request',
] as const

/** `MODEL_LIMIT_SUFFIXES` in contracts/auth.py. */
export const MODEL_LIMIT_SUFFIXES = ['tokens_cycle', 'usd_cycle', 'rpm', 'tpm'] as const
export type ModelLimitSuffix = (typeof MODEL_LIMIT_SUFFIXES)[number]

export function modelLimitType(model: string, suffix: ModelLimitSuffix): string {
  return `model:${model}:${suffix}`
}

/** The seven policies of `RoutingPolicy`, in the wire's order. */
export const ROUTING_POLICIES: RoutingPolicy[] = [
  'least_outstanding',
  'round_robin',
  'weighted_capacity',
  'cache_affinity',
  'failover',
  'local_first',
  'cost_aware',
]

// ── Limits ───────────────────────────────────────────────────────────────────

export type LimitPhase = 'ok' | 'near' | 'at'

/** Near is `fraction >= NEAR_LIMIT_FRACTION` and below 1; at is 1 or more.
 *  The same two thresholds `LimitService.states()` applies, so this agrees
 *  with the server's own `near`/`at` flags (table.check.mjs pins that). */
export function limitPhase(fraction: number): LimitPhase {
  if (!Number.isFinite(fraction)) return 'ok'
  if (fraction >= 1) return 'at'
  if (fraction >= NEAR_LIMIT_FRACTION) return 'near'
  return 'ok'
}

/** The limit closest to biting: the entry with the largest `fraction`. Ties
 *  keep the first, so the order the server lists them in decides. */
export function tightest(states: LimitState[] | null | undefined): LimitState | null {
  let best: LimitState | null = null
  for (const s of states ?? []) {
    if (!Number.isFinite(s.fraction)) continue
    if (best === null || s.fraction > best.fraction) best = s
  }
  return best
}

/** Whole percent, floored: 0.999 is "99%", never a rounded-up "100%" that
 *  would claim a limit is reached when it is not. */
export function percent(fraction: number): number {
  return Math.floor(fraction * 100 + 1e-9)
}

/** "Near limit: rpm 80%", "At limit: requests_cycle, resets in 3d", or null
 *  when nothing is near. */
export function limitText(s: LimitState | null, now: number): string | null {
  if (!s) return null
  const phase = limitPhase(s.fraction)
  if (phase === 'near') return `Near limit: ${s.limit_type} ${percent(s.fraction)}%`
  if (phase === 'at') {
    return s.resets_at == null
      ? `At limit: ${s.limit_type}`
      : `At limit: ${s.limit_type}, resets ${untilText(s.resets_at, now)}`
  }
  return null
}

/** The lamp one limit reads as: at is a lit warning, near a hollow one,
 *  under is live; no limit at all is a hollow idle. */
export function limitLamp(s: LimitState | null): { signal: 'live' | 'warn' | 'idle'; hollow: boolean; label: string } {
  if (s === null) return { signal: 'idle', hollow: true, label: 'no limits' }
  const phase = limitPhase(s.fraction)
  if (phase === 'at') return { signal: 'warn', hollow: false, label: 'at limit' }
  if (phase === 'near') return { signal: 'warn', hollow: true, label: 'near limit' }
  return { signal: 'live', hollow: false, label: 'under limit' }
}

/** A raise for this cycle is in force when the effective limit is not the
 *  configured one. */
export function raised(s: LimitState): boolean {
  return s.effective_limit !== s.limit
}

/** `(cycle_start, resets_at)` for an anchor, as `limits/cycle.py` computes it. */
export function cycleBounds(anchor: number, now: number): [number, number] {
  const span = CYCLE_DAYS * DAY_S
  const n = Math.max(0, Math.floor((now - anchor) / span))
  const start = anchor + n * span
  return [start, start + span]
}

// ── Lamp ─────────────────────────────────────────────────────────────────────

export type LampSignal = 'live' | 'warn' | 'fault' | 'idle'

export interface LampState {
  signal: LampSignal
  hollow: boolean
  label: string
}

/** active → live; near a limit → warn hollow; at a limit → warn; disabled →
 *  idle; expired → fault. A soft-deleted key is idle whatever it last was. */
export function lampFor(row: Pick<ApiKeyRow, 'status' | 'deleted_at' | 'limit_states'>): LampState {
  if (row.deleted_at != null) return { signal: 'idle', hollow: false, label: 'deleted' }
  if (row.status === 'disabled') return { signal: 'idle', hollow: false, label: 'disabled' }
  if (row.status === 'expired') return { signal: 'fault', hollow: false, label: 'expired' }
  const t = tightest(row.limit_states)
  const phase = t ? limitPhase(t.fraction) : 'ok'
  if (phase === 'at') return { signal: 'warn', hollow: false, label: 'active, at a limit' }
  if (phase === 'near') return { signal: 'warn', hollow: true, label: 'active, near a limit' }
  return { signal: 'live', hollow: false, label: 'active' }
}

// ── Time ─────────────────────────────────────────────────────────────────────

function span(seconds: number): string {
  const s = Math.max(0, Math.round(seconds))
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m`
  if (s < DAY_S) return `${Math.floor(s / 3600)}h`
  return `${Math.floor(s / DAY_S)}d`
}

/** "in 3d" for a future instant, "2h ago" for a past one. */
export function untilText(ts: number, now: number): string {
  return ts >= now ? `in ${span(ts - now)}` : `${span(now - ts)} ago`
}

export function agoText(ts: number | null | undefined, now: number): string {
  return ts == null ? EM : `${span(now - ts)} ago`
}

export function absTime(ts: number | null | undefined): string {
  return ts == null ? EM : new Date(ts * 1000).toLocaleString()
}

/** An admin's personal key and any key stored without an expiry never expire. */
export function expiresText(row: Pick<ApiKeyRow, 'expires_at' | 'is_admin_key'>, now: number): string {
  if (row.expires_at == null) return 'never'
  return row.expires_at < now ? `expired ${untilText(row.expires_at, now)}` : untilText(row.expires_at, now)
}

/** Last use: relative time plus the address, or "never used". The column is
 *  about an event, and a key nobody has sent is an answer, not a gap. */
export function lastUsedText(row: Pick<ApiKeyRow, 'last_used_at' | 'last_used_ip'>, now: number): string {
  if (row.last_used_at == null) return 'never used'
  return row.last_used_ip ? `${agoText(row.last_used_at, now)} · ${row.last_used_ip}` : agoText(row.last_used_at, now)
}

// ── Figures ──────────────────────────────────────────────────────────────────

/** A count. Null is an em dash; 0 is "0". */
export function count(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return EM
  return Math.round(v).toLocaleString('en-US')
}

/** Dollars. Null (nothing priced) is an em dash, never "$0.00". */
export function money(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return EM
  return `$${v.toFixed(Math.abs(v) < 1 ? 4 : 2)}`
}

/** A limit value or usage in its own unit: dollars for `*usd*`, counts
 *  otherwise. */
export function limitValue(limitType: string, v: number | null | undefined): string {
  return limitType.includes('usd') ? money(v) : count(v)
}

export interface UsageCells {
  requests: string
  tokens: string
  cost: string
}

/** The three 30-day columns. `usage_30d` null means the coordinator keeps no
 *  archive: every cell is an em dash. Inside it, 0 requests is a measurement
 *  and prints 0; a null cost means nothing was priced. */
export function usageCells(u: Usage30d | null | undefined): UsageCells {
  if (u == null) return { requests: EM, tokens: EM, cost: EM }
  return { requests: count(u.requests), tokens: count(u.tokens), cost: money(u.cost_usd) }
}

// ── Table rows ───────────────────────────────────────────────────────────────

export interface KeyTableRow {
  keyId: string
  name: string
  status: ApiKeyRow['status']
  deleted: boolean
  admin: boolean
  lamp: LampState
  tightestType: string | null
  tightestFraction: number | null
  limitText: string | null
  expires: string
  expiresAbs: string
  lastUsed: string
  usage: UsageCells
  createdBy: string
}

/** What the table renders for one `/api/keys` row. */
export function tableRow(row: ApiKeyRow, now: number): KeyTableRow {
  const t = tightest(row.limit_states)
  return {
    keyId: row.key_id,
    name: row.name,
    status: row.status,
    deleted: row.deleted_at != null,
    admin: row.is_admin_key,
    lamp: lampFor(row),
    tightestType: t ? t.limit_type : null,
    tightestFraction: t ? t.fraction : null,
    limitText: limitText(t, now),
    expires: expiresText(row, now),
    expiresAbs: absTime(row.expires_at),
    lastUsed: lastUsedText(row, now),
    usage: usageCells(row.usage_30d),
    createdBy: row.created_by,
  }
}

// ── Usage buckets ────────────────────────────────────────────────────────────

/** "error: not_reported (412 requests)" -- the server's code verbatim. */
export function chipText(code: string, n: number): string {
  return `error: ${code} (${count(n)} ${n === 1 ? 'request' : 'requests'})`
}

export interface FieldChip {
  field: string
  code: string
  n: number
  text: string
}

type ErrorCounts = Partial<Record<FieldError, number>>

function addCounts(into: Map<string, number>, counts: ErrorCounts | null | undefined) {
  for (const [code, n] of Object.entries(counts ?? {})) {
    if (typeof n === 'number' && n > 0) into.set(code, (into.get(code) ?? 0) + n)
  }
}

/** Why a measure is absent, summed over the window: one chip per (field,
 *  code). `cache_errors` explains the cache hit rate and `spec_errors` the
 *  spec-decode acceptance. */
export function fieldChips(rows: UsageBucket[]): FieldChip[] {
  const out: FieldChip[] = []
  const fields: [string, (b: UsageBucket) => ErrorCounts | null | undefined][] = [
    ['cache hit rate', (b) => b.cache_errors],
    ['spec-decode acceptance', (b) => b.spec_errors],
  ]
  for (const [field, pick] of fields) {
    const m = new Map<string, number>()
    for (const b of rows) addCounts(m, pick(b))
    for (const [code, n] of [...m.entries()].sort()) out.push({ field, code, n, text: chipText(code, n) })
  }
  return out
}

export const BUCKET_S: Record<string, number> = { '1m': 60, '1h': 3600, '1d': DAY_S }

/** One measure as a drawable series over `[from, to)`, one point per bucket
 *  step. A bucket the server left out (nothing reported it) is a null point,
 *  so the chart lifts its pen instead of drawing a line across the hole. Rows
 *  must be ungrouped (one per bucket). */
export function series(
  rows: UsageBucket[],
  pick: (b: UsageBucket) => number | null | undefined,
  from: number,
  to: number,
  bucket: string,
): TelemetryPoint[] {
  const step = BUCKET_S[bucket] ?? 3600
  const by = new Map<number, UsageBucket>()
  for (const r of rows) by.set(r.bucket, r)
  const out: TelemetryPoint[] = []
  for (let t = Math.floor(from / step) * step; t < to; t += step) {
    const r = by.get(t)
    const v = r ? pick(r) : null
    out.push({ t, v: v == null || !Number.isFinite(v) ? null : v })
  }
  return out
}

export const pctOf =
  (f: (b: UsageBucket) => Percentiles | null, p: 'p50' | 'p95') =>
  (b: UsageBucket): number | null =>
    f(b)?.[p] ?? null

export interface Breakdown {
  value: string
  requests: number
  failed: number
  tokensIn: number | null
  tokensOut: number | null
  cost: number | null
  cacheRead: number | null
  reasoning: number | null
  /** Requests no price covered, so `cost` (when not null) is a floor. */
  unpriced: number
}

/** A sum of measures that may be unreported: null until one is. */
export function addNullable(a: number | null, b: number | null | undefined): number | null {
  if (b == null) return a
  return (a ?? 0) + b
}

/** Grouped rows folded over the window, one entry per group value, busiest
 *  first. Sums of nullable measures stay null until something reported one.
 *  A group the rollups stored as `""` (no key, no provider, no context bucket
 *  -- telemetry/retention.py writes `or ""`) is the same absence as null and
 *  reads `—`, never an empty cell or a link to nothing. */
export function breakdown(rows: UsageBucket[], dim: string): Breakdown[] {
  const acc = new Map<string, Breakdown>()
  for (const r of rows) {
    const raw = r.group?.[dim]
    const value = raw == null || raw === '' ? EM : String(raw)
    const b = acc.get(value) ?? {
      value,
      requests: 0,
      failed: 0,
      tokensIn: null,
      tokensOut: null,
      cost: null,
      cacheRead: null,
      reasoning: null,
      unpriced: 0,
    }
    b.requests += r.requests
    b.failed += r.failed
    b.tokensIn = addNullable(b.tokensIn, r.tokens_in)
    b.tokensOut = addNullable(b.tokensOut, r.tokens_out)
    b.cost = addNullable(b.cost, r.cost_usd)
    b.cacheRead = addNullable(b.cacheRead, r.cache_read_tokens)
    b.reasoning = addNullable(b.reasoning, r.reasoning_tokens)
    b.unpriced += r.unpriced_requests
    acc.set(value, b)
  }
  return [...acc.values()].sort((a, b) => b.requests - a.requests || a.value.localeCompare(b.value))
}

/** Error classes summed over the window, most frequent first. */
export function errorClasses(rows: UsageBucket[]): [string, number][] {
  const m = new Map<string, number>()
  for (const r of rows) for (const [k, n] of Object.entries(r.errors ?? {})) m.set(k, (m.get(k) ?? 0) + n)
  return [...m.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
}
