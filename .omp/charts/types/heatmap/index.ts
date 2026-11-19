// Heatmap: a band x band grid of cells coloured by value on a sequential (or diverging)
// ramp, values printed in the cells when they fit.

import { defineChart, phrase, SIZES } from '../../core'
import type { BaseOptions, Formatters, Mark } from '../../core'
import { notes } from '../../core/helpers'
import { isDateString } from '../../adapters/rows'
import { granularityOf, parseDate } from '../../time'
import { bandPadding, colorScale, extremesOf, hatchPath, inkOn, orderNames } from '../_shared/MatrixCharts'
import { input, options, type HeatmapOptions, type HeatmapRow } from './schema'

interface Axis {
  /** Domain keys in display order (dates: epoch ms as text). */
  keys: string[]
  label: Map<string, string>
  dates: boolean
}
export interface Cell {
  x: string
  y: string
  xLabel: string
  yLabel: string
  value: number | null
  /** Rows that landed in this cell. */
  count: number
}
export interface HeatmapModel {
  x: Axis
  y: Axis
  cells: Cell[]
  duplicates: number
  nulls: number
}

type Opts = HeatmapOptions & BaseOptions

function axisOf(values: Array<Date | string>, fmt: Formatters, timeZone: string | undefined): { key: (v: Date | string) => string; dates: boolean; label: Map<string, string> } {
  const dates = values.length > 0 && values.every((v) => v instanceof Date || isDateString(v))
  const label = new Map<string, string>()
  if (!dates) {
    const text = (v: Date | string) => (v instanceof Date ? v.toISOString() : v)
    for (const v of values) label.set(text(v), text(v))
    return { key: text, dates, label }
  }
  const toDate = (v: Date | string): Date => (v instanceof Date ? v : parseDate(v, { timeZone }) ?? new Date(NaN))
  const uniq = [...new Map(values.map((v) => { const d = toDate(v); return [+d, d] as const })).values()]
  const g = granularityOf(uniq)
  const span = uniq.length ? Math.max(...uniq.map(Number)) - Math.min(...uniq.map(Number)) : 0
  for (const d of uniq) {
    const text = (g === 'hour' || g === 'minute') && span >= 864e5 ? `${fmt.date(d, 'day')} ${fmt.date(d, 'hour')}` : fmt.date(d, g)
    label.set(String(+d), text)
  }
  return { key: (v) => String(+toDate(v)), dates, label }
}

export function model(data: HeatmapRow[], o: Opts, fmt: Formatters): HeatmapModel {
  const ax = axisOf(data.map((r) => r.x), fmt, o.timeZone)
  const ay = axisOf(data.map((r) => r.y), fmt, o.timeZone)
  const acc = new Map<string, { x: string; y: string; sum: number; n: number; min: number; max: number; rows: number }>()
  const xs: string[] = [], ys: string[] = []
  const seenX = new Set<string>(), seenY = new Set<string>()
  let duplicates = 0
  for (const r of data) {
    const x = ax.key(r.x), y = ay.key(r.y)
    if (!seenX.has(x)) { seenX.add(x); xs.push(x) }
    if (!seenY.has(y)) { seenY.add(y); ys.push(y) }
    const k = `${x}\u0000${y}`
    let c = acc.get(k)
    if (!c) acc.set(k, (c = { x, y, sum: 0, n: 0, min: Infinity, max: -Infinity, rows: 0 }))
    else duplicates++
    c.rows++
    if (r.value === null) continue
    c.sum += r.value
    c.n++
    c.min = Math.min(c.min, r.value)
    c.max = Math.max(c.max, r.value)
  }
  const valueOf = (c: { sum: number; n: number; min: number; max: number; rows: number }): number | null => {
    if (o.aggregate === 'count') return c.rows
    if (c.n === 0) return null
    return o.aggregate === 'mean' ? c.sum / c.n : o.aggregate === 'max' ? c.max : o.aggregate === 'min' ? c.min : c.sum
  }
  const totals = (axis: 'x' | 'y') => {
    const t = new Map<string, number>()
    for (const c of acc.values()) { const v = valueOf(c); if (v !== null) t.set(c[axis], (t.get(c[axis]) ?? 0) + v) }
    return (k: string) => t.get(k) ?? 0
  }
  const chrono = (keys: string[]) => [...keys].sort((a, b) => +a - +b)
  const xKeys = ax.dates ? chrono(xs) : orderNames(xs, o.xOrder, totals('x'))
  const yKeys = ay.dates ? chrono(ys) : orderNames(ys, o.yOrder, totals('y'))
  const cells: Cell[] = []
  let nulls = 0
  for (const y of yKeys) for (const x of xKeys) {
    const c = acc.get(`${x}\u0000${y}`)
    if (!c) continue
    const value = valueOf(c)
    if (value === null) nulls++
    cells.push({ x, y, xLabel: ax.label.get(x) ?? x, yLabel: ay.label.get(y) ?? y, value, count: c.rows })
  }
  return { x: { keys: xKeys, label: ax.label, dates: ax.dates }, y: { keys: yKeys, label: ay.label, dates: ay.dates }, cells, duplicates, nulls }
}

