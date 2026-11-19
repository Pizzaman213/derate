// Shared plumbing for the trend charts (line, multi-line, step-line, area, stacked-area,
// stacked-area-100, streamgraph, range-area): the x field (dates, or plain numbers),
// series grouping with sorted x and summed duplicates, gap handling, point marks and
// the "<Name>, <what>, <span>, peak <v> in <when>" summary.

import { z } from 'zod'
import { area as d3area, curveLinear, curveMonotoneX, curveStep, curveStepAfter, curveStepBefore, line as d3line, type CurveFactory } from 'd3-shape'
import { phrase, SIZES } from '../../core'
import { finiteExtent, notes } from '../../core/helpers'
import type { CartesianFrame, ScaleSpec } from '../../core/cartesian'
import type { Formatters } from '../../core/format'
import type { CircleMark, DrawContext, DrawResult, Mark, Paint, Rect, TableModel } from '../../core/types'
import { parseDate } from '../../time/parse'
import { stackRows } from '../../adapters'

// ----- schema pieces

const toX = (v: unknown): unknown => {
  if (v instanceof Date || typeof v === 'number') return v
  if (typeof v !== 'string') return v
  const s = v.trim()
  if (s === '') return undefined
  const d = parseDate(s)
  if (d) return d
  const n = Number(s.replace(/(?<=\d),(?=\d{3}\b)/g, ''))
  return Number.isFinite(n) ? n : v
}

/** The x of a trend row: a date (ISO, epoch, written forms) or a plain number. */
export const xField = (description = 'date (or a number) on the x axis') =>
  z.preprocess(toX, z.unknown().superRefine((v, c) => {
    const ok = (v instanceof Date && !Number.isNaN(+v)) || (typeof v === 'number' && Number.isFinite(v))
    if (!ok) c.addIssue({ code: 'invalid_type', expected: z.ZodParsedType.date, received: v === undefined ? z.ZodParsedType.undefined : v === null ? z.ZodParsedType.null : z.ZodParsedType.string, message: 'date' })
  }).transform((v) => v as Date | number)).describe(description)

export const gapsOption = (def: 'gap' | 'zero' | 'interpolate') =>
  z.enum(['gap', 'zero', 'interpolate']).default(def).describe("missing values: 'gap' lifts the pen, 'zero' draws 0, 'interpolate' joins the neighbours")
export const xTypeOption = z.enum(['auto', 'time', 'number']).default('auto').describe("x axis: dates, plain numbers, or 'auto' (numbers stay numbers unless they look like epoch ms)")
export const pointsOption = z.enum(['auto', 'always', 'never']).default('auto').describe("point dots: 'auto' shows them while they are sparse; hover and keyboard work either way")

export type Gaps = 'gap' | 'zero' | 'interpolate'
export type XType = 'auto' | 'time' | 'number'
export type XValue = Date | number

// ----- the x axis

export interface XModel {
  time: boolean
  /** Numeric position: epoch ms or the number itself. */
  num(v: XValue): number
  /** Back to the user-facing value. */
  value(t: number): XValue
  spec(ts: number[]): ScaleSpec
  /** Tooltip / label text of one x. */
  label(t: number): string
  /** Coarser text for summaries. */
  short(t: number, span: number): string
  /** Table cell. */
  cell(t: number): string | number
  /** Table column name. */
  column: string
}

const SPAN_G = (span: number) => (span > 3 * 365 * 864e5 ? 'year' : span > 60 * 864e5 ? 'month' : span > 2 * 864e5 ? 'day' : 'minute') as 'year' | 'month' | 'day' | 'minute'

