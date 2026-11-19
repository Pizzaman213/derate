// Ridgeline (joy) plot: one kernel density per group on a shared x axis, stacked in
// rows that overlap, group names on the left. Lower ridges are painted over the ones
// above them, so each reads as a silhouette in front of the next.

import { defineChart, SIZES, stats } from '../../core'
import type { Mark } from '../../core'
import { notes } from '../../core/helpers'
import { bandwidthFor, boxOfPts, densityCurve, emptyValues, focusRange, groupStats, medianExtremes, polyD, prepGroups, statsRows, statsSentence, statsTable, type Group, type GroupStats, type Pt } from '../_shared/DistributionA'
import { input, options, type RidgeOptions, type RidgeRow } from './schema'

export interface Ridge {
  group: Group
  stats: GroupStats
  bandwidth: number
  curve: Pt[]
  /** Highest density inside the drawn range. */
  peak: Pt
}

type Opts = Pick<RidgeOptions, 'bandwidth' | 'sort' | 'maxGroups'> & Partial<Pick<RidgeOptions, 'focus'>>

/** Density per group on one shared grid (pure; the tests read it). `view` is the x range drawn. */
export function ridges(data: readonly RidgeRow[], o: Opts): { list: Ridge[]; nulls: number; collapsed: number; view: [number, number]; hidden: number } {
  const p = prepGroups(data, o.sort, o.maxGroups)
  const pooled = p.groups.flatMap((g) => g.values)
  if (!pooled.length) return { list: [], nulls: p.nulls, collapsed: p.collapsed, view: [0, 1], hidden: 0 }
  const hs = p.groups.map((g) => bandwidthFor(g.values, o.bandwidth, pooled))
  const reach = 3 * Math.max(...hs)
  const [lo, hi] = stats.extent(pooled)!
  const extent: [number, number] = [lo - reach, hi + reach]
  const focus = o.focus === 'auto' ? focusRange(pooled) : null
  const view: [number, number] = focus ? [focus.range[0] - reach, focus.range[1] + reach] : extent
  const list = p.groups.map((group, i) => {
    const curve = densityCurve(group.values, hs[i]!, extent, 200).filter((q) => q.x >= view[0] && q.x <= view[1])
    const peak = curve.reduce((a, q) => (q.y > a.y ? q : a), curve[0] ?? { x: view[0], y: 0 })
    return { group, stats: groupStats(group), bandwidth: hs[i]!, curve, peak }
  })
  return { list, nulls: p.nulls, collapsed: p.collapsed, view, hidden: focus?.hidden ?? 0 }
}

export const ridgeline = defineChart<RidgeRow[], RidgeOptions>({
  id: 'ridgeline',
  name: 'Ridgeline plot',
  category: 'distribution',
  intents: ['distribution', 'compare'],
  summary: 'Overlapping density curves, one row per group, on a shared axis.',
  input,
  options,
  shape: 'rows',
  fields: ['group', 'value'],
  numberFields: ['value'],
  aspect: 0.7,
  minHeight: 220,
  isEmpty: (data) => emptyValues(data),
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    const { list, nulls, collapsed, view, hidden } = ridges(ctx.data, o)
    const n = list.length
    const gap = 8
    const labelW = Math.min(Math.max(...list.map((r) => ctx.measure(r.group.name))), ctx.width * 0.3)
    const spec = (top: number) => ({
      x: { type: 'linear' as const, domain: view, zero: false, nice: false },
      y: { type: 'linear' as const, domain: [0, 1] as [number, number] },
      yAxis: 'none' as const,
      grid: 'x' as const,
      xTitle: o.xTitle,
      pad: { left: labelW + gap, top },
    })
    // the top row's ridge rises `overlap` rows above the first baseline: reserve that
    const h0 = ctx.cartesian(spec(0)).plot.height
    const f = ctx.cartesian(spec((h0 * o.overlap) / (n + o.overlap)))
    const step = f.plot.height / n
    const tall = step * (1 + o.overlap)
    const peakAll = Math.max(1e-12, ...list.map((r) => r.peak.y))
    const every = Math.max(1, Math.ceil((SIZES.tick * 1.4) / step))
    const marks: Mark[] = [...f.marks]
    list.forEach((r, i) => {
      const base = f.plot.y + (i + 1) * step
      const k = tall / (o.scale === 'each' ? Math.max(1e-12, r.peak.y) : peakAll)
      const color = o.colorBy === 'group' ? theme.cat(r.group.index) : theme.cat(0)
      const name = r.group.name
      if (r.curve.length >= 2) {
        const pts = r.curve.map((q) => [f.px(q.x), base - k * q.y] as const)
        const outline = [[pts[0]![0], base] as const, ...pts, [pts[pts.length - 1]![0], base] as const]
        marks.push({
          type: 'path', role: 'ridge', key: `r:${name}`, d: polyD(outline),
          fill: theme.mix(theme.surface, color, 0.55), stroke: color, strokeWidth: 1.5, pattern: o.colorBy === 'group' ? r.group.index : undefined,
          cx: f.px(r.peak.x), cy: base - k * r.peak.y, box: boxOfPts(outline),
          datum: { group: name, ...r.stats, bandwidth: r.bandwidth, peak: r.peak.x },
          label: `${name}: ${statsSentence(r.stats, fmt)}, peak at ${fmt.value(r.peak.x)}`,
          tooltip: { title: name, rows: [...statsRows(r.stats, fmt), ['Peak at', fmt.value(r.peak.x)]], color },
        })
      }
      if (i % every === 0) {
        const text = ctx.truncate(name, labelW)
        marks.push({ type: 'text', layer: 'labels', key: `l:${name}`, x: f.plot.x - gap, y: base - 2, text, full: text !== name ? name : undefined, anchor: 'end', baseline: 'alphabetic', size: SIZES.tick, fill: theme.inkSecondary, className: text !== name ? 'oc-trunc' : undefined })
      }
    })
    return {
      marks,
      plot: f.plot,
      note: notes([
        [nulls, nulls === 1 ? 'missing value dropped' : 'missing values dropped'],
        [collapsed, `smallest group${collapsed === 1 ? '' : 's'} pooled into "Other"`],
        [hidden, `${hidden === 1 ? 'far outlier' : 'far outliers'} beyond the axis (in the estimate; focus: 'all' shows them)`],
        [every > 1 ? n - Math.ceil(n / every) : 0, 'row labels left off for space (every ridge names its group on hover)'],
      ]),
    }
  },
  describe({ data, options: o, fmt, name }) {
    const { list } = ridges(data, o)
    const groups = list.length === 1 ? '' : `${list.length} groups, `
    return `${name}, ${groups}${medianExtremes(list.map((r) => r.stats), fmt)}`
  },
  table(data, o) {
    return statsTable(ridges(data, o).list.map((r) => r.stats))
  },
})

export default ridgeline
