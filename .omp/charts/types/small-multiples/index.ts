// Small multiples: one panel per value of a facet column, each drawn by another chart
// type, laid out in a grid that fits the width. By default every panel shares the y
// scale (the global extent of the value column), so panels compare at a glance.
// The data marks are the children's own.

import { defineChart, getModule, phrase, SIZES, type LegendItem, type Mark } from '../../core'
import { finiteExtent } from '../../core/helpers'
import { parseDate } from '../../time/parse'
import { fitGrid, moreMarks } from '../_shared/KpiCharts'
import { input, options, type FacetOptions, type FacetRow } from './schema'
// the common children register themselves, so a standalone bundle can draw them
import '../bar'
import '../line'

const TITLE_GAP = 6
/** Rows grouped by facet value, in first-seen order. */
export function facetGroups(rows: FacetRow[], facet: string): Array<{ key: string; rows: FacetRow[] }> {
  const map = new Map<string, FacetRow[]>()
  for (const r of rows) {
    const v = r[facet]
    const k = v === null || v === undefined || v === '' ? '(blank)' : v instanceof Date ? v.toISOString() : String(v)
    let g = map.get(k)
    if (!g) map.set(k, (g = []))
    g.push(r)
  }
  return [...map].map(([key, rows]) => ({ key, rows }))
}

const toNum = (v: unknown): number | null => {
  if (typeof v === 'number') return Number.isFinite(v) ? v : null
  if (typeof v === 'string' && v.trim() !== '') { const n = Number(v.replace(/,/g, '')); return Number.isFinite(n) ? n : null }
  return null
}

const mapped = (o: FacetOptions, canonical: string): string | undefined => {
  const f = o.chartOptions.fields
  if (f && typeof f === 'object' && !Array.isArray(f)) {
    const v = (f as Record<string, unknown>)[canonical]
    if (typeof v === 'string') return v
  }
  return undefined
}

/** The column the shared y scale spans. */
export function valueColumn(rows: FacetRow[], o: FacetOptions): string {
  return o.valueField ?? mapped(o, 'y') ?? mapped(o, 'value') ?? (rows.some((r) => 'y' in r) ? 'y' : 'value')
}

/** The shared y domain: the global extent of the value column (with 0 unless yZero is off). */
export function sharedY(rows: FacetRow[], o: FacetOptions): [number, number] | null {
  const col = valueColumn(rows, o)
  const ext = finiteExtent(rows.map((r) => toNum(r[col])))
  if (!ext) return null
  let [lo, hi] = ext
  if (o.yZero) { lo = Math.min(0, lo); hi = Math.max(0, hi) }
  if (lo === hi) { if (lo === 0) hi = 1; else { lo -= Math.abs(lo) * 0.1; hi += Math.abs(hi) * 0.1 } }
  return [lo, hi]
}

/** The shared x domain: numeric or date extent of the x column; null for categories. */
export function sharedX(rows: FacetRow[], o: FacetOptions): [number, number] | [Date, Date] | null {
  const col = o.xField ?? mapped(o, 'x') ?? 'x'
  const vals = rows.map((r) => r[col]).filter((v) => v !== null && v !== undefined && v !== '')
  if (!vals.length) return null
  const nums = vals.map(toNum)
  if (nums.every((n) => n !== null)) {
    const ext = finiteExtent(nums)
    return ext && ext[0] !== ext[1] ? ext : null
  }
  const ds = vals.map((v) => parseDate(v))
  if (ds.every((d) => d !== null)) {
    const ts = ds.map((d) => +d!)
    const lo = Math.min(...ts), hi = Math.max(...ts)
    return lo === hi ? null : [new Date(lo), new Date(hi)]
  }
  return null
}

