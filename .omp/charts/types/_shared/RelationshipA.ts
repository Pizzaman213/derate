import { nf } from '../../core/format'
// Shared by the x/y relationship charts (scatter, bubble, connected-scatter,
// regression-scatter, hexbin, contour-density): point cleaning, padded continuous
// domains, log-axis tick labels, collision-free direct labels, zoom results and the
// "x from .. to .., strong positive correlation" summary phrase.

import { SIZES, stats, phrase } from '../../core'
import type { CartesianFrame, DrawContext, Formatters, Mark, Palette, Rect, ScaleSpec } from '../../core'

export type AxisKind = 'linear' | 'log'

export interface XYRow {
  x: number | null
  y: number | null
}

export interface Pt<R> {
  row: R
  /** Index of the row in the input. */
  i: number
  x: number
  y: number
}

/** Rows with finite x and y (and positive ones on log axes), plus what was left out. */
export function cleanPoints<R extends XYRow>(rows: readonly R[], xKind: AxisKind = 'linear', yKind: AxisKind = 'linear'): { pts: Array<Pt<R>>; missing: number; nonPositive: number } {
  const pts: Array<Pt<R>> = []
  let missing = 0, nonPositive = 0
  rows.forEach((row, i) => {
    if (row.x === null || row.y === null) { missing++; return }
    if ((xKind === 'log' && row.x <= 0) || (yKind === 'log' && row.y <= 0)) { nonPositive++; return }
    pts.push({ row, i, x: row.x, y: row.y })
  })
  return { pts, missing, nonPositive }
}

/** Dot radius that keeps dense plots readable: big for a handful, small for thousands. */
export function autoRadius(n: number): number {
  return n > 3000 ? 2 : n > 1000 ? 2.5 : n > 300 ? 3.5 : 4.5
}

/** Min/max of finite numbers. */
export function extentOf(values: Iterable<number>): [number, number] | null {
  let lo = Infinity, hi = -Infinity
  for (const v of values) {
    if (!Number.isFinite(v)) continue
    if (v < lo) lo = v
    if (v > hi) hi = v
  }
  return lo === Infinity ? null : [lo, hi]
}

/**
 * A continuous scale spec around the data with `frac` of breathing room on each side,
 * so points on the extremes are not cut in half by the plot edge. Linear domains never
 * cross zero because of the padding alone (non-negative data keeps a 0 floor).
 */
export function axisSpec(values: Iterable<number>, kind: AxisKind, frac = 0.04): ScaleSpec {
  const ext = extentOf(values)
  if (kind === 'log') {
    if (!ext) return { type: 'log', domain: [1, 10] }
    const decades = Math.log10(ext[1] / ext[0]) || 1
    const k = 10 ** (decades * frac)
    return { type: 'log', domain: [ext[0] / k, ext[1] * k] }
  }
  if (!ext) return { type: 'linear', domain: [0, 1], zero: false }
  const [lo, hi] = ext
  const p = (hi - lo) * frac
  let a = lo - p, b = hi + p
  if (lo >= 0 && a < 0) a = 0
  if (hi <= 0 && b > 0) b = 0
  return { type: 'linear', domain: [a, b], zero: false }
}

/** Widen a linear/log spec so `px` pixels of a `len`-pixel axis are free at each end (bubbles). */
export function widen(spec: ScaleSpec, len: number, px: number): ScaleSpec {
  if (len <= 2 * px + 4 || px <= 0) return spec
  const f = px / (len - 2 * px)
  if (spec.type === 'linear') {
    const [a, b] = spec.domain
    const span = b - a || Math.abs(a) * 0.2 || 2
    const c = (a + b) / 2, half = (span / 2) * (1 + 2 * f)
    return { ...spec, domain: [c - half, c + half] }
  }
  if (spec.type === 'log') {
    const [a, b] = spec.domain
    const la = Math.log10(a), lb = Math.log10(b)
    const span = lb - la || 1
    return { ...spec, domain: [10 ** (la - span * f), 10 ** (lb + span * f)] }
  }
  return spec
}

/**
 * Log axes: over two or more decades, ticks at powers of ten only (a tick count equal to
 * the decade count of the niced domain makes d3 skip the 2..9 subdivisions); within one
 * decade, every subdivision, all labelled, so the frame's even thinning keeps a readable subset.
 */
export function logTicks(spec: ScaleSpec): number | undefined {
  if (spec.type !== 'log') return undefined
  const decades = Math.ceil(Math.log10(spec.domain[1]) - 1e-9) - Math.floor(Math.log10(spec.domain[0]) + 1e-9)
  return decades >= 2 ? decades : undefined
}

