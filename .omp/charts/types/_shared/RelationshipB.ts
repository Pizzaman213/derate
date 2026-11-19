import { nf } from '../../core/format'
// Helpers shared by the relationship charts (correlation-matrix, splom,
// parallel-coordinates, radar, quadrant): finding the numeric columns of wide rows,
// reading a cell as a number, fixed-decimal formatting and a greedy label placer.

import { AdapterError, toNumber } from '../../adapters'
import type { Rect } from '../../core'

type Row = Record<string, unknown>

/** A cell as a finite number (numbers and numeric strings), else null. */
export function cellNum(row: Row, col: string): number | null {
  return toNumber(row[col])
}

/** Every column name in first-seen order. */
export function columnNames(rows: readonly Row[]): string[] {
  const seen = new Set<string>()
  for (const r of rows) for (const k of Object.keys(r)) seen.add(k)
  return [...seen]
}

/** True when every non-empty cell of `col` is numeric and at least one is present. */
export function isNumericColumn(rows: readonly Row[], col: string): boolean {
  let any = false
  for (const r of rows) {
    const v = r[col]
    if (v === null || v === undefined || v === '') continue
    if (toNumber(v) === null) return false
    any = true
  }
  return any
}

export interface ColumnPick {
  /** The numeric columns used, in order. */
  columns: string[]
  /** Numeric columns left out because of `max`. */
  extra: string[]
  /** Non-numeric columns ignored (excluding the reserved ones). */
  ignored: string[]
}

/**
 * The numeric columns of wide rows. `pick` (an explicit list) wins and must name
 * existing numeric columns; otherwise every numeric column not in `reserved`, up to `max`.
 */
export function numericColumns(rows: readonly Row[], reserved: readonly string[], pick: readonly string[] | undefined, max = Infinity): ColumnPick {
  const all = columnNames(rows)
  if (pick && pick.length) {
    for (const c of pick) {
      if (!all.includes(c)) throw new AdapterError(`option 'columns': column '${c}' not found (columns: ${all.join(', ')})`)
      if (rows.length && !isNumericColumn(rows, c)) throw new AdapterError(`option 'columns': column '${c}' is not numeric`)
    }
    return { columns: [...pick], extra: [], ignored: [] }
  }
  const numeric: string[] = []
  const ignored: string[] = []
  for (const c of all) {
    if (reserved.includes(c)) continue
    if (isNumericColumn(rows, c)) numeric.push(c)
    else ignored.push(c)
  }
  return { columns: numeric.slice(0, max), extra: numeric.slice(max), ignored }
}

/** Fixed-decimal number in the chart's locale, with a true minus sign. */
export function fixed(v: number, digits: number, locale: string): string {
  const s = nf(locale, { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(v)
  return s.replace(/^-/, '−')
}

/** Overlap test with a margin. */
export function overlaps(a: Rect, b: Rect, pad = 0): boolean {
  return a.x < b.x + b.width + pad && b.x < a.x + a.width + pad && a.y < b.y + b.height + pad && b.y < a.y + a.height + pad
}

/** Inside `outer` entirely. */
export function inside(a: Rect, outer: Rect): boolean {
  return a.x >= outer.x && a.y >= outer.y && a.x + a.width <= outer.x + outer.width && a.y + a.height <= outer.y + outer.height
}

/** Sentence fragment for |r|: the conventional strength bands. */
export function strength(r: number): string {
  const a = Math.abs(r)
  const s = a >= 0.8 ? 'very strong' : a >= 0.6 ? 'strong' : a >= 0.4 ? 'moderate' : a >= 0.2 ? 'weak' : 'negligible'
  return a < 0.2 ? s : `${s} ${r > 0 ? 'positive' : 'negative'}`
}
