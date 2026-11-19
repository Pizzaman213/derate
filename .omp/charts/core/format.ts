// Locale-aware formatting: numbers with SI suffixes (1.2k, 3.4M), currency, percent,
// and dates at a granularity in a time zone. One Formatters object per render.

import { format as d3format } from 'd3-format'

export type DateGranularity = 'minute' | 'hour' | 'day' | 'week' | 'month' | 'quarter' | 'year'

export interface Formatters {
  locale: string
  timeZone: string
  /** Plain number with grouping, up to `digits` fraction digits. */
  num(v: number | null | undefined, digits?: number): string
  /** Compact: 1.2k, 3.4M, 5B, 1.1T (locale decimal separator). */
  si(v: number | null | undefined): string
  /** Fraction (0.25) -> "25%". */
  pct(v: number | null | undefined, digits?: number): string
  currency(v: number | null | undefined, compact?: boolean): string
  integer(v: number | null | undefined): string
  /** The option-driven value format: auto | si | currency | percent | integer | a d3-format specifier. */
  value(v: number | null | undefined): string
  /** Date at a granularity ("Mar 2026", "Q1 2026", "Mar 3", "14:05"). */
  date(d: Date | null | undefined, g?: DateGranularity): string
  /** Full date-time for tooltips. */
  dateTime(d: Date | null | undefined): string
  /** Any value: numbers by `value`, dates by `dateTime`, rest as text. */
  any(v: unknown): string
}

const DASH = '–'
const SI: Array<[number, string]> = [[1e12, 'T'], [1e9, 'B'], [1e6, 'M'], [1e3, 'k']]

const cache = new Map<string, Intl.NumberFormat | Intl.DateTimeFormat>()
/** A cached Intl.NumberFormat (constructing one costs ~50x formatting with it). */
export function nf(locale: string, o: Intl.NumberFormatOptions): Intl.NumberFormat {
  const k = 'n' + locale + JSON.stringify(o)
  let f = cache.get(k) as Intl.NumberFormat | undefined
  if (!f) { f = new Intl.NumberFormat(locale, o); cache.set(k, f) }
  return f
}
/** A cached Intl.DateTimeFormat. */
export function df(locale: string, o: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const k = 'd' + locale + JSON.stringify(o)
  let f = cache.get(k) as Intl.DateTimeFormat | undefined
  if (!f) { f = new Intl.DateTimeFormat(locale, o); cache.set(k, f) }
  return f
}
const bad = (v: number | null | undefined): v is null | undefined => v === null || v === undefined || !Number.isFinite(v)

