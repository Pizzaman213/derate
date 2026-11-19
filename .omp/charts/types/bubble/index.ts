// Bubble chart: a scatter plot whose dots are sized by a third measure (area ∝ size).

import { defineChart, SIZES } from '../../core'
import { notes } from '../../core/helpers'
import type { DrawContext, Mark } from '../../core'
import { axisSpec, cleanPoints, describeXY, frameResult, inPlot, logTicks, placeLabels, seriesOf, shortName, uniqueKeys, xyRows, xyTable, zoomed, widen, type LabelItem } from '../_shared/RelationshipA'
import { input, options, type BubbleOptions, type BubbleRow } from './schema'

/** Radius for a size: sqrt scale through the origin, so area is proportional to size. */
export function radiusScale(maxSize: number, maxRadius: number): (size: number) => number {
  return (size) => (maxSize > 0 && size > 0 ? maxRadius * Math.sqrt(size / maxSize) : 0)
}

/** Largest 1-2-5 number not above v. */
function niceFloor(v: number): number {
  const p = 10 ** Math.floor(Math.log10(v))
  const m = v / p
  return (m >= 5 ? 5 : m >= 2 ? 2 : 1) * p
}

/** The bubbles that can be drawn (finite x/y, size >= 0) and what was left out. */
function usable(rows: readonly BubbleRow[], o: BubbleOptions) {
  const { pts, missing, nonPositive } = cleanPoints(rows, o.xScale, o.yScale)
  const ok = pts.filter((p) => p.row.size !== null && p.row.size >= 0)
  return {
    pts: ok.map((p) => ({ ...p, size: p.row.size as number })),
    missing,
    nonPositive,
    noSize: pts.filter((p) => p.row.size === null).length,
    negative: pts.filter((p) => p.row.size !== null && p.row.size < 0).length,
  }
}

/** Effective largest radius: the option, shrunk so small charts are not all bubble. */
function maxRadiusFor(ctx: Pick<DrawContext, 'width' | 'height'>, o: BubbleOptions): number {
  return Math.min(o.maxRadius, Math.max(6, Math.min(ctx.width, ctx.height) / 12))
}

/** Nested-circle size key, right-aligned in the band above the plot. */
function sizeKey(ctx: DrawContext<BubbleRow[], BubbleOptions>, maxSize: number, rOf: (s: number) => number): { marks: Mark[]; height: number } {
  const { theme, fmt, options: o } = ctx
  const vals = [...new Set([niceFloor(maxSize), niceFloor(maxSize / 4), niceFloor(maxSize / 16)])].filter((v) => v > 0 && rOf(v) >= 1.5)
  if (!vals.length) return { marks: [], height: 0 }
  const FS = SIZES.tick
  const R = rOf(vals[0]!)
  const base = 4 + FS / 2 + 2 * R
  const texts = vals.map((v) => fmt.value(v))
  const labelW = Math.max(...texts.map((t) => ctx.measure(t)))
  const labelX = ctx.width - 6 - labelW
  const cx = labelX - 8 - R
  if (cx - R < ctx.width * 0.35) return { marks: [], height: 0 }
  const marks: Mark[] = []
  let lastY = -Infinity
  vals.forEach((v, i) => {
    const r = rOf(v)
    const top = base - 2 * r
    marks.push({ type: 'circle', layer: 'labels', key: `sk:c${i}`, cx, cy: base - r, r, fill: 'none', stroke: theme.muted, strokeWidth: 1, inert: true })
    if (top - lastY < FS + 1) return
    lastY = top
    marks.push({ type: 'line', layer: 'labels', key: `sk:l${i}`, x1: cx, x2: labelX - 3, y1: top, y2: top, stroke: theme.muted, strokeWidth: 0.75, dash: '2 2', inert: true })
    marks.push({ type: 'text', layer: 'labels', key: `sk:t${i}`, x: labelX, y: top, text: texts[i]!, anchor: 'start', baseline: 'central', size: FS, fill: theme.inkSecondary })
  })
  const title = o.sizeTitle ?? 'Size'
  const room = cx - R - 8 - ctx.width * 0.35
  const t = ctx.truncate(title, room, FS)
  if (room > 24 && t && t !== '…') marks.push({ type: 'text', layer: 'labels', key: 'sk:title', x: cx - R - 8, y: base - Math.min(R, FS), text: t, full: t !== title ? title : undefined, anchor: 'end', baseline: 'central', size: FS, fill: theme.muted, className: t !== title ? 'oc-trunc' : undefined })
  return { marks, height: 2 * R + FS / 2 + 6 }
}

