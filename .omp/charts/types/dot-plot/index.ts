// Cleveland dot plot: one line per category, a dot per value on a shared value axis.
// Position on a common scale is the most accurate visual encoding, and the axis does
// not have to include zero, so small differences between large values stay visible.

import { defineChart, phrase } from '../../core'
import { finiteExtent, notes, uniq } from '../../core/helpers'
import type { LegendItem, Mark } from '../../core'
import { input, options, type DotPlotOptions, type DotPlotRow } from './schema'

/** Rows without a series share one; it is only ever shown when other rows have one. */
const seriesOf = (d: DotPlotRow) => d.series ?? '(no series)'

export const dotPlot = defineChart<DotPlotRow[], DotPlotOptions>({
  id: 'dot-plot',
  name: 'Dot plot',
  category: 'comparison',
  intents: ['compare', 'rank'],
  summary: 'Cleveland dot plot: a dot per value on one line per category, several series in colour.',
  input,
  options,
  shape: 'rows',
  fields: ['label', 'value', 'series'],
  numberFields: ['value'],
  minHeight: 160,
  draw(ctx) {
    const { theme, options: o, fmt, hidden } = ctx
    const series = uniq(ctx.data.map(seriesOf))
    const multi = series.length > 1
    const colorOf = new Map(series.map((s, i) => [s, theme.cat(i)]))
    const visible = ctx.data.filter((d) => !hidden.has(seriesOf(d)))
    // category order: first seen, or by the mean of the visible values
    let labels = uniq(ctx.data.map((d) => d.label))
    if (o.sort !== 'none') {
      const mean = new Map<string, number>()
      for (const l of labels) {
        const vs = visible.filter((d) => d.label === l && d.value !== null).map((d) => d.value as number)
        mean.set(l, vs.length ? vs.reduce((a, b) => a + b, 0) / vs.length : NaN)
      }
      const dir = o.sort === 'ascending' ? 1 : -1
      labels = [...labels].sort((a, b) => {
        const ma = mean.get(a)!, mb = mean.get(b)!
        return Number.isNaN(ma) ? 1 : Number.isNaN(mb) ? -1 : dir * (ma - mb)
      })
    }
    const ext = finiteExtent(visible.map((d) => d.value)) ?? finiteExtent(ctx.data.map((d) => d.value)) ?? [0, 1]
    let [lo, hi] = o.zero ? [Math.min(0, ext[0]), Math.max(0, ext[1])] : ext
    // room for a dot's radius past the extremes (a zero end stays flush)
    const span = hi - lo || Math.abs(hi) * 0.2 || 1
    if (!(o.zero && lo === 0)) lo -= span * 0.02
    if (!(o.zero && hi === 0)) hi += span * 0.02
    const f = ctx.cartesian({
      y: { type: 'band', domain: labels, padding: 0 },
      x: { type: 'linear', domain: [lo, hi], zero: o.zero },
      xTitle: o.xTitle,
      yTitle: o.yTitle,
    })
    const marks: Mark[] = [...f.marks]
    const r = Math.max(2, Math.min(5, f.bh * 0.3))
    if (o.guides && f.bh >= 6) {
      for (const l of labels) {
        const y = f.py(l) + f.bh / 2
        marks.push({ type: 'line', layer: 'grid', key: `g:${l}`, x1: f.plot.x, x2: f.plot.x + f.plot.width, y1: y, y2: y, stroke: theme.grid, strokeWidth: 1, dash: '1 3', inert: true })
      }
    }
    // a faint span from the lowest to the highest dot of each category reads the gap
    if (multi && f.bh >= 6) {
      const byLabel = new Map<string, number[]>()
      for (const d of visible) if (d.value !== null) byLabel.set(d.label, [...(byLabel.get(d.label) ?? []), d.value])
      for (const [l, vs] of byLabel) {
        if (vs.length < 2) continue
        const y = f.py(l) + f.bh / 2
        marks.push({ type: 'line', key: `span:${l}`, x1: f.px(Math.min(...vs)), x2: f.px(Math.max(...vs)), y1: y, y2: y, stroke: theme.track, strokeWidth: Math.min(3, r * 0.8), linecap: 'round', inert: true })
      }
    }
    const seen = new Map<string, number>()
    let duplicates = 0
    for (const d of ctx.data) {
      if (d.value === null) continue
      const s = seriesOf(d)
      const id = `${d.label}\u0000${s}`
      const n = (seen.get(id) ?? 0) + 1
      seen.set(id, n)
      if (n > 1) duplicates++
      const color = colorOf.get(s)!
      const name = multi ? `${d.label}, ${s}` : d.label
      marks.push({
        type: 'circle', role: 'point', key: `${d.label}|${s}${n > 1 ? `#${n}` : ''}`, series: multi ? s : undefined,
        cx: f.px(d.value), cy: f.py(d.label) + f.bh / 2, r, fill: color, stroke: theme.surface, strokeWidth: r >= 3 ? 1 : 0,
        pattern: multi ? series.indexOf(s) : undefined,
        datum: d, label: `${name}: ${fmt.value(d.value)}`,
        tooltip: { title: d.label, rows: [[multi ? s : 'Value', fmt.value(d.value)]], color },
      })
    }
    const legend = multi ? { items: series.map((s): LegendItem => ({ key: s, label: s, color: colorOf.get(s)!, shape: 'circle' })) } : undefined
    const nulls = ctx.data.filter((d) => d.value === null).length
    return {
      marks, legend, plot: f.plot,
      note: notes([[duplicates, multi ? 'repeated category/series pairs drawn as separate dots' : 'repeated categories drawn as separate dots'], [nulls, 'missing values skipped']]),
    }
  },
  describe({ data, fmt, name }) {
    const series = uniq(data.map(seriesOf))
    const multi = series.length > 1
    const cats = uniq(data.map((d) => d.label)).length
    const what = multi ? `${phrase.count(cats, 'category', 'categories')}, ${phrase.count(series.length, 'series', 'series')} (${phrase.listPhrase(series)})` : phrase.count(cats, 'category', 'categories')
    const items = data.map((d) => ({ label: multi ? `${d.label} (${seriesOf(d)})` : d.label, value: d.value }))
    return `${name}, ${what}, ${phrase.extremes(items, fmt)}`
  },
  table(data) {
    const multi = uniq(data.map(seriesOf)).length > 1
    return multi
      ? { columns: ['Label', 'Series', 'Value'], rows: data.map((d) => [d.label, seriesOf(d), d.value]) }
      : { columns: ['Label', 'Value'], rows: data.map((d) => [d.label, d.value]) }
  },
})

export default dotPlot