/** Decide time vs number and build the x formatters. */
export function xModel(xs: XValue[], xType: XType, fmt: Formatters): XModel {
  const time = xType === 'time' ? true : xType === 'number' ? false
    : xs.some((v) => v instanceof Date) || (xs.length > 0 && xs.every((v) => typeof v === 'number' && Math.abs(v) >= 1e11))
  const num = (v: XValue) => (v instanceof Date ? +v : v)
  if (!time) {
    return {
      time, num, column: 'X',
      value: (t) => t,
      spec: (ts) => { const e = finiteExtent(ts) ?? [0, 1]; return { type: 'linear', domain: e, zero: false, nice: false } },
      label: (t) => fmt.num(t),
      short: (t) => fmt.num(t),
      cell: (t) => t,
    }
  }
  const sorted = [...new Set(xs.map(num))].sort((a, b) => a - b)
  const gaps: number[] = []
  for (let i = 1; i < sorted.length; i++) gaps.push(sorted[i]! - sorted[i - 1]!)
  gaps.sort((a, b) => a - b)
  const med = gaps.length ? gaps[gaps.length >> 1]! : 864e5
  const hasTime = sorted.some((t) => fmt.date(new Date(t), 'minute') !== fmt.date(new Date(sorted[0]!), 'minute') && med < 864e5)
  const g = med >= 360 * 864e5 ? 'year' : med >= 85 * 864e5 ? 'quarter' : med >= 27 * 864e5 ? 'month' : med >= 864e5 ? 'day' : hasTime ? 'minute' : 'day'
  return {
    time, num, column: 'Date',
    value: (t) => new Date(t),
    spec: (ts) => { const e = finiteExtent(ts) ?? [0, 864e5]; return { type: 'time', domain: [new Date(e[0]), new Date(e[1])] } },
    label: (t) => (g === 'minute' ? fmt.dateTime(new Date(t)) : fmt.date(new Date(t), g)),
    short: (t, span) => fmt.date(new Date(t), SPAN_G(span) === 'minute' ? (g === 'minute' ? 'minute' : 'day') : SPAN_G(span)),
    cell: (t) => new Date(t).toISOString(),
  }
}

/** "Jan 2026 to Dec 2026" or "x 1 to 50". */
export function spanPhrase(x: XModel, ts: number[], fmt: Formatters): string {
  const e = finiteExtent(ts)
  if (!e) return 'no data'
  if (x.time) return phrase.dateSpan([new Date(e[0]), new Date(e[1])], fmt)
  return e[0] === e[1] ? `x ${fmt.num(e[0])}` : `x ${fmt.num(e[0])} to ${fmt.num(e[1])}`
}

// ----- series

export interface Pt<R> { t: number; y: number | null; row: R }
export interface Series<R> { name: string; index: number; points: Pt<R>[] }

/** Group rows into series (first-seen order), sort each by x, sum duplicate x's. */
export function groupSeries<R>(rows: R[], x: XModel, getX: (r: R) => XValue, getSeries: (r: R) => string, getY: (r: R) => number | null): { series: Series<R>[]; duplicates: number } {
  const map = new Map<string, Map<number, Pt<R>>>()
  let duplicates = 0
  for (const r of rows) {
    const s = getSeries(r)
    let m = map.get(s)
    if (!m) map.set(s, (m = new Map()))
    const t = x.num(getX(r))
    const y = getY(r)
    const hit = m.get(t)
    if (!hit) m.set(t, { t, y, row: r })
    else {
      duplicates++
      hit.y = hit.y === null ? y : y === null ? hit.y : hit.y + y
    }
  }
  const series = [...map.entries()].map(([name, m], index) => ({ name, index, points: [...m.values()].sort((a, b) => a.t - b.t) }))
  return { series, duplicates }
}

/** Apply the gap policy; returns the new y's and how many were missing. */
export function applyGaps(ts: number[], ys: Array<number | null>, mode: Gaps): { ys: Array<number | null>; missing: number } {
  const missing = ys.filter((v) => v === null).length
  if (mode === 'gap' || !missing) return { ys, missing }
  if (mode === 'zero') return { ys: ys.map((v) => v ?? 0), missing }
  const out = [...ys]
  let prev = -1
  for (let i = 0; i < ys.length; i++) {
    if (ys[i] === null) continue
    if (prev >= 0 && i - prev > 1) {
      const a = ys[prev]!, b = ys[i]!, ta = ts[prev]!, tb = ts[i]!
      for (let j = prev + 1; j < i; j++) out[j] = tb === ta ? a : a + ((b - a) * (ts[j]! - ta)) / (tb - ta)
    }
    prev = i
  }
  return { ys: out, missing }
}

export function gapNote(missing: number, mode: Gaps): [number, string] {
  return [missing, mode === 'gap' ? 'missing values left as gaps' : mode === 'zero' ? 'missing values drawn as zero' : 'missing values interpolated (or left as gaps at the ends)']
}

