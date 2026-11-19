/**
 * Calendar granularities, floored and stepped in wall-clock time of a zone.
 * Nothing here assumes a day is 24 hours: day and coarser units go through
 * `fromZoned`, so a DST day is 23 or 25 hours of elapsed time.
 */
import { daysInMonth, fromZoned, zonedParts } from './tz'

export type Granularity = 'minute' | 'hour' | 'day' | 'week' | 'month' | 'quarter' | 'year'

/** Granularities from finest to coarsest. */
export const GRANULARITIES: readonly Granularity[] = ['minute', 'hour', 'day', 'week', 'month', 'quarter', 'year']

/** Nominal length of each granularity in ms; for sizing only, never for stepping. */
export const NOMINAL_MS: Readonly<Record<Granularity, number>> = {
  minute: 60_000,
  hour: 3_600_000,
  day: 86_400_000,
  week: 7 * 86_400_000,
  month: 30.436875 * 86_400_000,
  quarter: 91.310625 * 86_400_000,
  year: 365.2425 * 86_400_000,
}

export interface CalendarOptions {
  /** IANA zone the calendar is read in; default 'UTC'. */
  timeZone?: string
  /** First day of a week: 0 = Sunday, 1 = Monday (default). */
  weekStart?: 0 | 1
}

/** Start of the `g` bucket containing `d`, in wall-clock time of the zone. */
export function floorTo(d: Date, g: Granularity, opts: CalendarOptions = {}): Date {
  const tz = opts.timeZone ?? 'UTC'
  const t = d.getTime()
  // Minute and hour floors are exact in elapsed time (every zone offset in use is whole
  // minutes), which keeps the repeated hour of a fall-back day as two distinct buckets.
  if (g === 'minute') return new Date(t - (((t % 60_000) + 60_000) % 60_000))
  const p = zonedParts(d, tz)
  if (g === 'hour') {
    const intoMinute = ((t % 1000) + 1000) % 1000
    return new Date(t - intoMinute - (p.minute * 60 + p.second) * 1000)
  }
  switch (g) {
    case 'day':
      return fromZoned({ year: p.year, month: p.month, day: p.day }, tz)
    case 'week': {
      const back = (p.weekday - (opts.weekStart ?? 1) + 7) % 7
      return fromZoned({ year: p.year, month: p.month, day: p.day - back }, tz)
    }
    case 'month':
      return fromZoned({ year: p.year, month: p.month, day: 1 }, tz)
    case 'quarter':
      return fromZoned({ year: p.year, month: Math.floor((p.month - 1) / 3) * 3 + 1, day: 1 }, tz)
    case 'year':
      return fromZoned({ year: p.year, month: 1, day: 1 }, tz)
  }
}

/**
 * `d` moved by `n` units of `g`. Minutes and hours are elapsed time; days and coarser keep
 * the wall-clock time (one day across spring-forward is 23h elapsed) and months clamp the
 * day (Jan 31 + 1 month = Feb 28/29).
 */
export function offsetBy(d: Date, g: Granularity, n: number, opts: CalendarOptions = {}): Date {
  if (!Number.isInteger(n)) throw new RangeError(`offsetBy needs a whole number of steps, got ${n}`)
  if (g === 'minute') return new Date(d.getTime() + n * 60_000)
  if (g === 'hour') return new Date(d.getTime() + n * 3_600_000)
  const tz = opts.timeZone ?? 'UTC'
  const p = zonedParts(d, tz)
  const ms = ((d.getTime() % 1000) + 1000) % 1000
  const time = { hour: p.hour, minute: p.minute, second: p.second, millisecond: ms }
  if (g === 'day' || g === 'week') {
    return fromZoned({ year: p.year, month: p.month, day: p.day + n * (g === 'week' ? 7 : 1), ...time }, tz)
  }
  const months = n * (g === 'month' ? 1 : g === 'quarter' ? 3 : 12)
  const index = p.year * 12 + (p.month - 1) + months
  const year = Math.floor(index / 12)
  const month = index - year * 12 + 1
  return fromZoned({ year, month, day: Math.min(p.day, daysInMonth(year, month)), ...time }, tz)
}

