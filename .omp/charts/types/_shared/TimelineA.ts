// Shared by the timeline charts (gantt, event-timeline, swimlane-timeline, roadmap,
// milestone-timeline): zone-aware date re-parsing, a yes/no field, the `today` option,
// greedy interval packing into lanes, the lane-column layout, and small geometry helpers.

import { z } from 'zod'
import { parseDate } from '../../time/parse'
import type { DrawContext, LegendItem, Mark, Palette, Paint } from '../../core'
import { SIZES } from '../../core'

type Obj = Record<string, unknown>

/**
 * Re-parse tz-less date strings of `keys` in `timeZone`. core's `fields.date` reads them as
 * UTC, but the base option promises "IANA zone for parsing tz-less dates". Strings with an
 * explicit offset parse to the same instant either way; unparseable values are left alone so
 * the schema reports them.
 */
export function zonedRows(raw: unknown, keys: string[], timeZone: string | undefined): unknown {
  if (!Array.isArray(raw)) return raw
  const tz = timeZone ?? 'UTC'
  return raw.map((r: unknown) => {
    if (!r || typeof r !== 'object' || Array.isArray(r)) return r
    const o: Obj = { ...(r as Obj) }
    for (const k of keys) {
      const v = o[k]
      if (typeof v === 'string') {
        const d = parseDate(v, { timeZone: tz })
        if (d) o[k] = d
      }
    }
    return o
  })
}

const YES = new Set(['true', 'yes', 'y', '1', 'done', 'x', '✓', '✔', 'complete', 'completed'])
const NO = new Set(['false', 'no', 'n', '0', '', '-', 'todo', 'open', 'pending'])
/** A yes/no field: booleans, 1/0, "yes"/"no", "done"; missing = undefined. */
export const flag = (description: string) =>
  z.preprocess((v) => {
    if (v === null || v === undefined) return undefined
    if (typeof v === 'number') return v !== 0
    if (typeof v === 'string') {
      const t = v.trim().toLowerCase()
      if (YES.has(t)) return true
      if (NO.has(t)) return false
    }
    return v
  }, z.boolean({ invalid_type_error: 'yes/no value' }).optional()).describe(description)

/** `today`: a date, 'now', or false (no marker). Parsed in the chart's zone at draw time. */
export const todayOption = z
  .union([
    z.literal(false),
    z.literal('now'),
    z.date(),
    z.number().finite(),
    z.string().refine((s) => parseDate(s) !== null, { message: "must be a date, 'now' or false" }),
  ])
  .default(false)
  .describe("'today' marker: a date, 'now', or false for none")

export type TodayOption = z.output<typeof todayOption>

/** The instant of the `today` option, or null. */
export function resolveToday(v: TodayOption | undefined, timeZone: string): Date | null {
  if (v === undefined || v === false) return null
  if (v === 'now') return new Date(Date.now())
  return parseDate(v, { timeZone })
}

export interface Span {
  start: number
  end: number
}

/**
 * Greedy interval packing. Spans are visited by start (then end); each goes into the first
 * lane whose last end + gap <= its start, else a new lane. Returns the lane of every span
 * (input order) and the lane count. Within a lane no two spans overlap.
 */
export function packLanes(spans: Span[], gap = 0): { lane: number[]; lanes: number } {
  const order = spans.map((_, i) => i).sort((a, b) => spans[a]!.start - spans[b]!.start || spans[a]!.end - spans[b]!.end || a - b)
  const ends: number[] = []
  const lane = new Array<number>(spans.length).fill(0)
  for (const i of order) {
    const s = spans[i]!
    let l = 0
    while (l < ends.length && ends[l]! + gap > s.start) l++
    if (l === ends.length) ends.push(s.end)
    else ends[l] = s.end
    lane[i] = l
  }
  return { lane, lanes: ends.length }
}

/** Pad a time extent by `frac` of its span on both sides (a day when it is a single instant). */
export function paddedDomain(lo: number, hi: number, frac = 0.03): [Date, Date] {
  const span = hi - lo
  const p = span > 0 ? span * frac : 864e5
  return [new Date(lo - p), new Date(hi + p)]
}

/** Closed diamond centred on (cx, cy). */
export function diamond(cx: number, cy: number, r: number): string {
  const f = (n: number) => Math.round(n * 100) / 100
  return `M${f(cx)},${f(cy - r)}L${f(cx + r)},${f(cy)}L${f(cx)},${f(cy + r)}L${f(cx - r)},${f(cy)}Z`
}