// ----- geometry

export type CurveName = 'linear' | 'monotone' | 'step-after' | 'step-before' | 'step-center'
export const CURVES: Record<CurveName, CurveFactory> = { linear: curveLinear, monotone: curveMonotoneX, 'step-after': curveStepAfter, 'step-before': curveStepBefore, 'step-center': curveStep }

export interface XY { x: number; y: number | null }
/** Line path through points; null y's lift the pen. */
export function linePath(pts: XY[], curve: CurveFactory): string {
  return d3line<XY>().defined((p) => p.y !== null && Number.isFinite(p.y)).x((p) => p.x).y((p) => p.y ?? 0).curve(curve)(pts) ?? ''
}
export interface Band { x: number; y0: number | null; y1: number | null }
/** Filled band between y0 and y1; nulls split it. */
export function bandPath(pts: Band[], curve: CurveFactory): string {
  return d3area<Band>().defined((p) => p.y0 !== null && p.y1 !== null && Number.isFinite(p.y0) && Number.isFinite(p.y1)).x((p) => p.x).y0((p) => p.y0 ?? 0).y1((p) => p.y1 ?? 0).curve(curve)(pts) ?? ''
}

/** Show dots while they are sparse enough to read as dots. */
export function pointsVisible(mode: 'auto' | 'always' | 'never', perSeries: number, plotWidth: number): boolean {
  if (mode !== 'auto') return mode === 'always'
  return perSeries <= 1 || (perSeries <= 60 && plotWidth / Math.max(1, perSeries - 1) >= 14)
}

/** A point data mark. `isolated` points (no neighbours to draw a line to) are always visible. */
export function pointMark(p: { key: string; cx: number; cy: number; color: Paint; surface: Paint; series?: string; datum: unknown; label: string; title: string; rows: Array<[string, string]>; show: boolean; isolated?: boolean }): CircleMark {
  const show = p.show || !!p.isolated
  return {
    type: 'circle', role: 'point', key: p.key, cx: p.cx, cy: p.cy, r: show ? 3 : 2.5,
    fill: p.isolated ? p.color : p.surface, stroke: p.color, strokeWidth: 1.5, opacity: show ? 1 : 0,
    series: p.series, datum: p.datum, label: p.label, tooltip: { title: p.title, rows: p.rows, color: p.color },
  }
}

/** Whether point i of a null-split sequence has no defined neighbour (draws as a lone dot). */
export function isolated(ys: Array<number | null>, i: number): boolean {
  return ys[i] !== null && (i === 0 || ys[i - 1] === null) && (i === ys.length - 1 || ys[i + 1] === null)
}

/** Zoom wiring for the DrawResult: clip only when zoomable (with room for edge dots). */
export function zoomResult(f: CartesianFrame, zoom: boolean | undefined, x: XModel): { plot: Rect; clip?: Rect; invertX: (px: number) => unknown } {
  const p = f.plot
  return {
    plot: p,
    clip: zoom ? { x: p.x - 4, y: p.y - 4, width: p.width + 8, height: p.height + 8 } : undefined,
    invertX: (px: number) => { const v = f.invertX(px); return v instanceof Date ? v : typeof v === 'number' && x.time ? new Date(v) : v },
  }
}

/** Direct end labels at the right of the plot; null when they cannot all fit without overlapping. */
export function endLabels(items: Array<{ key: string; text: string; full: string; y: number; color: Paint }>, plot: Rect, lineH: number): Array<{ key: string; text: string; full: string; y: number; color: Paint }> | null {
  if (!items.length) return []
  if (items.length * lineH > plot.height + lineH) return null
  const s = [...items].sort((a, b) => a.y - b.y).map((d) => ({ ...d }))
  for (let i = 1; i < s.length; i++) s[i]!.y = Math.max(s[i]!.y, s[i - 1]!.y + lineH)
  const bottom = plot.y + plot.height + lineH / 2
  const over = s[s.length - 1]!.y - bottom
  if (over > 0) {
    s[s.length - 1]!.y -= over
    for (let i = s.length - 2; i >= 0; i--) s[i]!.y = Math.min(s[i]!.y, s[i + 1]!.y - lineH)
  }
  if (s[0]!.y < plot.y - lineH / 2) return null
  return s
}

