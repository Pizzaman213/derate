// Burndown: work remaining over time against the ideal straight line to zero, with
// scope changes flagged along the top.

import { defineChart, phrase } from '../../core'
import { finiteExtent, notes } from '../../core/helpers'
import type { Mark } from '../../core'
import { parseDate } from '../../time/parse'
import { areaToBase, dotsVisible, EVENT_PAD, eventMarks, isolated, linePath, padExtent, pointMark, sortBy, timeTickCount, zoomFields, type TimelineEvent } from '../_shared/TimelineB'
import { input, options, type BurndownOptions, type BurndownRow } from './schema'

export interface BurndownPoint {
  date: Date
  remaining: number | null
  /** The ideal remaining work at this date (given or computed); null before the first reading. */
  ideal: number | null
  scopeChange: number | null
  note?: string
}

export interface Ideal {
  /** true when the rows carried their own ideal values. */
  given: boolean
  /** Computed ideal: the straight line from (t0, start) to (tEnd, 0); null when the rows supply it. */
  line: { t0: number; start: number; tEnd: number } | null
  /** The ideal at a time (computed line), or null outside it. */
  at(t: number): number | null
}

/**
 * The ideal line. When any row carries `ideal`, those values are used as given. Otherwise it
 * runs straight from the first non-null remaining value (at its date) to zero at `end`
 * (default: the last date).
 */
export function computeIdeal(rows: ReadonlyArray<{ date: Date; remaining: number | null; ideal?: number | null }>, end?: Date | null): Ideal {
  if (rows.some((r) => r.ideal !== null && r.ideal !== undefined)) return { given: true, line: null, at: () => null }
  const first = rows.find((r) => r.remaining !== null)
  if (!first || first.remaining === null) return { given: false, line: null, at: () => null }
  const t0 = +first.date
  const last = rows.length ? +rows[rows.length - 1]!.date : t0
  const tEnd = end && +end > t0 ? +end : last
  const start = first.remaining
  const line = { t0, start, tEnd }
  return {
    given: false,
    line,
    at: (t) => {
      if (t < t0) return null
      if (tEnd === t0) return t === t0 ? start : null
      if (t > tEnd) return 0
      return start * (1 - (t - t0) / (tEnd - t0))
    },
  }
}

export interface PreparedBurndown {
  points: BurndownPoint[]
  ideal: Ideal
  end: Date | null
  duplicates: number
  nulls: number
}

/** Sort by date, merge duplicate dates (the last reading wins, scope changes add up), and attach the ideal. */
export function prepareBurndown(rows: readonly BurndownRow[], o: { end?: BurndownOptions['end']; timeZone?: string }): PreparedBurndown {
  const sorted = sortBy(rows, (r) => +r.date)
  const merged: Array<{ date: Date; remaining: number | null; ideal: number | null; scopeChange: number | null; note?: string }> = []
  let duplicates = 0
  for (const r of sorted) {
    const prev = merged[merged.length - 1]
    if (prev && +prev.date === +r.date) {
      duplicates++
      if (r.remaining !== null) prev.remaining = r.remaining
      if (r.ideal !== null) prev.ideal = r.ideal
      if (r.scopeChange !== null) prev.scopeChange = (prev.scopeChange ?? 0) + r.scopeChange
      if (r.note) prev.note = prev.note && prev.note !== r.note ? `${prev.note}; ${r.note}` : r.note
      continue
    }
    merged.push({ date: r.date, remaining: r.remaining, ideal: r.ideal, scopeChange: r.scopeChange, note: r.note })
  }
  const end = o.end === undefined ? null : parseDate(o.end, { timeZone: o.timeZone })
  const ideal = computeIdeal(merged, end)
  const points: BurndownPoint[] = merged.map((r) => ({ ...r, ideal: ideal.given ? r.ideal : ideal.at(+r.date) }))
  return { points, ideal, end: end && merged.length && +end > +merged[merged.length - 1]!.date ? end : null, duplicates, nulls: merged.filter((r) => r.remaining === null).length }
}

