// Event timeline: point events (dots) and range events (bars) on one time axis. Events
// are packed greedily into lanes by their pixel extent *including the label*, so neither
// marks nor labels in a lane overlap; when the labelled lanes cannot fit, labels drop and
// the marks alone are packed.

import { defineChart, phrase, SIZES } from '../../core'
import type { Formatters, Mark } from '../../core'
import { categoryColors, fit, packLanes, paddedDomain, zonedRows } from '../_shared/TimelineA'
import { input, options, type EventOptions, type EventRow } from './schema'

export interface Placed {
  i: number
  /** Mark extent in px. */
  x0: number
  x1: number
  /** Label box in px (null when unlabelled). */
  label: { x: number; w: number; anchor: 'start' | 'end'; text: string; full?: string } | null
  /** Packing extent: mark plus label. */
  left: number
  right: number
  lane: number
}

/**
 * Place every event: mark extent from the scale, the label inside a wide bar, else after the
 * mark, else before it (whichever stays inside [minX, maxX]); then pack by the union extent.
 */
export function placeEvents(
  spans: Array<{ x0: number; x1: number; point: boolean; text: string }>,
  opts: { r: number; minX: number; maxX: number; gap: number; labelMax: number; measure: (t: string) => number; truncate: (t: string, w: number) => string } | { r: number; gap: number; labels: false },
): { placed: Placed[]; lanes: number } {
  const placed = spans.map((s, i): Placed => {
    const x0 = s.point ? s.x0 - opts.r : s.x0, x1 = s.point ? s.x0 + opts.r : Math.max(s.x0 + 1, s.x1)
    let label: Placed['label'] = null
    if (!('labels' in opts)) {
      const t = opts.truncate(s.text, opts.labelMax)
      const w = opts.measure(t)
      const full = t !== s.text ? s.text : undefined
      if (!s.point && x1 - x0 >= w + 8) label = { x: x0 + 4, w, anchor: 'start', text: t, full }
      else if (x1 + 4 + w <= opts.maxX) label = { x: x1 + 4, w, anchor: 'start', text: t, full }
      else if (x0 - 4 - w >= opts.minX) label = { x: x0 - 4, w, anchor: 'end', text: t, full }
    }
    const lx0 = label ? (label.anchor === 'start' ? label.x : label.x - label.w) : x0
    const lx1 = label ? (label.anchor === 'start' ? label.x + label.w : label.x) : x1
    return { i, x0, x1, label, left: Math.min(x0, lx0), right: Math.max(x1, lx1), lane: 0 }
  })
  const { lane, lanes } = packLanes(placed.map((p) => ({ start: p.left, end: p.right })), opts.gap)
  placed.forEach((p, i) => (p.lane = lane[i]!))
  return { placed, lanes }
}

/**
 * Dense mode: lanes are already packed by mark extent alone. Give a label to every event
 * whose label fits after (else before) its mark without touching anything else in its lane.
 */
export function fillLabels(placed: Placed[], texts: string[], opts: { minX: number; maxX: number; gap: number; labelMax: number; measure: (t: string) => number; truncate: (t: string, w: number) => string }): void {
  const byLane = new Map<number, Array<{ owner: number; a: number; b: number }>>()
  for (const p of placed) byLane.set(p.lane, [...(byLane.get(p.lane) ?? []), { owner: p.i, a: p.x0, b: p.x1 }])
  const free = (p: Placed, a: number, b: number) => byLane.get(p.lane)!.every((o) => o.owner === p.i || b + opts.gap <= o.a || a >= o.b + opts.gap)
  for (const p of [...placed].sort((a, b) => a.x0 - b.x0)) {
    const text = texts[p.i]!
    const t = opts.truncate(text, opts.labelMax)
    const w = opts.measure(t)
    const full = t !== text ? text : undefined
    if (p.x1 + 4 + w <= opts.maxX && free(p, p.x1 + 4, p.x1 + 4 + w)) p.label = { x: p.x1 + 4, w, anchor: 'start', text: t, full }
    else if (p.x0 - 4 - w >= opts.minX && free(p, p.x0 - 4 - w, p.x0 - 4)) p.label = { x: p.x0 - 4, w, anchor: 'end', text: t, full }
    else continue
    const box = p.label.anchor === 'start' ? { a: p.label.x, b: p.label.x + w } : { a: p.label.x - w, b: p.label.x }
    byLane.get(p.lane)!.push({ owner: -1, ...box })
  }
}

