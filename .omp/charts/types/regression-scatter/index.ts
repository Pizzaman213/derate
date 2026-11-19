// Regression scatter: the points, a least-squares fit (line, quadratic or cubic) with its
// 95% confidence band, and the equation and R² in the emptiest corner of the plot.

import { defineChart, SIZES } from '../../core'
import { notes } from '../../core/helpers'
import type { Mark, Rect } from '../../core'
import { autoRadius, axisSpec, cleanPoints, describeXY, frameResult, inPlot, overlapping, sig, xyRows, xyTable, zoomed, type Pt } from '../_shared/RelationshipA'
import { equation, fitPolynomial, type Fit } from './fit'
import { input, options, type RegressionOptions, type RegressionRow } from './schema'

const KIND = ['', 'linear', 'quadratic', 'cubic']

/** The corner (inset 6px) whose box covers the fewest points and fit samples; ties go TL, TR, BL, BR. */
export function emptiestCorner(plot: Rect, w: number, h: number, pts: ReadonlyArray<[number, number]>, curve: ReadonlyArray<[number, number]>): { x: number; y: number } {
  const inset = 6
  const xs = [plot.x + inset, plot.x + plot.width - inset - w]
  const ys = [plot.y + inset, plot.y + plot.height - inset - h]
  const boxes = [{ x: xs[0]!, y: ys[0]! }, { x: xs[1]!, y: ys[0]! }, { x: xs[0]!, y: ys[1]! }, { x: xs[1]!, y: ys[1]! }]
  const inside = (b: { x: number; y: number }, p: [number, number]) => p[0] >= b.x - 4 && p[0] <= b.x + w + 4 && p[1] >= b.y - 4 && p[1] <= b.y + h + 4
  let best = boxes[0]!, score = Infinity
  for (const b of boxes) {
    const s = pts.reduce((k, p) => k + (inside(b, p) ? 1 : 0), 0) + curve.reduce((k, p) => k + (inside(b, p) ? 3 : 0), 0)
    if (s < score) { score = s; best = b }
  }
  return best
}

function fitOf(rows: readonly RegressionRow[], o: RegressionOptions): { fit: Fit | null; pts: Array<Pt<RegressionRow>>; missing: number } {
  const { pts, missing } = cleanPoints(rows)
  return { fit: fitPolynomial(pts, o.degree), pts, missing }
}

