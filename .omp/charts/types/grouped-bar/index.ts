// Grouped bar chart: per category, one bar per series side by side. Hiding a series
// in the legend regroups the remaining bars to fill the band.

import { defineChart, phrase } from '../../core'
import { finiteExtent, notes } from '../../core/helpers'
import type { LegendItem, Mark } from '../../core'
import { bandOf, barGeom, cell, collapseSeries, endLabel, wideTable } from '../_shared/ComparisonA'
import { z } from 'zod'
import { orientation, seriesInput } from '../_shared/ComparisonA'

/** Long rows: one bar per (label, series). Duplicate pairs are summed (and noted). */
export const input = seriesInput

export const options = z.object({
  orientation,
  showValues: z.boolean().default(false).describe('print each value at the end of its bar (dropped when bars are too thin)'),
  groupPadding: z.number().min(0).max(0.8).default(0.2).describe('gap between category groups, as a share of the step'),
  xTitle: z.string().optional(),
  yTitle: z.string().optional(),
})

export type GroupedRow = z.output<typeof input>[number]
export type GroupedOptions = z.output<typeof options>

export const groupedBar = defineChart<GroupedRow[], GroupedOptions>({
  id: 'grouped-bar',
  name: 'Grouped bar chart',
  category: 'comparison',
  intents: ['compare', 'rank'],
  summary: 'Bars side by side per category, one per series, for comparing series within and across categories.',
  input,
  options,
  shape: 'rows',
  fields: ['label', 'series', 'value'],
  numberFields: ['value'],
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    const t = collapseSeries(ctx.data)
    const horizontal = o.orientation === 'horizontal'
    const visible = t.series.filter((s) => !ctx.hidden.has(s))
    const vals = t.labels.flatMap((l) => visible.map((s) => cell(t, l, s)))
    const ext = finiteExtent(vals) ?? [0, 1]
    const domain: [number, number] = [Math.min(0, ext[0]), Math.max(0, ext[1])]
    const cat = { type: 'band' as const, domain: t.labels, padding: o.groupPadding }
    const val = { type: 'linear' as const, domain, zero: true }
    const valueW = o.showValues ? Math.max(ctx.measure(fmt.value(ext[0])), ctx.measure(fmt.value(ext[1]))) + 6 : 0
    const f = ctx.cartesian({
      x: horizontal ? val : cat,
      y: horizontal ? cat : val,
      xTitle: o.xTitle,
      yTitle: o.yTitle,
      pad: horizontal ? { right: ext[1] > 0 ? valueW : 0, left: ext[0] < 0 ? valueW : 0 } : { top: o.showValues ? 14 : 0, bottom: o.showValues && ext[0] < 0 ? 14 : 0 },
    })
    const marks: Mark[] = [...f.marks]
    const band = bandOf(f, horizontal)
    const n = Math.max(1, visible.length)
    const step = band / n
    const thick = n > 1 ? step * 0.88 : step
    const fits = horizontal ? thick >= 9 : thick >= valueW * 0.9
    for (const l of t.labels) {
      visible.forEach((s, j) => {
        const v = cell(t, l, s)
        if (v === null) return
        const si = t.series.indexOf(s)
        const fill = theme.cat(si)
        const off = j * step + (step - thick) / 2
        marks.push({
          type: 'rect', role: 'bar', key: `${l}::${s}`, series: s, ...barGeom(f, horizontal, l, off, thick, 0, v),
          rx: Math.min(2, thick / 4), fill, pattern: si,
          datum: { label: l, series: s, value: v }, label: `${l}, ${s}: ${fmt.value(v)}`,
          tooltip: { title: l, rows: [[s, fmt.value(v)]], color: fill },
        })
        if (o.showValues && fits) {
          marks.push({ type: 'text', layer: 'labels', key: `v:${l}::${s}`, ...endLabel(f, horizontal, l, off, thick, v), text: fmt.value(v), fill: theme.ink, inert: true })
        }
      })
    }
    const legend: LegendItem[] = t.series.map((s, i) => ({ key: s, label: s, color: theme.cat(i), shape: 'square', pattern: i }))
    return {
      marks,
      plot: f.plot,
      legend: t.series.length > 1 ? { items: legend } : undefined,
      note: notes([[t.duplicates, 'duplicate label/series pairs summed'], [t.nulls, 'missing values left blank']]),
    }
  },
  describe({ data, fmt, name }) {
    const t = collapseSeries(data)
    const items = t.labels.flatMap((l) => t.series.map((s) => ({ label: t.series.length > 1 ? `${l} (${s})` : l, value: cell(t, l, s) })))
    return `${name}, ${phrase.count(t.labels.length, 'category', 'categories')}, ${phrase.count(t.series.length, 'series', 'series')} (${phrase.listPhrase(t.series)}), ${phrase.extremes(items, fmt)}`
  },
  table(data) {
    return wideTable(collapseSeries(data))
  },
})

export default groupedBar
