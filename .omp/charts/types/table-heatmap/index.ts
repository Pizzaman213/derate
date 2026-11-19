// Table heatmap: a data table drawn as a grid -- header row, text cells, and numeric
// columns shaded on a colour ramp (per column or across the table). Column widths follow
// their content and shrink, with ellipses, to fit the width.

import { defineChart, phrase, SIZES } from '../../core'
import type { BaseOptions, Formatters, Mark, Palette } from '../../core'
import { notes } from '../../core/helpers'
import { columnsOf } from '../../adapters/util'
import { colorScale, extremesOf, hatchPath, inkOn, type ColorScale } from '../_shared/MatrixCharts'
import { input, options, type TableHeatmapOptions, type TableRow } from './schema'

type Opts = TableHeatmapOptions & BaseOptions
type Value = TableRow[string]

export interface Column {
  name: string
  numeric: boolean
}
export interface TableModel {
  columns: Column[]
  rows: TableRow[]
  /** The first text column names rows in labels and the summary. */
  labelColumn: string | undefined
}

const isBlank = (v: Value | undefined) => v === null || v === undefined || v === ''

export function model(data: TableRow[], o: Opts): TableModel {
  const names = o.columns ?? columnsOf(data)
  const columns = names.map((name) => {
    const vals = data.map((r) => r[name]).filter((v) => !isBlank(v))
    return { name, numeric: vals.length > 0 && vals.every((v) => typeof v === 'number') }
  })
  return { columns, rows: data, labelColumn: columns.find((c) => !c.numeric)?.name }
}

const text = (v: Value | undefined, numeric: boolean, fmt: Formatters) => (isBlank(v) ? '–' : numeric && typeof v === 'number' ? fmt.value(v) : String(v))
const rowName = (m: TableModel, r: TableRow, i: number) => (m.labelColumn && !isBlank(r[m.labelColumn]) ? String(r[m.labelColumn]) : `Row ${i + 1}`)

/** Column widths: content width, shrunk by capping the widest columns until the sum fits. */
export function fitWidths(want: number[], total: number, min: number): number[] {
  const n = want.length
  if (!n) return []
  if (min * n >= total) return want.map(() => total / n)
  const sum = want.reduce((a, b) => a + b, 0)
  if (sum <= total) return want.map((w) => w * (total / sum))
  // water-fill: the cap c with sum(min(w, c)) = total
  const sorted = [...want].sort((a, b) => a - b)
  let rest = total, cap = 0
  for (let i = 0; i < n; i++) {
    const c = rest / (n - i)
    if (sorted[i]! >= c) { cap = c; break }
    rest -= sorted[i]!
    cap = sorted[i]!
  }
  return want.map((w) => Math.max(min, Math.min(w, cap)))
}

function scales(theme: Palette, m: TableModel, o: Opts): Map<string, ColorScale> {
  const out = new Map<string, ColorScale>()
  const nums = (c: Column) => m.rows.map((r) => r[c.name]).filter((v): v is number => typeof v === 'number')
  const numeric = m.columns.filter((c) => c.numeric)
  const diverging = o.diverging ?? 'auto'
  if (o.scale === 'table') {
    const s = colorScale(theme, numeric.flatMap(nums), { diverging })
    for (const c of numeric) out.set(c.name, s)
  } else for (const c of numeric) out.set(c.name, colorScale(theme, nums(c), { diverging }))
  return out
}

