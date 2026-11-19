// Box plot: per group, the quartile box, a median line, Tukey whiskers (k x IQR) and
// outliers as points; optionally every value jittered over the box and a mean diamond.

import { defineChart, stats } from '../../core'
import type { BoxStats } from '../../core/stats'
import type { Mark as AnyMark } from '../../core'

type Seg = { x1: number; x2: number; y1: number; y2: number }
import { notes } from '../../core/helpers'
import { emptyValues, groupStats, medianExtremes, polyD, prepGroups, statsRows, statsSentence, type Group } from '../_shared/DistributionA'
import { input, options, type BoxOptions, type BoxRow } from './schema'

export interface GroupBox {
  group: Group
  box: BoxStats
}

/** Box statistics per group (pure; the tests read it). */
export function boxes(data: readonly BoxRow[], o: Pick<BoxOptions, 'sort' | 'maxGroups' | 'whisker'>): { list: GroupBox[]; nulls: number; collapsed: number } {
  const p = prepGroups(data, o.sort, o.maxGroups)
  return { list: p.groups.map((group) => ({ group, box: stats.boxStats(group.values, { whisker: o.whisker })! })), nulls: p.nulls, collapsed: p.collapsed }
}

export const boxPlot = defineChart<BoxRow[], BoxOptions>({
  id: 'box-plot',
  name: 'Box plot',
  category: 'distribution',
  intents: ['distribution', 'compare'],
  summary: 'Median, quartiles, whiskers and outliers of a value across groups.',
  input,
  options,
  shape: 'rows',
  fields: ['group', 'value'],
  numberFields: ['value'],
  isEmpty: (data) => emptyValues(data),
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    const { list, nulls, collapsed } = boxes(ctx.data, o)
    const vertical = o.orientation === 'vertical'
    let lo = Infinity, hi = -Infinity
    for (const { box } of list) { lo = Math.min(lo, box.min); hi = Math.max(hi, box.max) }
    const names = list.map((g) => g.group.name)
    const band = { type: 'band' as const, domain: names, padding: 0.3 }
    // a little air so extreme points and whisker caps do not sit on the axis line
    const air = (hi - lo) * 0.04
    const value = { type: 'linear' as const, domain: [lo - air, hi + air] as [number, number], zero: false }
    const f = ctx.cartesian({
      x: vertical ? band : value,
      y: vertical ? value : band,
      xTitle: o.xTitle,
      yTitle: o.yTitle,
      grid: vertical ? 'y' : 'x',
    })
    const bw = vertical ? f.bw : f.bh
    const vp = (v: number) => (vertical ? f.py(v) : f.px(v))
    const bp = (name: string) => (vertical ? f.px(name) : f.py(name))
    /** A rect from a band span [b0, b1] and a value-pixel span [p0, p1]. */
    const rect = (b0: number, b1: number, p0: number, p1: number) => (vertical
      ? { x: b0, width: b1 - b0, y: Math.min(p0, p1), height: Math.abs(p1 - p0) }
      : { y: b0, height: b1 - b0, x: Math.min(p0, p1), width: Math.abs(p1 - p0) })
    const seg = (b0: number, b1: number, p0: number, p1: number): Seg => (vertical ? { x1: b0, x2: b1, y1: p0, y2: p1 } : { y1: b0, y2: b1, x1: p0, x2: p1 })
    const pt = (b: number, p: number) => (vertical ? { cx: b, cy: p } : { cx: p, cy: b })

    const marks: AnyMark[] = [...f.marks]
    const dots: AnyMark[] = []
    const boxW = Math.min(bw, 72)
    const capW = boxW * 0.4
    let outlierTotal = 0
    list.forEach(({ group, box }) => {
      const color = theme.cat(group.index)
      const c = bp(group.name) + bw / 2
      const b0 = c - boxW / 2, b1 = c + boxW / 2
      const s = groupStats(group)
      const pq1 = vp(box.q1), pq3 = vp(box.q3)
      const k = group.name
      const line = (key: string, l: Seg, width = 1.5, stroke = color): AnyMark => ({ type: 'line', key: `${key}:${k}`, ...l, stroke, strokeWidth: width, inert: true })
      marks.push(line('wlo', seg(c, c, vp(box.lowerWhisker), pq1)), line('whi', seg(c, c, pq3, vp(box.upperWhisker))))
      marks.push(line('clo', seg(c - capW / 2, c + capW / 2, vp(box.lowerWhisker), vp(box.lowerWhisker))), line('chi', seg(c - capW / 2, c + capW / 2, vp(box.upperWhisker), vp(box.upperWhisker))))
      const r = rect(b0, b1, pq1, pq3)
      // a zero-IQR box is still a visible, hoverable 2px bar
      if (vertical && r.height < 2) { r.y -= (2 - r.height) / 2; r.height = 2 }
      if (!vertical && r.width < 2) { r.x -= (2 - r.width) / 2; r.width = 2 }
      outlierTotal += box.outliers.length
      marks.push({
        type: 'rect', role: 'box', key: `box:${k}`, ...r, rx: 2,
        fill: color, fillOpacity: o.points === 'all' ? 0.15 : 0.28, stroke: color, strokeWidth: 1.5, pattern: group.index,
        datum: { group: k, ...box, outliers: box.outliers.length },
        label: `${k}: ${statsSentence(s, fmt)}, ${box.outliers.length === 1 ? '1 outlier' : `${box.outliers.length} outliers`}`,
        tooltip: { title: k, rows: [...statsRows(s, fmt), ['Whiskers', `${fmt.value(box.lowerWhisker)} to ${fmt.value(box.upperWhisker)}`], ['Outliers', fmt.integer(box.outliers.length)]], color },
      })
      marks.push(line('med', seg(b0, b1, vp(box.median), vp(box.median)), 2.5, theme.ink))
      if (o.showMean) {
        const m = pt(c, vp(box.mean))
        const d = Math.min(5, boxW / 4)
        marks.push({ type: 'path', key: `mean:${k}`, d: polyD([[m.cx, m.cy - d], [m.cx + d, m.cy], [m.cx, m.cy + d], [m.cx - d, m.cy]]), fill: theme.surface, stroke: theme.ink, strokeWidth: 1.5, inert: true })
      }
      if (o.points === 'all') {
        const spreadW = boxW * 0.7
        group.values.forEach((v, i) => {
          if (v < box.lowerWhisker || v > box.upperWhisker) return
          const p = pt(c + (ctx.random() - 0.5) * spreadW, vp(v))
          dots.push({ type: 'circle', role: 'point', key: `p:${k}:${i}`, ...p, r: 2, fill: color, fillOpacity: 0.55, datum: { group: k, value: v }, label: `${k}: ${fmt.value(v)}` })
        })
      }
      box.outliers.forEach((v, i) => {
        dots.push({ type: 'circle', role: 'outlier', key: `o:${k}:${i}`, ...pt(c, vp(v)), r: 3.5, fill: theme.surface, stroke: color, strokeWidth: 1.5, datum: { group: k, value: v }, label: `${k} outlier: ${fmt.value(v)}`, tooltip: { title: k, rows: [['Outlier', fmt.value(v)]], color } })
      })
    })
    if (o.points !== 'none') marks.push(...dots)
    return {
      marks,
      plot: f.plot,
      note: notes([[nulls, nulls === 1 ? 'missing value dropped' : 'missing values dropped'], [collapsed, `smallest group${collapsed === 1 ? '' : 's'} pooled into "Other"`]]),
    }
  },
  describe({ data, options: o, fmt, name }) {
    const { list } = boxes(data, o)
    const out = list.reduce((t, g) => t + g.box.outliers.length, 0)
    const groups = list.length === 1 ? '' : `${list.length} groups, `
    return `${name}, ${groups}${medianExtremes(list.map((g) => groupStats(g.group)), fmt)}, ${out === 1 ? '1 outlier' : `${out} outliers`}`
  },
  table(data, o) {
    const { list } = boxes(data, o)
    return {
      columns: ['Group', 'n', 'Min', 'Lower whisker', 'Q1', 'Median', 'Q3', 'Upper whisker', 'Max', 'Mean', 'Outliers'],
      rows: list.map(({ group, box }) => [group.name, box.n, box.min, box.lowerWhisker, box.q1, box.median, box.q3, box.upperWhisker, box.max, box.mean, box.outliers.length]),
    }
  },
})

export default boxPlot
