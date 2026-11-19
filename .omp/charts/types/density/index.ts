// Density plot: a kernel density estimate per series on one shared grid, as a filled
// area (or a line). Every curve integrates to 1, so series of different sizes compare
// by shape.

import { defineChart, phrase, stats } from '../../core'
import type { Mark } from '../../core'
import { notes } from '../../core/helpers'
import { adaptValueList, bandwidthFor, boxOfPts, densityCurve, densityFormat, emptyValues, focusRange, groupValues, lineD, polyD, type Pt } from '../_shared/DistributionA'
import { input, options, type DensityOptions, type DensityRow } from './schema'

export interface SeriesCurve {
  name: string
  index: number
  values: number[]
  bandwidth: number
  curve: Pt[]
  peak: Pt
}

const GRID = 200

/** KDE per series on a shared grid (pure; the tests read it). `view` is the x range drawn. */
export function curves(data: readonly DensityRow[], o: Pick<DensityOptions, 'bandwidth' | 'kernel'> & Partial<Pick<DensityOptions, 'focus'>>): { list: SeriesCurve[]; nulls: number; extent: [number, number]; view: [number, number]; hidden: number } {
  const { groups, nulls } = groupValues(data, (r) => r.series ?? '')
  const pooled = groups.flatMap((g) => g.values)
  if (!pooled.length) return { list: [], nulls, extent: [0, 1], view: [0, 1], hidden: 0 }
  const hs = groups.map((g) => bandwidthFor(g.values, o.bandwidth, pooled))
  const [lo, hi] = stats.extent(pooled)!
  // gaussian tails out to 4h hold all but 0.006% of the mass; epanechnikov ends at h
  const reach = Math.max(...hs) * (o.kernel === 'gaussian' ? 4 : 1)
  const extent: [number, number] = [lo - reach, hi + reach]
  const focus = o.focus === 'auto' ? focusRange(pooled) : null
  const view: [number, number] = focus ? [focus.range[0] - reach, focus.range[1] + reach] : extent
  const list = groups.map((g, i) => {
    const curve = densityCurve(g.values, hs[i]!, extent, GRID, o.kernel)
    const peak = curve.reduce((a, p) => (p.y > a.y ? p : a), curve[0]!)
    return { name: g.name, index: g.index, values: g.values, bandwidth: hs[i]!, curve, peak }
  })
  return { list, nulls, extent, view, hidden: focus?.hidden ?? 0 }
}

