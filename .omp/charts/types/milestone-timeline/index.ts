// Milestone timeline: diamonds on one horizontal line, names alternating above and below
// on stems. A label that would collide climbs to the next tier on its side, then tries the
// other side, and is only dropped (tooltip keeps it) when no tier has room. Done milestones
// are filled, upcoming ones hollow, overdue ones (upcoming and before `today`) outlined in
// the negative colour.

import { defineChart, phrase, SIZES } from '../../core'
import type { Formatters, LegendItem, Mark } from '../../core'
import { diamond, fit, paddedDomain, resolveToday, todayMarks, zonedRows } from '../_shared/TimelineA'
import { input, options, type MilestoneOptions, type MilestoneRow } from './schema'

export interface Slot {
  /** -1 above the line, 1 below. */
  side: -1 | 1
  tier: number
  /** Left edge of the label box (clamped inside the bounds). */
  left: number
}

/**
 * Place label boxes (centred on x, width w) left to right: alternate sides, climb tiers on
 * the preferred side, then the other side; null when no tier has room. Boxes sharing a side
 * and tier never overlap (gap between them).
 */
export function placeLabels(items: Array<{ x: number; w: number }>, opts: { minX: number; maxX: number; tiers: number; gap: number }): Array<Slot | null> {
  const ends: Record<string, number> = {}
  let next: -1 | 1 = -1
  return items.map(({ x, w }) => {
    const left = Math.max(opts.minX, Math.min(opts.maxX - w, x - w / 2))
    for (const side of [next, -next as -1 | 1]) {
      for (let tier = 0; tier < opts.tiers; tier++) {
        const k = `${side}:${tier}`
        if ((ends[k] ?? -Infinity) + opts.gap <= left) {
          ends[k] = left + w
          next = -side as -1 | 1
          return { side, tier, left }
        }
      }
    }
    return null
  })
}

type State = 'done' | 'upcoming' | 'overdue'
const stateOf = (m: MilestoneRow, today: Date | null): State => (m.done ? 'done' : today && +m.date < +today ? 'overdue' : 'upcoming')
const STATE_LABEL: Record<State, string> = { done: 'Done', upcoming: 'Upcoming', overdue: 'Overdue' }

function milestoneLabel(m: MilestoneRow, s: State, fmt: Formatters): string {
  return `${m.label}: ${fmt.dateTime(m.date)}, ${STATE_LABEL[s].toLowerCase()}`
}