/** True when the chart is zoomed away from identity. */
export function zoomed(ctx: Pick<DrawContext, 'zoom'>): boolean {
  const z = ctx.zoom
  return !!z && (z.k !== 1 || z.x !== 0 || z.y !== 0)
}

/** Point (with a radius of slack) inside the plot. */
export function inPlot(plot: Rect, x: number, y: number, slack = 0): boolean {
  return x >= plot.x - slack && x <= plot.x + plot.width + slack && y >= plot.y - slack && y <= plot.y + plot.height + slack
}

/** What a zoomable x/y chart returns besides its marks. */
export function frameResult(f: CartesianFrame): { plot: Rect; clip: Rect; invertX: (p: number) => unknown; invertY: (p: number) => unknown } {
  return { plot: f.plot, clip: f.plot, invertX: f.invertX, invertY: f.invertY }
}

/** Stable unique keys: the preferred base, suffixed when it repeats. */
export function uniqueKeys(bases: readonly string[]): string[] {
  const seen = new Map<string, number>()
  return bases.map((b) => {
    const n = seen.get(b) ?? 0
    seen.set(b, n + 1)
    return n ? `${b}~${n}` : b
  })
}

/** Points that share an exact position with an earlier point. */
export function overlapping(pts: ReadonlyArray<{ x: number; y: number }>): number {
  const seen = new Set<string>()
  let n = 0
  for (const p of pts) {
    const k = `${p.x}|${p.y}`
    if (seen.has(k)) n++
    else seen.add(k)
  }
  return n
}

/** "strong positive correlation (r = 0.86)", or null when r is undefined. */
export function correlationPhrase(xs: readonly number[], ys: readonly number[], fmt: Formatters): string | null {
  if (xs.length < 3) return null
  const r = stats.pearson(xs, ys)
  if (!Number.isFinite(r)) return null
  const a = Math.abs(r)
  const strength = a >= 0.7 ? 'strong' : a >= 0.4 ? 'moderate' : a >= 0.2 ? 'weak' : null
  const rText = `r = ${fmt.num(r, 2)}`
  return strength ? `${strength} ${r > 0 ? 'positive' : 'negative'} correlation (${rText})` : `no clear correlation (${rText})`
}

/** "x from 1.2 to 9.8, y from 3 to 80" (titles used when given). */
export function rangePhrase(pts: ReadonlyArray<{ x: number; y: number }>, fmt: Formatters, xName = 'x', yName = 'y'): string {
  const ex = extentOf(pts.map((p) => p.x)), ey = extentOf(pts.map((p) => p.y))
  if (!ex || !ey) return 'no values'
  const r = (name: string, e: [number, number]) => (e[0] === e[1] ? `${name} at ${fmt.value(e[0])}` : `${name} from ${fmt.value(e[0])} to ${fmt.value(e[1])}`)
  return `${r(xName, ex)}, ${r(yName, ey)}`
}

/** The standard summary: "<Name>, 40 points in 3 series, x from .., y from .., strong positive correlation (r = 0.86)". */
export function describeXY(name: string, pts: ReadonlyArray<{ x: number; y: number }>, fmt: Formatters, opts: { noun?: string; series?: number; span?: string; xName?: string; yName?: string; extra?: string[] } = {}): string {
  const noun = opts.noun ?? 'point'
  if (!pts.length) return `${name}, no ${noun}s with both values`
  const parts = [`${name}, ${phrase.count(pts.length, noun)}${opts.span ?? ''}${opts.series && opts.series > 1 ? ` in ${opts.series} series` : ''}`, rangePhrase(pts, fmt, opts.xName, opts.yName)]
  const c = correlationPhrase(pts.map((p) => p.x), pts.map((p) => p.y), fmt)
  if (c) parts.push(c)
  if (opts.extra) parts.push(...opts.extra)
  return parts.join(', ')
}

/** Series names in first-seen order; `undefined` (no series column) collapses to one unnamed series. */
export function seriesOf<R extends { series?: string }>(rows: readonly R[]): string[] {
  const out: string[] = []
  const seen = new Set<string>()
  for (const r of rows) {
    if (r.series === undefined || seen.has(r.series)) continue
    seen.add(r.series)
    out.push(r.series)
  }
  return out
}

/** Tooltip rows for an x/y point; long axis titles are shortened (the tooltip is narrow). */
export function xyRows(x: number, y: number, fmt: Formatters, xTitle?: string, yTitle?: string): Array<[string, string]> {
  return [[shortName(xTitle, 'x'), fmt.value(x)], [shortName(yTitle, 'y'), fmt.value(y)]]
}

/** An axis title fit for a tooltip key: at most 28 characters. */
export function shortName(title: string | undefined, fallback: string): string {
  if (!title) return fallback
  return title.length > 28 ? `${title.slice(0, 27).trimEnd()}…` : title
}

