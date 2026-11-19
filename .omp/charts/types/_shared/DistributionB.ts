// Helpers shared by the beeswarm, strip, ecdf and qq-plot charts: splitting finite values
// from missing ones, stable unique mark keys, and the "from a to b, median m" phrase.

import { stats, type Formatters } from '../../core'

export interface Indexed<T> {
  row: T
  value: number
  /** Position of the row in the input. */
  index: number
}

/** Rows with a finite value (input order kept), and how many were missing. */
export function finiteValues<T extends { value: number | null }>(rows: readonly T[]): { ok: Array<Indexed<T>>; nulls: number } {
  const ok: Array<Indexed<T>> = []
  let nulls = 0
  rows.forEach((row, index) => {
    if (typeof row.value === 'number' && Number.isFinite(row.value)) ok.push({ row, value: row.value, index })
    else nulls++
  })
  return { ok, nulls }
}

/** Keys that stay unique when names repeat ("a", "a#2"); missing names fall back to the row index. */
export function uniqueKeys(names: ReadonlyArray<string | undefined>, prefix: string): string[] {
  const seen = new Map<string, number>()
  return names.map((n, i) => {
    const base = n === undefined ? `${prefix}${i}` : `${prefix}${n}`
    const c = (seen.get(base) ?? 0) + 1
    seen.set(base, c)
    return c === 1 ? base : `${base}#${c}`
  })
}

/** "from 12 to 98, median 54" ("all 7" when every value is the same). */
export function spread(values: readonly number[], fmt: Formatters): string {
  const ext = stats.extent(values)
  if (!ext) return 'no values'
  if (ext[0] === ext[1]) return values.length === 1 ? `value ${fmt.value(ext[0])}` : `all ${fmt.value(ext[0])}`
  return `from ${fmt.value(ext[0])} to ${fmt.value(ext[1])}, median ${fmt.value(stats.median(values))}`
}

/** A point radius that leaves room for n points in an area (px), clamped to [min, max]. */
export function autoRadius(n: number, area: number, min = 1, max = 6, fill = 0.25): number {
  if (n <= 0) return max
  return Math.max(min, Math.min(max, Math.sqrt((area * fill) / (n * Math.PI))))
}

/** Widen [lo, hi] by a share of its span on each side, so points at the extremes clear the axes. */
export function padExtent(ext: [number, number], share = 0.04): [number, number] {
  const d = (ext[1] - ext[0]) * share
  return [ext[0] - d, ext[1] + d]
}