export const milestoneTimeline = defineChart<MilestoneRow[], MilestoneOptions>({
  id: 'milestone-timeline',
  name: 'Milestone timeline',
  category: 'timeline',
  intents: ['timeline', 'schedule'],
  summary: 'Milestones as diamonds on one line, labels alternating above and below; done vs upcoming.',
  input,
  options,
  shape: 'rows',
  fields: ['date', 'label', 'done'],
  dateFields: ['date'],
  zoom: 'x',
  aspect: 0.4,
  minHeight: 280,
  maxHeight: 420,
  adapt: (raw, o) => zonedRows(raw, ['date'], o.timeZone),
  draw(ctx) {
    const { theme, options: o, fmt, width: W } = ctx
    const FS = SIZES.tick
    const today = resolveToday(o.today, fmt.timeZone)
    const all = [...ctx.data].sort((a, b) => +a.date - +b.date)
    const states = all.map((m) => stateOf(m, today))
    const present = (['done', 'upcoming', 'overdue'] as State[]).filter((s) => states.includes(s))
    const legendOn = present.length > 1
    const shown = all.map((m, i) => ({ m, s: states[i]! })).filter((d) => !(legendOn && ctx.hidden.has(d.s)))
    let lo = +all[0]!.date, hi = +all[all.length - 1]!.date
    if (today && +today >= lo - (hi - lo) * 0.25 && +today <= hi + (hi - lo) * 0.25) { lo = Math.min(lo, +today); hi = Math.max(hi, +today) }
    const f = ctx.cartesian({ x: { type: 'time', domain: paddedDomain(lo, hi, 0.05) }, y: { type: 'linear', domain: [0, 1] }, yAxis: 'none', grid: 'x', zoom: true })
    const { plot } = f
    const marks: Mark[] = [...f.marks]
    const lineY = plot.y + plot.height / 2
    const inX = (x: number) => x >= plot.x - 0.5 && x <= plot.x + plot.width + 0.5
    const visible = shown.filter((d) => inX(f.px(d.m.date)))

    const r = Math.max(2.5, Math.min(7, (plot.width / Math.max(1, visible.length)) * 0.35))
    const accent = theme.cat(0)
    const paint: Record<State, { fill: string; stroke: string }> = {
      done: { fill: theme.positive, stroke: theme.positive },
      upcoming: { fill: theme.surface, stroke: accent },
      overdue: { fill: theme.surface, stroke: theme.negative },
    }
    marks.push({ type: 'line', layer: 'grid', key: 'spine', x1: plot.x, x2: plot.x + plot.width, y1: lineY, y2: lineY, stroke: theme.axis, strokeWidth: 2, linecap: 'round', inert: true })
    // the stretch already behind us (to today, else to the last done milestone)
    const lastDone = [...all].reverse().find((m) => m.done)
    const reached = today ?? lastDone?.date
    if (reached) {
      const xr = Math.min(plot.x + plot.width, Math.max(plot.x, f.px(reached)))
      if (xr > plot.x) marks.push({ type: 'line', layer: 'grid', key: 'spine-done', x1: plot.x, x2: xr, y1: lineY, y2: lineY, stroke: theme.positive, strokeWidth: 3, linecap: 'round', inert: true })
    }

    // labels: two lines (name, date), in tiers above and below the line
    const span = hi - lo
    const dateText = (d: Date) => (span < 300 * 864e5 ? fmt.date(d, 'day') : fmt.dateTime(d))
    const lineH = FS + 3
    const boxH = o.showDates ? lineH * 2 : lineH
    const stem = Math.max(r + 6, 12)
    const tierH = boxH + 6
    const tiers = Math.max(0, Math.floor((plot.height / 2 - stem) / tierH))
    const texts = visible.map((d) => {
      const name = fit(ctx, d.m.label, Math.min(o.maxLabelWidth, plot.width * 0.45), FS)
      const date = dateText(d.m.date)
      return { name, date, w: Math.max(ctx.measure(name.text, FS, 600), o.showDates ? ctx.measure(date, FS) : 0) }
    })
    const slots = tiers > 0 ? placeLabels(visible.map((d, i) => ({ x: f.px(d.m.date), w: texts[i]!.w })), { minX: 2, maxX: W - 2, tiers, gap: 10 }) : visible.map(() => null)

    visible.forEach((d, i) => {
      const x = f.px(d.m.date)
      const p = paint[d.s]
      const slot = slots[i]
      const t = texts[i]!
      if (slot) {
        const near = lineY + slot.side * (stem + slot.tier * tierH)
        const top = slot.side === 1 ? near : near - boxH
        marks.push({ type: 'line', layer: 'grid', key: `stem:${i}:${+d.m.date}`, x1: x, x2: x, y1: lineY + slot.side * r, y2: near, stroke: theme.grid, strokeWidth: 1, inert: true })
        const cx = slot.left + t.w / 2
        marks.push({ type: 'text', layer: 'labels', key: `n:${i}:${+d.m.date}`, x: cx, y: top + lineH / 2, text: t.name.text, full: t.name.full, className: t.name.full ? 'oc-trunc' : undefined, anchor: 'middle', baseline: 'central', size: FS, weight: 600, fill: d.s === 'done' ? theme.inkSecondary : theme.ink })
        if (o.showDates) marks.push({ type: 'text', layer: 'labels', key: `d:${i}:${+d.m.date}`, x: cx, y: top + lineH * 1.5, text: t.date, anchor: 'middle', baseline: 'central', size: FS, fill: d.s === 'overdue' ? theme.negative : theme.muted, inert: true })
      }
      marks.push({
        type: 'path', role: 'milestone', key: `${i}:${+d.m.date}:${d.m.label}`, d: diamond(x, lineY, r), cx: x, cy: lineY, box: { x: x - r, y: lineY - r, width: 2 * r, height: 2 * r },
        fill: p.fill, stroke: p.stroke, strokeWidth: 2, series: legendOn ? d.s : undefined, datum: d.m, label: milestoneLabel(d.m, d.s, fmt),
        tooltip: { title: d.m.label, color: p.stroke, rows: [['Date', fmt.dateTime(d.m.date)], ['Status', STATE_LABEL[d.s]]] },
      })
    })
    if (today) {
      const x = f.px(today)
      if (inX(x)) marks.push(...todayMarks(ctx, x, plot))
    }
    const hidden = slots.filter((s) => s === null).length
    const notes: string[] = []
    if (hidden) notes.push(`${hidden} labels hidden where milestones crowd (hover for details)`)
    if (today && !marks.some((m) => m.key === 'today-line')) notes.push(`today (${fmt.date(today, 'day')}) is outside the timeline`)
    const legend: LegendItem[] = present.map((s) => ({ key: s, label: STATE_LABEL[s], color: paint[s].stroke, shape: 'square' }))
    return { marks, plot, clip: { x: plot.x - r - 2, y: plot.y, width: plot.width + 2 * r + 4, height: plot.height }, invertX: f.invertX, note: notes.length ? notes.join('; ') : undefined, legend: legendOn ? { items: legend } : undefined }
  },
  describe({ data, options: o, fmt, name }) {
    const sorted = [...data].sort((a, b) => +a.date - +b.date)
    const done = sorted.filter((m) => m.done).length
    const next = sorted.find((m) => !m.done)
    const parts = [phrase.count(sorted.length, 'milestone'), phrase.dateSpan(sorted.map((m) => m.date), fmt), `${done} done`]
    if (next) parts.push(`next ${next.label} on ${fmt.date(next.date, 'day')}`)
    const today = resolveToday(o.today, fmt.timeZone)
    const overdue = today ? sorted.filter((m) => stateOf(m, today) === 'overdue').length : 0
    if (overdue) parts.push(`${overdue} overdue`)
    return `${name}, ${parts.join(', ')}`
  },
  table(data) {
    return { columns: ['Date', 'Milestone', 'Done'], rows: [...data].sort((a, b) => +a.date - +b.date).map((m) => [m.date.toISOString(), m.label, m.done ? 'yes' : 'no']) }
  },
})

export default milestoneTimeline
