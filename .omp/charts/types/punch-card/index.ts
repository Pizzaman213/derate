import { df } from '../../core/format'
// Punch card: a weekday x hour-of-day grid of circles whose area is proportional to the
// value. Takes pre-bucketed { day, hour, value } rows or raw timestamps, bucketed in the
// chart's time zone.

import { defineChart, phrase, SIZES } from '../../core'
import type { BaseOptions, Formatters, Mark } from '../../core'
import { notes } from '../../core/helpers'
import { parseDate, zonedParts } from '../../time'
import { colorScale, extremesOf } from '../_shared/MatrixCharts'
import { dayToken, input, options, type PunchOptions, type PunchRow } from './schema'

type Opts = PunchOptions & BaseOptions

export interface PunchModel {
  /** value[weekday 0 = Sunday][hour]; undefined = nothing in that bucket. */
  value: Array<Array<number | undefined>>
  /** Weekdays (0 = Sunday) in display order. */
  days: number[]
  dayName: string[]
  total: number
  rows: number
  duplicates: number
  nulls: number
  negatives: number
}

const PLAIN_DATE = /^\d{4}-\d{2}-\d{2}$/

/** Weekday and hour of one row in `timeZone`; a plain calendar date keeps its own weekday. */
export function bucketOf(r: PunchRow, timeZone: string): { day: number; hour: number } | null {
  if (r.date !== undefined) {
    const d = parseDate(r.date, { timeZone })
    if (!d) return null
    const p = zonedParts(d, timeZone)
    return { day: p.weekday, hour: p.hour }
  }
  if (r.day === undefined || r.hour === null) return null
  const t = dayToken(r.day)
  if (t === null) return null
  if (t !== 'date') return { day: t, hour: r.hour }
  if (typeof r.day === 'string' && PLAIN_DATE.test(r.day.trim())) return { day: new Date(`${r.day.trim()}T00:00:00Z`).getUTCDay(), hour: r.hour }
  const d = parseDate(r.day, { timeZone })
  return d ? { day: zonedParts(d, timeZone).weekday, hour: r.hour } : null
}

export function model(data: PunchRow[], o: Opts, fmt: Formatters): PunchModel {
  const tz = fmt.timeZone
  const value: Array<Array<number | undefined>> = Array.from({ length: 7 }, () => new Array<number | undefined>(24).fill(undefined))
  let total = 0, duplicates = 0, nulls = 0, negatives = 0
  for (const r of data) {
    if (r.value === null) { nulls++; continue }
    if (r.value < 0) { negatives++; continue }
    const b = bucketOf(r, tz)
    if (!b) continue
    const row = value[b.day]!
    // summing is the point for timestamps; for pre-bucketed rows a repeat is worth a note
    if (row[b.hour] !== undefined && r.date === undefined) duplicates++
    row[b.hour] = (row[b.hour] ?? 0) + r.value
    total += r.value
  }
  const days = o.weekStart === 'sunday' ? [0, 1, 2, 3, 4, 5, 6] : [1, 2, 3, 4, 5, 6, 0]
  const names = df(fmt.locale, { weekday: o.dayFormat, timeZone: 'UTC' })
  // 2026-01-04 was a Sunday
  const dayName = Array.from({ length: 7 }, (_, i) => names.format(new Date(Date.UTC(2026, 0, 4 + i))))
  return { value, days, dayName, total, rows: data.length, duplicates, nulls, negatives }
}

const hourText = (h: number) => String(h).padStart(2, '0')
const slot = (h: number) => `${hourText(h)}:00–${hourText((h + 1) % 24)}:00`

