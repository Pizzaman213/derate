// The request detail page's arithmetic and wording (`RequestDetail.tsx`),
// kept out of React so `requests.check.mjs` can run it in node: `check.mjs`
// bundles verifiers with `platform: 'neutral'` and cannot import a component.
//
// Nothing here invents a figure. Every number is a field of one
// `GET /api/requests` row; an absent one reads as an em dash or a sentence
// saying it was not recorded, never as 0.

import type { RequestRow } from '../../api/types'
import { count, money } from '../keys/rows'

type Row = RequestRow

export type Tone = 'live' | 'warn' | 'fault' | 'muted'

/** A duration: null is an em dash, under a second whole milliseconds, above
 *  it seconds (two decimals under ten seconds, one above). */
export function fmtMs(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms)) return '—'
  if (ms < 1000) return `${Math.round(ms)} ms`
  return `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)} s`
}

/** The status pill: the code and why it ended. */
export function statusPill(
  row: Pick<Row, 'status' | 'finish_reason' | 'error_code' | 'error_class'>,
): { text: string; tone: Tone } {
  const s = row.status
  if (s == null) return { text: 'no status', tone: 'muted' }
  const why = row.finish_reason || row.error_code || row.error_class
  const text = why ? `${s} · ${why}` : `${s}`
  const tone: Tone = s >= 200 && s < 300 ? 'live' : s === 429 ? 'warn' : s >= 400 ? 'fault' : 'muted'
  return { text, tone }
}

/** The device and where it is, split out of the label `telemetry/tailnet.py`
 *  writes for a tailnet peer (`peer.host` is Tailscale's HostName). Any other
 *  label -- a database city, a pinned place, nothing -- is null. */
export function clientDevice(
  label: string | null | undefined,
): { device: string; where: string; short: string } | null {
  if (!label) return null
  let m = /^(.+) \(now near the (.+) relay\)$/.exec(label)
  if (m) return { device: m[1]!, where: `near ${m[2]} relay`, short: `near ${m[2]}` }
  m = /^(.+) \(now in (.+)\)$/.exec(label)
  if (m) return { device: m[1]!, where: `in ${m[2]}`, short: `in ${m[2]}` }
  m = /^(.+), now on this coordinator's network$/.exec(label)
  if (m) {
    const where = "on this coordinator's network"
    return { device: m[1]!, where, short: where }
  }
  return null
}

