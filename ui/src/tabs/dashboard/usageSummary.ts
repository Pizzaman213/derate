// The dashboard Overview's folds: `GET /api/usage` rows (1d buckets, grouped by
// model and provider, or by key) and the key list, into tiles and chart rows.
// Pure, so `usageSummary.check.mjs` runs it in node.
//
// The rule is `keys/rows.ts`'s: a figure nobody reported is null, never 0.
// A day with no rollup at all is the exception and is 0 -- every request goes
// through this gateway, so a day it recorded nothing for served nothing.

import { errorKind } from '../../api/errorKinds'
import type { UsageBucket } from '../../api/types'
import type { PackRow, PackTrendRow } from '../../components/PackChart'
import { addNullable, EM } from '../keys/rows'

export const USAGE_DAYS = [7, 30, 90] as const
export type UsageDays = (typeof USAGE_DAYS)[number]
export const DEFAULT_DAYS: UsageDays = 30
/** Models drawn by name; the rest fold into OTHER. */
export const MODEL_SEGMENTS = 5
export const OTHER = 'other'
export const LOCAL = 'local'
export const CLOUD = 'cloud'

const DAY_S = 86400

/** `start` for /api/usage: today plus days-1 before it, so the 1d floor
 *  yields exactly `days` UTC days. */
export function usageStart(days: UsageDays): string {
  return `-${days - 1}d`
}

/** UTC day starts (epoch s), oldest first, `days` of them, ending at the day
 *  `to` falls in. */
export function dayStarts(to: number, days: number): number[] {
  const last = Math.floor(to / DAY_S) * DAY_S
  return Array.from({ length: days }, (_, i) => last - (days - 1 - i) * DAY_S)
}

const DAY_FMT = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' })

/** "Nov 18". UTC, because the buckets are UTC days. */
export function dayLabel(dayStart: number): string {
  return DAY_FMT.format(new Date(dayStart * 1000))
}

/** tokens_in + tokens_out; null when both are null. */
export function rowTokens(r: UsageBucket): number | null {
  return addNullable(addNullable(null, r.tokens_in), r.tokens_out)
}

/** A row served by a local target: the rollups store a local request's
 *  provider as `""`; null is accepted too (`spend/usage.ts::isLocal`). */
function sideOf(r: UsageBucket): typeof LOCAL | typeof CLOUD {
  return r.group?.provider ? CLOUD : LOCAL
}

function modelOf(r: UsageBucket): string {
  const m = r.group?.model
  return m == null || m === '' ? EM : String(m)
}

/** One row per day × [LOCAL, CLOUD], always both and in that order, which
 *  fixes their colours. A day absent from the rows is 0; a day with rows
 *  whose tokens are all null is null (unreported, not zero). */
export function tokensPerDay(rows: UsageBucket[], days: number[]): PackRow[] {
  const sums = new Map<string, { seen: boolean; tokens: number | null }>()
  for (const r of rows) {
    const k = `${Math.floor(r.bucket / DAY_S) * DAY_S}|${sideOf(r)}`
    const cur = sums.get(k) ?? { seen: true, tokens: null }
    cur.tokens = addNullable(cur.tokens, rowTokens(r))
    sums.set(k, cur)
  }
  return days.flatMap((d) =>
    [LOCAL, CLOUD].map((side) => {
      const s = sums.get(`${d}|${side}`)
      return { label: dayLabel(d), series: side, value: s ? s.tokens : 0 }
    }),
  )
}

/** Two bars, 'Tokens' and 'Requests', series [LOCAL, CLOUD]. A side's tokens
 *  are null when nothing on it reported any. */
export function localCloudShare(rows: UsageBucket[]): PackRow[] {
  const tokens = { [LOCAL]: null as number | null, [CLOUD]: null as number | null }
  const requests = { [LOCAL]: 0, [CLOUD]: 0 }
  const seen = new Set<string>()
  for (const r of rows) {
    const side = sideOf(r)
    seen.add(side)
    tokens[side] = addNullable(tokens[side], rowTokens(r))
    requests[side] += r.requests
  }
  // A side with no rows served nothing: 0, not unreported.
  for (const side of [LOCAL, CLOUD] as const) if (!seen.has(side)) tokens[side] = 0
  return [
    { label: 'Tokens', series: LOCAL, value: tokens[LOCAL] },
    { label: 'Tokens', series: CLOUD, value: tokens[CLOUD] },
    { label: 'Requests', series: LOCAL, value: requests[LOCAL] },
    { label: 'Requests', series: CLOUD, value: requests[CLOUD] },
  ]
}

