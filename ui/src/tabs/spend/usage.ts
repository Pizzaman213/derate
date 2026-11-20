// The spend screen's window: who spent what over `?win=`, folded out of
// `GET /api/usage`. Pure, so `usage.check.mjs` runs it in node.
//
// The rule is `rows.ts`'s: a figure nobody reported is null and renders as an
// em dash, never 0. `/api/usage` already says so per bucket -- every optional
// measure is null when nothing reported it, `cost_usd` is null when nothing in
// the bucket was priced -- and every sum here keeps that until something does.
// A window with priced dollars AND unpriced requests under them is a floor
// (`≥`), exactly as the cloud tiles mark it.

import type { LimitState, UsageBucket } from '../../api/types'
import type { SpendWindow } from '../../state/routes'
import type { TelemetryPoint } from '../../state/useTelemetry'
import { addNullable, EM, percent, series, tightest, type Breakdown } from '../keys/rows'
import { money } from './rows'

/** What `/usage` with no `?win=` draws: everything the archive still holds. */
export const DEFAULT_WINDOW: SpendWindow = 'all'
/** Rows a table shows before "show all". */
export const ROW_CAP = 25

/** The `start` string `/api/usage` takes. `all` is `-400d`, the 1d rollups'
 *  retention (`telemetry/config.py`), so it asks for exactly what is kept. */
export function startFor(win: SpendWindow): string {
  return win === 'all' ? '-400d' : `-${win}`
}

/** `24h` at hour resolution, everything longer per day. */
export function bucketFor(win: SpendWindow): '1h' | '1d' {
  return win === '24h' ? '1h' : '1d'
}

/** The words the headings use. */
export function windowLabel(win: SpendWindow): string {
  return win === 'all' ? 'all time' : `last ${win}`
}

// ── Totals ───────────────────────────────────────────────────────────────────

export interface WindowTotals {
  requests: number
  localRequests: number
  cloudRequests: number
  tokensIn: number | null
  tokensOut: number | null
  cacheRead: number | null
  /** cacheRead / tokensIn; null when either is null or tokensIn is 0. */
  cachedShare: number | null
  cost: number | null
  metered: number | null
  estimated: number | null
  localUnpriced: number
  cloudUnpriced: number
  /** Priced dollars with unpriced requests under them: the cost is a floor. */
  floor: boolean
}

/** A row served by a local target. The rollups store a local request's
 *  provider as `""` (telemetry/retention.py writes `provider_id or ""`), and
 *  that is what `group.provider` carries on the wire; null is accepted too. */
function isLocal(r: UsageBucket): boolean {
  return !r.group?.provider
}

/** Rows from `group_by: ['provider']`. Sums are null until something reported
 *  them. */
export function windowTotals(rows: UsageBucket[]): WindowTotals {
  const t: WindowTotals = {
    requests: 0,
    localRequests: 0,
    cloudRequests: 0,
    tokensIn: null,
    tokensOut: null,
    cacheRead: null,
    cachedShare: null,
    cost: null,
    metered: null,
    estimated: null,
    localUnpriced: 0,
    cloudUnpriced: 0,
    floor: false,
  }
  for (const r of rows) {
    const local = isLocal(r)
    t.requests += r.requests
    if (local) {
      t.localRequests += r.requests
      t.localUnpriced += r.unpriced_requests
    } else {
      t.cloudRequests += r.requests
      t.cloudUnpriced += r.unpriced_requests
    }
    t.tokensIn = addNullable(t.tokensIn, r.tokens_in)
    t.tokensOut = addNullable(t.tokensOut, r.tokens_out)
    t.cacheRead = addNullable(t.cacheRead, r.cache_read_tokens)
    t.cost = addNullable(t.cost, r.cost_usd)
    t.metered = addNullable(t.metered, r.metered_usd)
    t.estimated = addNullable(t.estimated, r.estimated_usd)
  }
  t.cachedShare = share(t.cacheRead, t.tokensIn)
  t.floor = t.cost != null && t.localUnpriced + t.cloudUnpriced > 0
  return t
}

/** Cost per bucket for one side of the fleet over `[from, to)`, a hole as a
 *  null point (`series`' contract). Rows are grouped by provider, so a bucket
 *  is folded across the providers on its side first. */