// ----- line / area renderer shared by line, multi-line, step-line and area

export interface LineRow { x: XValue; y: number | null; series?: string }
export interface LineOptions { gaps: Gaps; xType: XType; points: 'auto' | 'always' | 'never'; xTitle?: string; yTitle?: string }

export function drawLines(ctx: DrawContext<LineRow[], LineOptions>, cfg: { curve: CurveFactory; area: boolean; endLabels: boolean }): DrawResult {
  const { theme, fmt, options: o } = ctx
  const x = xModel(ctx.data.map((r) => r.x), o.xType, fmt)
  const { series, duplicates } = groupSeries(ctx.data, x, (r) => r.x, (r) => r.series ?? '', (r) => r.y)
  let missing = 0
  const prepared = series.map((s) => {
    const ts = s.points.map((p) => p.t)
    const g = applyGaps(ts, s.points.map((p) => p.y), o.gaps)
    missing += g.missing
    return { ...s, ts, ys: g.ys, raw: s.points.map((p) => p.y) }
  })
  const multi = series.length > 1
  const visible = prepared.filter((s) => !ctx.hidden.has(s.name))
  const ext = finiteExtent(visible.flatMap((s) => s.ys)) ?? [0, 1]
  const lineH = SIZES.tick + 3
  const wantEnd = cfg.endLabels && multi && ctx.width >= 420 && visible.length * lineH <= ctx.height - 60
  const labelW = wantEnd ? Math.min(Math.max(...series.map((s) => ctx.measure(s.name))), ctx.width * 0.18) + 8 : 0
  const f = ctx.cartesian({
    x: x.spec(prepared.flatMap((s) => s.ts)),
    y: { type: 'linear', domain: cfg.area ? [Math.min(0, ext[0]), Math.max(0, ext[1])] : ext, zero: cfg.area },
    xTitle: o.xTitle, yTitle: o.yTitle,
    pad: { right: labelW },
    zoom: !!o.zoom,
  })
  const marks: Mark[] = [...f.marks]
  const perSeries = Math.max(0, ...visible.map((s) => s.ts.length))
  const show = pointsVisible(o.points, perSeries, f.plot.width * (ctx.zoom?.k ?? 1))
  const yd = f.y.domain() as number[]
  const base = f.py(Math.max(Math.min(...yd), Math.min(Math.max(...yd), 0)))
  const ends: Array<{ key: string; text: string; full: string; y: number; color: Paint }> = []
  // Several series: a point's tooltip names every visible series at that x, so one hover reads the whole day.
  const atX = new Map<number, Array<[string, string]>>()
  if (multi) for (const s of visible) s.ts.forEach((t, i) => {
    const y = s.ys[i]
    if (y === null || y === undefined) return
    const filled = s.raw[i] === null ? (o.gaps === 'zero' ? ' (missing, drawn as zero)' : ' (interpolated)') : ''
    let rows = atX.get(t)
    if (!rows) atX.set(t, (rows = []))
    rows.push([s.name, fmt.value(y) + filled])
  })
  for (const s of visible) {
    const color = theme.cat(s.index)
    const sk = multi ? s.name : undefined
    const pts = s.ts.map((t, i) => ({ x: f.px(x.value(t)), y: s.ys[i] === null ? null : f.py(s.ys[i]) }))
    if (cfg.area) marks.push({ type: 'path', key: `area:${s.name}`, d: bandPath(pts.map((p) => ({ x: p.x, y0: p.y === null ? null : base, y1: p.y })), cfg.curve), fill: color, fillOpacity: 0.22, stroke: 'none', pattern: multi ? s.index : undefined, series: sk, inert: true })
    marks.push({ type: 'path', key: `line:${s.name}`, d: linePath(pts, cfg.curve), fill: 'none', stroke: color, strokeWidth: 2, linecap: 'round', linejoin: 'round', series: sk, inert: true })
    s.ts.forEach((t, i) => {
      const y = s.ys[i]
      const p = pts[i]!
      if (y === null || y === undefined || p.y === null) return
      const filled = s.raw[i] === null ? (o.gaps === 'zero' ? ' (missing, drawn as zero)' : ' (interpolated)') : ''
      const when = x.label(t)
      marks.push(pointMark({
        key: `p:${s.name}:${t}`, cx: p.x, cy: p.y, color, surface: theme.surface, series: sk, show,
        isolated: isolated(s.ys, i),
        datum: { x: x.value(t), y, ...(multi ? { series: s.name } : {}) },
        label: `${multi || s.name ? `${s.name}, ` : ''}${when}: ${fmt.value(y)}${filled}`,
        title: when,
        // A lone series still says what it is when the rows named it; unnamed (plain `line`) reads "Value".
        rows: multi ? (atX.get(t) ?? []) : [[s.name || 'Value', fmt.value(y) + filled]],
      }))
    })
    if (wantEnd) {
      let last = -1
      for (let i = 0; i < pts.length; i++) { const p = pts[i]!; if (p.y !== null && p.x <= f.plot.x + f.plot.width + 0.5 && p.x >= f.plot.x - 0.5) last = i }
      const lp = pts[last]
      if (lp && lp.y !== null) {
        const text = ctx.truncate(s.name, labelW - 8)
        ends.push({ key: s.name, text, full: s.name, y: lp.y, color })
      }
    }
  }
  const placed = wantEnd ? endLabels(ends, f.plot, lineH) : null
  for (const l of placed ?? []) {
    marks.push({ type: 'text', layer: 'labels', key: `end:${l.key}`, x: f.plot.x + f.plot.width + 6, y: l.y, text: l.text, full: l.text !== l.full ? l.full : undefined, anchor: 'start', baseline: 'central', size: SIZES.tick, weight: 600, fill: l.color, series: l.key })
  }
  return {
    marks,
    ...zoomResult(f, o.zoom, x),
    legend: multi ? { items: series.map((s) => ({ key: s.name, label: s.name, color: theme.cat(s.index), shape: cfg.area ? 'square' : 'line', pattern: cfg.area ? s.index : undefined })) } : undefined,
    note: notes([[duplicates, 'duplicate dates summed'], gapNote(missing, o.gaps)]),
  }
}

