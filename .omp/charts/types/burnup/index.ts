// Burnup: completed work climbing toward the total scope, both drawn so scope changes
// stay visible, with a least-squares projection of the completion date.

import { defineChart, phrase, SIZES, stats, type Formatters } from '../../core'
import { floorTo } from '../../time/granularity'
import { finiteExtent, notes } from '../../core/helpers'
import type { Mark } from '../../core'
import { areaToBase, curveStepAfter, diamond, dotsVisible, EVENT_PAD, eventMarks, isolated, linePath, padExtent, pointMark, sortBy, timeTickCount, zoomFields, type TimelineEvent } from '../_shared/TimelineB'
import { input, options, type BurnupOptions, type BurnupRow } from './schema'

const DAY = 864e5
/** A projection further out than this many data spans is reported, not drawn to its end. */
const MAX_AHEAD = 3

export interface BurnupPoint {
  date: Date
  completed: number | null
  scope: number | null
  note?: string
}

export type Projection =
  | { status: 'projected'; date: Date; target: number; perDay: number; from: { t: number; value: number }; fit: stats.LinearFit }
  | { status: 'done'; date: Date; target: number }
  | { status: 'stalled' | 'insufficient' | 'no-scope'; target: number | null }

/**
 * Least-squares line through completed work (x in days), over the last `window` readings
 * (default all). The projected date is where that line reaches the latest scope; when the
 * line has already passed it while the work is not done, the fitted rate is carried on from
 * the last reading instead.
 */
export function projectCompletion(points: readonly BurnupPoint[], window?: number): Projection {
  const scopes = points.filter((p) => p.scope !== null)
  const target = scopes.length ? scopes[scopes.length - 1]!.scope! : null
  const done = points.filter((p): p is BurnupPoint & { completed: number } => p.completed !== null)
  if (target === null) return { status: 'no-scope', target }
  const reached = done.find((p) => p.completed >= target && +p.date >= +scopes[scopes.length - 1]!.date)
  const last = done[done.length - 1]
  if (last && last.completed >= target) return { status: 'done', date: (reached ?? last).date, target }
  const used = window ? done.slice(-window) : done
  if (used.length < 2) return { status: 'insufficient', target }
  const base = +used[0]!.date
  const fit = stats.linearRegression(used.map((p) => ({ x: (+p.date - base) / DAY, y: p.completed })))
  if (!fit || !(fit.slope > 0)) return { status: 'stalled', target }
  const x = (target - fit.intercept) / fit.slope
  const lastX = (+last!.date - base) / DAY
  if (x > lastX) return { status: 'projected', date: new Date(base + x * DAY), target, perDay: fit.slope, from: { t: base, value: fit.intercept }, fit }
  // the fitted line already passed the scope while the work is not done: carry the rate on from the last reading
  return { status: 'projected', date: new Date(+last!.date + ((target - last!.completed) / fit.slope) * DAY), target, perDay: fit.slope, from: { t: +last!.date, value: last!.completed }, fit }
}

export interface PreparedBurnup { points: BurnupPoint[]; duplicates: number; nulls: number }

/** Sort by date; the same date twice keeps the last reading of each field (and notes both notes). */
export function prepareBurnup(rows: readonly BurnupRow[]): PreparedBurnup {
  const out: BurnupPoint[] = []
  let duplicates = 0
  for (const r of sortBy(rows, (x) => +x.date)) {
    const prev = out[out.length - 1]
    if (prev && +prev.date === +r.date) {
      duplicates++
      if (r.completed !== null) prev.completed = r.completed
      if (r.scope !== null) prev.scope = r.scope
      if (r.note) prev.note = prev.note && prev.note !== r.note ? `${prev.note}; ${r.note}` : r.note
      continue
    }
    out.push({ date: r.date, completed: r.completed, scope: r.scope, note: r.note })
  }
  return { points: out, duplicates, nulls: out.filter((p) => p.completed === null || p.scope === null).length }
}

