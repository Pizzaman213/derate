// Control chart (Shewhart / XmR): measurements in time order around their mean, with
// control limits at mean ± k·sigma. Points outside the limits are signals: drawn in the
// negative colour AND as diamonds, so they read without colour.

import { defineChart, phrase, stats } from '../../core'
import { finiteExtent, notes } from '../../core/helpers'
import type { Mark } from '../../core'
import { parseDate } from '../../time/parse'
import { diamond, dotsVisible, isolated, linePath, padExtent, pointMark, ruleLabels, sortBy, timeTickCount, zoomFields } from '../_shared/TimelineB'
import { input, options, type ControlOptions, type ControlRow } from './schema'

export interface ControlPoint {
  /** Position on x: epoch ms (time), the given number, or the 1-based row number (index). */
  t: number
  value: number | null
  label?: string
  /** Original row number (0-based), for stable keys. */
  row: number
  /** 'above' / 'below' the control limits, or null when in control (or no value). */
  signal: 'above' | 'below' | null
}

export interface ControlModel {
  kind: 'time' | 'number' | 'index'
  points: ControlPoint[]
  limits: stats.ControlLimits
  /** Limits exist (at least two values with some variation). */
  hasLimits: boolean
  noVariation: boolean
  skippedX: number
  nulls: number
  repeatedX: number
}

/**
 * Resolve x (dates win: when any x is a date, numbers are read as epoch times; no x at all
 * means row order), sort, and compute the limits with stats.controlLimits.
 */
export function controlModel(rows: readonly ControlRow[], o: { method: ControlOptions['method']; sigmas: number }): ControlModel {
  const anyX = rows.some((r) => r.x !== null)
  const kind: ControlModel['kind'] = !anyX ? 'index' : rows.some((r) => r.x instanceof Date) ? 'time' : 'number'
  let skippedX = 0
  const pts: ControlPoint[] = []
  rows.forEach((r, i) => {
    let t: number
    if (kind === 'index') t = i + 1
    else if (r.x === null) { skippedX++; return }
    else if (r.x instanceof Date) t = +r.x
    else if (kind === 'time') { const d = parseDate(r.x); if (!d) { skippedX++; return } t = +d }
    else t = r.x
    pts.push({ t, value: r.value, label: r.label, row: i, signal: null })
  })
  const points = sortBy(pts, (p) => p.t)
  const limits = stats.controlLimits(points.map((p) => p.value), { method: o.method, sigmas: o.sigmas })
  const hasLimits = Number.isFinite(limits.sigma) && limits.sigma > 0
  if (hasLimits) for (const i of limits.outOfControl) points[i]!.signal = points[i]!.value! > limits.ucl ? 'above' : 'below'
  let repeatedX = 0
  for (let i = 1; i < points.length; i++) if (points[i]!.t === points[i - 1]!.t) repeatedX++
  return { kind, points, limits, hasLimits, noVariation: limits.sigma === 0, skippedX, nulls: points.filter((p) => p.value === null).length, repeatedX }
}

const methodName = (m: ControlOptions['method']) => (m === 'individuals' ? 'individuals' : 'standard deviation')