export const bubble = defineChart<BubbleRow[], BubbleOptions>({
  id: 'bubble',
  name: 'Bubble chart',
  category: 'relationship',
  intents: ['relationship', 'correlation'],
  summary: 'A scatter plot whose dots are sized by a third measure: bubble area is proportional to size.',
  input,
  options,
  shape: 'rows',
  fields: ['x', 'y', 'size', 'series', 'label'],
  numberFields: ['x', 'y', 'size'],
  zoom: 'xy',
  aspect: 0.7,
  isEmpty: (data, o) => (usable(data, o).pts.length === 0 ? (data.length ? 'No rows have an x, a y and a size' : true) : false),
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    const { pts, missing, nonPositive, noSize, negative } = usable(ctx.data, o)
    const series = seriesOf(ctx.data)
    const multi = series.length > 1
    const maxSize = Math.max(0, ...pts.map((p) => p.size))
    const R = maxRadiusFor(ctx, o)
    const rOf = radiusScale(maxSize, R)
    const key = o.sizeLegend && maxSize > 0 ? sizeKey(ctx, maxSize, rOf) : { marks: [], height: 0 }
    const base = { xTitle: o.xTitle, yTitle: o.yTitle, grid: 'both' as const, zoom: true, pad: { top: key.height } }
    const xs0 = axisSpec(pts.map((p) => p.x), o.xScale, 0), ys0 = axisSpec(pts.map((p) => p.y), o.yScale, 0)
    const f0 = ctx.cartesian({ ...base, x: xs0, y: ys0, xTickCount: logTicks(xs0), yTickCount: logTicks(ys0) })
    // leave room for the largest bubble at each end, so none is cut by the plot edge
    const xs = widen(xs0, f0.plot.width, R + 2), ys = widen(ys0, f0.plot.height, R + 2)
    const f = ctx.cartesian({ ...base, x: xs, y: ys, xTickCount: logTicks(xs), yTickCount: logTicks(ys) })
    const z = zoomed(ctx)
    const keys = uniqueKeys(pts.map((p) => `${p.row.series ?? ''}:${p.row.label ?? `${p.x},${p.y}`}`))
    // biggest first, so small bubbles stay on top and reachable
    const order = pts.map((p, i) => ({ p, k: keys[i]! })).sort((a, b) => b.p.size - a.p.size)
    const marks: Mark[] = [...f.marks, ...key.marks]
    const outside: LabelItem[] = []
    const sizeName = shortName(o.sizeTitle, 'size')
    let zeros = 0
    for (const { p, k } of order) {
      const cx = f.px(p.x), cy = f.py(p.y)
      const r = rOf(p.size)
      if (z && !inPlot(f.plot, cx, cy, r)) continue
      const color = theme.cat(p.row.series === undefined ? 0 : Math.max(0, series.indexOf(p.row.series)))
      const name = p.row.label ?? (multi ? p.row.series : undefined)
      const what = `x ${fmt.value(p.x)}, y ${fmt.value(p.y)}, ${sizeName} ${fmt.value(p.size)}`
      const zero = p.size === 0
      if (zero) zeros++
      marks.push({
        type: 'circle', role: 'bubble', key: k, cx, cy, r: zero ? 2.5 : Math.max(1.5, r),
        fill: zero ? theme.surface : color, fillOpacity: zero ? 1 : 0.72, stroke: zero ? color : theme.surface, strokeWidth: zero ? 1.25 : 1,
        series: multi ? p.row.series : undefined,
        datum: p.row, label: name ? `${name}: ${what}` : what,
        tooltip: { title: name, rows: [...(p.row.label && multi && p.row.series ? [['Series', shortName(p.row.series, '')] as [string, string]] : []), ...xyRows(p.x, p.y, fmt, o.xTitle, o.yTitle), [sizeName, fmt.value(p.size)]], color },
      })
      if (o.labels === 'none' || p.row.label === undefined || (multi && ctx.hidden.has(p.row.series ?? ''))) continue
      const t = ctx.truncate(p.row.label, 2 * r - 6, SIZES.tick)
      if (r >= 12 && t === p.row.label && inPlot(f.plot, cx, cy, -r)) {
        marks.push({ type: 'text', layer: 'labels', key: `lbl:${k}`, x: cx, y: cy, text: t, anchor: 'middle', baseline: 'central', size: SIZES.tick, fill: theme.ink, halo: true, inert: true, series: multi ? p.row.series : undefined })
      } else outside.push({ key: k, x: cx, y: cy, r: Math.max(r, 2.5), text: p.row.label, series: multi ? p.row.series : undefined })
    }
    if (outside.length && outside.length <= 60) marks.push(...placeLabels(outside, f.plot, ctx, theme))
    return {
      marks,
      ...frameResult(f),
      legend: multi ? { items: series.map((s, i) => ({ key: s, label: s, color: theme.cat(i), shape: 'circle' as const })) } : undefined,
      note: notes([[missing, 'rows missing x or y skipped'], [noSize, 'rows missing a size skipped'], [negative, 'negative sizes omitted'], [nonPositive, 'values ≤ 0 omitted on the log scale'], [zeros, 'zero sizes drawn as rings']]),
    }
  },
  describe({ data, options: o, fmt, name }) {
    const { pts } = usable(data, o)
    const big = pts.reduce<(typeof pts)[number] | null>((m, p) => (!m || p.size > m.size ? p : m), null)
    const sizeName = o.sizeTitle ?? 'size'
    const extra = big ? [`largest ${sizeName} ${big.row.label ? `${big.row.label} at ` : ''}${fmt.value(big.size)}`] : []
    return describeXY(name, pts, fmt, { noun: 'bubble', series: seriesOf(data).length, xName: o.xTitle, yName: o.yTitle, extra })
  },
  table(data, o) {
    return xyTable(data, [{ name: o.sizeTitle || 'Size', get: (r) => r.size }], o.xTitle, o.yTitle)
  },
})

export default bubble
