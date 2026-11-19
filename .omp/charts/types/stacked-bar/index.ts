// Stacked bar chart: one bar per category built from its series. Diverging offset:
// positive segments stack up from zero, negative ones down. Hiding a series restacks.

import { defineChart, phrase } from '../../core'
import { stackRows } from '../../adapters'
import { notes } from '../../core/helpers'
import type { LegendItem, Mark } from '../../core'
import { barGeom, cell, collapseSeries, endLabel, wideTable } from '../_shared/ComparisonA'
import { z } from 'zod'
import { orientation, seriesInput } from '../_shared/ComparisonA'

/** Long rows: one segment per (label, series). Negatives stack below zero; duplicates are summed. */
export const input = seriesInput

export const options = z.object({
  orientation,
  showTotals: z.boolean().default(false).describe('print each bar\'s net total past the end of its stack'),
  xTitle: z.string().optional(),
  yTitle: z.string().optional(),
})

export type StackedRow = z.output<typeof input>[number]
export type StackedOptions = z.output<typeof options>

export const stackedBar = defineChart<StackedRow[], StackedOptions>({
  id: 'stacked-bar',
  name: 'Stacked bar chart',
  category: 'comparison',
  intents: ['compare', 'composition'],
  summary: 'Bars built from stacked series segments: totals per category and what makes them up; negatives stack below zero.',
  input,
  options,
  shape: 'rows',
  fields: ['label', 'series', 'value'],
  numberFields: ['value'],
  isEmpty(data) {
    if (!data.length) return true
    return data.every((r) => r.value === null || r.value === 0) ? 'All values are zero' : false
  },
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    const t = collapseSeries(ctx.data)
    const horizontal = o.orientation === 'horizontal'
    const visible = t.series.filter((s) => !ctx.hidden.has(s))
    const rows = t.labels.flatMap((l) => visible.map((s) => ({ label: l, series: s, value: cell(t, l, s) ?? 0 })))
    const st = stackRows(rows, { x: 'label', series: 'series', value: 'value', offset: 'diverging', seriesOrder: visible })
    // net total and the outer ends of each stack, per category
    const net = new Map<string, number>(), hi = new Map<string, number>(), lo = new Map<string, number>()
    for (const l of t.labels) {
      const vs = visible.map((s) => cell(t, l, s) ?? 0)
      net.set(l, vs.reduce((a, b) => a + b, 0))
      hi.set(l, vs.filter((v) => v > 0).reduce((a, b) => a + b, 0))
      lo.set(l, vs.filter((v) => v < 0).reduce((a, b) => a + b, 0))
    }
    const min = Math.min(0, ...lo.values()), max = Math.max(0, ...hi.values())
    const totalW = o.showTotals ? Math.max(ctx.measure(fmt.value(min)), ctx.measure(fmt.value(max))) + 6 : 0
    const cat = { type: 'band' as const, domain: t.labels, padding: 0.25 }
    const val = { type: 'linear' as const, domain: [min, max] as [number, number], zero: true }
    const f = ctx.cartesian({
      x: horizontal ? val : cat,
      y: horizontal ? cat : val,
      xTitle: o.xTitle,
      yTitle: o.yTitle,
      pad: horizontal ? { right: totalW, left: min < 0 ? totalW : 0 } : { top: o.showTotals ? 14 : 0, bottom: o.showTotals && min < 0 ? 14 : 0 },
    })
    const marks: Mark[] = [...f.marks]
    const thick = horizontal ? f.bh : f.bw
    for (const [i, l] of t.labels.entries()) {
      for (const layer of st.layers) {
        const si = t.series.indexOf(layer.key)
        const fill = theme.cat(si)
        const p = layer.points[i]
        if (!p) continue
        const v = cell(t, l, layer.key)
        if (v === null || v === 0) continue
        marks.push({
          type: 'rect', role: 'bar', key: `${l}::${layer.key}`, series: layer.key, ...barGeom(f, horizontal, l, 0, thick, p.y0, p.y1),
          fill, stroke: thick >= 6 ? theme.surface : undefined, strokeWidth: thick >= 6 ? 0.5 : undefined, pattern: si,
          datum: { label: l, series: layer.key, value: v }, label: `${l}, ${layer.key}: ${fmt.value(v)}`,
          tooltip: { title: l, rows: [[layer.key, fmt.value(v)], ['Total', fmt.value(net.get(l) ?? 0)]], color: fill },
        })
      }
    }
    if (o.showTotals && (horizontal ? f.bh >= 9 : f.bw >= totalW * 0.9)) {
      for (const l of t.labels) {
        const n = net.get(l) ?? 0
        const end = n >= 0 ? hi.get(l) ?? 0 : lo.get(l) ?? 0
        const p = endLabel(f, horizontal, l, 0, thick, end)
        if (n < 0 && !horizontal) { p.y = f.py(end) + 4; p.baseline = 'hanging' }
        if (n < 0 && horizontal) { p.x = f.px(end) - 4; p.anchor = 'end' }
        marks.push({ type: 'text', layer: 'labels', key: `total:${l}`, ...p, text: fmt.value(n), fill: theme.ink, weight: 600, inert: true })
      }
    }
    const legend: LegendItem[] = t.series.map((s, i) => ({ key: s, label: s, color: theme.cat(i), shape: 'square', pattern: i }))
    return {
      marks,
      plot: f.plot,
      legend: t.series.length > 1 ? { items: legend } : undefined,
      note: notes([[t.duplicates, 'duplicate label/series pairs summed'], [t.nulls, 'missing values left out']]),
    }
  },
  describe({ data, fmt, name }) {
    const t = collapseSeries(data)
    const totals = t.labels.map((l) => ({ label: l, value: t.series.reduce((a, s) => a + (cell(t, l, s) ?? 0), 0) }))
    return `${name}, ${phrase.count(t.labels.length, 'bar')} of ${phrase.count(t.series.length, 'series', 'series')} (${phrase.listPhrase(t.series)}), totals: ${phrase.extremes(totals, fmt)}`
  },
  table(data) {
    const t = collapseSeries(data)
    const w = wideTable(t)
    return { columns: [...w.columns, 'Total'], rows: w.rows.map((r, i) => [...r, t.series.reduce((a, s) => a + (cell(t, t.labels[i]!, s) ?? 0), 0)]) }
  },
})

export default stackedBar