/** "<Name>, <what>, <span>, peak <v> in <when>" (one series) or "highest <series> at <v> in <when>" (several). */
export function describeTrend<R>(p: { name: string; rows: R[]; getX: (r: R) => XValue; getY: (r: R) => number | null; getSeries?: (r: R) => string; xType: XType; fmt: Formatters; what?: string; peakWord?: string }): string {
  const { fmt } = p
  const x = xModel(p.rows.map(p.getX), p.xType, fmt)
  const { series } = groupSeries(p.rows, x, p.getX, p.getSeries ?? (() => ''), p.getY)
  const ts = series.flatMap((s) => s.points.map((q) => q.t))
  const e = finiteExtent(ts) ?? [0, 0]
  let best: { s: string; t: number; y: number } | null = null
  for (const s of series) for (const q of s.points) if (q.y !== null && (!best || q.y > best.y)) best = { s: s.name, t: q.t, y: q.y }
  const multi = series.length > 1
  const what = p.what ?? (multi ? phrase.count(series.length, 'series', 'series') : phrase.count(ts.length, 'point'))
  const peak = !best ? 'no values' : multi ? `highest ${best.s} at ${fmt.value(best.y)} in ${x.short(best.t, e[1] - e[0])}` : `${p.peakWord ?? 'peak'} ${fmt.value(best.y)} in ${x.short(best.t, e[1] - e[0])}`
  return `${p.name}, ${what}, ${spanPhrase(x, ts, fmt)}, ${peak}`
}

/** Table of what is drawn: one row per (x, series), sorted by x. */
export function trendTable(rows: LineRow[], xType: XType, fmt: Formatters, withSeries: boolean): TableModel {
  const x = xModel(rows.map((r) => r.x), xType, fmt)
  const { series } = groupSeries(rows, x, (r) => r.x, (r) => r.series ?? '', (r) => r.y)
  const out = series.flatMap((s) => s.points.map((q) => ({ s: s.name, i: s.index, t: q.t, y: q.y }))).sort((a, b) => a.t - b.t || a.i - b.i)
  return {
    columns: withSeries ? [x.column, 'Series', 'Value'] : [x.column, 'Value'],
    rows: out.map((d) => (withSeries ? [x.cell(d.t), d.s, d.y] : [x.cell(d.t), d.y])),
  }
}