/** Text shortened to `max` px, with the full text when it was cut. */
export function fit(ctx: Pick<DrawContext, 'truncate'>, text: string, max: number, size: number = SIZES.tick): { text: string; full?: string } {
  const t = ctx.truncate(text, max, size)
  return t === text ? { text } : { text: t, full: text }
}

/** Categorical colours in first-seen order, and the legend that names them. */
export function categoryColors(values: Array<string | undefined>, theme: Palette): { color: (v: string | undefined) => Paint; index: (v: string | undefined) => number; items: LegendItem[] } {
  const keys: string[] = []
  for (const v of values) if (v !== undefined && !keys.includes(v)) keys.push(v)
  const idx = new Map(keys.map((k, i) => [k, i]))
  return {
    color: (v) => theme.cat(v === undefined ? 0 : idx.get(v) ?? 0),
    index: (v) => (v === undefined ? 0 : idx.get(v) ?? 0),
    items: keys.map((k, i) => ({ key: k, label: k, color: theme.cat(i), shape: 'square' as const, pattern: i })),
  }
}

export interface LaneIn {
  key: string
  label: string
  /** Sub-rows the lane needs (at least 1). */
  rows: number
}
export interface LaneOut extends LaneIn {
  /** Top and height of the lane band. */
  y: number
  h: number
  /** Height of one sub-row. */
  rowH: number
  /** Padding above the first sub-row (and below the last). */
  pad: number
}

/**
 * Stack lanes top to bottom in [top, top + height], each as tall as its sub-rows, with a
 * little padding inside every lane band.
 */
export function layoutLanes(lanes: LaneIn[], top: number, height: number): LaneOut[] {
  const total = lanes.reduce((n, l) => n + Math.max(1, l.rows), 0) || 1
  const pad = Math.min(4, (height / Math.max(1, lanes.length)) * 0.08)
  const rowH = Math.max(0.05, (height - pad * 2 * lanes.length) / total)
  let y = top
  return lanes.map((l) => {
    const h = Math.max(1, l.rows) * rowH + pad * 2
    const out: LaneOut = { ...l, y, h, rowH, pad }
    y += h
    return out
  })
}

/** Lane labels in the left column, alternate-lane shading and separators (decorations). */
export function laneChrome(ctx: Pick<DrawContext, 'truncate' | 'theme'>, lanes: LaneOut[], plot: { x: number; width: number }, labelX: number, labelMax: number): Mark[] {
  const { theme } = ctx
  const marks: Mark[] = []
  lanes.forEach((l, i) => {
    if (i % 2 === 1) marks.push({ type: 'rect', layer: 'back', key: `lane-bg:${l.key}`, x: 0, y: l.y, width: plot.x + plot.width, height: l.h, fill: theme.track, fillOpacity: 0.35, inert: true })
    if (i > 0) marks.push({ type: 'line', layer: 'grid', key: `lane-sep:${l.key}`, x1: 0, x2: plot.x + plot.width, y1: l.y, y2: l.y, stroke: theme.grid, strokeWidth: 1, inert: true })
    if (l.h >= SIZES.tick + 2) {
      const t = fit(ctx, l.label, labelMax, SIZES.tick)
      marks.push({ type: 'text', layer: 'labels', key: `lane-label:${l.key}`, x: labelX, y: l.y + Math.min(l.h / 2, SIZES.tick + 4), text: t.text, full: t.full, className: t.full ? 'oc-trunc' : undefined, anchor: 'start', baseline: 'central', size: SIZES.tick, weight: 600, fill: theme.ink })
    }
  })
  return marks
}

/** Vertical 'today' line with a small label, inside the plot (decorations). */
export function todayMarks(ctx: Pick<DrawContext, 'theme' | 'measure'>, x: number, plot: { x: number; y: number; width: number; height: number }): Mark[] {
  const { theme } = ctx
  const text = 'Today'
  const w = ctx.measure(text, SIZES.tick, 600)
  const right = x + 4 + w <= plot.x + plot.width
  return [
    { type: 'line', layer: 'labels', key: 'today-line', x1: x, x2: x, y1: plot.y, y2: plot.y + plot.height, stroke: theme.highlight, strokeWidth: 1.5, dash: '4 3', inert: true },
    { type: 'text', layer: 'labels', key: 'today-label', x: right ? x + 4 : x - 4, y: plot.y + 2, text, anchor: right ? 'start' : 'end', baseline: 'hanging', size: SIZES.tick, weight: 600, fill: theme.highlight, halo: true, inert: true },
  ]
}
