// Calendar heatmap: one block per year, weeks as columns and weekdays as rows (GitHub
// style), each day shaded by its value. Days are read in options.timeZone, so a reading
// late in the evening lands on the local day, across DST changes too.

import { defineChart, SIZES, type Formatters, type Mark } from '../../core'
import { df } from '../../core/format'
import { notes } from '../../core/helpers'
import { zonedParts } from '../../time'
import { dayFormat, perRender } from '../_shared/TrendB'
import { input, options, type CalendarOptions, type CalendarRow } from './schema'

export interface CalendarCell {
  year: number
  month: number
  day: number
  /** Column: week of the year, 0 = the (partial) week holding Jan 1. */
  week: number
  /** Row: 0 = weekStart. */
  row: number
}

const dayNumber = (y: number, m: number, d: number) => Date.UTC(y, m - 1, d) / 864e5

/** Week column and weekday row of an instant's local day. */
export function cellOf(date: Date, timeZone: string, weekStart: 0 | 1): CalendarCell {
  const p = zonedParts(date, timeZone)
  return cellOfDay(p.year, p.month, p.day, weekStart)
}

/** Week column and weekday row of a calendar day. */
export function cellOfDay(year: number, month: number, day: number, weekStart: 0 | 1): CalendarCell {
  const doy = dayNumber(year, month, day) - dayNumber(year, 1, 1)
  const jan1 = new Date(Date.UTC(year, 0, 1)).getUTCDay()
  const offset = (jan1 - weekStart + 7) % 7
  const wd = new Date(Date.UTC(year, month - 1, day)).getUTCDay()
  return { year, month, day, week: Math.floor((doy + offset) / 7), row: (wd - weekStart + 7) % 7 }
}

interface Day { key: string; cell: CalendarCell; value: number | null; rows: CalendarRow[]; date: Date }

/** Rows -> one entry per local day (values summed), in date order. */
export function calendarDays(data: CalendarRow[], timeZone: string, weekStart: 0 | 1): { days: Day[]; duplicates: number } {
  const map = new Map<string, Day>()
  let duplicates = 0
  for (const r of data) {
    const cell = cellOf(r.date, timeZone, weekStart)
    const key = `${cell.year}-${String(cell.month).padStart(2, '0')}-${String(cell.day).padStart(2, '0')}`
    const hit = map.get(key)
    if (!hit) { map.set(key, { key, cell, value: r.value, rows: [r], date: new Date(Date.UTC(cell.year, cell.month - 1, cell.day, 12)) }); continue }
    duplicates++
    hit.rows.push(r)
    hit.value = hit.value === null ? r.value : r.value === null ? hit.value : hit.value + r.value
  }
  return { days: [...map.values()].sort((a, b) => (a.key < b.key ? -1 : 1)), duplicates }
}

const pad2 = Array.from({ length: 32 }, (_, i) => String(i).padStart(2, '0'))

/** Short weekday names indexed by UTC weekday (0 = Sunday). */
const weekdayNames = (locale: string) => {
  const f = df(locale, { weekday: 'short', timeZone: 'UTC' })
  return Array.from({ length: 7 }, (_, wd) => f.format(new Date(Date.UTC(2026, 0, 4 + wd))))
}

/**
 * Days, scale and every data day's label once per render: draw runs twice (again after the
 * legend is measured) and describe and table read the same days.
 */
const calendarModel = perRender((data: CalendarRow[], o: CalendarOptions, fmt: Formatters) => {
  const { days, duplicates } = calendarDays(data, fmt.timeZone, o.weekStart)
  const wd = weekdayNames(fmt.locale)
  const day = dayFormat(fmt.locale, 'UTC')
  let lo = Infinity, hi = -Infinity, nulls = 0
  const byKey = new Map<string, { d: Day; label: string; value: string }>()
  for (const d of days) {
    if (d.value === null) { nulls++; continue }
    lo = Math.min(lo, d.value); hi = Math.max(hi, d.value)
    byKey.set(d.key, { d, label: `${wd[d.date.getUTCDay()]} ${day.format(d.date)}`, value: fmt.value(d.value) })
  }
  if (lo > hi) { lo = 0; hi = 1 }
  return { days, duplicates, nulls, byKey, lo, hi, wd, day }
})