/** The y-noun for summaries: the axis title, else the mapped column name. */
export function yNoun(o: { yTitle?: string; fields?: Record<string, string> }, field = 'y'): string | undefined {
  return o.yTitle ?? o.fields?.[field]
}

// ----- stacked areas (stacked-area, stacked-area-100, streamgraph)

export interface StackRow { x: XValue; series: string; y: number | null }
export interface StackOptions { gaps: Gaps; xType: XType; points: 'auto' | 'always' | 'never'; curve: 'linear' | 'monotone'; xTitle?: string; yTitle?: string }
export type StackMode = 'none' | 'expand' | 'wiggle'

interface StackModel {
  x: XModel
  names: string[]
  ts: number[]
  /** value[seriesIndex][xIndex] after the gap policy, negatives clamped; null = not drawn. */
  values: Array<Array<number | null>>
  duplicates: number
  missing: number
  negatives: number
}

function stackModel(rows: StackRow[], xType: XType, gaps: Gaps, fmt: Formatters): StackModel {
  const x = xModel(rows.map((r) => r.x), xType, fmt)
  const { series, duplicates } = groupSeries(rows, x, (r) => r.x, (r) => r.series, (r) => r.y)
  const ts = [...new Set(series.flatMap((s) => s.points.map((p) => p.t)))].sort((a, b) => a - b)
  let missing = 0, negatives = 0
  const values = series.map((s) => {
    const at = new Map(s.points.map((p) => [p.t, p.y]))
    const ys = ts.map((t) => (at.has(t) ? at.get(t)! : null))
    const g = applyGaps(ts, ys, gaps)
    missing += s.points.filter((p) => p.y === null).length
    return g.ys.map((v) => {
      if (v !== null && v < 0) { negatives++; return 0 }
      return v
    })
  })
  return { x, names: series.map((s) => s.name), ts, values, duplicates, missing, negatives }
}

export function stackIsEmpty(rows: StackRow[]): boolean | string {
  if (!rows.length) return true
  return rows.every((r) => !r.y || r.y <= 0) ? 'All values are zero' : false
}

