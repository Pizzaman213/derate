// Vertical bar chart: one bar per category, from zero.

import { defineChart, phrase } from '../../core'
import { collapseLabels, finiteExtent, notes, sortItems } from '../../core/helpers'
import type { Mark } from '../../core'
import { input, options, type BarOptions, type BarRow } from './schema'

export const bar = defineChart<BarRow[], BarOptions>({
  id: 'bar',
  name: 'Bar chart',
  category: 'comparison',
  intents: ['compare', 'rank'],
  summary: 'Vertical bars comparing one value across categories.',
  input,
  options,
  shape: 'rows',
  fields: ['label', 'value'],
  numberFields: ['value'],
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    const { items: raw, duplicates } = collapseLabels(ctx.data)
    const items = sortItems(raw, o.sort)
    const ext = finiteExtent(items.map((d) => d.value)) ?? [0, 1]
    const valueSpace = o.showValues ? ctx.measure('000.0k') : 0
    const f = ctx.cartesian({
      x: { type: 'band', domain: items.map((d) => d.label), padding: 0.2 },
      y: { type: 'linear', domain: [Math.min(0, ext[0]), Math.max(0, ext[1])], zero: true },
      xTitle: o.xTitle,
      yTitle: o.yTitle,
      pad: { top: o.showValues ? 14 : 0 },
    })
    const y0 = f.py(0)
    const marks: Mark[] = [...f.marks]
    items.forEach((d, i) => {
      if (d.value === null) return
      const x = f.px(d.label), yv = f.py(d.value)
      const fill = o.colorBy === 'category' ? theme.cat(i) : theme.cat(0)
      marks.push({
        type: 'rect', role: 'bar', key: d.label, x, width: Math.max(0.5, f.bw), y: d.value === 0 ? y0 - 0.5 : Math.min(y0, yv), height: d.value === 0 ? 1 : Math.abs(yv - y0),
        rx: Math.min(2, f.bw / 4), fill, pattern: o.colorBy === 'category' ? i : undefined,
        datum: d, label: `${d.label}: ${fmt.value(d.value)}`, tooltip: { title: d.label, rows: [['Value', fmt.value(d.value)]], color: fill },
      })
      if (o.showValues && f.bw >= valueSpace * 0.6) {
        marks.push({ type: 'text', layer: 'labels', key: `v:${d.label}`, x: x + f.bw / 2, y: d.value >= 0 ? yv - 4 : yv + 4, text: fmt.value(d.value), anchor: 'middle', baseline: d.value >= 0 ? 'alphabetic' : 'hanging', fill: theme.ink, size: 11, inert: true })
      }
    })
    const nulls = items.filter((d) => d.value === null).length
    return {
      marks,
      plot: f.plot,
      note: notes([[duplicates, 'duplicate labels summed'], [nulls, 'missing values left blank']]),
    }
  },
  describe({ data, fmt, name }) {
    const { items } = collapseLabels(data)
    return `${name}, ${phrase.count(items.length, 'bar')}, ${phrase.extremes(items, fmt)}`
  },
  table(data) {
    const { items } = collapseLabels(data)
    return { columns: ['Label', 'Value'], rows: items.map((d) => [d.label, d.value]) }
  },
})

export default bar