/** Every bucket start from floor(start) up to and including `end`. */
export function rangeOf(start: Date, end: Date, g: Granularity, opts: CalendarOptions = {}): Date[] {
  const out: Date[] = []
  const last = end.getTime()
  let cur = floorTo(start, g, opts)
  while (cur.getTime() <= last) {
    out.push(cur)
    if (out.length > 1_000_000) throw new RangeError(`rangeOf would produce more than 1,000,000 ${g} buckets`)
    // Re-floor: a midnight skipped by DST lands the day's first instant at 01:00, and the
    // next step must return to midnight rather than carry the hour forward.
    let next = floorTo(offsetBy(cur, g, 1, opts), g, opts)
    if (next.getTime() <= cur.getTime()) next = floorTo(offsetBy(cur, g, 2, opts), g, opts)
    cur = next
  }
  return out
}

export type Aggregate = 'sum' | 'mean' | 'count' | 'min' | 'max' | 'last'

export interface BucketOptions<T> extends CalendarOptions {
  date: (r: T) => Date | null
  /** Absent: every row contributes 1. A null/NaN value drops the row. */
  value?: (r: T) => number | null
  granularity: Granularity
  /** Default 'sum'. */
  agg?: Aggregate
}

export interface Bucket {
  date: Date
  value: number
  count: number
}

/** Rows aggregated into `granularity` buckets, sorted by bucket start; `count` is contributing rows. */
export function bucketBy<T>(rows: T[], opts: BucketOptions<T>): Bucket[] {
  const agg = opts.agg ?? 'sum'
  const acc = new Map<number, { sum: number; count: number; min: number; max: number; last: number }>()
  for (const r of rows) {
    const d = opts.date(r)
    if (!d || Number.isNaN(d.getTime())) continue
    const v = opts.value ? opts.value(r) : 1
    if (v === null || !Number.isFinite(v)) continue
    const key = floorTo(d, opts.granularity, opts).getTime()
    const a = acc.get(key)
    if (a) {
      a.sum += v
      a.count += 1
      a.min = Math.min(a.min, v)
      a.max = Math.max(a.max, v)
      a.last = v
    } else {
      acc.set(key, { sum: v, count: 1, min: v, max: v, last: v })
    }
  }
  return [...acc.entries()]
    .sort((x, y) => x[0] - y[0])
    .map(([key, a]) => {
      const value = { sum: a.sum, mean: a.sum / a.count, count: a.count, min: a.min, max: a.max, last: a.last }[agg]
      return { date: new Date(key), value, count: a.count }
    })
}

/** Finest granularity that splits `spanMs` into at most `maxBuckets` buckets ('year' if none does). */
export function pickGranularity(spanMs: number, maxBuckets = 60): Granularity {
  const span = Math.max(0, spanMs)
  for (const g of GRANULARITIES) {
    if (Math.max(1, Math.ceil(span / NOMINAL_MS[g])) <= maxBuckets) return g
  }
  return 'year'
}

/** Granularity whose nominal length is closest (in ratio) to the median spacing of `dates`; 'day' when undecidable. */
export function granularityOf(dates: Date[]): Granularity {
  const ts = dates.map((d) => d.getTime()).filter(Number.isFinite).sort((a, b) => a - b)
  const gaps: number[] = []
  for (let i = 1; i < ts.length; i++) {
    const gap = (ts[i] ?? 0) - (ts[i - 1] ?? 0)
    if (gap > 0) gaps.push(gap)
  }
  if (gaps.length === 0) return 'day'
  gaps.sort((a, b) => a - b)
  const mid = gaps.length >> 1
  const median = gaps.length % 2 ? (gaps[mid] ?? 0) : ((gaps[mid - 1] ?? 0) + (gaps[mid] ?? 0)) / 2
  let best: Granularity = 'day'
  let bestDistance = Infinity
  for (const g of GRANULARITIES) {
    const distance = Math.abs(Math.log(median / NOMINAL_MS[g]))
    if (distance < bestDistance) {
      best = g
      bestDistance = distance
    }
  }
  return best
}