export const smallMultiples = defineChart<FacetRow[], FacetOptions>({
  id: 'small-multiples',
  name: 'Small multiples',
  category: 'kpi',
  intents: ['kpi', 'compare'],
  summary: 'One small chart per group, on a shared scale, in a grid that fits the width.',
  input,
  options,
  shape: 'rows',
  adapt(raw, o) {
    const rows = Array.isArray(raw) ? raw : []
    if (o.chart === 'small-multiples') throw new Error("small multiples cannot contain small multiples; pick a chart such as 'bar' or 'line'")
    if (o.chart && !getModule(o.chart)) throw new Error(`unknown chart '${o.chart}' for the panels`)
    if (o.facet && rows.length) {
      const cols = new Set(rows.flatMap((r) => (r && typeof r === 'object' ? Object.keys(r) : [])))
      if (!cols.has(o.facet)) throw new Error(`facet column '${o.facet}' not found (columns: ${[...cols].join(', ')})`)
    }
    return raw
  },
  aspect: 0.7,
  minHeight: 280,
  maxHeight: 900,
  isEmpty: (data) => data.length === 0,
  draw(ctx) {
    const { theme, options: o, width: W, height: H } = ctx
    const groups = facetGroups(ctx.data, o.facet)
    const n = groups.length
    const titleH = SIZES.label + TITLE_GAP
    const grid = o.columns
      ? fitGrid(n, W, H, { minW: 1, minH: 90, gap: 16, ratio: 1, maxCols: o.columns, score: () => 0 })
      : fitGrid(n, W, H, { minW: 180, minH: 110, gap: 16, ratio: 0.62 })
    const shareY = o.sharedScale === 'y' || o.sharedScale === 'both'
    const shareX = o.sharedScale === 'x' || o.sharedScale === 'both'
    const yDomain = shareY ? sharedY(ctx.data, o) : null
    const xDomain = shareX ? sharedX(ctx.data, o) : null
    const marks: Mark[] = []
    const legend = new Map<string, LegendItem>()
    const childNotes: string[] = []
    for (let i = 0; i < grid.shown; i++) {
      const g = groups[i]!
      const c = i % grid.cols, r = Math.floor(i / grid.cols)
      const x = c * (grid.cellW + grid.gap), y = r * (grid.cellH + grid.gap)
      // truncate measures regular weight; the title is semibold, so leave it room
      const t = ctx.truncate(g.key, grid.cellW * 0.9, SIZES.label)
      marks.push({ type: 'text', key: `title:${g.key}`, layer: 'labels', x, y: y + SIZES.label, text: t, full: t !== g.key ? g.key : undefined, anchor: 'start', baseline: 'alphabetic', size: SIZES.label, weight: 600, fill: theme.ink })
      const childOpts: Record<string, unknown> = { ...o.chartOptions }
      if (yDomain) childOpts.yDomain = yDomain
      if (xDomain) childOpts.xDomain = xDomain
      const res = ctx.drawChild(o.chart, g.rows, childOpts, { x, y: y + titleH, width: grid.cellW, height: Math.max(20, grid.cellH - titleH) }, `f:${g.key}`)
      marks.push(...res.marks)
      for (const it of res.legend?.items ?? []) if (!legend.has(it.key)) legend.set(it.key, it)
      if (res.note) childNotes.push(`${g.key}: ${res.note}`)
    }
    marks.push(...moreMarks(ctx, grid, 'panels'))
    return {
      marks,
      legend: legend.size > 1 ? { items: [...legend.values()] } : undefined,
      note: childNotes.length ? childNotes.join('; ') : undefined,
    }
  },
  describe({ data, options: o, fmt, name }) {
    const groups = facetGroups(data, o.facet)
    const child = getModule(o.chart)?.def.name.toLowerCase() ?? o.chart
    const y = o.sharedScale === 'y' || o.sharedScale === 'both' ? sharedY(data, o) : null
    const scale = y ? `, shared y scale ${fmt.value(y[0])} to ${fmt.value(y[1])}` : o.sharedScale === 'none' ? ', independent scales' : ''
    return `${name}, ${phrase.count(groups.length, 'panel')} of ${child} by ${o.facet} (${phrase.listPhrase(groups.map((g) => g.key), 6)})${scale}`
  },
  table(data) {
    const cols = [...new Set(data.flatMap((r) => Object.keys(r)))]
    const cell = (v: unknown): string | number | null =>
      v === null || v === undefined ? null : typeof v === 'number' || typeof v === 'string' ? v : v instanceof Date ? v.toISOString() : typeof v === 'boolean' ? String(v) : JSON.stringify(v)
    return { columns: cols, rows: data.map((r) => cols.map((c) => cell(r[c]))) }
  },
})

export default smallMultiples