export const regressionScatter = defineChart<RegressionRow[], RegressionOptions>({
  id: 'regression-scatter',
  name: 'Regression scatter plot',
  category: 'relationship',
  intents: ['relationship', 'correlation'],
  summary: 'A scatter plot with a least-squares fit, its confidence band, and the equation and R².',
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
    const { fit, pts, missing } = fitOf(ctx.data, o)
    const f = ctx.cartesian({ x: axisSpec(pts.map((p) => p.x), 'linear'), y: axisSpec(pts.map((p) => p.y), 'linear'), xTitle: o.xTitle, yTitle: o.yTitle, grid: 'both', zoom: true })
    const z = zoomed(ctx)
    const marks: Mark[] = [...f.marks]
    const pointColor = theme.cat(0), fitColor = theme.cat(1)
    const num = (v: number) => sig(v, fmt.locale, 3)

    // the fitted curve over the data's x range (what is visible of it when zoomed)
    let curve: Array<[number, number]> = []
    let fitMark: Mark | null = null
    let eq = ''
    if (fit) {
      const xs = pts.map((p) => p.x)
      let lo = Math.min(...xs), hi = Math.max(...xs)
      if (z) {
        const a = Number(f.invertX(f.plot.x)), b = Number(f.invertX(f.plot.x + f.plot.width))
        lo = Math.max(lo, Math.min(a, b)); hi = Math.min(hi, Math.max(a, b))
      }
      const steps = o.degree === 1 ? 1 : 96
      const sx = Array.from({ length: steps + 1 }, (_, i) => lo + ((hi - lo) * i) / steps)
      curve = sx.map((x) => [f.px(x), f.py(fit.predict(x))])
      const hw = sx.map((x) => fit.halfWidth(x))
      if (o.band && hw.every((v): v is number => v !== null && Number.isFinite(v)) && hi > lo) {
        const bandX = Array.from({ length: 65 }, (_, i) => lo + ((hi - lo) * i) / 64)
        const up = bandX.map((x) => [f.px(x), f.py(fit.predict(x) + (fit.halfWidth(x) ?? 0))] as const)
        const dn = bandX.map((x) => [f.px(x), f.py(fit.predict(x) - (fit.halfWidth(x) ?? 0))] as const).reverse()
        const d = [...up, ...dn].map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(2)},${p[1].toFixed(2)}`).join('') + 'Z'
        marks.push({ type: 'path', layer: 'marks', key: 'band', d, fill: fitColor, fillOpacity: 0.14, stroke: 'none', inert: true })
      }
      eq = equation(fit.coefficients, num)
      if (hi > lo) {
        const mid = curve[Math.floor(curve.length / 2)]!
        const ys = curve.map((p) => p[1])
        const box = { x: Math.min(curve[0]![0], curve[curve.length - 1]![0]), y: Math.min(...ys), width: Math.abs(curve[curve.length - 1]![0] - curve[0]![0]), height: Math.max(2, Math.max(...ys) - Math.min(...ys)) }
        const fitLabel = `${KIND[o.degree]} least-squares fit: ${eq}, R² = ${fmt.num(fit.r2, 3)}`
        fitMark = {
          type: 'path', role: 'fit', key: 'fit', d: curve.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(2)},${p[1].toFixed(2)}`).join(''),
          fill: 'none', stroke: fitColor, strokeWidth: 2.25, linecap: 'round', linejoin: 'round', cx: mid[0], cy: mid[1], box,
          datum: { degree: fit.degree, coefficients: fit.coefficients, r2: fit.r2, n: fit.n },
          label: fitLabel[0]!.toUpperCase() + fitLabel.slice(1),
          tooltip: { title: `${KIND[o.degree]![0]!.toUpperCase()}${KIND[o.degree]!.slice(1)} fit`, rows: [['Equation', eq], ['R²', fmt.num(fit.r2, 3)], ['n', fmt.integer(fit.n)]], color: fitColor },
        }
      }
    }

    const r = autoRadius(pts.length)
    const placed: Array<[number, number]> = []
    pts.forEach((p) => {
      const cx = f.px(p.x), cy = f.py(p.y)
      if (z && !inPlot(f.plot, cx, cy, r)) return
      placed.push([cx, cy])
      const resid = fit ? p.y - fit.predict(p.x) : null
      const where = `x ${fmt.value(p.x)}, y ${fmt.value(p.y)}`
      marks.push({
        type: 'circle', role: 'point', key: `p:${p.i}`, cx, cy, r, fill: pointColor, fillOpacity: pts.length > 200 ? 0.55 : 0.8,
        stroke: pts.length > 2000 ? undefined : theme.surface, strokeWidth: 0.75,
        datum: p.row, label: `${p.row.label ? `${p.row.label}: ` : ''}${where}${resid !== null ? `, residual ${fmt.value(resid)}` : ''}`,
        tooltip: { title: p.row.label, rows: [...xyRows(p.x, p.y, fmt, o.xTitle, o.yTitle), ...(fit ? [['Fitted', fmt.value(fit.predict(p.x))] as [string, string]] : [])], color: pointColor },
      })
    })
    if (fitMark) marks.push(fitMark)

    // equation + R² in the emptiest corner
    if (fit && o.showEquation) {
      const FS = SIZES.tick, pad = 6, lineH = FS + 4
      const r2Text = `R² = ${fmt.num(fit.r2, 3)} · n = ${fmt.integer(fit.n)}`
      // inner width: the swatch (14px) and the equation on line one, R² on line two
      const maxW = f.plot.width - 12 - 2 * pad
      const w = Math.min(maxW, Math.max(14 + ctx.measure(eq, FS), ctx.measure(r2Text, FS))) + 2 * pad
      const h = 2 * lineH + pad
      if (w <= f.plot.width - 12 && h <= f.plot.height - 12) {
        const at = emptiestCorner(f.plot, w, h, placed, curve.length > 2 ? curve : interpolate(curve))
        marks.push({ type: 'rect', layer: 'labels', key: 'eq:box', x: at.x, y: at.y, width: w, height: h, rx: 3, fill: theme.surfaceRaised, fillOpacity: 0.92, stroke: theme.grid, strokeWidth: 1, inert: true })
        marks.push({ type: 'line', layer: 'labels', key: 'eq:swatch', x1: at.x + pad, x2: at.x + pad + 10, y1: at.y + pad / 2 + lineH / 2, y2: at.y + pad / 2 + lineH / 2, stroke: fitColor, strokeWidth: 2.25, inert: true })
        const eqShown = ctx.truncate(eq, w - 2 * pad - 14, FS)
        marks.push({ type: 'text', layer: 'labels', key: 'eq:text', x: at.x + pad + 14, y: at.y + pad / 2 + lineH / 2, text: eqShown, full: eqShown !== eq ? eq : undefined, anchor: 'start', baseline: 'central', size: FS, fill: theme.ink, className: eqShown !== eq ? 'oc-trunc' : undefined, inert: eqShown === eq })
        marks.push({ type: 'text', layer: 'labels', key: 'eq:r2', x: at.x + pad, y: at.y + pad / 2 + lineH * 1.5, text: r2Text, anchor: 'start', baseline: 'central', size: FS, fill: theme.inkSecondary, inert: true })
      }
    }
    const need = o.degree + 1
    return {
      marks,
      ...frameResult(f),
      note: notes([[missing, 'rows missing x or y skipped'], [overlapping(pts), 'points overlap another at the same position']])
        ?? (fit ? undefined : `A ${KIND[o.degree]} fit needs at least ${need} distinct x values`),
    }
  },
  describe({ data, options: o, fmt, name }) {
    const { fit, pts } = fitOf(data, o)
    const extra = fit ? [`${KIND[o.degree]} fit ${equation(fit.coefficients, (v) => sig(v, fmt.locale, 3))}, R² = ${fmt.num(fit.r2, 3)}`] : [`too few distinct x values for a ${KIND[o.degree]} fit`]
    return describeXY(name, pts, fmt, { xName: o.xTitle, yName: o.yTitle, extra })
  },
  table(data, o) {
    const fit = fitOf(data, o).fit
    return xyTable(data, fit ? [{ name: 'Fitted', get: (r) => (r.x === null ? null : fit.predict(r.x)) }] : [], o.xTitle, o.yTitle)
  },
})

/** A straight fit has two samples; give the corner search points along it. */
function interpolate(curve: ReadonlyArray<[number, number]>): Array<[number, number]> {
  if (curve.length !== 2) return [...curve]
  const [a, b] = curve as [[number, number], [number, number]]
  return Array.from({ length: 33 }, (_, i) => [a[0] + ((b[0] - a[0]) * i) / 32, a[1] + ((b[1] - a[1]) * i) / 32])
}

export default regressionScatter
