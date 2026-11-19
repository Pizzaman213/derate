// Shared by the comparison bar family (grouped-bar, stacked-bar, stacked-bar-100,
// horizontal-bar, diverging-bar, pareto): long-row collapsing and orientation-aware
// bar geometry, so every bar chart in the family places and labels bars alike.

import { z } from 'zod'
import { fields } from '../../core'
import type { CartesianFrame, RectMark } from '../../core'

/** Long-format input shared by grouped / stacked / stacked-100. */
export const seriesInput = fields.rows({
  label: fields.label('category (one bar group / stack per label)'),
  series: fields.label('series within the category; one colour and one legend entry each'),
  value: fields.numOrNull('segment / bar value; null is left out (noted)'),
}, 'rows of { label, series, value }')

export type SeriesRow = z.output<typeof seriesInput>[number]

export const orientation = z.enum(['vertical', 'horizontal']).default('vertical').describe('bars stand up (vertical) or run left to right (horizontal)')

export interface SeriesTable {
  labels: string[]
  series: string[]
  /** label -> series -> summed value (null when every row for the cell was null). */
  cells: Map<string, Map<string, number | null>>
  duplicates: number
  nulls: number
}

/** Collapse long rows: first-seen label and series order, duplicate (label, series) pairs summed. */
export function collapseSeries(rows: readonly SeriesRow[]): SeriesTable {
  const labels: string[] = [], series: string[] = []
  const seenS = new Set<string>()
  const cells = new Map<string, Map<string, number | null>>()
  let duplicates = 0, nulls = 0
  for (const r of rows) {
    let row = cells.get(r.label)
    if (!row) { row = new Map(); cells.set(r.label, row); labels.push(r.label) }
    if (!seenS.has(r.series)) { seenS.add(r.series); series.push(r.series) }
    if (r.value === null) nulls++
    if (!row.has(r.series)) { row.set(r.series, r.value); continue }
    duplicates++
    const prev = row.get(r.series) ?? null
    row.set(r.series, prev === null ? r.value : r.value === null ? prev : prev + r.value)
  }
  return { labels, series, cells, duplicates, nulls }
}

/** Value of one cell, or null when absent. */
export function cell(t: SeriesTable, label: string, series: string): number | null {
  return t.cells.get(label)?.get(series) ?? null
}

/** Wide table rows: one row per label, one column per series. */
export function wideTable(t: SeriesTable): { columns: string[]; rows: Array<Array<string | number | null>> } {
  return { columns: ['Label', ...t.series], rows: t.labels.map((l) => [l, ...t.series.map((s) => cell(t, l, s))]) }
}

/** Band thickness along the category axis. */
export function bandOf(f: CartesianFrame, horizontal: boolean): number {
  return horizontal ? f.bh : f.bw
}

/**
 * Rect geometry for a bar spanning values a..b inside category `label`, offset `off`
 * into its band with thickness `thick`. A zero-length bar becomes a 1px stub on the
 * baseline so it is still visible and hoverable.
 */
export function barGeom(f: CartesianFrame, horizontal: boolean, label: string, off: number, thick: number, a: number, b: number): Pick<RectMark, 'x' | 'y' | 'width' | 'height'> {
  const t = Math.max(0.5, thick)
  if (horizontal) {
    const pa = f.px(a), pb = f.px(b)
    const len = Math.abs(pb - pa)
    return len < 1 ? { x: Math.min(pa, pb) - 0.5, width: 1, y: f.py(label) + off, height: t } : { x: Math.min(pa, pb), width: len, y: f.py(label) + off, height: t }
  }
  const pa = f.py(a), pb = f.py(b)
  const len = Math.abs(pb - pa)
  return len < 1 ? { y: Math.min(pa, pb) - 0.5, height: 1, x: f.px(label) + off, width: t } : { y: Math.min(pa, pb), height: len, x: f.px(label) + off, width: t }
}

/** Where a value label sits at the end of a bar ending at value v (outside, past the end). */
export function endLabel(f: CartesianFrame, horizontal: boolean, label: string, off: number, thick: number, v: number, base = 0): { x: number; y: number; anchor: 'start' | 'middle' | 'end'; baseline: 'central' | 'alphabetic' | 'hanging' } {
  const up = v >= base
  if (horizontal) return { x: f.px(v) + (up ? 4 : -4), y: f.py(label) + off + thick / 2, anchor: up ? 'start' : 'end', baseline: 'central' }
  return { x: f.px(label) + off + thick / 2, y: f.py(v) + (up ? -4 : 4), anchor: 'middle', baseline: up ? 'alphabetic' : 'hanging' }
}