/** A projection is a day-level estimate: its calendar day (in the chart's zone), with the year. */
function projectedDay(fmt: Formatters, d: Date): string {
  return fmt.dateTime(floorTo(d, 'day', { timeZone: fmt.timeZone }))
}

export const burnup = defineChart<BurnupRow[], BurnupOptions>({
  id: 'burnup',
  name: 'Burnup chart',
  category: 'timeline',
  intents: ['timeline', 'schedule', 'trend', 'kpi'],
  summary: 'Completed work climbing toward total scope, with the projected completion date.',
  input,
  options,
  shape: 'rows',
  fields: ['date', 'completed', 'scope', 'note'],
  dateFields: ['date'],
  numberFields: ['completed', 'scope'],
  zoom: 'x',
  isEmpty: (data) => data.length === 0 || (data.every((r) => r.completed === null && r.scope === null) ? 'No completed or scope values' : false),
  draw(ctx) {
    const { theme, options: o, fmt, hidden } = ctx
    const { points: pts, duplicates, nulls } = prepareBurnup(ctx.data)
    const proj = o.projection ? projectCompletion(pts, o.projectionWindow) : null
    const t0 = +pts[0]!.date
    const tLast = +pts[pts.length - 1]!.date
    const span = Math.max(tLast - t0, DAY)
    /** Headroom over the plot for the "Done ~date" label when the scope is the top of the axis. */
    const LABEL_ROOM = SIZES.tick + 6
    const projected = proj?.status === 'projected' && !hidden.has('completed') ? proj : null
    const beyond = projected ? +projected.date > tLast + MAX_AHEAD * span : false
    const t1 = projected && !beyond ? Math.max(tLast, +projected.date) : tLast
    const events = pts.filter((p) => p.note)
    const dom = padExtent(t0, t1, ctx.width, ctx.measure)
    const ext = finiteExtent([...pts.map((p) => p.completed), ...pts.map((p) => p.scope)]) ?? [0, 1]
    const f = ctx.cartesian({
      x: { type: 'time', domain: [new Date(dom[0]), new Date(dom[1])] },
      y: { type: 'linear', domain: [Math.min(0, ext[0]), Math.max(0, ext[1])], zero: true },
      xTitle: o.xTitle,
      yTitle: o.yTitle,
      zoom: true,
      xTickCount: timeTickCount(pts.length, ctx.width),
      pad: { top: (events.length ? EVENT_PAD : 4) + (projected && !beyond ? LABEL_ROOM : 0) },
    })
    const marks: Mark[] = [...f.marks]
    const X = (t: number) => f.px(new Date(t))
    const y0 = f.py(0)
    const cDone = theme.cat(0), cScope = theme.cat(1)
    const show = dotsVisible(pts.length, f.plot.width)

    const scopeLine = pts.map((p) => ({ x: X(+p.date), y: p.scope === null ? null : f.py(p.scope) }))
    const doneLine = pts.map((p) => ({ x: X(+p.date), y: p.completed === null ? null : f.py(p.completed) }))
    if (!hidden.has('completed') && o.area) {
      const d = areaToBase(doneLine, y0)
      if (d) marks.push({ type: 'path', key: 'area', d, fill: cDone, fillOpacity: 0.1, stroke: 'none', series: 'completed', inert: true })
    }
    if (!hidden.has('scope')) {
      const d = linePath(scopeLine, curveStepAfter)
      if (d) marks.push({ type: 'path', key: 'scope', d, fill: 'none', stroke: cScope, strokeWidth: 2, linejoin: 'round', series: 'scope', inert: true })
    }
    if (!hidden.has('completed')) {
      const d = linePath(doneLine)
      if (d) marks.push({ type: 'path', key: 'completed', d, fill: 'none', stroke: cDone, strokeWidth: 2, linejoin: 'round', linecap: 'round', series: 'completed', inert: true })
    }
    // projection: the fitted line, from the first fitted reading to the latest scope
    if (projected) {
      const end = beyond ? dom[1] : +projected.date
      const endValue = beyond ? projected.from.value + ((projected.target - projected.from.value) * (end - projected.from.t)) / (+projected.date - projected.from.t) : projected.target
      // a negative intercept would start the line under the axis: begin where it crosses zero
      const s = projected.from.value < 0 ? { t: projected.from.t + (-projected.from.value / projected.perDay) * DAY, value: 0 } : projected.from
      const d = linePath([{ x: X(s.t), y: f.py(s.value) }, { x: X(end), y: f.py(Math.min(endValue, projected.target)) }])
      marks.push({ type: 'path', key: 'projection', d, fill: 'none', stroke: cDone, strokeWidth: 1.5, dash: '5 4', strokeOpacity: 0.85, series: 'projection', inert: true })
      if (!beyond) {
        const cx = X(+projected.date), cy = f.py(projected.target)
        const sameYear = new Date(projected.date).getUTCFullYear() === new Date(t0).getUTCFullYear()
        const when = sameYear ? fmt.date(projected.date, 'day') : projectedDay(fmt, projected.date)
        const full = projectedDay(fmt, projected.date)
        // the latest scope, carried on (dashed) to where the projection meets it
        const lastScope = [...pts].reverse().find((p) => p.scope !== null)
        if (lastScope && !hidden.has('scope') && cx - X(+lastScope.date) > 8) {
          marks.push({ type: 'line', key: 'scope-ahead', x1: X(+lastScope.date), x2: cx - 6, y1: cy, y2: cy, stroke: cScope, strokeWidth: 1.5, dash: '2 4', strokeOpacity: 0.8, series: 'scope', inert: true })
        }
        marks.push({
          type: 'path', role: 'projection', key: 'projected', d: diamond(cx, cy, 5.5), cx, cy, box: { x: cx - 7, y: cy - 7, width: 14, height: 14 },
          fill: theme.surface, stroke: cDone, strokeWidth: 2, series: 'projection', datum: projected,
          label: `Projected completion ${full}, at ${fmt.value(projected.perDay)} per day`,
          tooltip: { title: 'Projected completion', rows: [['Date', full], ['Scope', fmt.value(projected.target)], ['Rate', `${fmt.value(projected.perDay)} / day`]], color: cDone },
        })
        // label above the scope level (clear of the rising projection); below it when there is no headroom
        const text = `Done ~${when}`
        const w = ctx.measure(text)
        const right = cx - 9
        const above = cy - 6 - SIZES.tick >= f.plot.y - LABEL_ROOM
        if (right - w >= f.plot.x) marks.push({ type: 'text', layer: 'labels', key: 'projected-label', x: right, y: above ? cy - 6 : cy + 6, text, anchor: 'end', baseline: above ? 'alphabetic' : 'hanging', fill: theme.inkSecondary, halo: true, series: 'projection' })
      }
    }
    // points: completed, then scope
    const series: Array<{ key: 'completed' | 'scope'; name: string; color: string; get: (p: BurnupPoint) => number | null }> = [
      { key: 'completed', name: 'Completed', color: cDone, get: (p) => p.completed },
      { key: 'scope', name: 'Scope', color: cScope, get: (p) => p.scope },
    ]
    for (const s of series) {
      const ys = pts.map(s.get)
      pts.forEach((p, i) => {
        const v = ys[i]
        if (v === null || v === undefined) return
        const dt = fmt.dateTime(p.date)
        const rows: Array<[string, string]> = [['Completed', fmt.value(p.completed)], ['Scope', fmt.value(p.scope)]]
        if (p.completed !== null && p.scope !== null && p.scope !== 0) rows.push(['Done', fmt.pct(p.completed / p.scope)])
        marks.push(pointMark({
          key: `${s.key}:${+p.date}`, cx: X(+p.date), cy: f.py(v), color: s.color, surface: theme.surface, series: s.key, datum: p,
          label: `${dt}: ${s.name.toLowerCase()} ${fmt.value(v)}`, title: dt, rows, show: show || isolated(ys, i), r: s.key === 'scope' && show ? 3 : undefined,
        }))
      })
    }
    if (!hidden.has('notes') && events.length) {
      const ev: TimelineEvent[] = events.map((p) => ({ key: String(+p.date), t: +p.date, text: p.note!, label: `${fmt.dateTime(p.date)}: ${p.note}`, rows: [['Date', fmt.dateTime(p.date)], ['Completed', fmt.value(p.completed)], ['Scope', fmt.value(p.scope)]], datum: p, color: theme.muted, series: 'notes' }))
      marks.push(...eventMarks(ctx, f, ev, projected && !beyond ? LABEL_ROOM : 0))
    }

    const items: Array<{ key: string; label: string; color: string; shape: 'line' | 'dash' }> = [
      { key: 'completed', label: 'Completed', color: cDone, shape: 'line' },
      { key: 'scope', label: 'Scope', color: cScope, shape: 'line' },
    ]
    if (proj?.status === 'projected') items.push({ key: 'projection', label: 'Projection', color: cDone, shape: 'dash' })
    if (events.length) items.push({ key: 'notes', label: 'Notes', color: theme.muted, shape: 'line' })
    const projNote = !o.projection || !proj ? undefined
      : proj.status === 'stalled' ? 'no projection: completed work is not increasing'
      : proj.status === 'insufficient' ? 'no projection: needs at least 2 completed readings'
      : proj.status === 'no-scope' ? 'no projection: no scope values'
      : projected && beyond ? `projected completion ${projectedDay(fmt, projected.date)}, beyond the chart`
      : undefined
    const base = notes([[duplicates, 'duplicate dates merged (last reading kept)'], [nulls, `date${nulls === 1 ? '' : 's'} with a missing value left as gaps`]])
    return {
      marks,
      legend: { items },
      note: [base, projNote].filter(Boolean).join('; ') || undefined,
      ...zoomFields(f, o.zoom),
    }
  },
  describe({ data, options: o, fmt, name }) {
    const { points } = prepareBurnup(data)
    const done = points.filter((p) => p.completed !== null)
    const scopes = points.filter((p) => p.scope !== null)
    const parts = [name, `${phrase.count(points.length, 'date')} ${points.length === 1 ? 'on' : 'from'} ${phrase.dateSpan(points.map((p) => p.date), fmt)}`]
    const lastDone = done[done.length - 1], firstScope = scopes[0], lastScope = scopes[scopes.length - 1]
    if (lastDone && lastScope) parts.push(`${fmt.value(lastDone.completed)} of ${fmt.value(lastScope.scope)} completed${lastScope.scope ? ` (${fmt.pct(lastDone.completed! / lastScope.scope!)})` : ''}`)
    else if (lastDone) parts.push(`${fmt.value(lastDone.completed)} completed`)
    if (firstScope && lastScope && firstScope.scope !== lastScope.scope) parts.push(`scope ${fmt.value(firstScope.scope)} to ${fmt.value(lastScope.scope)}`)
    if (o.projection) {
      const p = projectCompletion(points, o.projectionWindow)
      if (p.status === 'projected') parts.push(`projected to finish ${projectedDay(fmt, p.date)}`)
      else if (p.status === 'done') parts.push(`scope completed ${fmt.dateTime(p.date)}`)
    }
    return parts.join(', ')
  },
  table(data, _o, fmt) {
    const { points } = prepareBurnup(data)
    return { columns: ['Date', 'Completed', 'Scope', 'Note'], rows: points.map((p) => [fmt.dateTime(p.date), p.completed, p.scope, p.note ?? null]) }
  },
})

export default burnup