export const density = defineChart<DensityRow[], DensityOptions>({
  id: 'density',
  name: 'Density plot',
  category: 'distribution',
  intents: ['distribution'],
  summary: 'Smoothed distribution (kernel density estimate) of one or more series.',
  input,
  options,
  shape: 'object',
  fields: ['value', 'series'],
  numberFields: ['value'],
  adapt: (raw, o) => adaptValueList(raw, o.fields),
  isEmpty: (data) => emptyValues(data),
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    const { list, nulls, view, hidden } = curves(ctx.data, o)
    const multi = list.length > 1
    const visible = list.filter((s) => !ctx.hidden.has(s.name))
    const inView = (p: Pt) => p.x >= view[0] && p.x <= view[1]
    const ymax = Math.max(1e-12, ...visible.flatMap((s) => s.curve.filter(inView).map((p) => p.y)))
    const n = list.reduce((t, s) => t + s.values.length, 0)
    const rug = o.rug && n <= 2000
    const f = ctx.cartesian({
      x: { type: 'linear', domain: view, zero: false, nice: false },
      y: { type: 'linear', domain: [0, ymax], zero: true },
      yFormat: densityFormat(fmt, ymax),
      xTitle: o.xTitle,
      yTitle: o.yTitle ?? 'Density',
    })
    const marks: Mark[] = [...f.marks]
    const y0 = f.py(0)
    for (const s of visible) {
      const color = theme.cat(s.index)
      const pts = s.curve.filter(inView).map((p) => [f.px(p.x), f.py(p.y)] as const)
      if (pts.length < 2) continue
      const area = [[pts[0]![0], y0] as const, ...pts, [pts[pts.length - 1]![0], y0] as const]
      const label = `${multi ? `${s.name}: ` : ''}peak density at ${fmt.value(s.peak.x)}, mean ${fmt.value(stats.mean(s.values))}, ${phrase.count(s.values.length, 'value')}`
      const data = {
        type: 'path' as const, role: 'density', key: `d:${s.name}`, series: multi ? s.name : undefined,
        cx: f.px(s.peak.x), cy: f.py(s.peak.y), box: boxOfPts(area),
        datum: { series: s.name || undefined, n: s.values.length, bandwidth: s.bandwidth, peak: s.peak.x },
        label,
        tooltip: { title: s.name || 'All values', rows: [['Values', fmt.integer(s.values.length)], ['Mean', fmt.value(stats.mean(s.values))], ['Median', fmt.value(stats.median(s.values))], ['Peak at', fmt.value(s.peak.x)], ['Bandwidth', fmt.value(s.bandwidth)]] as Array<[string, string]>, color },
      }
      if (o.fill) {
        marks.push({ ...data, d: polyD(area), fill: color, fillOpacity: multi ? 0.22 : 0.3, pattern: multi ? s.index : undefined })
        marks.push({ type: 'path', key: `l:${s.name}`, series: multi ? s.name : undefined, d: lineD(pts), fill: 'none', stroke: color, strokeWidth: 2, inert: true })
      } else {
        marks.push({ ...data, d: lineD(pts), fill: 'none', stroke: color, strokeWidth: 2 })
      }
      if (rug) {
        const len = Math.min(8, f.plot.height * 0.05)
        s.values.forEach((v, i) => { if (v >= view[0] && v <= view[1]) marks.push({ type: 'line', layer: 'labels', key: `r:${s.name}:${i}`, x1: f.px(v), x2: f.px(v), y1: y0, y2: y0 - len, stroke: color, strokeWidth: 1, strokeOpacity: 0.6, inert: true }) })
      }
    }
    return {
      marks,
      plot: f.plot,
      legend: multi ? { items: list.map((s) => ({ key: s.name, label: s.name, color: theme.cat(s.index), pattern: s.index })) } : undefined,
      note: notes([[nulls, nulls === 1 ? 'missing value dropped' : 'missing values dropped'], [hidden, `${hidden === 1 ? 'far outlier' : 'far outliers'} beyond the axis (in the estimate; focus: 'all' shows them)`], [o.rug && !rug ? n : 0, 'values too many for a rug; rug left off']]),
    }
  },
  describe({ data, options: o, fmt, name }) {
    const { list } = curves(data, o)
    const all = list.flatMap((s) => s.values)
    const [lo, hi] = stats.extent(all)!
    const top = list.reduce((a, s) => (s.peak.y > a.peak.y ? s : a), list[0]!)
    const lead = list.length > 1 ? `${phrase.count(list.length, 'series', 'series')}, ` : ''
    const span = lo === hi ? `all at ${fmt.value(lo)}` : `from ${fmt.value(lo)} to ${fmt.value(hi)}`
    return `${name}, ${lead}${phrase.count(all.length, 'value')} ${span}, highest peak at ${fmt.value(top.peak.x)}${list.length > 1 ? ` (${top.name})` : ''}`
  },
  table(data, o) {
    const { list } = curves(data, o)
    return {
      columns: ['Series', 'n', 'Mean', 'Median', 'SD', 'Min', 'Max', 'Peak at', 'Bandwidth'],
      rows: list.map((s) => {
        const [lo, hi] = stats.extent(s.values)!
        return [s.name || 'All values', s.values.length, stats.mean(s.values), stats.median(s.values), s.values.length > 1 ? stats.stdev(s.values) : null, lo, hi, s.peak.x, s.bandwidth]
      }),
    }
  },
})

export default density
