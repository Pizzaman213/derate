// Violin plot: per group, a kernel density estimate mirrored about the group's centre
// line, with a slim box plot (or quartile lines) inside. Width at a value is proportional
// to the density there.

import { defineChart, stats } from '../../core'
import type { Mark } from '../../core'
import { notes } from '../../core/helpers'
import { bandwidthFor, boxOfPts, densityCurve, emptyValues, focusRange, groupStats, medianExtremes, polyD, prepGroups, statsRows, statsSentence, statsTable, type Group, type GroupStats, type Pt } from '../_shared/DistributionA'
import { input, options, type ViolinOptions, type ViolinRow } from './schema'

export interface ViolinShape {
  group: Group
  stats: GroupStats
  box: stats.BoxStats
  bandwidth: number
  curve: Pt[]
  peak: number
}

type Opts = Pick<ViolinOptions, 'bandwidth' | 'trim' | 'sort' | 'maxGroups'> & Partial<Pick<ViolinOptions, 'focus'>>

/** Density per group (pure; the tests read it). `view` is the value range drawn. */
export function violins(data: readonly ViolinRow[], o: Opts): { list: ViolinShape[]; nulls: number; collapsed: number; view: [number, number]; hidden: number } {
  const p = prepGroups(data, o.sort, o.maxGroups)
  const pooled = p.groups.flatMap((g) => g.values)
  let lo = Infinity, hi = -Infinity
  let reachMax = 0
  const list = p.groups.map((group) => {
    const h = bandwidthFor(group.values, o.bandwidth, pooled)
    const [a, b] = stats.extent(group.values)!
    // tails run 3 bandwidths past the data: a lone value ends in a point, not a flat cap
    const extent: [number, number] = o.trim && a < b ? [a, b] : [a - 3 * h, b + 3 * h]
    lo = Math.min(lo, extent[0])
    hi = Math.max(hi, extent[1])
    reachMax = Math.max(reachMax, 3 * h)
    const curve = densityCurve(group.values, h, extent, 120)
    return { group, stats: groupStats(group), box: stats.boxStats(group.values)!, bandwidth: h, curve, peak: Math.max(...curve.map((q) => q.y)) }
  })
  const focus = o.focus === 'auto' && pooled.length ? focusRange(pooled) : null
  const view: [number, number] = focus ? [focus.range[0] - reachMax, focus.range[1] + reachMax] : [lo, hi]
  return { list, nulls: p.nulls, collapsed: p.collapsed, view, hidden: focus?.hidden ?? 0 }
}

/** Density at v by linear interpolation along the curve. */
function densityAt(curve: readonly Pt[], v: number): number {
  for (let i = 1; i < curve.length; i++) {
    const a = curve[i - 1]!, b = curve[i]!
    if (v >= a.x && v <= b.x) return b.x === a.x ? a.y : a.y + ((b.y - a.y) * (v - a.x)) / (b.x - a.x)
  }
  return 0
}

export const violin = defineChart<ViolinRow[], ViolinOptions>({
  id: 'violin',
  name: 'Violin plot',
  category: 'distribution',
  intents: ['distribution', 'compare'],
  summary: 'Mirrored density curves per group, with the quartiles inside.',
  input,
  options,
  shape: 'rows',
  fields: ['group', 'value'],
  numberFields: ['value'],
  isEmpty: (data) => emptyValues(data),
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    const { list, nulls, collapsed, view, hidden } = violins(ctx.data, o)
    const f = ctx.cartesian({
      x: { type: 'band', domain: list.map((v) => v.group.name), padding: 0.15 },
      y: { type: 'linear', domain: view, zero: false },
      xTitle: o.xTitle,
      yTitle: o.yTitle,
      grid: 'y',
    })
    const half = Math.min(f.bw / 2, 64)
    const inView = (q: Pt) => q.x >= view[0] && q.x <= view[1]
    const peakAll = Math.max(1e-12, ...list.map((v) => v.peak))
    const countMax = Math.max(1e-12, ...list.map((v) => v.peak * v.group.values.length))
    const marks: Mark[] = [...f.marks]
    list.forEach((v) => {
      const { group, box } = v
      const k = o.scale === 'width' ? half / Math.max(1e-12, v.peak) : o.scale === 'count' ? (half * group.values.length) / countMax : half / peakAll
      const color = theme.cat(group.index)
      const c = f.px(group.name) + f.bw / 2
      const pts = v.curve.filter(inView)
      if (pts.length < 2) return
      const right = pts.map((q) => [c + k * q.y, f.py(q.x)] as const)
      const left = [...pts].reverse().map((q) => [c - k * q.y, f.py(q.x)] as const)
      const outline = [...right, ...left]
      const name = group.name
      marks.push({
        type: 'path', role: 'violin', key: `v:${name}`, d: polyD(outline),
        fill: color, fillOpacity: 0.45, stroke: color, strokeWidth: 1.5, pattern: group.index,
        cx: c, cy: f.py(box.median), box: boxOfPts(outline),
        datum: { group: name, ...v.stats, bandwidth: v.bandwidth },
        label: `${name}: ${statsSentence(v.stats, fmt)}`,
        tooltip: { title: name, rows: [...statsRows(v.stats, fmt), ['Bandwidth', fmt.value(v.bandwidth)]], color },
      })
      if (o.inner === 'box') {
        const w = Math.max(3, Math.min(10, half * 0.22))
        const yq3 = f.py(box.q3), yq1 = f.py(box.q1)
        marks.push({ type: 'line', key: `iw:${name}`, x1: c, x2: c, y1: f.py(box.upperWhisker), y2: f.py(box.lowerWhisker), stroke: theme.ink, strokeWidth: 1.5, inert: true })
        marks.push({ type: 'rect', key: `ib:${name}`, x: c - w / 2, width: w, y: yq3, height: Math.max(1.5, yq1 - yq3), rx: Math.min(2, w / 3), fill: theme.ink, inert: true })
        marks.push({ type: 'circle', key: `im:${name}`, cx: c, cy: f.py(box.median), r: Math.min(3.5, w / 2 + 0.5), fill: theme.surface, stroke: theme.ink, strokeWidth: 1, inert: true })
      } else if (o.inner === 'quartiles') {
        for (const [key, q, dash] of [['q1', box.q1, '4 3'], ['q2', box.median, undefined], ['q3', box.q3, '4 3']] as const) {
          const hw = k * densityAt(v.curve, q)
          marks.push({ type: 'line', key: `${key}:${name}`, x1: c - hw, x2: c + hw, y1: f.py(q), y2: f.py(q), stroke: theme.ink, strokeWidth: key === 'q2' ? 2 : 1, dash, inert: true })
        }
      }
    })
    return {
      marks,
      plot: f.plot,
      clip: f.plot,
      note: notes([
        [nulls, nulls === 1 ? 'missing value dropped' : 'missing values dropped'],
        [collapsed, `smallest group${collapsed === 1 ? '' : 's'} pooled into "Other"`],
        [hidden, `${hidden === 1 ? 'far outlier' : 'far outliers'} beyond the axis (in the estimate; focus: 'all' shows them)`],
      ]),
    }
  },
  describe({ data, options: o, fmt, name }) {
    const { list } = violins(data, o)
    const groups = list.length === 1 ? '' : `${list.length} groups, `
    return `${name}, ${groups}${medianExtremes(list.map((v) => v.stats), fmt)}`
  },
  table(data, o) {
    return statsTable(violins(data, o).list.map((v) => v.stats))
  },
})

export default violin