export function costSeries(
  rows: UsageBucket[],
  kind: 'local' | 'cloud',
  from: number,
  to: number,
  bucket: string,
): TelemetryPoint[] {
  const side = rows.filter((r) => isLocal(r) === (kind === 'local'))
  const folded = new Map<number, number | null>()
  for (const r of side) folded.set(r.bucket, addNullable(folded.get(r.bucket) ?? null, r.cost_usd))
  return series(side, (b) => folded.get(b.bucket), from, to, bucket)
}

/** The muted lines under the spend tile: what the total leaves out, and for
 *  local traffic, what to do about it. */
export function totalsNotes(t: WindowTotals): string[] {
  const notes: string[] = []
  // `2 local requests`, `1 cloud request`.
  const n = (v: number, side: string) =>
    `${v.toLocaleString('en-US')} ${side} ${v === 1 ? 'request' : 'requests'}`
  if (t.localUnpriced > 0) {
    notes.push(`${n(t.localUnpriced, 'local')} unpriced: set a rate for the model under Settings → Pricing`)
  }
  if (t.cloudUnpriced > 0) {
    notes.push(`${n(t.cloudUnpriced, 'cloud')} went to models the provider publishes no price for`)
  }
  return notes
}

/** `metered $1.20 · estimated $0.3400`: only the halves somebody reported;
 *  null when neither was. */
export function totalsBasis(t: Pick<WindowTotals, 'metered' | 'estimated'>): string | null {
  const parts: string[] = []
  if (t.metered != null) parts.push(`metered ${money(t.metered)}`)
  if (t.estimated != null) parts.push(`estimated ${money(t.estimated)}`)
  return parts.length ? parts.join(' · ') : null
}

// ── Tables ───────────────────────────────────────────────────────────────────

/** Cost desc (unpriced last), then requests desc, then value. */
export function bySpend(rows: Breakdown[]): Breakdown[] {
  return [...rows].sort(
    (a, b) =>
      (b.cost ?? -1) - (a.cost ?? -1) || b.requests - a.requests || a.value.localeCompare(b.value),
  )
}

/** The first ROW_CAP rows unless expanded. */
export function capRows<T>(rows: T[], expanded: boolean): T[] {
  return expanded ? rows : rows.slice(0, ROW_CAP)
}

/** 0..1 share of `total`; null when either is null or total is 0. */
export function share(part: number | null, total: number | null): number | null {
  if (part == null || total == null || total === 0) return null
  return part / total
}

/** `42% of spend · 60% of requests`; each half is `—` when its share is null. */
export function shareTitle(spend: number | null, requests: number | null): string {
  const pct = (v: number | null) => (v == null ? EM : `${percent(v)}%`)
  return `${pct(spend)} of spend · ${pct(requests)} of requests`
}

/** `≥` applies when the row has priced dollars AND unpriced requests under them. */
export function floorOf(b: Breakdown): boolean {
  return b.cost != null && b.unpriced > 0
}

/** `12% of input cached`; null when the share is. The Cached cell's tooltip. */
export function cachedTitle(cacheRead: number | null, tokensIn: number | null): string | null {
  const s = share(cacheRead, tokensIn)
  return s == null ? null : `${percent(s)}% of input cached`
}

/** The configured dollar limit closest to biting. */
export function budgetOf(states: LimitState[] | null | undefined): LimitState | null {
  return tightest((states ?? []).filter((s) => s.limit_type.includes('usd')))
}

/** `40% of $50.00 local_usd_cycle`, or `—` with no dollar limit. */
export function budgetText(s: LimitState | null): string {
  if (s == null) return EM
  return `${percent(s.fraction)}% of ${money(s.effective_limit)} ${s.limit_type}`
}

export type ModelKind = 'local' | 'cloud' | 'local + cloud'

/** Where each served model ran, from rows grouped by `['model', 'provider']`.
 *  Keyed the way `breakdown` keys a row, so a null or `""` model is `—` in both. */
export function kindByModel(rows: UsageBucket[]): Map<string, ModelKind> {
  const sides = new Map<string, Set<'local' | 'cloud'>>()
  for (const r of rows) {
    const raw = r.group?.model
    const model = raw == null || raw === '' ? EM : String(raw)
    const set = sides.get(model) ?? new Set()
    set.add(isLocal(r) ? 'local' : 'cloud')
    sides.set(model, set)
  }
  const out = new Map<string, ModelKind>()
  for (const [model, set] of sides) {
    out.set(model, set.size === 2 ? 'local + cloud' : set.has('local') ? 'local' : 'cloud')
  }
  return out
}