/** Build the formatters for a locale / currency / time zone / value format. */
export function makeFormatters(opts: { locale?: string; currency?: string; timeZone?: string; valueFormat?: string } = {}): Formatters {
  const locale = safeLocale(opts.locale)
  const timeZone = opts.timeZone ?? 'UTC'
  const currencyCode = opts.currency ?? 'USD'
  const num = (v: number | null | undefined, digits = 2) => (bad(v) ? DASH : nf(locale, { maximumFractionDigits: digits }).format(v))
  const si = (v: number | null | undefined) => {
    if (bad(v)) return DASH
    const a = Math.abs(v)
    for (const [n, s] of SI) {
      if (a >= n * 0.9995) {
        const m = v / n
        return nf(locale, { maximumFractionDigits: Math.abs(m) < 10 ? 1 : 0 }).format(m) + s
      }
    }
    return nf(locale, { maximumFractionDigits: a < 1 ? 3 : a < 10 ? 2 : 1 }).format(v)
  }
  const pct = (v: number | null | undefined, digits = 0) =>
    bad(v) ? DASH : nf(locale, { style: 'percent', maximumFractionDigits: digits, minimumFractionDigits: 0 }).format(v)
  const currency = (v: number | null | undefined, compact = false) => {
    if (bad(v)) return DASH
    if (compact && Math.abs(v) >= 1e4) {
      const sym = nf(locale, { style: 'currency', currency: currencyCode, maximumFractionDigits: 0 }).formatToParts(0).find((p) => p.type === 'currency')?.value ?? ''
      return (v < 0 ? '-' : '') + sym + si(Math.abs(v))
    }
    return nf(locale, { style: 'currency', currency: currencyCode, minimumFractionDigits: 0, maximumFractionDigits: Math.abs(v) >= 100 || Number.isInteger(v) ? 0 : 2 }).format(v)
  }
  const integer = (v: number | null | undefined) => (bad(v) ? DASH : nf(locale, { maximumFractionDigits: 0 }).format(v))
  const vf = opts.valueFormat ?? 'auto'
  let custom: ((n: number) => string) | null = null
  if (!['auto', 'si', 'currency', 'percent', 'integer', 'number'].includes(vf)) {
    try { custom = d3format(vf) } catch { custom = null }
  }
  const value = (v: number | null | undefined) => {
    if (bad(v)) return DASH
    switch (vf) {
      case 'si': return si(v)
      case 'currency': return currency(v, true)
      case 'percent': return pct(v, Math.abs(v) < 0.1 ? 1 : 0)
      case 'integer': return integer(v)
      case 'number': return num(v)
      case 'auto': return Math.abs(v) >= 1e4 ? si(v) : num(v, Math.abs(v) < 1 ? 3 : 2)
      default: return custom ? custom(v) : num(v)
    }
  }
  const date = (d: Date | null | undefined, g: DateGranularity = 'day') => {
    if (!d || Number.isNaN(+d)) return DASH
    const tz = { timeZone }
    switch (g) {
      case 'year': return df(locale, { ...tz, year: 'numeric' }).format(d)
      case 'quarter': {
        const parts = df(locale, { ...tz, year: 'numeric', month: 'numeric' }).formatToParts(d)
        const m = +(parts.find((p) => p.type === 'month')?.value ?? '1')
        return `Q${Math.floor((m - 1) / 3) + 1} ${parts.find((p) => p.type === 'year')?.value ?? ''}`
      }
      case 'month': return df(locale, { ...tz, month: 'short', year: 'numeric' }).format(d)
      case 'week':
      case 'day': return df(locale, { ...tz, month: 'short', day: 'numeric' }).format(d)
      case 'hour':
      case 'minute': return df(locale, { ...tz, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(d)
    }
  }
  const dateTime = (d: Date | null | undefined) => {
    if (!d || Number.isNaN(+d)) return DASH
    const hasTime = (() => {
      const p = df('en-US', { timeZone, hour: 'numeric', minute: 'numeric', second: 'numeric', hourCycle: 'h23' }).format(d)
      return p !== '00:00:00' && p !== '24:00:00'
    })()
    return df(locale, hasTime
      ? { timeZone, year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }
      : { timeZone, year: 'numeric', month: 'short', day: 'numeric' }).format(d)
  }
  const any = (x: unknown): string => {
    if (x === null || x === undefined) return DASH
    if (typeof x === 'number') return value(x)
    if (x instanceof Date) return dateTime(x)
    return String(x)
  }
  return { locale, timeZone, num, si, pct, currency, integer, value, date, dateTime, any }
}

function safeLocale(l: string | undefined): string {
  if (!l) return 'en-US'
  try { return Intl.getCanonicalLocales(l)[0] ?? 'en-US' } catch { return 'en-US' }
}

/** Pick the date granularity whose labels suit ticks spanning `spanMs`. */
export function tickGranularity(spanMs: number, ticks = 6): DateGranularity {
  const step = spanMs / Math.max(1, ticks)
  const H = 3600e3, D = 24 * H
  if (step < H) return 'minute'
  if (step < D) return 'hour'
  if (step < 28 * D) return 'day'
  if (step < 85 * D) return 'month'
  if (step < 360 * D) return 'quarter'
  return 'year'
}
