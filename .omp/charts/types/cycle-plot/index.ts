import { df } from '../../core/format'
// Cycle plot: one small panel per seasonal position (each month, weekday or quarter);
// inside a panel a line runs across the cycles (years, or weeks) and a horizontal rule
// marks that position's mean. Reads seasonality and trend-within-season together.

import { defineChart, type Formatters, type Mark } from '../../core'
import { notes } from '../../core/helpers'
import { floorTo, zonedParts } from '../../time'
import { dayFormat, linePath, perRender } from '../_shared/TrendB'
import { input, options, type CycleOptions, type CycleRow } from './schema'

type Period = CycleOptions['period']

export interface CyclePoint { cycle: number; value: number; n: number; rows: CycleRow[] }
export interface CyclePosition { index: number; label: string; points: CyclePoint[]; mean: number | null }
export interface CycleModel {
  positions: CyclePosition[]
  /** Cycle keys (a year, or a week start in epoch ms) in order. */
  cycles: number[]
  cycleLabel: (c: number) => string
  averaged: number
  nulls: number
}

const POSITIONS: Record<Period, number> = { 'month-of-year': 12, 'day-of-week': 7, quarter: 4 }

/** Group readings by seasonal position and cycle; repeated (position, cycle) readings are averaged. */
export function cycleModel(data: CycleRow[], period: Period, timeZone: string, weekStart: 0 | 1, locale = 'en-US'): CycleModel {
  const cells = new Map<string, CyclePoint & { pos: number }>()
  let nulls = 0, averaged = 0
  for (const r of data) {
    if (r.value === null) { nulls++; continue }
    const p = zonedParts(r.date, timeZone)
    const pos = period === 'month-of-year' ? p.month - 1 : period === 'quarter' ? Math.floor((p.month - 1) / 3) : (p.weekday - weekStart + 7) % 7
    const cycle = period === 'day-of-week' ? +floorTo(r.date, 'week', { timeZone, weekStart }) : p.year
    const k = `${pos}:${cycle}`
    const hit = cells.get(k)
    if (!hit) { cells.set(k, { pos, cycle, value: r.value, n: 1, rows: [r] }); continue }
    averaged++
    hit.value = (hit.value * hit.n + r.value) / (hit.n + 1)
    hit.n++
    hit.rows.push(r)
  }
  const cycles = [...new Set([...cells.values()].map((c) => c.cycle))].sort((a, b) => a - b)
  const names = (i: number) => {
    if (period === 'quarter') return `Q${i + 1}`
    if (period === 'month-of-year') return df(locale, { month: 'short', timeZone: 'UTC' }).format(new Date(Date.UTC(2026, i, 15)))
    return df(locale, { weekday: 'short', timeZone: 'UTC' }).format(new Date(Date.UTC(2026, 0, 4 + i + weekStart)))
  }
  const positions: CyclePosition[] = Array.from({ length: POSITIONS[period] }, (_, index) => {
    const points = [...cells.values()].filter((c) => c.pos === index).sort((a, b) => a.cycle - b.cycle).map(({ pos: _p, ...pt }) => pt)
    const mean = points.length ? points.reduce((s, p) => s + p.value, 0) / points.length : null
    return { index, label: names(index), points, mean }
  })
  const day = dayFormat(locale, timeZone)
  const cycleLabel = period === 'day-of-week' ? (c: number) => `week of ${day.format(new Date(c))}` : (c: number) => String(c)
  return { positions, cycles, cycleLabel, averaged, nulls }
}

const PERIOD_TEXT: Record<Period, string> = { 'month-of-year': 'month of year', 'day-of-week': 'day of week', quarter: 'quarter' }

/** The model and every point's label and tooltip rows, once per render (draw runs twice; describe and table read it too). */
const cycleRender = perRender((data: CycleRow[], o: CycleOptions, fmt: Formatters) => {
  const m = cycleModel(data, o.period, fmt.timeZone, o.weekStart, fmt.locale)
  const text = m.positions.map((p) => {
    const mean = fmt.value(p.mean)
    return p.points.map((pt) => {
      const who = `${p.label}, ${m.cycleLabel(pt.cycle)}`
      const value = fmt.value(pt.value)
      const rows: Array<[string, string]> = [['Value', value], [`${p.label} mean`, mean]]
      if (pt.n > 1) rows.push(['Readings averaged', String(pt.n)])
      return { who, rows, label: `${who}: ${value}${pt.n > 1 ? ` (mean of ${pt.n})` : ''}` }
    })
  })
  return { m, text }
})