/** Models ranked by tokens (models with no reported tokens rank after, by
 *  requests). The top MODEL_SEGMENTS are drawn by name and the rest fold into
 *  OTHER, emitted only when something folded. Labels 'Tokens' and 'Requests'. */
export function modelShare(rows: UsageBucket[]): PackRow[] {
  const byModel = new Map<string, { tokens: number | null; requests: number }>()
  for (const r of rows) {
    const m = modelOf(r)
    const cur = byModel.get(m) ?? { tokens: null, requests: 0 }
    cur.tokens = addNullable(cur.tokens, rowTokens(r))
    cur.requests += r.requests
    byModel.set(m, cur)
  }
  const ranked = [...byModel.entries()].sort(([an, a], [bn, b]) => {
    if (a.tokens != null && b.tokens != null && a.tokens !== b.tokens) return b.tokens - a.tokens
    if ((a.tokens == null) !== (b.tokens == null)) return a.tokens == null ? 1 : -1
    return b.requests - a.requests || an.localeCompare(bn)
  })
  const kept = ranked.slice(0, MODEL_SEGMENTS)
  const rest = ranked.slice(MODEL_SEGMENTS)
  const segments = kept.map(([name, v]) => ({ name, ...v }))
  if (rest.length > 0) {
    segments.push({
      name: OTHER,
      tokens: rest.reduce<number | null>((t, [, v]) => addNullable(t, v.tokens), null),
      requests: rest.reduce((n, [, v]) => n + v.requests, 0),
    })
  }
  return [
    ...segments.map((s) => ({ label: 'Tokens', series: s.name, value: s.tokens })),
    ...segments.map((s) => ({ label: 'Requests', series: s.name, value: s.requests })),
  ]
}

export interface UsageHeadline {
  tokens: number | null
  tokensIn: number | null
  tokensOut: number | null
  requests: number
  /** Cloud tokens / all tokens, 0..1; null when total tokens is null or 0. */
  cloudShare: number | null
  /** Distinct model names with requests > 0. */
  models: number
  /** Requests that failed (every error class). */
  failed: number
  /** Summed `cost_usd`; null until something in the window was priced. */
  cost: number | null
  /** Priced dollars with unpriced requests beside them: the cost is a floor. */
  costFloor: boolean
  /** Cache-read prompt tokens over all prompt tokens, 0..1; null when either is
   *  unreported or no prompt tokens were counted. */
  cachedShare: number | null
  /** Requests that were retried on another target (`fallback_rate × requests`,
   *  which is the server's own count). */
  fallbacks: number
}

export function headline(rows: UsageBucket[]): UsageHeadline {
  let tokensIn: number | null = null
  let tokensOut: number | null = null
  let cloud: number | null = null
  let cacheRead: number | null = null
  let cost: number | null = null
  let requests = 0
  let failed = 0
  let fallbacks = 0
  let unpriced = 0
  const models = new Set<string>()
  for (const r of rows) {
    tokensIn = addNullable(tokensIn, r.tokens_in)
    tokensOut = addNullable(tokensOut, r.tokens_out)
    cacheRead = addNullable(cacheRead, r.cache_read_tokens)
    cost = addNullable(cost, r.cost_usd)
    if (sideOf(r) === CLOUD) cloud = addNullable(cloud, rowTokens(r))
    requests += r.requests
    failed += r.failed
    fallbacks += Math.round((r.fallback_rate ?? 0) * r.requests)
    unpriced += r.unpriced_requests
    if (r.requests > 0) models.add(modelOf(r))
  }
  const tokens = addNullable(tokensIn, tokensOut)
  return {
    tokens,
    tokensIn,
    tokensOut,
    requests,
    cloudShare: tokens == null || tokens === 0 ? null : (cloud ?? 0) / tokens,
    models: models.size,
    failed,
    cost,
    costFloor: cost != null && unpriced > 0,
    cachedShare: cacheRead == null || tokensIn == null || tokensIn === 0 ? null : cacheRead / tokensIn,
    fallbacks,
  }
}

