// Connected scatter plot: (x, y) points joined in input order, so the path shows how two
// measures moved together over time. Direction arrows on the segments; the ends labelled.

import { line, curveCardinal, curveLinear } from 'd3-shape'
import { defineChart } from '../../core'
import { notes } from '../../core/helpers'
import type { Mark } from '../../core'
import { axisSpec, cleanPoints, describeXY, frameResult, inPlot, placeLabels, shortName, uniqueKeys, xyTable, zoomed, type LabelItem, type Pt } from '../_shared/RelationshipA'
import { input, options, type ConnectedOptions, type ConnectedRow } from './schema'

type P = [number, number]

/** Consecutive runs of drawable rows; a row missing x or y ends a run. */
export function runs<R extends { x: number | null; y: number | null }>(rows: readonly R[]): Array<Array<Pt<R>>> {
  const out: Array<Array<Pt<R>>> = []
  let cur: Array<Pt<R>> = []
  rows.forEach((row, i) => {
    if (row.x === null || row.y === null) {
      if (cur.length) out.push(cur)
      cur = []
      return
    }
    cur.push({ row, i, x: row.x, y: row.y })
  })
  if (cur.length) out.push(cur)
  return out
}

/**
 * Midpoint and direction of segment a->b. For the smooth curve this is the point at t = 0.5
 * on the same cubic d3's curveCardinal.tension(0) draws (uniform Catmull-Rom; the ends
 * repeat their own point as the missing neighbour), so arrows sit on the curve.
 */
export function segmentMid(prev: P, a: P, b: P, next: P, smooth: boolean): { x: number; y: number; dx: number; dy: number } {
  if (!smooth) return { x: (a[0] + b[0]) / 2, y: (a[1] + b[1]) / 2, dx: b[0] - a[0], dy: b[1] - a[1] }
  const c1: P = [a[0] + (b[0] - prev[0]) / 6, a[1] + (b[1] - prev[1]) / 6]
  const c2: P = [b[0] - (next[0] - a[0]) / 6, b[1] - (next[1] - a[1]) / 6]
  return {
    x: (a[0] + 3 * c1[0] + 3 * c2[0] + b[0]) / 8,
    y: (a[1] + 3 * c1[1] + 3 * c2[1] + b[1]) / 8,
    dx: b[0] + c2[0] - c1[0] - a[0],
    dy: b[1] + c2[1] - c1[1] - a[1],
  }
}

/** Filled arrowhead centred on (x, y), pointing along (dx, dy). */
function arrowPath(x: number, y: number, dx: number, dy: number, s: number): string {
  const len = Math.hypot(dx, dy) || 1
  const ux = dx / len, uy = dy / len, px = -uy, py = ux
  const tip: P = [x + ux * s, y + uy * s]
  const l: P = [x - ux * s + px * s * 0.8, y - uy * s + py * s * 0.8]
  const r: P = [x - ux * s - px * s * 0.8, y - uy * s - py * s * 0.8]
  const f = (p: P) => `${Math.round(p[0] * 100) / 100},${Math.round(p[1] * 100) / 100}`
  return `M${f(tip)}L${f(l)}L${f(r)}Z`
}