function eventLabel(e: EventRow, fmt: Formatters): string {
  return e.end && +e.end !== +e.date ? `${e.label} (${fmt.dateTime(e.date)} – ${fmt.dateTime(e.end)})` : `${e.label}: ${fmt.dateTime(e.date)}`
}

const isRange = (e: EventRow) => !!e.end && +e.end > +e.date

export const eventTimeline = defineChart<EventRow[], EventOptions>({
  id: 'event-timeline',
  name: 'Event timeline',
  category: 'timeline',
  intents: ['timeline', 'schedule'],
  summary: 'Point and range events on one time axis, packed into lanes so nothing overlaps.',
  input,
  options,
  shape: 'rows',
  fields: ['date', 'end', 'label', 'category'],
  dateFields: ['date', 'end'],
  zoom: 'x',
  aspect: 0.4,
  minHeight: 240,
  maxHeight: 440,
  adapt: (raw, o) => zonedRows(raw, ['date', 'end'], o.timeZone),
  draw(ctx) {
    const { theme, options: o, fmt, width: W } = ctx
    const FS = SIZES.tick
    const colors = categoryColors(ctx.data.map((e) => e.category), theme)
    const multi = colors.items.length > 1
    const all = [...ctx.data].sort((a, b) => +a.date - +b.date || +(a.end ?? a.date) - +(b.end ?? b.date))
    const events = all.filter((e) => !(e.category !== undefined && ctx.hidden.has(e.category)))
    const lo = Math.min(...all.map((e) => +e.date)), hi = Math.max(...all.map((e) => +(e.end ?? e.date)))
    const f = ctx.cartesian({ x: { type: 'time', domain: paddedDomain(lo, hi, 0.04) }, y: { type: 'linear', domain: [0, 1] }, yAxis: 'none', grid: 'x', zoom: true })
    const { plot } = f
    const marks: Mark[] = [...f.marks]

    const visible = events.filter((e) => f.px(e.end ?? e.date) >= plot.x - 1 && f.px(e.date) <= plot.x + plot.width + 1)
    const spans = visible.map((e) => ({ x0: Math.max(plot.x, f.px(e.date)), x1: Math.min(plot.x + plot.width, f.px(e.end ?? e.date)), point: !isRange(e), text: e.label }))
    const labelH = FS + 8
    let r = 4
    let laid = placeEvents(spans, { r, gap: 2, labels: false })
    let labelled = false
    if (o.labels) {
      // full labels first, then shorter truncations, before giving labels up
      for (const max of [o.maxLabelWidth, 100, 64]) {
        if (max > o.maxLabelWidth) continue
        const withLabels = placeEvents(spans, { r, minX: 2, maxX: W - 2, gap: 8, labelMax: Math.min(max, plot.width * 0.6), measure: (t) => ctx.measure(t, FS), truncate: (t, w) => ctx.truncate(t, w, FS) })
        if (withLabels.lanes * labelH <= plot.height) { laid = withLabels; labelled = true; break }
      }
    }
    const laneH = Math.min(labelled ? 44 : 24, plot.height / Math.max(1, laid.lanes))
    let unlabelled = 0
    if (!labelled) {
      r = Math.max(1, Math.min(4, laneH * 0.4))
      laid = placeEvents(spans, { r, gap: 1, labels: false })
      // dense: keep the marks packed, label whichever events have room in their own lane
      if (o.labels && laneH >= FS + 4) fillLabels(laid.placed, spans.map((s) => s.text), { minX: 2, maxX: W - 2, gap: 6, labelMax: Math.min(o.maxLabelWidth, 100), measure: (t) => ctx.measure(t, FS), truncate: (t, w) => ctx.truncate(t, w, FS) })
      if (o.labels) unlabelled = laid.placed.filter((p) => !p.label).length
    }
    const barH = Math.max(0.5, Math.min(14, laneH * 0.6))
    // lane 0 sits on the axis; later lanes stack upward
    const yOf = (lane: number) => plot.y + plot.height - (lane + 0.5) * laneH

    for (const p of laid.placed) {
      const e = visible[p.i]!
      const c = multi ? colors.color(e.category) : theme.cat(0)
      const pat = multi ? colors.index(e.category) : undefined
      const y = yOf(p.lane)
      const key = `${+e.date}:${e.label}:${p.i}`
      const tip = { title: e.label, color: c, rows: [[isRange(e) ? 'From' : 'Date', fmt.dateTime(e.date)], ...(isRange(e) ? [['To', fmt.dateTime(e.end)]] : []), ...(e.category !== undefined ? [['Category', e.category]] : [])] as Array<[string, string]> }
      const series = multi ? e.category : undefined
      if (isRange(e)) marks.push({ type: 'rect', role: 'range', key, x: p.x0, y: y - barH / 2, width: p.x1 - p.x0, height: barH, rx: Math.min(3, barH / 3), fill: theme.mix(c, theme.surface, 0.55), stroke: c, strokeWidth: barH >= 4 ? 1 : 0, pattern: pat, series, datum: e, label: eventLabel(e, fmt), tooltip: tip })
      else marks.push({ type: 'circle', role: 'point', key, cx: (p.x0 + p.x1) / 2, cy: y, r, fill: c, stroke: theme.surface, strokeWidth: r >= 3 ? 1 : 0, series, datum: e, label: eventLabel(e, fmt), tooltip: tip })
      if (p.label) marks.push({ type: 'text', layer: 'labels', key: `l:${key}`, x: p.label.x, y, text: p.label.text, full: p.label.full, className: p.label.full ? 'oc-trunc' : undefined, anchor: p.label.anchor, baseline: 'central', size: FS, fill: theme.ink })
    }
    const note = unlabelled ? `${unlabelled} labels hidden where events crowd (hover for details)` : undefined
    return {
      marks, plot, clip: plot, invertX: f.invertX, note,
      legend: multi ? { items: colors.items } : undefined,
    }
  },
  describe({ data, fmt, name }) {
    const ranges = data.filter(isRange).length
    const cats = new Set(data.map((e) => e.category).filter((c) => c !== undefined)).size
    const parts = [`${phrase.count(data.length, 'event')}${ranges ? ` (${phrase.count(ranges, 'range')})` : ''}${cats > 1 ? ` in ${phrase.count(cats, 'category', 'categories')}` : ''}`, phrase.dateSpan(data.flatMap((e) => (e.end ? [e.date, e.end] : [e.date])), fmt)]
    const first = data.reduce<EventRow | null>((a, e) => (!a || +e.date < +a.date ? e : a), null)
    const last = data.reduce<EventRow | null>((a, e) => (!a || +e.date >= +a.date ? e : a), null)
    if (first && last && first !== last) parts.push(`from ${first.label} to ${last.label}`)
    return `${name}, ${parts.join(', ')}`
  },
  table(data) {
    const rows = [...data].sort((a, b) => +a.date - +b.date)
    return { columns: ['Date', 'End', 'Label', 'Category'], rows: rows.map((e) => [e.date.toISOString(), e.end ? e.end.toISOString() : null, e.label, e.category ?? null]) }
  },
})

export default eventTimeline