export function drawStack(ctx: DrawContext<StackRow[], StackOptions>, mode: StackMode): DrawResult {
  const { theme, fmt, options: o } = ctx
  const m = stackModel(ctx.data, o.xType, o.gaps, fmt)
  const vis = m.names.map((n, i) => ({ n, i })).filter((s) => !ctx.hidden.has(s.n))
  const longRows = vis.flatMap((s) => m.ts.map((t, j) => ({ x: t, series: s.n, value: m.values[s.i]![j] ?? 0 })))
  const st = stackRows(longRows, { x: 'x', series: 'series', value: 'value', offset: mode, order: mode === 'wiggle' ? 'insideOut' : 'input', seriesOrder: vis.map((s) => s.n) })
  const layers = new Map(st.layers.map((l) => [l.key, l.points]))
  const totals = m.ts.map((_, j) => vis.reduce((a, s) => a + (m.values[s.i]![j] ?? 0), 0))
  let lo = 0, hi = 0
  for (const l of st.layers) for (const p of l.points) { lo = Math.min(lo, p.y0); hi = Math.max(hi, p.y1) }
  if (mode === 'wiggle' && lo === hi) { lo = -1; hi = 1 }
  // the streamgraph has no value axis: shift it onto [0, span] (with a little air) so no zero rule is drawn through it
  const shift = mode === 'wiggle' ? lo - (hi - lo) * 0.04 : 0
  const f = ctx.cartesian({
    x: m.x.spec(m.ts),
    y: mode === 'expand' ? { type: 'linear', domain: [0, 1], nice: false } : mode === 'wiggle' ? { type: 'linear', domain: [0, hi - shift + (hi - lo) * 0.04], nice: false } : { type: 'linear', domain: [0, Math.max(hi, 1e-9)] },
    yFormat: mode === 'expand' ? ((v: number) => fmt.pct(v)) as (v: never) => string : undefined,
    yAxis: mode === 'wiggle' ? 'none' : 'left',
    grid: mode === 'wiggle' ? 'x' : 'y',
    xTitle: o.xTitle, yTitle: mode === 'wiggle' ? undefined : o.yTitle,
    pad: mode === 'wiggle' ? { left: 8 } : undefined,
    zoom: !!o.zoom,
  })
  const marks: Mark[] = [...f.marks]
  const curve = CURVES[o.curve]
  const show = pointsVisible(o.points, m.ts.length, f.plot.width * (ctx.zoom?.k ?? 1))
  const multi = m.names.length > 1
  for (const s of vis) {
    const pts = layers.get(s.n) ?? []
    const raw = m.values[s.i]!
    const color = theme.cat(s.i)
    const sk = multi ? s.n : undefined
    const band = pts.map((p, j) => ({ x: f.px(m.x.value(m.ts[j]!)), y0: raw[j] === null ? null : f.py(p.y0 - shift), y1: raw[j] === null ? null : f.py(p.y1 - shift) }))
    marks.push({ type: 'path', key: `area:${s.n}`, d: bandPath(band, curve), fill: color, fillOpacity: 0.85, stroke: theme.surface, strokeWidth: 0.75, pattern: s.i, series: sk, inert: true })
    pts.forEach((p, j) => {
      const v = raw[j]
      const b = band[j]!
      if (v === null || v === undefined || b.y0 === null || b.y1 === null) return
      const when = m.x.label(m.ts[j]!)
      const total = totals[j] ?? 0
      const share = total > 0 ? fmt.pct(v / total) : fmt.pct(0)
      marks.push(pointMark({
        key: `p:${s.n}:${m.ts[j]}`, cx: b.x, cy: mode === 'none' ? b.y1 : (b.y0 + b.y1) / 2, color, surface: theme.surface, series: sk, show,
        datum: { x: m.x.value(m.ts[j]!), series: s.n, y: v, y0: p.y0, y1: p.y1, total },
        label: `${s.n}, ${when}: ${fmt.value(v)} (${share} of ${fmt.value(total)})`,
        title: s.n,
        rows: [[m.x.column, when], ['Value', fmt.value(v)], ['Share', share], ['Total', fmt.value(total)]],
      }))
    })
  }
  return {
    marks,
    ...zoomResult(f, o.zoom, m.x),
    legend: multi ? { items: m.names.map((n, i) => ({ key: n, label: n, color: theme.cat(i), shape: 'square' as const, pattern: i })) } : undefined,
    note: notes([[m.duplicates, 'duplicate (date, series) pairs summed'], [m.negatives, 'negative values drawn as zero'], gapNote(m.missing, o.gaps)]),
  }
}

export function describeStack(name: string, rows: StackRow[], o: StackOptions, fmt: Formatters, mode: StackMode): string {
  const m = stackModel(rows, o.xType, o.gaps, fmt)
  const span = spanPhrase(m.x, m.ts, fmt)
  const what = phrase.count(m.names.length, 'series', 'series')
  const sums = m.names.map((n, i) => ({ label: n, value: m.values[i]!.reduce<number>((a, v) => a + (v ?? 0), 0) }))
  const largest = sums.reduce((a, b) => (b.value > a.value ? b : a), sums[0] ?? { label: '', value: 0 })
  const totals = m.ts.map((_, j) => m.values.reduce((a, s) => a + (s[j] ?? 0), 0))
  const grand = totals.reduce((a, b) => a + b, 0)
  const e = finiteExtent(m.ts) ?? [0, 0]
  if (mode === 'none') {
    let j = 0
    totals.forEach((t, k) => { if (t > totals[j]!) j = k })
    return `${name}, ${what}, ${span}, total peaks at ${fmt.value(totals[j] ?? 0)} in ${m.x.short(m.ts[j] ?? 0, e[1] - e[0])}, largest ${largest.label}`
  }
  return `${name}, ${what}, ${span}, largest ${largest.label} with ${fmt.pct(grand > 0 ? largest.value / grand : 0)} overall`
}

export function stackTable(rows: StackRow[], o: StackOptions, fmt: Formatters): TableModel {
  const m = stackModel(rows, o.xType, o.gaps, fmt)
  const out: Array<Array<string | number | null>> = []
  m.ts.forEach((t, j) => m.names.forEach((n, i) => { const v = m.values[i]![j]; if (v !== null && v !== undefined) out.push([m.x.cell(t), n, v]) }))
  return { columns: [m.x.column, 'Series', 'Value'], rows: out }
}