const signed = (v: number, f: (n: number) => string) => (v > 0 ? `+${f(v)}` : v < 0 ? `−${f(-v)}` : f(0))

export const burndown = defineChart<BurndownRow[], BurndownOptions>({
  id: 'burndown',
  name: 'Burndown chart',
  category: 'timeline',
  intents: ['timeline', 'schedule', 'trend', 'kpi'],
  summary: 'Work remaining over time against the ideal line to zero, with scope changes flagged.',
  input,
  options,
  shape: 'rows',
  fields: ['date', 'remaining', 'ideal', 'scopeChange', 'note'],
  dateFields: ['date'],
  numberFields: ['remaining', 'ideal', 'scopeChange'],
  zoom: 'x',
  isEmpty: (data) => data.length === 0 || (data.every((r) => r.remaining === null) ? 'No remaining-work values' : false),
  draw(ctx) {
    const { theme, options: o, fmt, hidden } = ctx
    const p = prepareBurndown(ctx.data, { end: o.end, timeZone: o.timeZone })
    const pts = p.points
    const events = pts.filter((r) => (r.scopeChange !== null && r.scopeChange !== 0) || r.note)
    const t0 = +pts[0]!.date
    const t1 = Math.max(+pts[pts.length - 1]!.date, p.end ? +p.end : -Infinity)
    const ext = finiteExtent([...pts.map((r) => r.remaining), ...(hidden.has('ideal') ? [] : pts.map((r) => r.ideal)), p.ideal.line?.start]) ?? [0, 1]
    const f = ctx.cartesian({
      x: { type: 'time', domain: padExtent(t0, t1, ctx.width, ctx.measure).map((t) => new Date(t)) as [Date, Date] },
      y: { type: 'linear', domain: [Math.min(0, ext[0]), Math.max(0, ext[1])], zero: true },
      xTitle: o.xTitle,
      yTitle: o.yTitle,
      zoom: true,
      xTickCount: timeTickCount(pts.length, ctx.width),
      pad: { top: events.length ? EVENT_PAD : 4 },
    })
    const marks: Mark[] = [...f.marks]
    const X = (d: Date | number) => f.px(d instanceof Date ? d : new Date(d))
    const y0 = f.py(0)
    const actual = theme.cat(0), idealPaint = theme.muted
    const ys = pts.map((r) => r.remaining)

    // ideal line
    if (!hidden.has('ideal')) {
      const d = p.ideal.line
        ? linePath(p.ideal.line.tEnd === p.ideal.line.t0 ? [] : [{ x: X(p.ideal.line.t0), y: f.py(p.ideal.line.start) }, { x: X(p.ideal.line.tEnd), y: y0 }])
        : linePath(pts.map((r) => ({ x: X(r.date), y: r.ideal === null ? null : f.py(r.ideal) })))
      if (d) marks.push({ type: 'path', key: 'ideal', d, fill: 'none', stroke: idealPaint, strokeWidth: 1.5, dash: '6 4', series: 'ideal', inert: true })
    }
    // actual remaining
    if (!hidden.has('remaining')) {
      const line = pts.map((r) => ({ x: X(r.date), y: r.remaining === null ? null : f.py(r.remaining) }))
      if (o.area) {
        const d = areaToBase(line, y0)
        if (d) marks.push({ type: 'path', key: 'area', d, fill: actual, fillOpacity: 0.1, stroke: 'none', series: 'remaining', inert: true })
      }
      const d = linePath(line)
      if (d) marks.push({ type: 'path', key: 'remaining', d, fill: 'none', stroke: actual, strokeWidth: 2, linejoin: 'round', linecap: 'round', series: 'remaining', inert: true })
    }
    const show = dotsVisible(pts.length, f.plot.width)
    pts.forEach((r, i) => {
      if (r.remaining === null) return
      const dt = fmt.dateTime(r.date)
      const diff = r.ideal === null ? null : r.remaining - r.ideal
      const rows: Array<[string, string]> = [['Remaining', fmt.value(r.remaining)]]
      if (r.ideal !== null) rows.push(['Ideal', fmt.value(r.ideal)], ['vs ideal', diff === 0 ? 'on track' : `${signed(diff!, fmt.value)} ${diff! > 0 ? 'behind' : 'ahead'}`])
      if (r.scopeChange) rows.push(['Scope change', signed(r.scopeChange, fmt.value)])
      marks.push(pointMark({
        key: `p:${+r.date}`, cx: X(r.date), cy: f.py(r.remaining), color: actual, surface: theme.surface, series: 'remaining', datum: r,
        label: `${dt}: ${fmt.value(r.remaining)} remaining${r.ideal !== null ? `, ideal ${fmt.value(r.ideal)}` : ''}`,
        title: dt, rows, show: show || isolated(ys, i),
      }))
    })
    // scope changes and notes
    if (!hidden.has('scope') && events.length) {
      const ev: TimelineEvent[] = events.map((r) => {
        const change = r.scopeChange ? `${signed(r.scopeChange, fmt.value)} scope` : ''
        const text = r.note ? (change ? `${change}: ${r.note}` : r.note) : change
        const rows: Array<[string, string]> = [['Date', fmt.dateTime(r.date)]]
        if (r.scopeChange) rows.push(['Scope change', signed(r.scopeChange, fmt.value)])
        return { key: String(+r.date), t: +r.date, text, label: `${fmt.dateTime(r.date)}: ${text}`, rows, datum: r, color: theme.warning, series: 'scope' }
      })
      marks.push(...eventMarks(ctx, f, ev))
    }

    const items = [
      { key: 'remaining', label: 'Remaining', color: actual, shape: 'line' as const },
      { key: 'ideal', label: p.ideal.given ? 'Ideal' : 'Ideal (straight line to zero)', color: idealPaint, shape: 'dash' as const },
    ]
    if (events.length) items.push({ key: 'scope', label: 'Scope change', color: theme.warning, shape: 'line' as const })
    return {
      marks,
      legend: { items },
      note: notes([[p.duplicates, 'duplicate dates merged (last reading kept, scope changes added)'], [p.nulls, `date${p.nulls === 1 ? '' : 's'} without a remaining value left as gaps`]]),
      ...zoomFields(f, o.zoom),
    }
  },
  describe({ data, options: o, fmt, name }) {
    const p = prepareBurndown(data, { end: o.end, timeZone: o.timeZone })
    const vals = p.points.filter((r): r is BurndownPoint & { remaining: number } => r.remaining !== null)
    const first = vals[0], last = vals[vals.length - 1]
    if (!first || !last) return `${name}, no remaining-work values`
    const span = `${phrase.count(p.points.length, 'date')} ${p.points.length === 1 ? 'on' : 'from'} ${phrase.dateSpan(p.points.map((r) => r.date), fmt)}`
    const parts = [name, span, first === last ? `${fmt.value(last.remaining)} remaining` : `remaining ${fmt.value(first.remaining)} to ${fmt.value(last.remaining)}`]
    if (last.ideal !== null) {
      const d = last.remaining - last.ideal
      parts.push(d === 0 ? `on the ideal line at ${fmt.date(last.date, 'day')}` : `${fmt.value(Math.abs(d))} ${d > 0 ? 'behind' : 'ahead of'} ideal at ${fmt.date(last.date, 'day')}`)
    }
    if (p.end) parts.push(`ends ${fmt.date(p.end, 'day')}`)
    const added = p.points.reduce((s, r) => s + (r.scopeChange ?? 0), 0)
    const changes = p.points.filter((r) => r.scopeChange).length
    if (changes) parts.push(`${phrase.count(changes, 'scope change')} (net ${signed(added, fmt.value)})`)
    return parts.join(', ')
  },
  table(data, o, fmt) {
    const p = prepareBurndown(data, { end: o.end, timeZone: o.timeZone })
    return {
      columns: ['Date', 'Remaining', 'Ideal', 'Scope change', 'Note'],
      rows: p.points.map((r) => [fmt.dateTime(r.date), r.remaining, r.ideal === null ? null : Math.round(r.ideal * 1000) / 1000, r.scopeChange, r.note ?? null]),
    }
  },
})

export default burndown
