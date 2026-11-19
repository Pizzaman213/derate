// Diverging bar chart: horizontal bars running left or right of a centre line
// (zero, a target, a scale midpoint), coloured by side.

import { defineChart, phrase } from '../../core'
import { collapseLabels, finiteExtent, notes, sortItems } from '../../core/helpers'
import type { LegendItem, Mark } from '../../core'
import { barGeom, endLabel } from '../_shared/ComparisonA'
import { input, options, type DivergingOptions, type DivergingRow } from './schema'

export const divergingBar = defineChart<DivergingRow[], DivergingOptions>({
  id: 'diverging-bar',
  name: 'Diverging bar chart',
  category: 'comparison',
  intents: ['deviation', 'compare'],
  summary: 'Bars extending left or right of a centre line, coloured by side: deviation from zero, a target or a scale midpoint.',
  input,
  options,
  shape: 'rows',
  fields: ['label', 'value'],
  numberFields: ['value'],
  aspect: 0.7,
  minHeight: 240,
  maxHeight: 640,
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    const mid = o.midpoint
    const { items: raw, duplicates } = collapseLabels(ctx.data)
    const items = sortItems(raw, o.sort)
    const ext = finiteExtent(items.map((d) => d.value)) ?? [mid, mid]
    let lo = Math.min(mid, ext[0]), hi = Math.max(mid, ext[1])
    if (o.symmetric) { const m = Math.max(mid - lo, hi - mid); lo = mid - m; hi = mid + m }
    const valueW = o.showValues ? Math.max(ctx.measure(fmt.value(ext[0])), ctx.measure(fmt.value(ext[1]))) + 6 : 0
    const f = ctx.cartesian({
      y: { type: 'band', domain: items.map((d) => d.label), padding: 0.2 },
      x: { type: 'linear', domain: [lo, hi], zero: false },
      xTitle: o.xTitle,
      yTitle: o.yTitle,
      pad: { right: valueW, left: valueW },
    })
    const midIsZeroLine = mid === 0 && (f.x.domain() as number[]).some((v) => v < 0) && (f.x.domain() as number[]).some((v) => v > 0)
    const marks: Mark[] = [...f.marks]
    if (!midIsZeroLine) {
      const px = f.px(mid)
      marks.push({ type: 'line', layer: 'grid', key: 'midline', x1: px, x2: px, y1: f.plot.y, y2: f.plot.y + f.plot.height, stroke: theme.ruleStrong, strokeWidth: 1, inert: true })
    }
    const above = o.aboveLabel ?? (mid === 0 ? 'Positive' : `Above ${fmt.value(mid)}`)
    const below = o.belowLabel ?? (mid === 0 ? 'Negative' : `Below ${fmt.value(mid)}`)
    const showVals = o.showValues && f.bh >= 9
    for (const d of items) {
      if (d.value === null) continue
      const v = d.value
      const side = v > mid ? 'above' : v < mid ? 'below' : undefined
      const fill = side === 'above' ? theme.positive : side === 'below' ? theme.negative : theme.neutral
      const diff = v - mid
      const rows: Array<[string, string]> = [['Value', fmt.value(v)]]
      if (mid !== 0) rows.push([`vs ${fmt.value(mid)}`, `${diff > 0 ? '+' : ''}${fmt.value(diff)}`])
      marks.push({
        type: 'rect', role: 'bar', key: d.label, series: side, ...barGeom(f, true, d.label, 0, f.bh, mid, v),
        rx: Math.min(2, f.bh / 4), fill, pattern: side === 'above' ? 0 : side === 'below' ? 1 : undefined,
        datum: d, label: mid === 0 ? `${d.label}: ${fmt.value(v)}` : `${d.label}: ${fmt.value(v)}, ${diff > 0 ? '+' : ''}${fmt.value(diff)} vs ${fmt.value(mid)}`,
        tooltip: { title: d.label, rows, color: fill },
      })
      if (showVals) marks.push({ type: 'text', layer: 'labels', key: `v:${d.label}`, ...endLabel(f, true, d.label, 0, f.bh, v, mid), text: fmt.value(v), fill: theme.ink, inert: true })
    }
    const legend: LegendItem[] = [
      { key: 'above', label: above, color: theme.positive, shape: 'square', pattern: 0 },
      { key: 'below', label: below, color: theme.negative, shape: 'square', pattern: 1 },
    ]
    const nulls = items.filter((d) => d.value === null).length
    return { marks, plot: f.plot, legend: { items: legend }, note: notes([[duplicates, 'duplicate labels summed'], [nulls, 'missing values left blank']]) }
  },
  describe({ data, options: o, fmt, name }) {
    const { items } = collapseLabels(data)
    const mid = o.midpoint ?? 0
    const ok = items.filter((d) => d.value !== null)
    const up = ok.filter((d) => d.value! > mid).length, down = ok.filter((d) => d.value! < mid).length
    const ref = mid === 0 ? '' : ` ${fmt.value(mid)}`
    return `${name}, ${phrase.count(items.length, 'bar')} around${ref || ' zero'}, ${up} above and ${down} below, ${phrase.extremes(items, fmt)}`
  },
  table(data, o) {
    const { items } = collapseLabels(data)
    const mid = o.midpoint ?? 0
    return { columns: ['Label', 'Value', 'Difference from midpoint'], rows: items.map((d) => [d.label, d.value, d.value === null ? null : d.value - mid]) }
  },
})

export default divergingBar