export const calendarHeatmap = defineChart<CalendarRow[], CalendarOptions>({
  id: 'calendar-heatmap',
  name: 'Calendar heatmap',
  category: 'trend',
  intents: ['trend', 'change-over-time'],
  summary: 'Days laid out as weeks × weekdays per year, shaded by value (GitHub style).',
  input,
  options,
  shape: 'rows',
  fields: ['date', 'value'],
  dateFields: ['date'],
  numberFields: ['value'],
  aspect: 0.17,
  minHeight: 130,
  maxHeight: 900,
  isEmpty: (data) => data.length === 0,
  draw(ctx) {
    const { theme, fmt, options: o, width: W, height: H } = ctx
    const { days, duplicates, nulls, byKey, lo, hi, wd } = calendarModel(ctx.data, o, fmt)
    const years: number[] = []
    for (let y = days[0]!.cell.year; y <= days[days.length - 1]!.cell.year; y++) years.push(y)
    const FS = SIZES.tick
    const wdName = (row: number) => wd[(row + o.weekStart) % 7]!
    const gutter0 = Math.ceil(Math.max(ctx.measure(String(years[0])), ...[1, 3, 5].map((r) => ctx.measure(wdName(r))))) + 8
    let gutter = gutter0
    const monthRow = FS + 6, yearGap = years.length > 1 ? 10 : 0
    const weeksOf = (y: number) => cellOfDay(y, 12, 31, o.weekStart).week + 1
    const cols = Math.max(...years.map(weeksOf))
    const cellW = (W - gutter0 - 2) / cols
    const cellH = (H - 2 - years.length * (monthRow + yearGap) + yearGap) / (7 * years.length)
    const cell = Math.max(1.5, Math.min(cellW, cellH, 28))
    const gap = cell > 8 ? 2 : cell > 4 ? 1 : 0
    // centre the weeks in the spare width; the year and weekday labels stay beside them
    gutter += Math.max(0, Math.floor((W - gutter0 - 2 - cols * cell) / 2))
    const r = cell > 8 ? 2 : 0
    const shade = (v: number) => theme.seq(hi === lo ? 0.6 : 0.12 + 0.88 * ((v - lo) / (hi - lo)))
    const side = Math.max(0.5, cell - gap)
    const marks: Mark[] = []
    const fmtMonth = df(fmt.locale, { month: 'short', timeZone: 'UTC' })
    years.forEach((y, yi) => {
      const top = 1 + yi * (monthRow + 7 * cell + yearGap)
      const gy = top + monthRow
      marks.push({ type: 'text', layer: 'axes', key: `year:${y}`, x: gutter - gutter0, y: top + FS / 2 + 1, text: String(y), anchor: 'start', baseline: 'central', size: FS, weight: 600, fill: theme.ink })
      if (cell * 2 >= FS + 1) for (const row of [1, 3, 5]) marks.push({ type: 'text', layer: 'axes', key: `wd:${y}:${row}`, x: gutter - 6, y: gy + row * cell + cell / 2, text: wdName(row), anchor: 'end', baseline: 'central', size: FS, fill: theme.inkSecondary })
      // month labels at the column holding the 1st, skipped when they would collide
      let free = gutter
      for (let m = 1; m <= 12; m++) {
        const c = cellOfDay(y, m, 1, o.weekStart)
        const x = gutter + c.week * cell + (c.row === 0 ? 0 : cell)
        const text = fmtMonth.format(new Date(Date.UTC(2026, m - 1, 15)))
        const tw = ctx.measure(text)
        if (x < free || x + tw > W - 1) continue
        marks.push({ type: 'text', layer: 'axes', key: `m:${y}:${m}`, x, y: top + FS / 2 + 1, text, anchor: 'start', baseline: 'central', size: FS, fill: theme.inkSecondary })
        free = x + tw + 6
      }
      // every day of the year, walked as (day of year + Jan 1's row) -> column, row (see
      // cellOfDay): data days are marks, the rest are empty track squares
      const offset = (new Date(Date.UTC(y, 0, 1)).getUTCDay() - o.weekStart + 7) % 7
      let i = 0
      for (let m = 1; m <= 12; m++) {
        const len = new Date(Date.UTC(y, m, 0)).getUTCDate()
        for (let dd = 1; dd <= len; dd++, i++) {
          const x = gutter + Math.floor((i + offset) / 7) * cell + gap / 2, yy = gy + ((i + offset) % 7) * cell + gap / 2
          const key = `${y}-${pad2[m]}-${pad2[dd]}`
          const hit = byKey.get(key)
          if (hit) {
            const fill = shade(hit.d.value!)
            marks.push({
              type: 'rect', role: 'cell', key, x, y: yy, width: side, height: side, rx: r, fill,
              datum: hit.d.rows[0], label: `${hit.label}: ${hit.value}`, tooltip: { title: hit.label, rows: [['Value', hit.value]], color: fill },
            })
          } else {
            marks.push({ type: 'rect', layer: 'back', key: `e:${key}`, x, y: yy, width: side, height: side, rx: r, fill: theme.track, fillOpacity: 0.6, inert: true })
          }
        }
      }
    })
    return {
      marks,
      legend: { gradient: { stops: theme.seqStops(), min: fmt.value(lo), max: fmt.value(hi) } },
      note: notes([[duplicates, 'rows sharing a day summed'], [nulls, 'days without a value shown empty']]),
    }
  },
  describe({ data, fmt, options: o, name }) {
    const { days, day } = calendarModel(data, o, fmt)
    const ok = days.filter((d) => d.value !== null)
    const label = (d: Day) => day.format(d.date)
    const span = days.length > 1 ? `${label(days[0]!)} to ${label(days[days.length - 1]!)}` : label(days[0]!)
    if (!ok.length) return `${name}, ${days.length} days, ${span}, no values`
    let hi = ok[0]!, lo = ok[0]!
    for (const d of ok) { if (d.value! > hi.value!) hi = d; if (d.value! < lo.value!) lo = d }
    const total = ok.reduce((s, d) => s + d.value!, 0)
    return `${name}, ${ok.length} ${ok.length === 1 ? 'day' : 'days'}, ${span}, total ${fmt.value(total)}, highest ${label(hi)} at ${fmt.value(hi.value)}, lowest ${label(lo)} at ${fmt.value(lo.value)}`
  },
  table(data, o, fmt) {
    return { columns: ['Date', 'Value'], rows: calendarModel(data, o, fmt).days.map((d) => [d.key, d.value]) }
  },
})

export default calendarHeatmap