export const tableHeatmap = defineChart<TableRow[], TableHeatmapOptions>({
  id: 'table-heatmap',
  name: 'Table heatmap',
  category: 'matrix',
  intents: ['compare', 'correlation', 'relationship'],
  summary: 'A data table whose numeric columns are shaded by value.',
  input,
  options,
  shape: 'rows',
  aspect: 0.5,
  minHeight: 200,
  maxHeight: 600,
  adapt(raw, o) {
    if (!o.columns || !Array.isArray(raw)) return raw
    const present = columnsOf(raw as TableRow[])
    for (const c of o.columns) if (!present.includes(c)) throw new Error(`option 'columns': column '${c}' not found (columns: ${present.join(', ')})`)
    return raw
  },
  isEmpty: (data, o) => data.length === 0 || (o.columns ?? columnsOf(data)).length === 0,
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    const m = model(ctx.data, o)
    const sc = scales(theme, m, o)
    const size = SIZES.tick
    const headH = Math.ceil(size + 14)
    // widths from the header and every cell's text, plus 8px each side
    const want = m.columns.map((c) => {
      let w = ctx.measure(c.name, size, 600)
      for (const r of m.rows) w = Math.max(w, ctx.measure(text(r[c.name], c.numeric, fmt), size))
      return Math.ceil(w + 16)
    })
    const minW = Math.min(48, ctx.measure('…', size) + 16)
    // the row-naming column keeps up to 35% of the width when the rest has to squeeze
    const L = m.columns.length > 1 ? m.columns.findIndex((c) => c.name === m.labelColumn) : -1
    let widths: number[]
    if (L >= 0 && want.reduce((a, b) => a + b, 0) > ctx.width) {
      const others = want.filter((_, j) => j !== L)
      const wL = Math.min(want[L]!, Math.max(ctx.width * 0.35, ctx.width - others.reduce((a, b) => a + b, 0)))
      const rest = fitWidths(others, ctx.width - wL, minW)
      widths = m.columns.map((_, j) => (j === L ? wL : rest[j < L ? j : j - 1]!))
    } else widths = fitWidths(want, ctx.width, minW)
    const padOf = (w: number) => Math.min(8, Math.max(3, w * 0.12))
    const xs: number[] = []
    widths.reduce((x, w, i) => { xs[i] = x; return x + w }, 0)
    // row height: up to a comfortable 2.4 lines, down to 1px; beyond that, the first rows only
    const avail = Math.max(1, ctx.height - headH - 1)
    const rowH = Math.max(1, Math.min(Math.round(size * 2.4), avail / m.rows.length))
    const visible = Math.min(m.rows.length, Math.floor(avail / rowH))
    const values = o.showValues && rowH >= size * 1.2
    const gap = rowH >= 6 ? 1 : 0
    const marks: Mark[] = []
    // header
    m.columns.forEach((c, j) => {
      const pad = padOf(widths[j]!)
      const t = ctx.truncate(c.name, (widths[j]! - 2 * pad) * 0.94, size) // bold runs ~6% wider than the regular face truncate measures
      marks.push({ type: 'text', layer: 'axes', key: `h:${c.name}`, x: c.numeric ? xs[j]! + widths[j]! - pad : xs[j]! + pad, y: headH / 2, text: t, full: t !== c.name ? c.name : undefined, anchor: c.numeric ? 'end' : 'start', baseline: 'central', size, weight: 600, fill: theme.ink, className: t !== c.name ? 'oc-trunc' : undefined })
    })
    marks.push({ type: 'line', layer: 'axes', key: 'h-rule', x1: 0, x2: ctx.width, y1: headH - 0.5, y2: headH - 0.5, stroke: theme.axis, strokeWidth: 1, inert: true })
    const stripe = theme.mix(theme.surface, theme.track, 0.35)
    let nulls = 0
    for (let i = 0; i < visible; i++) {
      const r = m.rows[i]!
      const y = headH + i * rowH
      const who = rowName(m, r, i)
      m.columns.forEach((c, j) => {
        const v = r[c.name]
        const x = xs[j]!, w = widths[j]!
        const s = sc.get(c.name)
        const display = text(v, c.numeric, fmt)
        const key = `${i}|${c.name}`
        const datum = { row: i, column: c.name, value: v ?? null }
        const label = `${who}, ${c.name}: ${isBlank(v) ? 'empty' : display}`
        const tooltip = { title: who, rows: [[c.name, isBlank(v) ? 'empty' : display]] as Array<[string, string]> }
        let fill: string, ink = theme.ink
        if (c.numeric && typeof v === 'number' && s) {
          fill = s.paint(v)
          ink = inkOn(theme, s.t(v), s.diverging)
        } else if (c.numeric) {
          nulls++
          fill = theme.surface
          ink = theme.muted
        } else fill = i % 2 ? stripe : theme.surface
        marks.push({ type: 'rect', role: 'cell', key, x: x + gap / 2, y: y + gap / 2, width: Math.max(0.5, w - gap), height: Math.max(0.5, rowH - gap), fill, datum, label, tooltip: { ...tooltip, color: c.numeric ? fill : undefined } })
        if (c.numeric && isBlank(v) && rowH >= 6) marks.push({ type: 'path', key: `hatch:${key}`, d: hatchPath(x + gap / 2, y + gap / 2, w - gap, rowH - gap), fill: 'none', stroke: theme.grid, strokeWidth: 1, inert: true })
        if (!values) return
        const pad = padOf(w)
        // a number cut short is a different number: numbers that do not fit are left to the tooltip
        if (c.numeric && ctx.measure(display, size) > w - 2 * pad) return
        const t = ctx.truncate(display, w - 2 * pad, size)
        if (!t) return
        marks.push({ type: 'text', layer: 'labels', key: `t:${key}`, x: c.numeric ? x + w - pad : x + pad, y: y + rowH / 2, text: t, full: t !== display ? display : undefined, anchor: c.numeric ? 'end' : 'start', baseline: 'central', size, fill: ink, inert: true })
      })
    }
    const numeric = m.columns.filter((c) => c.numeric)
    const one = o.scale === 'table' && numeric.length ? sc.get(numeric[0]!.name) : undefined
    const legend = one
      ? { gradient: one.legend(fmt) }
      : numeric.length
        ? { title: 'Shaded per column', gradient: { stops: [...sc.values()].some((s) => s.diverging) ? theme.divStops() : theme.seqStops(), min: 'low', max: 'high' } }
        : undefined
    return {
      marks,
      plot: { x: 0, y: headH, width: ctx.width, height: visible * rowH },
      legend,
      note: notes([[m.rows.length - visible, `more rows not shown (showing ${visible} of ${m.rows.length})`], [nulls, 'empty numeric cells hatched']]),
    }
  },
  describe({ data, options: o, fmt, name }) {
    const m = model(data, o)
    const numeric = m.columns.filter((c) => c.numeric)
    const head = `${name}, ${phrase.count(m.rows.length, 'row')} by ${phrase.count(m.columns.length, 'column')} (${numeric.length} numeric)`
    if (!numeric.length) return head
    if (o.scale === 'table') {
      const items = m.rows.flatMap((r, i) => numeric.map((c) => ({ label: `${rowName(m, r, i)} ${c.name}`, value: typeof r[c.name] === 'number' ? (r[c.name] as number) : null })))
      return `${head}, ${extremesOf(items, fmt, 'cell')}`
    }
    const c = numeric[0]!
    const items = m.rows.map((r, i) => ({ label: rowName(m, r, i), value: typeof r[c.name] === 'number' ? (r[c.name] as number) : null }))
    return `${head}, ${c.name} ${extremesOf(items, fmt, 'row')}`
  },
  table(data, o) {
    const m = model(data, o)
    return {
      columns: m.columns.map((c) => c.name),
      rows: m.rows.map((r) => m.columns.map((c) => {
        const v = r[c.name]
        return v === undefined || v === null ? null : typeof v === 'boolean' ? String(v) : v
      })),
    }
  },
})

export default tableHeatmap