export const controlChart = defineChart<ControlRow[], ControlOptions>({
  id: 'control-chart',
  name: 'Control chart',
  category: 'timeline',
  intents: ['timeline', 'schedule', 'trend', 'deviation'],
  summary: 'Measurements over time around their mean, with control limits and out-of-control points flagged.',
  input,
  options,
  shape: 'rows',
  fields: ['x', 'value', 'label'],
  numberFields: ['value'],
  zoom: 'x',
  isEmpty: (data) => data.length === 0 || (data.every((r) => r.value === null) ? 'No measurements' : false),
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    const m = controlModel(ctx.data, o)
    const { limits: L, points } = m
    const time = m.kind === 'time'
    const xText = (t: number) => (time ? fmt.dateTime(new Date(t)) : m.kind === 'index' ? `#${fmt.num(t)}` : fmt.num(t))
    const nameOf = (p: ControlPoint) => p.label ?? xText(p.t)

    // the rule labels sit in the right margin
    const rules: Array<{ key: string; v: number; text: string; color: string }> = []
    if (Number.isFinite(L.mean)) rules.push({ key: 'mean', v: L.mean, text: `Mean ${fmt.value(L.mean)}`, color: theme.ink })
    if (m.hasLimits) {
      rules.push({ key: 'ucl', v: L.ucl, text: `UCL ${fmt.value(L.ucl)}`, color: theme.negative })
      rules.push({ key: 'lcl', v: L.lcl, text: `LCL ${fmt.value(L.lcl)}`, color: theme.negative })
    }
    const padRight = Math.ceil(Math.max(0, ...rules.map((r) => ctx.measure(r.text)))) + 10

    const ts = points.map((p) => p.t)
    const t0 = ts[0]!, t1 = ts[ts.length - 1]!
    const dom = padExtent(t0, t1, ctx.width - padRight, ctx.measure, time ? 864e5 : 1)
    // sample numbers start at 1: padding must not push the axis past zero (and draw a zero rule)
    if (!time && t0 >= 0 && dom[0] < 0) dom[0] = 0
    const zoneEdge = o.zones && m.hasLimits ? [L.mean - 3 * L.sigma, L.mean + 3 * L.sigma] : []
    const ext = finiteExtent([...points.map((p) => p.value), ...rules.map((r) => r.v), ...zoneEdge]) ?? [0, 1]
    const f = ctx.cartesian({
      x: time ? { type: 'time', domain: [new Date(dom[0]), new Date(dom[1])] } : { type: 'linear', domain: dom, zero: false, nice: false },
      y: { type: 'linear', domain: ext, zero: false },
      xTitle: o.xTitle,
      yTitle: o.yTitle,
      zoom: true,
      xTickCount: m.kind === 'index' ? Math.max(2, Math.min(points.length - 1, 10)) : time ? timeTickCount(points.length, ctx.width) : undefined,
      xFormat: m.kind === 'index' ? (v: number) => (Number.isInteger(v) ? fmt.num(v) : '') : undefined,
      pad: { right: padRight },
    })
    const marks: Mark[] = [...f.marks]
    const X = (t: number) => (time ? f.px(new Date(t)) : f.px(t))
    const { plot } = f

    // zones C (±1σ), B (1–2σ), A (2–3σ)
    if (o.zones && m.hasLimits) {
      const paints = [theme.positive, theme.warning, theme.negative]
      for (let k = 0; k < 3; k++) {
        for (const sgn of [1, -1]) {
          const a = f.py(L.mean + sgn * k * L.sigma), b = f.py(L.mean + sgn * (k + 1) * L.sigma)
          const y = Math.max(plot.y, Math.min(a, b)), y2 = Math.min(plot.y + plot.height, Math.max(a, b))
          if (y2 - y <= 0) continue
          marks.push({ type: 'rect', layer: 'back', key: `zone:${k}:${sgn}`, x: plot.x, y, width: plot.width, height: y2 - y, fill: paints[k]!, fillOpacity: 0.09, inert: true })
        }
      }
    }
    // mean and limits
    for (const r of rules) {
      const y = f.py(r.v)
      if (r.key === 'mean') marks.push({ type: 'line', layer: 'grid', key: 'rule:mean', x1: plot.x, x2: plot.x + plot.width, y1: y, y2: y, stroke: r.color, strokeWidth: 1.25, inert: true })
      else marks.push({ type: 'line', layer: 'grid', key: `rule:${r.key}`, x1: plot.x, x2: plot.x + plot.width, y1: y, y2: y, stroke: r.color, strokeWidth: 1.25, dash: '6 4', inert: true })
    }
    marks.push(...ruleLabels(rules.map((r) => ({ key: r.key, text: r.text, y: f.py(r.v), color: r.color })), plot, ctx.height))

    // the measurements
    const ys = points.map((p) => p.value)
    if (o.line) {
      const d = linePath(points.map((p) => ({ x: X(p.t), y: p.value === null ? null : f.py(p.value) })))
      if (d) marks.push({ type: 'path', key: 'line', d, fill: 'none', stroke: theme.cat(0), strokeWidth: 1.5, linejoin: 'round', strokeOpacity: 0.9, inert: true })
    }
    const show = dotsVisible(points.length, plot.width)
    points.forEach((p, i) => {
      if (p.value === null) return
      const cx = X(p.t), cy = f.py(p.value)
      const name = nameOf(p)
      const rows: Array<[string, string]> = [['Value', fmt.value(p.value)]]
      if (p.label) rows.unshift([time ? 'Date' : m.kind === 'index' ? 'Sample' : 'X', xText(p.t)])
      if (m.hasLimits) rows.push(['vs mean', `${p.value - L.mean >= 0 ? '+' : '−'}${fmt.value(Math.abs(p.value - L.mean))} (${fmt.num(Math.abs(p.value - L.mean) / L.sigma, 1)}σ)`])
      if (p.signal) {
        const lim = p.signal === 'above' ? `above the upper control limit (${fmt.value(L.ucl)})` : `below the lower control limit (${fmt.value(L.lcl)})`
        rows.push(['Status', p.signal === 'above' ? 'Above UCL' : 'Below LCL'])
        marks.push({
          type: 'path', role: 'signal', key: `p:${p.row}`, d: diamond(cx, cy, 5.5), cx, cy, box: { x: cx - 7, y: cy - 7, width: 14, height: 14 },
          fill: theme.negative, stroke: theme.surface, strokeWidth: 1.25, datum: p,
          label: `${name}: ${fmt.value(p.value)}, out of control, ${lim}`, tooltip: { title: name, rows, color: theme.negative },
        })
        return
      }
      marks.push(pointMark({ key: `p:${p.row}`, cx, cy, color: theme.cat(0), surface: theme.surface, datum: p, label: `${name}: ${fmt.value(p.value)}`, title: name, rows, show: show || isolated(ys, i), r: show ? 3 : undefined }))
    })

    const signals = points.filter((p) => p.signal).length
    const items: Array<{ key: string; label: string; color: string; shape: 'circle' | 'line' | 'dash' }> = [{ key: 'measurement', label: 'Measurement', color: theme.cat(0), shape: 'circle' }]
    if (signals) items.push({ key: 'signal', label: 'Out of control (◆)', color: theme.negative, shape: 'circle' })
    items.push({ key: 'mean', label: 'Mean', color: theme.ink, shape: 'line' })
    if (m.hasLimits) items.push({ key: 'limits', label: `Control limits (±${fmt.num(o.sigmas)}σ, ${methodName(o.method)})`, color: theme.negative, shape: 'dash' })
    const valued = points.filter((p) => p.value !== null).length
    return {
      marks,
      legend: { items, toggle: false },
      note: [
        notes([
          [m.nulls, `missing value${m.nulls === 1 ? '' : 's'} skipped`],
          [m.skippedX, `row${m.skippedX === 1 ? '' : 's'} without an x skipped`],
          [m.repeatedX, `repeated x value${m.repeatedX === 1 ? '' : 's'} kept as separate measurements`],
        ]),
        valued < 2 ? 'control limits need at least 2 values' : m.noVariation ? 'every value is the same: no variation, so no control limits' : undefined,
      ].filter(Boolean).join('; ') || undefined,
      ...zoomFields(f, o.zoom),
    }
  },
  describe({ data, options: o, fmt, name }) {
    const m = controlModel(data, o)
    const { limits: L } = m
    const vals = m.points.filter((p) => p.value !== null)
    const xText = (t: number) => (m.kind === 'time' ? fmt.date(new Date(t), 'day') : m.kind === 'index' ? `#${fmt.num(t)}` : fmt.num(t))
    const parts = [name, phrase.count(vals.length, 'measurement')]
    if (m.kind === 'time' && vals.length) parts[1] += ` ${vals.length === 1 ? 'on' : 'from'} ${phrase.dateSpan(vals.map((p) => new Date(p.t)), fmt)}`
    if (Number.isFinite(L.mean)) parts.push(`mean ${fmt.value(L.mean)}`)
    if (m.hasLimits) {
      parts.push(`control limits ${fmt.value(L.lcl)} to ${fmt.value(L.ucl)} (${methodName(o.method)}, ${fmt.num(o.sigmas)}σ)`)
      const sig = m.points.filter((p) => p.signal)
      parts.push(sig.length ? `${sig.length} out of control: ${phrase.listPhrase(sig.map((p) => `${p.label ?? xText(p.t)} (${fmt.value(p.value)}, ${p.signal === 'above' ? 'above UCL' : 'below LCL'})`), 3)}` : 'all in control')
    } else if (m.noVariation && vals.length > 1) parts.push('no variation')
    return parts.join(', ')
  },
  table(data, o, fmt) {
    const m = controlModel(data, o)
    const head = m.kind === 'time' ? 'Date' : m.kind === 'index' ? 'Sample' : 'X'
    return {
      columns: [head, 'Label', 'Value', 'Status'],
      rows: m.points.map((p) => [m.kind === 'time' ? fmt.dateTime(new Date(p.t)) : p.t, p.label ?? null, p.value, p.value === null || !m.hasLimits ? null : p.signal === 'above' ? 'above UCL' : p.signal === 'below' ? 'below LCL' : 'in control']),
    }
  },
})

export default controlChart