export const cyclePlot = defineChart<CycleRow[], CycleOptions>({
  id: 'cycle-plot',
  name: 'Cycle plot',
  category: 'trend',
  intents: ['trend', 'change-over-time'],
  summary: 'One mini line per season position (month, weekday, quarter) across cycles, with its mean.',
  input,
  options,
  shape: 'rows',
  fields: ['date', 'value'],
  dateFields: ['date'],
  numberFields: ['value'],
  aspect: 0.5,
  minHeight: 220,
  isEmpty: (data) => data.length === 0 || data.every((r) => r.value === null),
  draw(ctx) {
    const { theme, fmt, options: o } = ctx
    const { m, text } = cycleRender(ctx.data, o, fmt)
    const all = m.positions.flatMap((p) => p.points.map((pt) => pt.value))
    const f = ctx.cartesian({
      x: { type: 'band', domain: m.positions.map((p) => String(p.index)), padding: 0.18 },
      y: { type: 'linear', domain: [Math.min(...all), Math.max(...all)], zero: false },
      xFormat: ((k: string) => m.positions[Number(k)]!.label) as (v: never) => string,
      yTitle: o.yTitle,
    })
    const marks: Mark[] = [...f.marks]
    const nC = m.cycles.length
    const ci = new Map(m.cycles.map((c, i) => [c, i]))
    const line = theme.cat(0), meanColor = theme.ink
    const step = f.bw / Math.max(1, nC)
    const r = Math.max(1, Math.min(3, step * 0.4))
    for (const p of m.positions) {
      const x0 = f.px(String(p.index))
      const xOf = (c: number) => x0 + (nC === 1 ? f.bw / 2 : (f.bw * (ci.get(c)! + 0.5)) / nC)
      marks.push({ type: 'rect', layer: 'back', key: `panel:${p.index}`, x: x0, y: f.plot.y, width: f.bw, height: f.plot.height, fill: theme.track, fillOpacity: 0.35, inert: true })
      if (p.mean !== null) {
        const y = f.py(p.mean)
        marks.push({ type: 'line', layer: 'labels', key: `mean:${p.index}`, role: 'mean', x1: x0, x2: x0 + f.bw, y1: y, y2: y, stroke: meanColor, strokeWidth: 2, inert: true })
      }
      // break the line where a cycle is missing
      const pts: Array<readonly [number, number] | null> = []
      p.points.forEach((pt, k) => {
        const prev = p.points[k - 1]
        if (prev && ci.get(pt.cycle)! - ci.get(prev.cycle)! > 1) pts.push(null)
        pts.push([xOf(pt.cycle), f.py(pt.value)])
      })
      marks.push({ type: 'path', key: `line:${p.index}`, role: 'line', d: linePath(pts), fill: 'none', stroke: line, strokeWidth: 1.5, linejoin: 'round', inert: true })
      p.points.forEach((pt, k) => {
        const t = text[p.index]![k]!
        marks.push({
          type: 'circle', role: 'point', key: `p:${p.index}:${pt.cycle}`, cx: xOf(pt.cycle), cy: f.py(pt.value), r, fill: line, stroke: theme.surface, strokeWidth: r > 2 ? 1 : 0,
          datum: pt.rows[0], label: t.label, tooltip: { title: t.who, rows: t.rows, color: line },
        })
      })
    }
    const empty = m.positions.filter((p) => !p.points.length).length
    return {
      marks,
      plot: f.plot,
      legend: { toggle: false, items: [{ key: 'values', label: `Each ${o.period === 'day-of-week' ? 'week' : 'year'}`, color: line, shape: 'line' }, { key: 'mean', label: 'Mean', color: meanColor, shape: 'line' }] },
      note: notes([[m.averaged, 'readings averaged into their period'], [m.nulls, 'missing values skipped'], [empty, `${o.period === 'day-of-week' ? 'weekdays' : o.period === 'quarter' ? 'quarters' : 'months'} without data`]]),
    }
  },
  describe({ data, fmt, options: o, name }) {
    const { m } = cycleRender(data, o, fmt)
    const n = m.positions.reduce((s, p) => s + p.points.length, 0)
    const withMean = m.positions.filter((p) => p.mean !== null)
    let hi = withMean[0]!, lo = withMean[0]!
    for (const p of withMean) { if (p.mean! > hi.mean!) hi = p; if (p.mean! < lo.mean!) lo = p }
    const span = m.cycles.length > 1 ? `${m.cycleLabel(m.cycles[0]!)} to ${m.cycleLabel(m.cycles[m.cycles.length - 1]!)}` : m.cycleLabel(m.cycles[0]!)
    const ext = withMean.length > 1 ? `highest mean ${hi.label} at ${fmt.value(hi.mean)}, lowest mean ${lo.label} at ${fmt.value(lo.mean)}` : `mean ${hi.label} at ${fmt.value(hi.mean)}`
    return `${name}, ${n} ${n === 1 ? 'value' : 'values'} by ${PERIOD_TEXT[o.period]}, ${span}, ${ext}`
  },
  table(data, o, fmt) {
    const { m } = cycleRender(data, o, fmt)
    return { columns: ['Position', 'Cycle', 'Value', 'Readings'], rows: m.positions.flatMap((p) => p.points.map((pt) => [p.label, m.cycleLabel(pt.cycle), pt.value, pt.n])) }
  },
})

export default cyclePlot
