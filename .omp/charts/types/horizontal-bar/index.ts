// Horizontal bar chart: one bar per category, categories down the y axis so long
// names read horizontally.

import { defineChart, phrase } from '../../core'
import { collapseLabels, finiteExtent, notes, sortItems } from '../../core/helpers'
import type { Mark } from '../../core'
import { barGeom, endLabel } from '../_shared/ComparisonA'
import { input, options, type HBarOptions, type HBarRow } from './schema'

export const horizontalBar = defineChart<HBarRow[], HBarOptions>({
  id: 'horizontal-bar',
  name: 'Horizontal bar chart',
  category: 'comparison',
  intents: ['compare', 'rank'],
  summary: 'Horizontal bars comparing one value across categories; long names read horizontally.',
  input,
  options,
  shape: 'rows',
  fields: ['label', 'value'],
  numberFields: ['value'],
  aspect: 0.7,
  minHeight: 260,
  maxHeight: 640,
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    const { items: raw, duplicates } = collapseLabels(ctx.data)
    const items = sortItems(raw, o.sort)
    const ext = finiteExtent(items.map((d) => d.value)) ?? [0, 1]
    const longest = o.showValues ? Math.max(ctx.measure(fmt.value(ext[0])), ctx.measure(fmt.value(ext[1]))) + 6 : 0
    const f = ctx.cartesian({
      y: { type: 'band', domain: items.map((d) => d.label), padding: 0.2 },
      x: { type: 'linear', domain: [Math.min(0, ext[0]), Math.max(0, ext[1])], zero: true },
      xTitle: o.xTitle,
      yTitle: o.yTitle,
      bandLabelMax: o.labelWidth,
      pad: { right: ext[1] > 0 ? longest : 0, left: ext[0] < 0 ? longest : 0 },
    })
    const marks: Mark[] = [...f.marks]
    const showVals = o.showValues && f.bh >= 9
    items.forEach((d, i) => {
      if (d.value === null) return
      const fill = o.colorBy === 'category' ? theme.cat(i) : theme.cat(0)
      marks.push({
        type: 'rect', role: 'bar', key: d.label, ...barGeom(f, true, d.label, 0, f.bh, 0, d.value),
        rx: Math.min(2, f.bh / 4), fill, pattern: o.colorBy === 'category' ? i : undefined,
        datum: d, label: `${d.label}: ${fmt.value(d.value)}`, tooltip: { title: d.label, rows: [['Value', fmt.value(d.value)]], color: fill },
      })
      if (showVals) {
        const p = endLabel(f, true, d.label, 0, f.bh, d.value)
        marks.push({ type: 'text', layer: 'labels', key: `v:${d.label}`, ...p, text: fmt.value(d.value), fill: theme.ink, inert: true })
      }
    })
    const nulls = items.filter((d) => d.value === null).length
    return { marks, plot: f.plot, note: notes([[duplicates, 'duplicate labels summed'], [nulls, 'missing values left blank']]) }
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

export default horizontalBar