export const connectedScatter = defineChart<ConnectedRow[], ConnectedOptions>({
  id: 'connected-scatter',
  name: 'Connected scatter plot',
  category: 'relationship',
  intents: ['relationship', 'correlation', 'change-over-time'],
  summary: 'Points at (x, y) joined in order, so the path shows how two measures moved together over time.',
  input,
  options,
  shape: 'rows',
  fields: ['x', 'y', 'label'],
  numberFields: ['x', 'y'],
  zoom: 'xy',
  aspect: 0.7,
  isEmpty: (data) => (cleanPoints(data).pts.length === 0 ? (data.length ? 'No rows have both an x and a y value' : true) : false),
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    const { pts, missing } = cleanPoints(ctx.data)
    const segs = runs(ctx.data)
    const f = ctx.cartesian({ x: axisSpec(pts.map((p) => p.x), 'linear'), y: axisSpec(pts.map((p) => p.y), 'linear'), xTitle: o.xTitle, yTitle: o.yTitle, grid: 'both', zoom: true })
    const z = zoomed(ctx)
    const n = pts.length
    const color = theme.cat(0)
    const smooth = o.curve === 'smooth'
    const marks: Mark[] = [...f.marks]
    const xy = (p: Pt<ConnectedRow>): P => [f.px(p.x), f.py(p.y)]
    const gen = line<P>().curve(smooth ? curveCardinal.tension(0) : curveLinear)
    // path and arrows: decorations under the points. Arrows are spaced by distance along the
    // path (about two dozen at most), each on the next segment long enough to carry one.
    const pixelRuns = segs.map((run) => run.map(xy))
    const total = pixelRuns.reduce((t, ps) => t + ps.slice(1).reduce((k, b, i) => k + Math.hypot(b[0] - ps[i]![0], b[1] - ps[i]![1]), 0), 0)
    const spacing = Math.max(56, total / 24)
    const s = n > 1000 ? 3 : 4.5
    const r = n > 1000 ? 1.5 : n > 200 ? 2.5 : 3.5
    let travelled = spacing / 2
    pixelRuns.forEach((ps, ri) => {
      const run = segs[ri]!
      if (ps.length > 1) marks.push({ type: 'path', layer: 'marks', key: `path:${ri}`, d: gen(ps) ?? '', fill: 'none', stroke: color, strokeWidth: n > 1000 ? 1 : 1.75, strokeOpacity: n > 1000 ? 0.6 : 0.9, linejoin: 'round', linecap: 'round', inert: true })
      if (!o.arrows) return
      for (let i = 0; i + 1 < ps.length; i++) {
        const a = ps[i]!, b = ps[i + 1]!
        const len = Math.hypot(b[0] - a[0], b[1] - a[1])
        travelled += len
        // short segments only carry an arrow on dense paths, aimed along the local chord
        const long = len >= s * 3 + 2 * r
        if (travelled < spacing || len < 1 || (!long && n <= 50)) continue
        const m = long ? segmentMid(ps[i - 1] ?? a, a, b, ps[i + 2] ?? b, smooth) : { x: (a[0] + b[0]) / 2, y: (a[1] + b[1]) / 2, dx: (ps[i + 2] ?? b)[0] - (ps[i - 1] ?? a)[0], dy: (ps[i + 2] ?? b)[1] - (ps[i - 1] ?? a)[1] }
        if (z && !inPlot(f.plot, m.x, m.y)) continue
        travelled = 0
        marks.push({ type: 'path', layer: 'marks', key: `arrow:${run[i]!.i}`, d: arrowPath(m.x, m.y, m.dx, m.dy, s), fill: color, stroke: theme.surface, strokeWidth: 0.75, inert: true })
      }
    })
    // points: start hollow, end filled and larger
    const keys = uniqueKeys(pts.map((p) => p.row.label ?? `${p.x},${p.y}`))
    const items: LabelItem[] = []
    const obstacles: Array<{ x: number; y: number; r: number }> = []
    pts.forEach((p, k) => {
      const [cx, cy] = xy(p)
      const first = k === 0, last = k === n - 1 && n > 1
      const pr = last ? r + 1.5 : first ? r + 0.5 : r
      if (z && !inPlot(f.plot, cx, cy, pr)) return
      const step = p.row.label ?? `Step ${k + 1}`
      const where = `x ${fmt.value(p.x)}, y ${fmt.value(p.y)}`
      marks.push({
        type: 'circle', role: 'point', key: keys[k], cx, cy, r: pr,
        fill: first ? theme.surface : color, stroke: first ? color : theme.surface, strokeWidth: first ? 1.75 : 1,
        datum: p.row, label: `${step}${first ? ' (start)' : last ? ' (end)' : ''}: ${where}`,
        tooltip: { title: step, rows: [['Step', `${k + 1} of ${n}`], [shortName(o.xTitle, 'x'), fmt.value(p.x)], [shortName(o.yTitle, 'y'), fmt.value(p.y)]], color },
      })
      obstacles.push({ x: cx, y: cy, r: pr })
      if (o.labels === 'none') return
      if (first || last) items.push({ key: keys[k]!, x: cx, y: cy, r: pr, text: p.row.label ?? (first ? 'Start' : 'End'), fill: theme.ink })
      else if (o.labels === 'auto' && p.row.label !== undefined && n <= 200) items.push({ key: keys[k]!, x: cx, y: cy, r: pr, text: p.row.label })
    })
    // the ends first, so they win any collision
    items.sort((a, b) => (b.fill ? 1 : 0) - (a.fill ? 1 : 0))
    marks.push(...placeLabels(items, f.plot, ctx, theme, obstacles))
    return {
      marks,
      ...frameResult(f),
      note: notes([[missing, 'rows missing x or y break the path']]),
    }
  },
  describe({ data, options: o, fmt, name }) {
    const { pts } = cleanPoints(data)
    const first = pts[0], last = pts[pts.length - 1]
    const extra: string[] = []
    if (first && last && pts.length > 1) {
      extra.push(`from (${fmt.value(first.x)}, ${fmt.value(first.y)}) to (${fmt.value(last.x)}, ${fmt.value(last.y)})`)
    }
    const span = first?.row.label !== undefined && last?.row.label !== undefined && pts.length > 1 ? ` from ${first.row.label} to ${last.row.label}` : ''
    return describeXY(name, pts, fmt, { noun: 'step', span, xName: o.xTitle, yName: o.yTitle, extra })
  },
  table(data, o) {
    return xyTable(data, [], o.xTitle, o.yTitle)
  },
})

export default connectedScatter
