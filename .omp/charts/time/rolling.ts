/**
 * Rolling-window statistics over a series with holes. Nulls (and NaN) are skipped, never
 * read as zero; a window with fewer than `minPeriods` real values yields null.
 */

export interface RollingOptions {
  /** Non-null values a window needs to produce a number; default the window size. */
  minPeriods?: number
  /** Centre the window on each point instead of trailing it (pandas `center=True`). */
  center?: boolean
}

function rolling(values: (number | null)[], window: number, opts: RollingOptions, reduce: (xs: number[]) => number): (number | null)[] {
  if (!Number.isInteger(window) || window < 1) throw new RangeError(`window must be a whole number >= 1, got ${window}`)
  const minPeriods = opts.minPeriods ?? window
  if (!Number.isInteger(minPeriods) || minPeriods < 1 || minPeriods > window) {
    throw new RangeError(`minPeriods must be a whole number from 1 to the window (${window}), got ${minPeriods}`)
  }
  return values.map((_, i) => {
    const first = opts.center ? i - Math.floor(window / 2) : i - window + 1
    const xs: number[] = []
    for (let k = Math.max(0, first); k <= Math.min(values.length - 1, first + window - 1); k++) {
      const v = values[k]
      if (v !== null && v !== undefined && Number.isFinite(v)) xs.push(v)
    }
    return xs.length >= minPeriods ? reduce(xs) : null
  })
}

/** Mean of each window's non-null values. */
export function rollingMean(values: (number | null)[], window: number, opts: RollingOptions = {}): (number | null)[] {
  return rolling(values, window, opts, (xs) => xs.reduce((a, b) => a + b, 0) / xs.length)
}

/** Sum of each window's non-null values. */
export function rollingSum(values: (number | null)[], window: number, opts: RollingOptions = {}): (number | null)[] {
  return rolling(values, window, opts, (xs) => xs.reduce((a, b) => a + b, 0))
}

/** Median of each window's non-null values (mean of the middle two for an even count). */
export function rollingMedian(values: (number | null)[], window: number, opts: RollingOptions = {}): (number | null)[] {
  return rolling(values, window, opts, (xs) => {
    const s = [...xs].sort((a, b) => a - b)
    const mid = s.length >> 1
    return s.length % 2 ? (s[mid] ?? 0) : ((s[mid - 1] ?? 0) + (s[mid] ?? 0)) / 2
  })
}

/**
 * Exponentially weighted moving average, s = alpha*x + (1-alpha)*s, seeded with the first
 * real value. A null input yields null and leaves the running average untouched.
 */
export function ewma(values: (number | null)[], alpha: number): (number | null)[] {
  if (!(alpha > 0 && alpha <= 1)) throw new RangeError(`alpha must be in (0, 1], got ${alpha}`)
  let s: number | null = null
  return values.map((v) => {
    if (v === null || !Number.isFinite(v)) return null
    s = s === null ? v : alpha * v + (1 - alpha) * s
    return s
  })
}