export const heatmap = defineChart<HeatmapRow[], HeatmapOptions>({
  id: 'heatmap',
  name: 'Heatmap',
  category: 'matrix',
  intents: ['correlation', 'compare', 'relationship', 'trend', 'distribution'],
  summary: 'A grid of cells coloured by value across two categorical or date dimensions.',
  input,
  options,
  shape: 'rows',
  fields: ['x', 'y', 'value'],
  numberFields: ['value'],
  aspect: 0.6,
  minHeight: 180,
  maxHeight: 640,
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    const m = model(ctx.data, o, fmt)
    const scale = colorScale(theme, m.cells.map((c) => c.value), { diverging: o.diverging ?? 'auto', center: o.center, transform: o.colorScale })
    const f = ctx.cartesian({
      x: { type: 'band', domain: m.x.keys, padding: bandPadding(ctx.width * 0.8, m.x.keys.length) },
      y: { type: 'band', domain: m.y.keys, padding: bandPadding(ctx.height * 0.8, m.y.keys.length) },
      xFormat: ((k: string) => m.x.label.get(k) ?? k) as (v: never) => string,
      yFormat: ((k: string) => m.y.label.get(k) ?? k) as (v: never) => string,
      xTitle: o.xTitle,
      yTitle: o.yTitle,
      grid: 'none',
    })
    const marks: Mark[] = [...f.marks]
    const bw = Math.max(0.5, f.bw), bh = Math.max(0.5, f.bh)
    // print values only when every value fits, so the grid reads evenly
    const longest = Math.max(0, ...m.cells.map((c) => (c.value === null ? 0 : ctx.measure(fmt.value(c.value)))))
    const values = o.showValues && longest + 6 <= bw && SIZES.tick + 4 <= bh
    const rx = Math.min(2, bw / 6, bh / 6)
    for (const c of m.cells) {
      const x = f.px(c.x), y = f.py(c.y)
      const name = `${c.yLabel}, ${c.xLabel}`
      const rows: Array<[string, string]> = c.count > 1 ? [['Rows combined', fmt.integer(c.count)]] : []
      if (c.value === null) {
        marks.push({
          type: 'rect', role: 'cell', key: `${c.x}|${c.y}`, x, y, width: bw, height: bh, rx, fill: theme.surface, stroke: theme.grid, strokeWidth: 1,
          datum: c, label: `${name}: no value`, tooltip: { title: name, rows: [['Value', 'missing'], ...rows] },
        })
        marks.push({ type: 'path', key: `hatch:${c.x}|${c.y}`, d: hatchPath(x, y, bw, bh), fill: 'none', stroke: theme.grid, strokeWidth: 1, inert: true })
        continue
      }
      const fill = scale.paint(c.value)
      marks.push({
        type: 'rect', role: 'cell', key: `${c.x}|${c.y}`, x, y, width: bw, height: bh, rx, fill,
        datum: c, label: `${name}: ${fmt.value(c.value)}`, tooltip: { title: name, rows: [['Value', fmt.value(c.value)], ...rows], color: fill },
      })
      if (values) {
        marks.push({ type: 'text', layer: 'labels', key: `v:${c.x}|${c.y}`, x: x + bw / 2, y: y + bh / 2, text: fmt.value(c.value), anchor: 'middle', baseline: 'central', size: SIZES.tick, fill: inkOn(theme, scale.t(c.value), scale.diverging), inert: true })
      }
    }
    return {
      marks,
      plot: f.plot,
      legend: m.cells.some((c) => c.value !== null) ? { gradient: scale.legend(fmt) } : undefined,
      note: notes([[m.duplicates, `duplicate cells combined (${o.aggregate})`], [m.nulls, 'missing values hatched']]),
    }
  },
  describe({ data, options: o, fmt, name }) {
    const m = model(data, o, fmt)
    const items = m.cells.map((c) => ({ label: `${c.yLabel}, ${c.xLabel}`, value: c.value }))
    return `${name}, ${phrase.count(m.y.keys.length, 'row')} by ${phrase.count(m.x.keys.length, 'column')}, ${extremesOf(items, fmt, 'cell')}`
  },
  table(data, o, fmt) {
    const m = model(data, o, fmt)
    return { columns: [o.yTitle ?? 'Row', o.xTitle ?? 'Column', 'Value'], rows: m.cells.map((c) => [c.yLabel, c.xLabel, c.value]) }
  },
})

export default heatmap
