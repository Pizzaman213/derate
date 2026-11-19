// 100% stacked bar chart: each category's bar is its mix of series, as shares of
// that category's total (d3 'expand' offset). Negative values have no share of a
// whole and are left out; a category whose total is zero is drawn as an empty
// outline and noted.

import { defineChart, phrase } from '../../core'
import { stackRows } from '../../adapters'
import { notes } from '../../core/helpers'
import type { LegendItem, Mark } from '../../core'
import { barGeom, cell, collapseSeries, type SeriesTable } from '../_shared/ComparisonA'
import { input, options, type Stacked100Options, type Stacked100Row } from './schema'

/** Per-category shares: label -> series -> [y0, y1] in 0..1, plus totals (negatives excluded). */
export function shares(t: SeriesTable, series: string[]): { layers: Array<{ key: string; spans: Map<string, [number, number]> }>; totals: Map<string, number> } {
  const rows = t.labels.flatMap((l) => series.map((s) => ({ label: l, series: s, value: Math.max(0, cell(t, l, s) ?? 0) })))
  const st = stackRows(rows, { x: 'label', series: 'series', value: 'value', offset: 'expand', seriesOrder: series })
  const totals = new Map(t.labels.map((l) => [l, series.reduce((a, s) => a + Math.max(0, cell(t, l, s) ?? 0), 0)]))
  return {
    totals,
    layers: st.layers.map((layer) => ({ key: layer.key, spans: new Map(layer.points.map((p) => [String(p.x), [p.y0, p.y1] as [number, number]])) })),
  }
}

export const stackedBar100 = defineChart<Stacked100Row[], Stacked100Options>({
  id: 'stacked-bar-100',
  name: '100% stacked bar chart',
  category: 'comparison',
  intents: ['composition', 'compare'],
  summary: 'Each category as a full-height bar split into its series shares, so mixes compare across categories.',
  input,
  options,
  shape: 'rows',
  fields: ['label', 'series', 'value'],
  numberFields: ['value'],
  isEmpty(data) {
    if (!data.length) return true
    return data.every((r) => r.value === null || r.value <= 0) ? 'All values are zero' : false
  },
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    const t = collapseSeries(ctx.data)
    const horizontal = o.orientation === 'horizontal'
    const visible = t.series.filter((s) => !ctx.hidden.has(s))
    const { layers, totals } = shares(t, visible)
    const pct = (v: number) => fmt.pct(v)
    const cat = { type: 'band' as const, domain: t.labels, padding: 0.25 }
    const val = { type: 'linear' as const, domain: [0, 1] as [number, number], nice: false }
    const f = ctx.cartesian({
      x: horizontal ? val : cat,
      y: horizontal ? cat : val,
      xFormat: horizontal ? pct : undefined,
      yFormat: horizontal ? undefined : pct,
      xTitle: o.xTitle,
      yTitle: o.yTitle,
    })
    const marks: Mark[] = [...f.marks]
    const thick = horizontal ? f.bh : f.bw
    const zeroTotal = t.labels.filter((l) => (totals.get(l) ?? 0) === 0)
    for (const l of zeroTotal) {
      marks.push({ type: 'rect', layer: 'back', key: `empty:${l}`, ...barGeom(f, horizontal, l, 0, thick, 0, 1), fill: 'none', stroke: theme.muted, strokeWidth: 1, dash: '3 3', inert: true })
    }
    for (const l of t.labels) {
      for (const layer of layers) {
        const si = t.series.indexOf(layer.key)
        const fill = theme.cat(si)
        const v = cell(t, l, layer.key)
        const span = layer.spans.get(l)
        if (v === null || v <= 0 || !span || span[1] - span[0] <= 0) continue
        const share = span[1] - span[0]
        const g = barGeom(f, horizontal, l, 0, thick, span[0], span[1])
        marks.push({
          type: 'rect', role: 'bar', key: `${l}::${layer.key}`, series: layer.key, ...g,
          fill, stroke: thick >= 6 ? theme.surface : undefined, strokeWidth: thick >= 6 ? 0.5 : undefined, pattern: si,
          datum: { label: l, series: layer.key, value: v, share }, label: `${l}, ${layer.key}: ${pct(share)} (${fmt.value(v)})`,
          tooltip: { title: l, rows: [[layer.key, fmt.value(v)], ['Share', pct(share)], ['Total', fmt.value(totals.get(l) ?? 0)]], color: fill },
        })
        const text = pct(share)
        if (o.showValues && g.width >= ctx.measure(text) + 6 && g.height >= 14) {
          marks.push({ type: 'text', layer: 'labels', key: `v:${l}::${layer.key}`, x: g.x + g.width / 2, y: g.y + g.height / 2, text, anchor: 'middle', baseline: 'central', fill: theme.ink, halo: true, inert: true })
        }
      }
    }
    const negatives = ctx.data.filter((r) => r.value !== null && r.value < 0).length
    const legend: LegendItem[] = t.series.map((s, i) => ({ key: s, label: s, color: theme.cat(i), shape: 'square', pattern: i }))
    return {
      marks,
      plot: f.plot,
      legend: t.series.length > 1 ? { items: legend } : undefined,
      note: notes([
        [zeroTotal.length, zeroTotal.length === 1 ? 'category with a zero total left empty' : 'categories with a zero total left empty'],
        [negatives, 'negative values omitted (no share of a whole)'],
        [t.duplicates, 'duplicate label/series pairs summed'],
        [t.nulls, 'missing values left out'],
      ]),
    }
  },
  describe({ data, fmt, name }) {
    const t = collapseSeries(data)
    const { layers } = shares(t, t.series)
    let best = { label: '', series: '', share: -1 }
    for (const layer of layers) for (const [l, [a, b]] of layer.spans) if (b - a > best.share) best = { label: l, series: layer.key, share: b - a }
    const top = best.share > 0 ? `, largest share ${best.series} in ${best.label} at ${fmt.pct(best.share)}` : ''
    return `${name}, ${phrase.count(t.labels.length, 'bar')} of ${phrase.count(t.series.length, 'series', 'series')} (${phrase.listPhrase(t.series)})${top}`
  },
  table(data) {
    const t = collapseSeries(data)
    const { layers, totals } = shares(t, t.series)
    const share = (l: string, s: string) => {
      const span = layers.find((x) => x.key === s)?.spans.get(l)
      return span && (totals.get(l) ?? 0) > 0 ? span[1] - span[0] : null
    }
    return {
      columns: ['Label', ...t.series, ...t.series.map((s) => `${s} share`)],
      rows: t.labels.map((l) => [l, ...t.series.map((s) => cell(t, l, s)), ...t.series.map((s) => share(l, s))]),
    }
  },
})

export default stackedBar100
