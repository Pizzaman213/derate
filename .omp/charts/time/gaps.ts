/**
 * Regular time grids out of irregular points: every bucket of a granularity between the
 * first and last point, with missing buckets filled by a chosen rule.
 */
import { type CalendarOptions, type Granularity, floorTo, offsetBy, rangeOf } from './granularity'

export type FillMode = 'gap' | 'zero' | 'previous' | 'interpolate'

export interface FillOptions extends CalendarOptions {
  granularity: Granularity
  /**
   * 'gap' leaves null, so a line lifts the pen; 'zero' writes 0; 'previous' carries the
   * last observed value forward; 'interpolate' draws a straight line in time between the
   * observed neighbours. Leading/trailing buckets with nothing to carry or interpolate
   * from stay null under 'previous' and 'interpolate'.
   */
  mode: FillMode
  /** Grid start (floored); default the earliest point. */
  start?: Date
  /** Grid end (inclusive); default the latest point. */
  end?: Date
}

export interface FilledPoint {
  date: Date
  value: number | null
  /** True when the bucket had no observed value, whatever the mode then wrote into it. */
  filled: boolean
}

/**
 * Points snapped to a complete grid of `granularity` buckets. Several points in one bucket
 * are NOT summed: the last one in input order wins (bucket with `bucketBy` first to sum).
 * A point whose value is null counts as missing and is filled like an absent one.
 */
export function fillGaps(points: { date: Date; value: number | null }[], opts: FillOptions): FilledPoint[] {
  const observed = new Map<number, number | null>()
  let lo = Infinity
  let hi = -Infinity
  for (const p of points) {
    const t = p.date.getTime()
    if (Number.isNaN(t)) continue
    observed.set(floorTo(p.date, opts.granularity, opts).getTime(), p.value !== null && Number.isFinite(p.value) ? p.value : null)
    lo = Math.min(lo, t)
    hi = Math.max(hi, t)
  }
  const start = opts.start ?? (Number.isFinite(lo) ? new Date(lo) : null)
  const end = opts.end ?? (Number.isFinite(hi) ? new Date(hi) : null)
  if (!start || !end) return []
  const grid: FilledPoint[] = rangeOf(start, end, opts.granularity, opts).map((date) => {
    const v = observed.get(date.getTime()) ?? null
    return { date, value: v, filled: v === null }
  })
  switch (opts.mode) {
    case 'gap':
      break
    case 'zero':
      for (const g of grid) if (g.filled) g.value = 0
      break
    case 'previous': {
      let prev: number | null = null
      for (const g of grid) {
        if (g.filled) g.value = prev
        else prev = g.value
      }
      break
    }
    case 'interpolate': {
      let left = -1
      for (let i = 0; i < grid.length; i++) {
        const right = grid[i]
        if (!right || right.filled) continue
        const l = grid[left]
        if (l && l.value !== null && right.value !== null) {
          const t0 = l.date.getTime()
          const span = right.date.getTime() - t0
          for (let k = left + 1; k < i; k++) {
            const g = grid[k]
            if (g) g.value = l.value + ((right.value - l.value) * (g.date.getTime() - t0)) / span
          }
        }
        left = i
      }
      break
    }
  }
  return grid
}

export interface Gap {
  /** First missing bucket. */
  from: Date
  /** Last missing bucket. */
  to: Date
  /** Number of missing buckets. */
  missing: number
}

/** Runs of empty `granularity` buckets between the earliest and latest of `dates`. */
export function findGaps(dates: Date[], granularity: Granularity, opts: CalendarOptions = {}): Gap[] {
  const keys = [...new Set(dates.filter((d) => !Number.isNaN(d.getTime())).map((d) => floorTo(d, granularity, opts).getTime()))].sort((a, b) => a - b)
  const step = (t: number) => floorTo(offsetBy(new Date(t), granularity, 1, opts), granularity, opts).getTime()
  const gaps: Gap[] = []
  for (let i = 1; i < keys.length; i++) {
    const prev = keys[i - 1] ?? 0
    const cur = keys[i] ?? 0
    let next = step(prev)
    if (next >= cur) continue
    const from = next
    let to = next
    let missing = 0
    while (next < cur) {
      to = next
      missing += 1
      next = step(next)
    }
    gaps.push({ from: new Date(from), to: new Date(to), missing })
  }
  return gaps
}