const BROWSERS: [RegExp, string][] = [
  [/Edg\/(\d+)/, 'Edge'],
  [/OPR\/(\d+)/, 'Opera'],
  [/Chrome\/(\d+)/, 'Chrome'],
  [/Firefox\/(\d+)/, 'Firefox'],
  [/Version\/(\d+)[^ ]*.*Safari\//, 'Safari'],
]

const SYSTEMS: [RegExp, string][] = [
  [/Macintosh/, 'macOS'],
  [/Windows NT/, 'Windows'],
  [/Android/, 'Android'],
  [/iPhone|iPad/, 'iOS'],
  [/CrOS/, 'ChromeOS'],
  [/Linux/, 'Linux'],
]

/** "Chrome 157 on macOS" for a browser; any other client (an SDK, a CLI)
 *  reads as its own user agent, verbatim. */
export function describeUserAgent(ua: string | null | undefined): string {
  if (!ua) return '—'
  for (const [re, browser] of BROWSERS) {
    const m = re.exec(ua)
    if (!m) continue
    const os = SYSTEMS.find(([r]) => r.test(ua))?.[1]
    return os ? `${browser} ${m[1]} on ${os}` : `${browser} ${m[1]}`
  }
  return ua
}

export interface TimeSegment {
  key: 'queue' | 'prefill' | 'decode' | 'total'
  label: string
  ms: number
}

export interface Tick {
  ms: number
  /** Percent along the bar. */
  at: number
  text: string
}

/** The largest of 1, 2 or 5 times a power of ten that is no more than x. */
function nice(x: number): number {
  const base = 10 ** Math.floor(Math.log10(x))
  for (const m of [5, 2, 1]) if (m * base <= x) return m * base
  return base
}

/** Where the wall time went: parked, then prefill to the first token, then
 *  decode as the remainder of `duration_ms` -- `decode_ms` overlaps the first
 *  token by up to one chunk, so the remainder is what makes the segments sum
 *  to the total. `duration_ms` excludes parking, so the total adds it back.
 *  Null when nothing was timed. */
export function timeSplit(
  row: Pick<Row, 'duration_ms' | 'ttft_ms' | 'parked_ms'>,
): { total: number; segments: TimeSegment[]; ticks: Tick[] } | null {
  const d = row.duration_ms
  if (d == null || !Number.isFinite(d)) return null
  const total = d + (row.parked_ms ?? 0)
  if (total <= 0) return null
  const segments: TimeSegment[] = []
  if (row.parked_ms != null) segments.push({ key: 'queue', label: 'queued', ms: row.parked_ms })
  if (row.ttft_ms != null) {
    segments.push({ key: 'prefill', label: 'prefill to first token', ms: row.ttft_ms })
    segments.push({ key: 'decode', label: 'decode', ms: Math.max(0, d - row.ttft_ms) })
  } else {
    segments.push({ key: 'total', label: 'total (first token not recorded)', ms: d })
  }
  const step = nice(total / 4)
  const ticks: Tick[] = []
  for (let ms = 0; ms < total * 0.92; ms += step) {
    ticks.push({ ms, at: (ms / total) * 100, text: String(Math.round(ms)) })
  }
  ticks.push({ ms: total, at: 100, text: fmtMs(total) })
  return { total, segments, ticks }
}

export interface TokenSegment {
  key: 'cached' | 'new' | 'output'
  label: string
  n: number
}

/** Prompt (cached and new, when the cache was reported) against output.
 *  Null when no token was counted. */
export function tokenSplit(
  row: Pick<Row, 'prompt_tokens' | 'completion_tokens' | 'cache_read_tokens'>,
): { total: number; segments: TokenSegment[] } | null {
  const prompt = row.prompt_tokens ?? 0
  const output = row.completion_tokens ?? 0
  if (prompt + output === 0) return null
  const all: TokenSegment[] =
    row.cache_read_tokens != null
      ? [
          { key: 'cached', label: 'prompt, cached', n: row.cache_read_tokens },
          { key: 'new', label: 'prompt, new', n: Math.max(0, prompt - row.cache_read_tokens) },
        ]
      : [{ key: 'new', label: 'prompt', n: prompt }]
  all.push({ key: 'output', label: 'output', n: output })
  const segments = all.filter((s) => s.n > 0)
  return { total: segments.reduce((a, s) => a + s.n, 0), segments }
}

/** The prompt cache's share, in percent, and the line under it. The server's
 *  own `prefix_cache_hit_rate` wins; else read over prompt. */
export function cacheHit(
  row: Pick<Row, 'prompt_tokens' | 'cache_read_tokens' | 'cache_write_tokens' | 'cache_error' | 'prefix_cache_hit_rate'>,
): { pct: number | null; caption: string } {
  const read = row.cache_read_tokens
  const prompt = row.prompt_tokens
  const rate =
    row.prefix_cache_hit_rate ?? (read != null && prompt != null && prompt > 0 ? read / prompt : null)
  const caption =
    read == null
      ? row.cache_error === 'not_reported'
        ? 'cache not reported by the server'
        : (row.cache_error ?? 'cache not reported')
      : `${count(read)} of ${count(prompt)} read · write ${count(row.cache_write_tokens)}`
  return { pct: rate == null ? null : rate * 100, caption }
}

/** What this request cost, and why when it cost nothing. `priceFor` is the
 *  local model Settings can price, when a missing local rate is the reason. */
export function costLine(
  row: Pick<Row, 'cost_usd' | 'cost_basis' | 'target_kind' | 'served_name'>,
): { value: string; note: string | null; priceFor: string | null } {
  if (row.cost_usd != null) return { value: money(row.cost_usd), note: row.cost_basis ?? null, priceFor: null }
  if (row.cost_basis === 'unpriced' && row.target_kind === 'local') {
    return { value: 'Unpriced', note: `No local rate for ${row.served_name}.`, priceFor: row.served_name }
  }
  if (row.cost_basis === 'unpriced') {
    return { value: 'Unpriced', note: 'The provider has no published price for this model.', priceFor: null }
  }
  return { value: 'Not priced', note: 'The request did not complete, so nothing was charged.', priceFor: null }
}

/** "1, no retries or fallback", or the count with what made it more. */
export function attemptsLine(
  row: Pick<Row, 'attempts' | 'retry_reason' | 'fallback_used' | 'fallback_reason'>,
): string {
  if (row.attempts <= 1 && !row.fallback_used) return '1, no retries or fallback'
  return (
    `${row.attempts}` +
    (row.retry_reason ? `, retried: ${row.retry_reason}` : '') +
    (row.fallback_used ? `, fallback: ${row.fallback_reason || 'used'}` : '')
  )
}

/** The summary strip: who got what from where, then how it was served.
 *  `where` is a short place phrase for the client, `providerName` the
 *  display name of a remote target's provider. */
export function headline(row: Row, ctx: { where: string | null; providerName: string | null }): string {
  const first =
    `${row.key_name ?? 'A caller with no key'}${ctx.where ? `, ${ctx.where},` : ''} ` +
    `got ${row.status != null ? `a ${row.status}` : 'no answer'}` +
    (row.served_name ? ` from ${row.served_name}` : '') +
    (row.node_id ? ` on ${row.node_id}` : '') +
    (row.duration_ms != null ? ` in ${fmtMs(row.duration_ms)}` : '') +
    '.'
  if (row.admission_code && !row.target_id) return `${first} Refused at admission: ${row.admission_code}.`
  const tries = row.attempts <= 1 ? 'on the first try' : `after ${row.attempts} attempts`
  const served =
    row.target_kind === 'local'
      ? `Served locally ${tries}`
      : row.target_kind === 'remote'
        ? `Served by ${ctx.providerName ?? row.provider_id} ${tries}`
        : null
  const pct = cacheHit(row).pct
  // A nonzero read never rounds to 0% (same rule as servedBy.ts::cachedShare).
  const share = pct == null ? null : pct > 0 && pct < 1 ? '<1%' : `${Math.round(pct)}%`
  const cache = share != null ? `${share} of the prompt came from cache` : null
  const prefill =
    row.ttft_ms != null && row.duration_ms
      ? `${Math.round((row.ttft_ms / row.duration_ms) * 100)}% of the time was prefill`
      : null
  const c = [served, cache, prefill].filter((x): x is string => !!x)
  if (c.length === 0) return first
  const second =
    c.length === 1 ? `${c[0]}.` : c.length === 2 ? `${c[0]}, and ${c[1]}.` : `${c[0]}, ${c[1]}, and ${c[2]}.`
  return `${first} ${second}`
}

/** Every field of the row in the order the server sent it, as text. */
export function rawFields(row: Row): [string, string][] {
  return Object.entries(row).map(([k, v]) => [
    k,
    v == null || v === '' ? '—' : typeof v === 'object' ? JSON.stringify(v) : String(v),
  ])
}