/** Lightest step of the sequential ramp used for densities: seq(0) is too close to the surface to read as "some". */
const DENSITY_FLOOR = 0.12

/** Sequential colour for a density share t in [0, 1]. */
export function densityColor(theme: Palette, t: number): string {
  return theme.seq(DENSITY_FLOOR + (1 - DENSITY_FLOOR) * Math.min(1, Math.max(0, t)))
}

/** Gradient-legend stops matching `densityColor`. */
export function densityStops(theme: Palette): string[] {
  return Array.from({ length: 7 }, (_, i) => densityColor(theme, i / 6))
}

export interface LabelItem {
  key: string
  x: number
  y: number
  /** Radius of the mark being labelled. */
  r: number
  text: string
  series?: string
  fill?: string
}

interface Box { x: number; y: number; w: number; h: number }
const hits = (a: Box, b: Box) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h

/**
 * Direct labels placed greedily (right, left, above, below the mark), kept inside the
 * plot and clear of every other label and of the marks given as obstacles. Labels that
 * find no place are dropped -- the tooltip still carries them.
 */
export function placeLabels(
  items: readonly LabelItem[],
  plot: Rect,
  ctx: Pick<DrawContext, 'measure' | 'truncate'>,
  theme: Palette,
  obstacles: ReadonlyArray<{ x: number; y: number; r: number }> = [],
  maxWidth = 120,
): Mark[] {
  const size = SIZES.tick
  const h = size + 2
  const taken: Box[] = obstacles.map((o) => ({ x: o.x - o.r, y: o.y - o.r, w: o.r * 2, h: o.r * 2 }))
  const out: Mark[] = []
  for (const it of items) {
    const text = ctx.truncate(it.text, Math.min(maxWidth, plot.width - 8), size)
    if (!text || text === '…') continue
    const w = ctx.measure(text, size)
    const g = it.r + 3
    const cands: Array<{ box: Box; x: number; y: number; anchor: 'start' | 'end' | 'middle' }> = [
      { box: { x: it.x + g, y: it.y - h / 2, w, h }, x: it.x + g, y: it.y, anchor: 'start' },
      { box: { x: it.x - g - w, y: it.y - h / 2, w, h }, x: it.x - g, y: it.y, anchor: 'end' },
      { box: { x: it.x - w / 2, y: it.y - g - h, w, h }, x: it.x, y: it.y - g - h / 2, anchor: 'middle' },
      { box: { x: it.x - w / 2, y: it.y + g, w, h }, x: it.x, y: it.y + g + h / 2, anchor: 'middle' },
    ]
    const own = { x: it.x - it.r, y: it.y - it.r, w: it.r * 2, h: it.r * 2 }
    const pick = cands.find((c) => c.box.x >= plot.x + 1 && c.box.x + c.box.w <= plot.x + plot.width - 1 && c.box.y >= plot.y && c.box.y + c.box.h <= plot.y + plot.height
      && taken.every((t) => (t.x === own.x && t.y === own.y && t.w === own.w) || !hits(t, c.box)))
    if (!pick) continue
    taken.push(pick.box)
    out.push({
      type: 'text', layer: 'labels', key: `lbl:${it.key}`, x: pick.x, y: pick.y, text, full: text !== it.text ? it.text : undefined,
      anchor: pick.anchor, baseline: 'central', size, fill: it.fill ?? theme.inkSecondary, halo: true, series: it.series, className: text !== it.text ? 'oc-trunc' : undefined, inert: text === it.text,
    })
  }
  return out
}

/** Number with `digits` significant digits in the locale ("0.00123", "12,300"). */
export function sig(v: number, locale: string, digits = 3): string {
  if (!Number.isFinite(v)) return '–'
  if (v === 0) return '0'
  return nf(locale, { maximumSignificantDigits: digits }).format(v)
}

/** Table columns/rows for x/y rows, with only the optional columns that carry something. */
export function xyTable<R extends XYRow & { series?: string; label?: string }>(rows: readonly R[], extra: Array<{ name: string; get: (r: R) => number | string | null }> = [], xTitle?: string, yTitle?: string): { columns: string[]; rows: Array<Array<string | number | null>> } {
  const hasLabel = rows.some((r) => r.label !== undefined)
  const hasSeries = rows.some((r) => r.series !== undefined)
  const columns = [...(hasLabel ? ['Label'] : []), ...(hasSeries ? ['Series'] : []), xTitle || 'X', yTitle || 'Y', ...extra.map((e) => e.name)]
  return {
    columns,
    rows: rows.map((r) => [...(hasLabel ? [r.label ?? null] : []), ...(hasSeries ? [r.series ?? null] : []), r.x, r.y, ...extra.map((e) => e.get(r))]),
  }
}
