// Shared by the timeline charts burndown, burnup, cumulative-flow and control-chart:
// date ordering, line/area paths with gaps, point marks that stay hoverable when the
// series is too dense to draw dots, dated event flags along the top of the plot, and
// right-margin line labels that never collide.

import { area, curveLinear, curveStepAfter, line, type CurveFactory } from 'd3-shape'
import type { CartesianFrame, CircleMark, DrawContext, Mark, Paint, PathMark, Rect } from '../../core'
import { SIZES } from '../../core'

export { curveLinear, curveStepAfter }

/** Stable sort by a date (or number) accessor. */
export function sortBy<T>(rows: readonly T[], key: (r: T) => number): T[] {
  return rows.map((r, i) => [r, i] as const).sort((a, b) => key(a[0]) - key(b[0]) || a[1] - b[1]).map((x) => x[0])
}

export interface XY { x: number; y: number | null }

/** Line through the points; a null y breaks the line. */
export function linePath(pts: XY[], curve: CurveFactory = curveLinear): string {
  return line<XY>().defined((p) => p.y !== null && Number.isFinite(p.y) && Number.isFinite(p.x)).x((p) => p.x).y((p) => p.y ?? 0).curve(curve)(pts) ?? ''
}

export interface Band { x: number; y0: number; y1: number }

/** Filled band between y0 and y1. */
export function bandPath(pts: Band[], curve: CurveFactory = curveLinear): string {
  return area<Band>().x((p) => p.x).y0((p) => p.y0).y1((p) => p.y1).curve(curve)(pts) ?? ''
}

/** Area from a baseline up to each point; a null y breaks it. */
export function areaToBase(pts: XY[], base: number, curve: CurveFactory = curveLinear): string {
  return area<XY>().defined((p) => p.y !== null && Number.isFinite(p.y)).x((p) => p.x).y0(base).y1((p) => p.y ?? base).curve(curve)(pts) ?? ''
}

/** Dots are drawn while there is room for them (>= 7px per point); otherwise points are invisible hit targets. */
export function dotsVisible(n: number, plotWidth: number): boolean {
  return n <= 1 || plotWidth / n >= 7
}

/** A point with no non-null neighbour draws no line segment, so it always shows its dot. */
export function isolated(ys: ReadonlyArray<number | null>, i: number): boolean {
  return (i === 0 || ys[i - 1] === null) && (i === ys.length - 1 || ys[i + 1] === null)
}

export interface PointSpec {
  key: string
  cx: number
  cy: number
  color: Paint
  surface: Paint
  series?: string
  role?: string
  datum: unknown
  label: string
  title: string
  rows: Array<[string, string]>
  show: boolean
  r?: number
}

/** A data point: a dot, or an invisible (fill-opacity 0) hit target when the series is dense. */
export function pointMark(p: PointSpec): CircleMark {
  return {
    type: 'circle', role: p.role ?? 'point', key: p.key, cx: p.cx, cy: p.cy, r: p.r ?? (p.show ? 3.5 : 3),
    fill: p.color, stroke: p.surface, strokeWidth: p.show ? 1.5 : 0, fillOpacity: p.show ? 1 : 0, strokeOpacity: p.show ? 1 : 0,
    series: p.series, datum: p.datum, label: p.label, tooltip: { title: p.title, rows: p.rows, color: p.color },
  }
}

/** Diamond path centred on (cx, cy) with half-diagonal s. */
export function diamond(cx: number, cy: number, s: number): string {
  return `M${cx},${cy - s}L${cx + s},${cy}L${cx},${cy + s}L${cx - s},${cy}Z`
}

/** Downward triangle with its tip at (cx, cy + h). */
export function flag(cx: number, top: number, h: number): string {
  return `M${cx - h * 0.75},${top}L${cx + h * 0.75},${top}L${cx},${top + h}Z`
}

/** Extra top padding the event flags need. */
export const EVENT_PAD = SIZES.tick + 10

export interface TimelineEvent {
  key: string
  t: number
  text: string
  label: string
  rows: Array<[string, string]>
  datum: unknown
  color: Paint
  series?: string
}

/**
 * Dated events (scope changes, milestones): a dashed rule through the plot, a flag at the
 * top (the data mark) and a label beside it, truncated to the room before the next flag.
 */
