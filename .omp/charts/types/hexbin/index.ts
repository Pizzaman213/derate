// Hexbin plot: points counted into hexagonal bins on the x/y plane, each hexagon coloured
// by its count. Reads density where a scatter plot would be a solid smear.

import { hexbin as d3hexbin } from 'd3-hexbin'
import { defineChart } from '../../core'
import { notes } from '../../core/helpers'
import type { Mark } from '../../core'
import { axisSpec, cleanPoints, densityColor, densityStops, describeXY, frameResult, inPlot, overlapping, widen, xyTable, zoomed } from '../_shared/RelationshipA'
import { input, options, type HexbinOptions, type HexbinRow } from './schema'

/** Default hexagon radius for a plot width: about 40 across, 5-16px. */
export function autoHexRadius(plotWidth: number): number {
  return Math.max(5, Math.min(16, plotWidth / 40))
}

/** Count share -> [0, 1] on the chosen colour scale (1 is the densest bin). */
export function countShare(count: number, max: number, scale: 'linear' | 'log'): number {
  if (max <= 1) return 1
  return scale === 'log' ? Math.log(count) / Math.log(max) : (count - 1) / (max - 1)
}

export const hexbin = defineChart<HexbinRow[], HexbinOptions>({
  id: 'hexbin',
  name: 'Hexbin plot',
  category: 'relationship',
  intents: ['relationship', 'correlation', 'distribution'],
  summary: 'Points counted into hexagonal bins, each coloured by how many points fall in it.',
  input,
  options,
  shape: 'rows',
  fields: ['x', 'y'],
  numberFields: ['x', 'y'],
  zoom: 'xy',
  aspect: 0.7,
  isEmpty: (data) => (cleanPoints(data).pts.length === 0 ? (data.length ? 'No rows have both an x and a y value' : true) : false),
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    const { pts, missing } = cleanPoints(ctx.data)
    const base = { xTitle: o.xTitle, yTitle: o.yTitle, grid: 'both' as const, zoom: true }
    const xs0 = axisSpec(pts.map((p) => p.x), 'linear', 0), ys0 = axisSpec(pts.map((p) => p.y), 'linear', 0)
    const f0 = ctx.cartesian({ ...base, x: xs0, y: ys0 })
    const r = o.radius ?? autoHexRadius(f0.plot.width)
    // a bin's centre can sit a radius past its outermost point and the hexagon reaches another
    // radius beyond that: leave 2r so edge bins are whole
    const f = ctx.cartesian({ ...base, x: widen(xs0, f0.plot.width, 2 * r), y: widen(ys0, f0.plot.height, 2 * r) })
    const { plot } = f
    const z = zoomed(ctx)
    const xy = pts.map((p) => [f.px(p.x), f.py(p.y)] as [number, number]).filter((p) => !z || inPlot(plot, p[0], p[1]))
    const hb = d3hexbin<[number, number]>().radius(r).extent([[plot.x, plot.y], [plot.x + plot.width, plot.y + plot.height]])
    const bins = hb(xy)
    const max = Math.max(1, ...bins.map((b) => b.length))
    const hex = hb.hexagon(r - 0.25)
    const total = xy.length
    const marks: Mark[] = [...f.marks]
    for (const b of bins) {
      const n = b.length
      const fill = densityColor(theme, countShare(n, max, o.colorScale))
      const cx = Number(f.invertX(b.x)), cy = Number(f.invertY(b.y))
      const what = `${fmt.integer(n)} ${n === 1 ? 'point' : 'points'}`
      marks.push({
        type: 'path', role: 'hex', key: `h:${Math.round(b.x * 10)}:${Math.round(b.y * 10)}`,
        d: `M${b.x.toFixed(2)},${b.y.toFixed(2)}${hex}`, cx: b.x, cy: b.y, box: { x: b.x - r, y: b.y - r, width: 2 * r, height: 2 * r },
        fill, stroke: theme.surface, strokeWidth: 0.5,
        datum: { x: cx, y: cy, count: n },
        label: `Around x ${fmt.value(cx)}, y ${fmt.value(cy)}: ${what}`,
        tooltip: { title: what, rows: [['x ≈', fmt.value(cx)], ['y ≈', fmt.value(cy)], ['Share', fmt.pct(n / total, 1)]], color: fill },
      })
    }
    const mid = o.colorScale === 'log' ? Math.round(Math.sqrt(max)) : Math.round((1 + max) / 2)
    return {
      marks,
      ...frameResult(f),
      legend: { title: 'Points per hexagon', gradient: { stops: densityStops(theme), min: fmt.integer(1), max: fmt.integer(max), mid: max >= 3 ? fmt.integer(mid) : undefined } },
      note: notes([[missing, 'rows missing x or y skipped'], [overlapping(pts), 'points share a position with another (all counted)']]),
    }
  },
  describe({ data, options: o, fmt, name }) {
    const { pts } = cleanPoints(data)
    return describeXY(name, pts, fmt, { xName: o.xTitle, yName: o.yTitle, extra: o.colorScale === 'log' ? ['colour on a log scale of counts'] : [] })
  },
  table(data, o) {
    return xyTable(data, [], o.xTitle, o.yTitle)
  },
})

export default hexbin
