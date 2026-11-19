// Range area: a band between low and high over time (min–max, confidence interval,
// forecast range), with an optional centre line.

import { defineChart, phrase } from '../../core'
import { finiteExtent, notes } from '../../core/helpers'
import type { Mark } from '../../core'
import { applyGaps, bandPath, CURVES, gapNote, groupSeries, isolated, linePath, pointMark, pointsVisible, spanPhrase, xModel, yNoun, zoomResult } from '../_shared/TrendA'
import type { Formatters } from '../../core/format'
import { input, options, type RangeOptions, type RangeRow } from './schema'

/** Rows sorted by x with duplicates collapsed (first kept), and the gap policy applied per column. */
function model(rows: RangeRow[], o: RangeOptions, fmt: Formatters) {
  const x = xModel(rows.map((r) => r.x), o.xType, fmt)
  const { series, duplicates } = groupSeries(rows, x, (r) => r.x, () => '', () => 0)
  const pts = series[0]?.points ?? []
  const ts = pts.map((p) => p.t)
  const hasY = rows.some((r) => r.y !== null)
  const lo = applyGaps(ts, pts.map((p) => p.row.low), o.gaps)
  const hi = applyGaps(ts, pts.map((p) => p.row.high), o.gaps)
  const ce = applyGaps(ts, pts.map((p) => p.row.y), hasY ? o.gaps : 'gap')
  const missing = pts.filter((p) => p.row.low === null || p.row.high === null || (hasY && p.row.y === null)).length
  return { x, ts, low: lo.ys, high: hi.ys, y: ce.ys, raw: pts.map((p) => p.row), hasY, duplicates, missing }
}

export const rangeArea = defineChart<RangeRow[], RangeOptions>({
  id: 'range-area',
  name: 'Range area chart',
  category: 'trend',
  intents: ['trend', 'change-over-time', 'distribution'],
  summary: 'A band between low and high over time (min–max or confidence interval), with an optional centre line.',
  input,
  options,
  shape: 'rows',
  fields: ['x', 'low', 'high', 'y'],
  dateFields: ['x'],
  numberFields: ['low', 'high', 'y'],
  zoom: 'x',
  isEmpty: (data) => data.length === 0,
  draw(ctx) {
    const { theme, fmt, options: o } = ctx
    const m = model(ctx.data, o, fmt)
    const ext = finiteExtent([...m.low, ...m.high, ...m.y]) ?? [0, 1]
    const f = ctx.cartesian({ x: m.x.spec(m.ts), y: { type: 'linear', domain: ext, zero: false }, xTitle: o.xTitle, yTitle: o.yTitle, zoom: !!o.zoom })
    const marks: Mark[] = [...f.marks]
    const color = theme.cat(0)
    const px = m.ts.map((t) => f.px(m.x.value(t)))
    const bandShown = !ctx.hidden.has('band'), lineShown = !ctx.hidden.has('line')
    const curve = CURVES[o.curve]
    if (bandShown) {
      marks.push({ type: 'path', key: 'band', d: bandPath(px.map((x, i) => ({ x, y0: m.low[i] === null || m.high[i] === null ? null : f.py(m.low[i]), y1: m.low[i] === null || m.high[i] === null ? null : f.py(m.high[i]) })), curve), fill: color, fillOpacity: 0.22, stroke: 'none', series: m.hasY ? 'band' : undefined, inert: true })
      for (const [k, ys] of [['low', m.low], ['high', m.high]] as const) marks.push({ type: 'path', key: `edge:${k}`, d: linePath(px.map((x, i) => ({ x, y: ys[i] === null || ys[i] === undefined ? null : f.py(ys[i]) })), curve), fill: 'none', stroke: color, strokeWidth: 1, strokeOpacity: 0.55, series: m.hasY ? 'band' : undefined, inert: true })
    }
    if (m.hasY && lineShown) marks.push({ type: 'path', key: 'centre', d: linePath(px.map((x, i) => ({ x, y: m.y[i] === null || m.y[i] === undefined ? null : f.py(m.y[i]) })), curve), fill: 'none', stroke: color, strokeWidth: 2, linecap: 'round', linejoin: 'round', series: 'line', inert: true })
    const show = pointsVisible(o.points, m.ts.length, f.plot.width * (ctx.zoom?.k ?? 1)) && (lineShown || !m.hasY)
    const mids = m.ts.map((_, i) => (m.low[i] !== null && m.high[i] !== null ? (m.low[i]! + m.high[i]!) / 2 : null))
    m.ts.forEach((t, i) => {
      const lo = m.low[i] ?? null, hi = m.high[i] ?? null, c = m.hasY ? m.y[i] ?? null : null
      const at = c ?? mids[i]
      if (at === null || at === undefined) return
      const when = m.x.label(t)
      const range = lo !== null && hi !== null ? `${fmt.value(lo)} – ${fmt.value(hi)}` : 'no range'
      const rows: Array<[string, string]> = [[o.bandLabel, range]]
      if (m.hasY) rows.unshift([o.lineLabel, c === null ? '–' : fmt.value(c)])
      marks.push(pointMark({
        key: `p:${t}`, cx: px[i]!, cy: f.py(at), color, surface: theme.surface, show: show && c !== null,
        isolated: m.hasY ? isolated(m.y, i) && lineShown : false,
        datum: { x: m.x.value(t), low: lo, high: hi, y: c },
        label: m.hasY ? `${when}: ${c === null ? 'no value' : fmt.value(c)} (${o.bandLabel.toLowerCase()} ${range})` : `${when}: ${o.bandLabel.toLowerCase()} ${range}`,
        title: when,
        rows,
      }))
    })
    return {
      marks,
      ...zoomResult(f, o.zoom, m.x),
      legend: m.hasY ? { items: [{ key: 'line', label: o.lineLabel, color, shape: 'line' }, { key: 'band', label: o.bandLabel, color, shape: 'square' }] } : undefined,
      note: notes([[m.duplicates, 'duplicate dates ignored (first row kept)'], gapNote(m.missing, o.gaps)]),
    }
  },
  describe({ data, options: o, fmt, name }) {
    const m = model(data, o, fmt)
    let wide = -1
    m.ts.forEach((_, i) => { const w = (m.high[i] ?? NaN) - (m.low[i] ?? NaN); if (Number.isFinite(w) && (wide < 0 || w > (m.high[wide]! - m.low[wide]!))) wide = i })
    const e = finiteExtent(m.ts) ?? [0, 0]
    const lo = finiteExtent(m.low), hi = finiteExtent(m.high)
    const what = yNoun(o) ?? phrase.count(m.ts.length, 'point')
    const range = lo && hi ? `from ${fmt.value(lo[0])} to ${fmt.value(hi[1])}` : 'no values'
    const widest = wide >= 0 ? `, widest in ${m.x.short(m.ts[wide]!, e[1] - e[0])}` : ''
    return `${name}, ${what}, ${spanPhrase(m.x, m.ts, fmt)}, ${range}${widest}`
  },
  table(data, o, fmt) {
    const m = model(data, o, fmt)
    return {
      columns: m.hasY ? [m.x.column, 'Low', 'High', 'Value'] : [m.x.column, 'Low', 'High'],
      rows: m.ts.map((t, i) => (m.hasY ? [m.x.cell(t), m.raw[i]!.low, m.raw[i]!.high, m.raw[i]!.y] : [m.x.cell(t), m.raw[i]!.low, m.raw[i]!.high])),
    }
  },
})

export default rangeArea