export function eventMarks(ctx: Pick<DrawContext, 'theme' | 'truncate' | 'measure'>, f: CartesianFrame, events: TimelineEvent[], lift = 0): Mark[] {
  const { theme } = ctx
  const plot = f.plot
  const out: Mark[] = []
  const xs = events.map((e) => f.px(new Date(e.t)))
  const fh = 7
  const top = plot.y - lift - EVENT_PAD + 2
  events.forEach((e, i) => {
    const x = xs[i]!
    if (!Number.isFinite(x) || x < plot.x - 0.5 || x > plot.x + plot.width + 0.5) return
    out.push({ type: 'line', layer: 'back', key: `ev-rule:${e.key}`, x1: x, x2: x, y1: top + fh, y2: plot.y + plot.height, stroke: e.color, strokeWidth: 1, dash: '2 3', strokeOpacity: 0.7, series: e.series, inert: true })
    const fl: PathMark = {
      type: 'path', role: 'event', layer: 'labels', key: `ev:${e.key}`, d: flag(x, top, fh), cx: x, cy: top + fh / 2, box: { x: x - 7, y: top - 2, width: 14, height: fh + 4 },
      fill: e.color, stroke: e.color, strokeWidth: 1, series: e.series, datum: e.datum, label: e.label, tooltip: { title: e.text, rows: e.rows, color: e.color },
    }
    out.push(fl)
    // label to the right of the flag, up to the next visible flag (or the plot edge)
    let room = plot.x + plot.width - (x + 8)
    for (let j = i + 1; j < xs.length; j++) if (Number.isFinite(xs[j]!) && xs[j]! > x) { room = Math.min(room, xs[j]! - x - 16); break }
    let anchor: 'start' | 'end' = 'start'
    let lx = x + 7
    if (room < 36 && i === events.length - 1) {
      // last flag hard against the right edge: label to its left instead
      room = Math.min(x - 7 - plot.x, (i > 0 && Number.isFinite(xs[i - 1]!) ? x - xs[i - 1]! - 16 : Infinity))
      anchor = 'end'
      lx = x - 7
    }
    if (room < 24) return
    const t = ctx.truncate(e.text, room)
    if (!t || t === '…') return
    out.push({ type: 'text', layer: 'labels', key: `ev-l:${e.key}`, x: lx, y: top + fh / 2, text: t, full: t !== e.text ? e.text : undefined, className: t !== e.text ? 'oc-trunc' : undefined, anchor, baseline: 'central', size: SIZES.tick, fill: theme.inkSecondary, series: e.series })
  })
  return out
}

export interface RuleLabel { key: string; text: string; y: number; color: Paint }

/** Labels in the right margin for horizontal rules (UCL, mean, LCL), nudged apart so they never overlap. */
export function ruleLabels(items: RuleLabel[], plot: Rect, height: number): Mark[] {
  const h = SIZES.tick + 2
  const sorted = [...items].sort((a, b) => a.y - b.y).map((it) => ({ ...it, ly: it.y }))
  const lo = h / 2 + 1, hi = height - h / 2 - 1
  for (let i = 0; i < sorted.length; i++) {
    const s = sorted[i]!
    s.ly = Math.max(lo, i > 0 ? Math.max(s.ly, sorted[i - 1]!.ly + h) : s.ly)
  }
  for (let i = sorted.length - 1; i >= 0; i--) {
    const s = sorted[i]!
    s.ly = Math.min(s.ly, i < sorted.length - 1 ? sorted[i + 1]!.ly - h : hi)
  }
  return sorted.map((s) => ({ type: 'text', layer: 'labels', key: `rl:${s.key}`, x: plot.x + plot.width + 6, y: s.ly, text: s.text, anchor: 'start', baseline: 'central', size: SIZES.tick, fill: s.color, inert: true }))
}

/** True when x lies within the plot horizontally. */
export function inPlotX(x: number, plot: Rect): boolean {
  return x >= plot.x - 0.5 && x <= plot.x + plot.width + 0.5
}

const DAY = 864e5
/** "12 days" / "3 weeks" style length of a span in ms. */
export function spanLength(ms: number): string {
  const d = Math.round(ms / DAY)
  if (d < 1) { const h = Math.max(1, Math.round(ms / 36e5)); return `${h} hour${h === 1 ? '' : 's'}` }
  if (d < 21) return `${d} day${d === 1 ? '' : 's'}`
  const w = Math.round(d / 7)
  if (d < 90) return `${w} weeks`
  const m = Math.round(d / 30.44)
  return `${m} months`
}

/**
 * Widen an x extent by ~a half tick label on each side, so dots are not cut by the axis and the
 * first date label does not run into the lowest value label at the plot's corner. A single
 * instant becomes +-`unit` around it (a day for dates, 1 for indices).
 */
export function padExtent(t0: number, t1: number, width: number, measure: (t: string) => number, unit = 864e5): [number, number] {
  if (t1 <= t0) [t0, t1] = [t0 - unit, t0 + unit]
  const px = measure('Mmm 00') / 2 + 2
  const k = px / Math.max(40, width - 80)
  return [t0 - (t1 - t0) * k, t1 + (t1 - t0) * k]
}

/**
 * Time-axis tick count: 3 for a single date (day ticks, not hours), 4 on narrow charts, where
 * core's default drops to a single month label for a few weeks of data (core thins any that
 * would collide); otherwise core's default.
 */
export function timeTickCount(n: number, width: number): number | undefined {
  return n === 1 ? 3 : width < 480 ? 4 : undefined
}

/** Zoom result fields for a time-x frame. */
export function zoomFields(f: CartesianFrame, zoom: boolean | undefined): { plot: Rect; clip?: Rect; invertX: (px: number) => unknown } {
  return { plot: f.plot, clip: zoom ? f.plot : undefined, invertX: (p) => f.invertX(p) }
}
