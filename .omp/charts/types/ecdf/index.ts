// Empirical cumulative distribution: for every x, the share of values <= x, drawn as a step
// function per series (stats.ecdf). Each step is a data mark, so every value is hoverable.

import { defineChart, phrase, stats, type LegendItem, type Mark, type ScaleSpec } from '../../core'
import { finiteExtent, notes, uniq } from '../../core/helpers'
import { finiteValues, padExtent, spread } from '../_shared/DistributionB'
import { input, options, type EcdfOptions, type EcdfRow } from './schema'

const AUTO_POINTS = 60

interface Curve { name: string; values: number[]; steps: Array<{ x: number; p: number }> }

/** Series in first-seen order with their finite (and, on a log scale, positive) values and steps. */
function curvesOf(rows: EcdfRow[], o: EcdfOptions): { curves: Curve[]; multi: boolean; nulls: number; nonPositive: number } {
  const { ok, nulls } = finiteValues(rows)
  const usable = o.scale === 'log' ? ok.filter((d) => d.value > 0) : ok
  const multi = rows.some((r) => r.series !== undefined)
  const names = uniq(usable.map((d) => (multi ? d.row.series ?? '(blank)' : '')))
  const curves = names.map((name) => {
    const values = usable.filter((d) => (multi ? d.row.series ?? '(blank)' : '') === name).map((d) => d.value)
    return { name, values, steps: stats.ecdf(values) }
  })
  return { curves, multi, nulls, nonPositive: ok.length - usable.length }
}

export const ecdf = defineChart<EcdfRow[], EcdfOptions>({
  id: 'ecdf',
  name: 'Empirical CDF',
  category: 'distribution',
  intents: ['distribution'],
  summary: 'Step curve of the share of values at or below each value, one per series.',
  input,
  options,
  shape: 'rows',
  fields: ['value', 'series'],
  numberFields: ['value'],
  isEmpty(data, o) {
    if (data.length === 0) return true
    return curvesOf(data, o).curves.length === 0 ? (o.scale === 'log' ? 'No positive values to plot on a log scale' : 'No values to plot') : false
  },
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    const { curves, multi, nulls, nonPositive } = curvesOf(ctx.data, o)
    const ext0 = finiteExtent(curves.flatMap((c) => [c.steps[0]?.x, c.steps[c.steps.length - 1]?.x])) ?? [0, 1]
    const ext = o.scale === 'log' ? ext0 : padExtent(ext0, 0.03)
    const xSpec: ScaleSpec = o.scale === 'log' ? { type: 'log', domain: ext } : { type: 'linear', domain: ext, zero: false }
    const f = ctx.cartesian({
      x: xSpec,
      y: { type: 'linear', domain: [0, 1], nice: false },
      yFormat: (v: number) => fmt.pct(v),
      xTitle: o.valueTitle,
      yTitle: o.yTitle,
      grid: 'both',
      pad: { top: 4 },
    })
    const left = f.plot.x, right = f.plot.x + f.plot.width
    const marks: Mark[] = [...f.marks]
    curves.forEach((c, si) => {
      if (ctx.hidden.has(c.name)) return
      const color = theme.cat(si)
      const n = c.values.length
      const y0 = f.py(0)
      // 0.01 px, as core rounds the step circles, so path and circles agree exactly
      const px = (v: number) => Math.round(v * 100) / 100
      let d = `M${px(left)},${px(y0)}`
      for (const s of c.steps) d += `H${px(f.px(s.x))}V${px(f.py(s.p))}`
      d += `H${px(right)}`
      marks.push({ type: 'path', key: `curve:${c.name}`, d, fill: 'none', stroke: color, strokeWidth: 2, linejoin: 'miter', series: multi ? c.name : undefined, inert: true })
      const dots = o.showPoints === 'always' || (o.showPoints === 'auto' && c.steps.length <= AUTO_POINTS)
      const prefix = multi ? `${c.name}: ` : ''
      for (const s of c.steps) {
        const k = Math.round(s.p * n)
        marks.push({
          type: 'circle', role: 'step', key: `step:${c.name}:${s.x}`,
          cx: f.px(s.x), cy: f.py(s.p), r: dots ? 3 : 2.5,
          fill: color, fillOpacity: dots ? 1 : 0, stroke: dots ? theme.surface : undefined, strokeWidth: dots ? 1 : undefined,
          series: multi ? c.name : undefined,
          datum: { series: multi ? c.name : undefined, value: s.x, share: s.p, count: k },
          label: `${prefix}${fmt.pct(s.p)} of values ≤ ${fmt.value(s.x)} (${k} of ${n})`,
          tooltip: { title: multi ? c.name : `≤ ${fmt.value(s.x)}`, rows: [...(multi ? [['Value', `≤ ${fmt.value(s.x)}`] as [string, string]] : []), ['Share', fmt.pct(s.p)], ['Count', `${k} of ${n}`]], color },
        })
      }
    })
    const legend: LegendItem[] = multi ? curves.map((c, i) => ({ key: c.name, label: c.name, color: theme.cat(i), shape: 'line' })) : []
    return {
      marks,
      plot: f.plot,
      legend: legend.length > 1 ? { items: legend } : undefined,
      note: notes([[nulls, 'missing values skipped'], [nonPositive, 'values ≤ 0 skipped on the log scale']]),
    }
  },
  describe({ data, options: o, fmt, name }) {
    const { curves, multi } = curvesOf(data, o)
    const all = curves.flatMap((c) => c.values)
    let s = `${name}, ${phrase.count(all.length, 'value')}`
    if (multi) s += ` in ${phrase.count(curves.length, 'series', 'series')}`
    s += `, ${spread(all, fmt)}`
    if (multi && curves.length > 1) {
      const medians = curves.map((c) => ({ label: c.name, value: stats.median(c.values) }))
      if (medians.some((m) => m.value !== medians[0]!.value)) s += `; medians ${phrase.extremes(medians, fmt)}`
    }
    return s
  },
  table(data, o) {
    const { curves, multi } = curvesOf(data, o)
    return {
      columns: [...(multi ? ['Series'] : []), 'Value', 'Count ≤ value', 'Share ≤ value'],
      rows: curves.flatMap((c) => c.steps.map((s) => [...(multi ? [c.name] : []), s.x, Math.round(s.p * c.values.length), s.p])),
    }
  },
})

export default ecdf