/** Error classes drawn as their own series in "Failures per day". */
export const ERROR_SERIES = 4
export const OTHER_ERRORS = 'other errors'
/** The one series drawn when nothing failed, so the chart is a zero line. */
export const FAILED = 'failed'

/** One row per day × error class, as `multi-line` rows (`x` the UTC date): the
 *  ERROR_SERIES classes with the most failures over the window, the rest
 *  folded into OTHER_ERRORS (a series only when something folded). Each class
 *  is named in plain words with its code (`errorKind`), so the legend and the
 *  hover say what went wrong; an empty class is 'unclassified'. A day with no
 *  failures is 0, and a window with none
 *  is the single series FAILED at 0 on every day -- a flat line says "nothing
 *  failed" as a chart. */
export function failuresPerDay(rows: UsageBucket[], days: number[]): PackTrendRow[] {
  const classOf = errorKind
  const totals = new Map<string, number>()
  for (const r of rows) {
    for (const [cls, n] of Object.entries(r.errors ?? {})) totals.set(classOf(cls), (totals.get(classOf(cls)) ?? 0) + n)
  }
  const ranked = [...totals.entries()].filter(([, n]) => n > 0).sort(([an, a], [bn, b]) => b - a || an.localeCompare(bn))
  const named = new Set(ranked.slice(0, ERROR_SERIES).map(([c]) => c))
  const series = ranked.length === 0 ? [FAILED] : [...named, ...(ranked.length > ERROR_SERIES ? [OTHER_ERRORS] : [])]
  const cells = new Map<string, number>()
  for (const r of rows) {
    const day = Math.floor(r.bucket / DAY_S) * DAY_S
    for (const [cls, n] of Object.entries(r.errors ?? {})) {
      const s = named.has(classOf(cls)) ? classOf(cls) : OTHER_ERRORS
      cells.set(`${day}|${s}`, (cells.get(`${day}|${s}`) ?? 0) + n)
    }
  }
  return days.flatMap((d) => series.map((s) => ({ x: dayIso(d), series: s, y: cells.get(`${d}|${s}`) ?? 0 })))
}

/** "2026-11-18": a day start as the date the pack's time axis reads (UTC). */
function dayIso(dayStart: number): string {
  return new Date(dayStart * 1000).toISOString().slice(0, 10)
}

export const OTHER_KEYS = 'other keys'
export const NO_KEY = 'no key'

/** Keys drawn as their own series in "Requests per day by key". */
export const KEY_SERIES = 5

/** One row per day × key series: the KEY_SERIES keys with the most requests
 *  over the window (labelled by key name from `names`, else the id; a null key
 *  is NO_KEY), the rest folded into
 *  OTHER_KEYS, which is a series only when something folded. Series order is
 *  fixed (most requests first) so colours hold across days; a day a key sent
 *  nothing is 0. */
export function requestsPerDayByKey(rows: UsageBucket[], days: number[], names: ReadonlyMap<string, string>): PackRow[] {
  const labelOf = (r: UsageBucket) => {
    const id = r.group?.key_id
    return id == null || id === '' ? NO_KEY : names.get(String(id)) ?? String(id)
  }
  const totals = new Map<string, number>()
  for (const r of rows) totals.set(labelOf(r), (totals.get(labelOf(r)) ?? 0) + r.requests)
  const ranked = [...totals.entries()].filter(([, n]) => n > 0).sort(([an, a], [bn, b]) => b - a || an.localeCompare(bn))
  const named = new Set(ranked.slice(0, KEY_SERIES).map(([k]) => k))
  const series = [...named, ...(ranked.length > KEY_SERIES ? [OTHER_KEYS] : [])]
  const cells = new Map<string, number>()
  for (const r of rows) {
    const k = labelOf(r)
    const s = named.has(k) ? k : OTHER_KEYS
    const cell = `${Math.floor(r.bucket / DAY_S) * DAY_S}|${s}`
    cells.set(cell, (cells.get(cell) ?? 0) + r.requests)
  }
  return days.flatMap((d) => series.map((s) => ({ label: dayLabel(d), series: s, value: cells.get(`${d}|${s}`) ?? 0 })))
}
