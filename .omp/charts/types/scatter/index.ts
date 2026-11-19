// Scatter plot: one dot per row at (x, y), coloured by series.

import { defineChart } from '../../core'
import { notes } from '../../core/helpers'
import type { Mark } from '../../core'
import { autoRadius, axisSpec, cleanPoints, describeXY, frameResult, inPlot, logTicks, overlapping, placeLabels, seriesOf, shortName, uniqueKeys, xyRows, xyTable, zoomed, type LabelItem } from '../_shared/RelationshipA'
import { input, options, type ScatterOptions, type ScatterRow } from './schema'

export const scatter = defineChart<ScatterRow[], ScatterOptions>({
  id: 'scatter',
  name: 'Scatter plot',
  category: 'relationship',
  intents: ['relationship', 'correlation'],
  summary: 'Dots at (x, y) showing how two measures move together, optionally grouped by series.',
  input,
  options,
  shape: 'rows',
  fields: ['x', 'y', 'series', 'label'],
  numberFields: ['x', 'y'],
  zoom: 'xy',
  aspect: 0.7,
  isEmpty: (data, o) => (cleanPoints(data, o.xScale, o.yScale).pts.length === 0 ? (data.length ? 'No rows have both an x and a y value' : true) : false),
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    const { pts, missing, nonPositive } = cleanPoints(ctx.data, o.xScale, o.yScale)
    const series = seriesOf(ctx.data)
    const multi = series.length > 1
    const colorOf = (s: string | undefined) => theme.cat(s === undefined ? 0 : Math.max(0, series.indexOf(s)))
    const xs = axisSpec(pts.map((p) => p.x), o.xScale), ys = axisSpec(pts.map((p) => p.y), o.yScale)
    const f = ctx.cartesian({ x: xs, y: ys, xTitle: o.xTitle, yTitle: o.yTitle, grid: 'both', zoom: true, xTickCount: logTicks(xs), yTickCount: logTicks(ys) })
    const r = o.pointRadius ?? autoRadius(pts.length)
    const dense = pts.length > 200
    const keys = uniqueKeys(pts.map((p) => `${p.row.series ?? ''}:${p.row.label ?? `${p.x},${p.y}`}`))
    const z = zoomed(ctx)
    const marks: Mark[] = [...f.marks]
    const labelled: LabelItem[] = []
    const placed: Array<{ x: number; y: number; r: number }> = []
    pts.forEach((p, i) => {
      const cx = f.px(p.x), cy = f.py(p.y)
      if (z && !inPlot(f.plot, cx, cy, r)) return
      const fill = colorOf(p.row.series)
      const name = p.row.label ?? (multi ? p.row.series : undefined)
      const where = `x ${fmt.value(p.x)}, y ${fmt.value(p.y)}`
      marks.push({
        type: 'circle', role: 'point', key: keys[i], cx, cy, r, fill, fillOpacity: dense ? 0.6 : 0.85,
        stroke: pts.length > 2000 ? undefined : theme.surface, strokeWidth: 0.75, series: multi ? p.row.series : undefined,
        datum: p.row, label: name ? `${name}: ${where}` : where,
        tooltip: { title: name, rows: [...(p.row.label && multi && p.row.series ? [['Series', shortName(p.row.series, '')] as [string, string]] : []), ...xyRows(p.x, p.y, fmt, o.xTitle, o.yTitle)], color: fill },
      })
      placed.push({ x: cx, y: cy, r })
      if (p.row.label !== undefined && (!multi || !ctx.hidden.has(p.row.series ?? ''))) labelled.push({ key: keys[i]!, x: cx, y: cy, r, text: p.row.label, series: multi ? p.row.series : undefined })
    })
    if (o.labels === 'auto' && labelled.length && labelled.length <= 60) marks.push(...placeLabels(labelled, f.plot, ctx, theme, placed))
    return {
      marks,
      ...frameResult(f),
      legend: multi ? { items: series.map((s, i) => ({ key: s, label: s, color: theme.cat(i), shape: 'circle' as const })) } : undefined,
      note: notes([[missing, 'rows missing x or y skipped'], [nonPositive, 'values ≤ 0 omitted on the log scale'], [overlapping(pts), 'points overlap another at the same position']]),
    }
  },
  describe({ data, options: o, fmt, name }) {
    const { pts } = cleanPoints(data, o.xScale, o.yScale)
    return describeXY(name, pts, fmt, { series: seriesOf(data).length, xName: o.xTitle, yName: o.yTitle })
  },
  table(data, o) {
    return xyTable(data, [], o.xTitle, o.yTitle)
  },
})

export default scatter