export const punchCard = defineChart<PunchRow[], PunchOptions>({
  id: 'punch-card',
  name: 'Punch card',
  category: 'matrix',
  intents: ['trend', 'distribution', 'compare', 'relationship', 'correlation'],
  summary: 'Activity by weekday and hour of day as circles sized by value.',
  input,
  options,
  shape: 'rows',
  fields: ['day', 'hour', 'date', 'value'],
  numberFields: ['hour', 'value'],
  aspect: 0.4,
  minHeight: 200,
  maxHeight: 420,
  isEmpty: (data) => data.length === 0,
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    const m = model(ctx.data, o, fmt)
    const hours = Array.from({ length: 24 }, (_, h) => String(h))
    const labelH = SIZES.tick + 10
    const f = ctx.cartesian({
      x: { type: 'band', domain: hours, padding: 0 },
      y: { type: 'band', domain: m.days.map(String), padding: 0 },
      yFormat: ((d: string) => m.dayName[Number(d)]!) as (v: never) => string,
      xAxis: 'none',
      grid: 'none',
      pad: { bottom: labelH + (o.xTitle ? SIZES.label + 6 : 0), right: 4 },
    })
    const marks: Mark[] = [...f.marks]
    const bw = f.bw, bh = f.bh
    const rMax = Math.max(1, (Math.min(bw, bh) / 2) * 0.9)
    const all = m.value.flat().filter((v): v is number => v !== undefined)
    const max = Math.max(0, ...all)
    const scale = colorScale(theme, all, { diverging: false, floor: 0.25 })
    const bottom = f.plot.y + f.plot.height
    // hour labels: every k-th hour, k from the space a label needs
    const k = [1, 2, 3, 4, 6, 12].find((s) => s * bw >= ctx.measure('00') + 8) ?? 12
    for (let h = 0; h < 24; h++) {
      const cx = f.px(String(h)) + bw / 2
      if (h % k === 0) {
        marks.push({ type: 'line', layer: 'axes', key: `xt:${h}`, x1: cx, x2: cx, y1: bottom, y2: bottom + 4, stroke: theme.axis, strokeWidth: 1, inert: true })
        marks.push({ type: 'text', layer: 'axes', key: `xl:${h}`, x: cx, y: bottom + 7, text: hourText(h), anchor: 'middle', baseline: 'hanging', size: SIZES.tick, fill: theme.inkSecondary })
      }
    }
    marks.push({ type: 'line', layer: 'axes', key: 'x-domain', x1: f.plot.x, x2: f.plot.x + f.plot.width, y1: bottom, y2: bottom, stroke: theme.axis, strokeWidth: 1, inert: true })
    if (o.xTitle) {
      const t = ctx.truncate(o.xTitle, f.plot.width, SIZES.label)
      marks.push({ type: 'text', layer: 'axes', key: 'x-title', x: f.plot.x + f.plot.width / 2, y: ctx.height - 4, text: t, full: t !== o.xTitle ? o.xTitle : undefined, anchor: 'middle', baseline: 'alphabetic', size: SIZES.label, fill: theme.muted })
    }
    for (const d of m.days) {
      const cy = f.py(String(d)) + bh / 2
      marks.push({ type: 'line', layer: 'grid', key: `row:${d}`, x1: f.plot.x, x2: f.plot.x + f.plot.width, y1: cy, y2: cy, stroke: theme.grid, strokeWidth: 1, inert: true })
      for (let h = 0; h < 24; h++) {
        const v = m.value[d]![h]
        if (v === undefined) continue
        const cx = f.px(String(h)) + bw / 2
        const name = `${m.dayName[d]} ${slot(h)}`
        const fill = o.color === 'value' ? scale.paint(v) : theme.cat(0)
        marks.push({
          type: 'circle', role: 'point', key: `${d}:${h}`, cx, cy, r: max > 0 && v > 0 ? Math.max(1, Math.sqrt(v / max) * rMax) : 1,
          fill: v > 0 ? fill : theme.muted, datum: { day: d, hour: h, value: v },
          label: `${name}: ${fmt.value(v)}`, tooltip: { title: name, rows: [['Value', fmt.value(v)], ['Share of total', m.total > 0 ? fmt.pct(v / m.total, 1) : '–']], color: fill },
        })
      }
    }
    return {
      marks,
      plot: f.plot,
      legend: o.color === 'value' && all.length ? { gradient: scale.legend(fmt) } : undefined,
      note: notes([[m.duplicates, 'duplicate day/hour rows summed'], [m.nulls, 'missing values skipped'], [m.negatives, 'negative values omitted']]),
    }
  },
  describe({ data, options: o, fmt, name }) {
    const m = model(data, o, fmt)
    const items = m.days.flatMap((d) => m.value[d]!.map((v, h) => ({ label: `${m.dayName[d]} ${hourText(h)}:00`, value: v ?? null }))).filter((x) => x.value !== null)
    const busiest = extremesOf(items, fmt, 'slot').replace(/^highest /, 'busiest ').replace(', lowest ', ', quietest ')
    return `${name}, ${fmt.value(m.total)} in total across ${phrase.count(items.length, 'weekday-hour slot')}, ${busiest}`
  },
  table(data, o, fmt) {
    const m = model(data, o, fmt)
    const rows: Array<[string, number, number]> = []
    for (const d of m.days) m.value[d]!.forEach((v, h) => { if (v !== undefined) rows.push([m.dayName[d]!, h, v]) })
    return { columns: ['Day', 'Hour', 'Value'], rows }
  },
})

export default punchCard
