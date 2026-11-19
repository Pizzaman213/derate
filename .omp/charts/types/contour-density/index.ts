// Contour density plot: a kernel density estimate of the points on the x/y plane, drawn as
// filled contour bands (darker = denser), optionally with the points on top.

import { contourDensity, type ContourMultiPolygon } from 'd3-contour'
import { geoIdentity, geoPath } from 'd3-geo'
import { defineChart } from '../../core'
import { notes } from '../../core/helpers'
import type { Mark } from '../../core'
import { axisSpec, cleanPoints, densityColor, densityStops, describeXY, frameResult, inPlot, sig, widen, xyRows, xyTable, zoomed } from '../_shared/RelationshipA'
import { input, options, type ContourOptions, type ContourRow } from './schema'

type P = [number, number]

/** Contours of the density of `pts` (plot-relative px) in a w x h box, lowest level first. */
export function densityContours(pts: readonly P[], w: number, h: number, bandwidth: number, thresholds: number): ContourMultiPolygon[] {
  return contourDensity<P>().x((d) => d[0]).y((d) => d[1]).size([Math.max(1, Math.round(w)), Math.max(1, Math.round(h))]).bandwidth(bandwidth).thresholds(thresholds)([...pts])
    .filter((c) => c.coordinates.length > 0)
}

/** Even-odd test of p against every ring of a multipolygon (holes subtract). */
export function inside(c: ContourMultiPolygon, p: P): boolean {
  let hit = false
  for (const poly of c.coordinates) for (const ring of poly) {
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      const [xi, yi] = ring[i] as P, [xj, yj] = ring[j] as P
      if ((yi > p[1]) !== (yj > p[1]) && p[0] < ((xj - xi) * (p[1] - yi)) / (yj - yi) + xi) hit = !hit
    }
  }
  return hit
}

/** Topmost vertex of the largest ring: on the contour's own edge, so each level anchors somewhere different. */
function anchorOf(c: ContourMultiPolygon): P {
  let best: number[][] = []
  for (const poly of c.coordinates) if (poly[0] && poly[0].length > best.length) best = poly[0]
  let top: P = [0, Infinity]
  for (const v of best) if ((v[1] ?? Infinity) < top[1]) top = [v[0] ?? 0, v[1] ?? 0]
  return top
}

const showPointsFor = (o: ContourOptions, n: number) => o.showPoints === 'always' || (o.showPoints === 'auto' && n <= 2000)

export const contourDensityChart = defineChart<ContourRow[], ContourOptions>({
  id: 'contour-density',
  name: 'Contour density plot',
  category: 'relationship',
  intents: ['relationship', 'correlation', 'distribution'],
  summary: 'A smoothed density of points on the x/y plane, drawn as filled contour bands.',
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
    // room for the density's tails (about two bandwidths), at most a fifth of the plot
    const tail = (len: number) => Math.min(2 * o.bandwidth, len / 5)
    const f = ctx.cartesian({ ...base, x: widen(xs0, f0.plot.width, tail(f0.plot.width)), y: widen(ys0, f0.plot.height, tail(f0.plot.height)) })
    const { plot } = f
    const z = zoomed(ctx)
    const xy = pts.map((p) => [f.px(p.x) - plot.x, f.py(p.y) - plot.y] as P)
    const contours = densityContours(xy, plot.width, plot.height, o.bandwidth, o.thresholds)
    // density per px² -> per (x unit · y unit)
    const pxPerUnit = Math.abs(f.px(1) - f.px(0)) * Math.abs(f.py(1) - f.py(0)) || 1
    const path = geoPath(geoIdentity().translate([plot.x, plot.y]))
    const marks: Mark[] = [...f.marks]
    const n = contours.length
    contours.forEach((c, i) => {
      const fill = densityColor(theme, n > 1 ? i / (n - 1) : 1)
      const d = path(c) ?? ''
      if (!d) return
      const [[bx0, by0], [bx1, by1]] = path.bounds(c)
      const [ax, ay] = anchorOf(c)
      const enclosed = xy.reduce((k, p) => k + (inside(c, p) ? 1 : 0), 0)
      const dens = sig(c.value * pxPerUnit, fmt.locale, 2)
      const share = fmt.pct(xy.length ? enclosed / xy.length : 0, 0)
      marks.push({
        type: 'path', role: 'contour', key: `c:${i}`, d, fill, fillOpacity: 0.9, stroke: theme.surface, strokeWidth: 0.6, strokeOpacity: 0.7,
        cx: ax + plot.x, cy: ay + plot.y, box: { x: bx0, y: by0, width: Math.max(2, bx1 - bx0), height: Math.max(2, by1 - by0) },
        datum: { level: i + 1, density: c.value * pxPerUnit, enclosed },
        label: `Density level ${i + 1} of ${n}: at least ${dens} points per unit², encloses ${share} of points`,
        tooltip: { title: `Level ${i + 1} of ${n}`, rows: [['Density ≥', `${dens} per unit²`], ['Encloses', `${share} (${fmt.integer(enclosed)})`]], color: fill },
      })
    })
    if (showPointsFor(o, pts.length)) {
      const r = pts.length > 500 ? 1.5 : 2.25
      pts.forEach((p, i) => {
        const cx = xy[i]![0] + plot.x, cy = xy[i]![1] + plot.y
        if (z && !inPlot(plot, cx, cy, r)) return
        marks.push({
          type: 'circle', role: 'point', key: `p:${p.i}`, cx, cy, r, fill: theme.ink, fillOpacity: 0.55,
          datum: p.row, label: `x ${fmt.value(p.x)}, y ${fmt.value(p.y)}`, tooltip: { rows: xyRows(p.x, p.y, fmt, o.xTitle, o.yTitle), color: theme.ink },
        })
      })
    }
    const lo = contours[0], hi = contours[contours.length - 1]
    return {
      marks,
      ...frameResult(f),
      legend: lo && hi ? { title: 'Density per unit²', gradient: { stops: densityStops(theme), min: sig(lo.value * pxPerUnit, fmt.locale, 2), max: sig(hi.value * pxPerUnit, fmt.locale, 2) } } : undefined,
      note: notes([[missing, 'rows missing x or y skipped']]) ?? (showPointsFor(o, pts.length) ? undefined : o.showPoints === 'auto' ? `${fmt.integer(pts.length)} points shown as density only (showPoints: 'always' to draw them)` : undefined),
    }
  },
  describe({ data, options: o, fmt, name }) {
    const { pts } = cleanPoints(data)
    return describeXY(name, pts, fmt, { xName: o.xTitle, yName: o.yTitle, extra: [`bandwidth ${o.bandwidth}px`] })
  },
  table(data, o) {
    return xyTable(data, [], o.xTitle, o.yTitle)
  },
})

export default contourDensityChart
