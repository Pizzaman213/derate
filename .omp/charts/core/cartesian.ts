// x/y frame: builds the scales, measures tick labels, picks rotation / truncation /
// thinning so labels never overlap or leave the box, sizes the margins from what it
// measured, and emits grid + axis marks. Every x/y chart goes through this, which is
// why their axes look and behave the same.

import { scaleBand, scaleLinear, scaleLog, scalePoint, scaleSymlog, scaleUtc, type ScaleBand, type ScaleLinear, type ScalePoint, type ScaleTime, type ScaleLogarithmic, type ScaleSymLog } from 'd3-scale'
import { floorTo, offsetBy, type Granularity } from '../time/granularity'
import { zonedParts } from '../time/tz'
import type { Formatters } from './format'
import { SIZES } from './theme'
import type { Palette } from './theme'
import type { Mark, Rect, TextMark, ZoomState } from './types'

export type ScaleSpec =
  | { type: 'band'; domain: string[]; padding?: number; paddingOuter?: number; align?: number }
  | { type: 'point'; domain: string[]; padding?: number }
  | { type: 'linear'; domain: [number, number]; nice?: boolean; zero?: boolean; reverse?: boolean }
  | { type: 'log'; domain: [number, number]; nice?: boolean }
  | { type: 'symlog'; domain: [number, number]; nice?: boolean }
  | { type: 'time'; domain: [Date, Date]; nice?: boolean }

export type AnyScale =
  | ScaleBand<string>
  | ScalePoint<string>
  | ScaleLinear<number, number>
  | ScaleLogarithmic<number, number>
  | ScaleSymLog<number, number>
  | ScaleTime<number, number>

export interface CartesianSpec {
  x: ScaleSpec
  y: ScaleSpec
  xFormat?: (v: never) => string
  yFormat?: (v: never) => string
  xTitle?: string
  yTitle?: string
  /** Gridlines: default is along the value axis. */
  grid?: 'x' | 'y' | 'both' | 'none'
  xAxis?: 'bottom' | 'top' | 'none'
  yAxis?: 'left' | 'right' | 'none'
  xTickCount?: number
  yTickCount?: number
  /** Extra room for direct labels. */
  pad?: Partial<Record<'top' | 'right' | 'bottom' | 'left', number>>
  /** Apply the zoom transform (zoomable charts). */
  zoom?: boolean
  /** Max px for band tick labels on the y axis (default 38% of width). */
  bandLabelMax?: number
}

export interface CartesianFrame {
  x: AnyScale
  y: AnyScale
  plot: Rect
  marks: Mark[]
  xTicks: unknown[]
  yTicks: unknown[]
  /** Position of v on x (band/point: start of band; others: value). */
  px(v: unknown): number
  py(v: unknown): number
  /** Band width (0 for continuous). */
  bw: number
  bh: number
  invertX(px: number): unknown
  invertY(py: number): unknown
}

export interface CartesianEnv {
  width: number
  height: number
  fmt: Formatters
  theme: Palette
  measure: (t: string, size?: number) => number
  truncate: (t: string, w: number, size?: number) => string
  zoom: ZoomState | null
  zoomAxes: 'x' | 'xy' | undefined
  xDomain?: [unknown, unknown]
  yDomain?: [number, number]
}

const TICK = 4, GAP = 4, FS = SIZES.tick
/** Extra drop for upright bottom x labels so the first one clears the lowest y label's box. */
const XDROP = 3
const isBand = (s: AnyScale): s is ScaleBand<string> => 'bandwidth' in s && 'paddingInner' in s
const isPoint = (s: AnyScale): s is ScalePoint<string> => 'step' in s && !('paddingInner' in s)
const isTime = (s: AnyScale): s is ScaleTime<number, number> => s.domain()[0] instanceof Date

function makeScale(spec: ScaleSpec, range: [number, number]): AnyScale {
  switch (spec.type) {
    case 'band':
      return scaleBand<string>().domain(spec.domain).range(range).paddingInner(spec.padding ?? 0.2).paddingOuter(spec.paddingOuter ?? (spec.padding ?? 0.2) / 2).align(spec.align ?? 0.5)
    case 'point':
      return scalePoint<string>().domain(spec.domain).range(range).padding(spec.padding ?? 0.5)
    case 'linear': {
      let [a, b] = spec.domain
      if (!Number.isFinite(a) || !Number.isFinite(b)) [a, b] = [0, 1]
      if (spec.zero !== false) { a = Math.min(0, a); b = Math.max(0, b) }
      if (a === b) { const d = Math.abs(a) * 0.1 || 1; a -= d; b += d }
      const s = scaleLinear().domain(spec.reverse ? [b, a] : [a, b]).range(range)
      return spec.nice === false ? s : s.nice()
    }
    case 'log': {
      let [a, b] = spec.domain
      a = a > 0 ? a : 1e-3
      b = b > a ? b : a * 10
      const s = scaleLog().domain([a, b]).range(range)
      return spec.nice === false ? s : s.nice()
    }
    case 'symlog': {
      let [a, b] = spec.domain
      if (a === b) { a -= 1; b += 1 }
      return scaleSymlog().domain([a, b]).range(range)
    }
    case 'time': {
      let [a, b] = spec.domain
      if (+a === +b) { a = new Date(+a - 864e5); b = new Date(+b + 864e5) }
      return scaleUtc().domain([a, b]).range(range)
    }
  }
}

/** Time-zone-aware ticks: wall-clock boundaries of a granularity, thinned to ~count. */
export function timeTicks(domain: [Date, Date], count: number, timeZone: string): { ticks: Date[]; g: Granularity } {
  const span = +domain[1] - +domain[0]
  const order: Array<[Granularity, number]> = [['minute', 1], ['minute', 5], ['minute', 15], ['minute', 30], ['hour', 1], ['hour', 3], ['hour', 6], ['hour', 12], ['day', 1], ['day', 2], ['week', 1], ['month', 1], ['month', 3], ['month', 6], ['year', 1], ['year', 2], ['year', 5], ['year', 10], ['year', 25], ['year', 50], ['year', 100]]
  const approx: Record<Granularity, number> = { minute: 6e4, hour: 36e5, day: 864e5, week: 6048e5, month: 2629746e3, quarter: 7889238e3, year: 31556952e3 }
  let pick = order[order.length - 1]!
  for (const o of order) if (span / (approx[o[0]] * o[1]) <= Math.max(1, count)) { pick = o; break }
  const [g, step] = pick
  const ticks = ticksAt(domain, g, step, timeZone)
  // a coarse unit can leave fewer than two ticks inside a short span: step down
  if (ticks.length < 2) {
    const i = order.findIndex((o) => o[0] === g && o[1] === step)
    for (let k = i - 1; k >= 0; k--) {
      const [g2, s2] = order[k]!
      const t2 = ticksAt(domain, g2, s2, timeZone)
      if (t2.length >= 2) return { ticks: t2.length > count * 2 ? t2.filter((_, j) => j % Math.ceil(t2.length / Math.max(2, count)) === 0) : t2, g: g2 === 'month' && s2 === 3 ? 'quarter' : g2 === 'week' ? 'day' : g2 }
    }
  }
  const gran: Granularity = g === 'month' && step === 3 ? 'quarter' : g === 'week' ? 'day' : g
  return { ticks, g: gran }
}

function ticksAt(domain: [Date, Date], g: Granularity, step: number, timeZone: string): Date[] {
  const ticks: Date[] = []
  let t = floorTo(domain[0], g, { timeZone })
  if (+t < +domain[0]) t = offsetBy(t, g, 1, { timeZone })
  // align multi-unit steps to round values: every 6 hours from 00:00, every 3 months
  // from January, every 5 years from a multiple of 5
  if (step > 1 && g !== 'week' && g !== 'day') {
    const p = zonedParts(t, timeZone)
    const unit = g === 'minute' ? p.minute : g === 'hour' ? p.hour : g === 'month' ? p.month - 1 : p.year
    const skip = (step - (((unit % step) + step) % step)) % step
    if (skip) t = offsetBy(t, g, skip, { timeZone })
  }
  let guard = 0
  while (+t <= +domain[1] && guard++ < 1000) {
    ticks.push(t)
    t = offsetBy(t, g, step, { timeZone })
  }
  return ticks
}

/** Build the frame for one x/y chart. */
export function cartesian(env: CartesianEnv, spec: CartesianSpec): CartesianFrame {
  const { width: W, height: H, fmt, theme, measure, truncate } = env
  const pad = { top: 0, right: 0, bottom: 0, left: 0, ...spec.pad }
  const xAxis = spec.xAxis ?? 'bottom', yAxis = spec.yAxis ?? 'left'
  const xSpec = applyDomain(spec.x, env.xDomain), ySpec = applyDomain(spec.y, env.yDomain)
  const gridMode = spec.grid ?? (spec.x.type === 'band' || spec.x.type === 'point' ? 'y' : spec.y.type === 'band' || spec.y.type === 'point' ? 'x' : 'y')

  const titleH = SIZES.label + 6
  const top0 = Math.ceil(FS / 2) + 4 + pad.top
  let top = top0
  let left = 8, bottom = 8, right = 10 + pad.right

  const fmtFor = (_s: AnyScale, f: ((v: never) => string) | undefined, g?: Granularity) => (v: unknown): string => {
    if (f) return f(v as never)
    if (v instanceof Date) return fmt.date(v, g ?? 'day')
    if (typeof v === 'number') return fmt.value(v)
    return String(v)
  }

  type Lab = { v: unknown; text: string; full?: string; pos: number }
  let x!: AnyScale, y!: AnyScale
  let xl: { labels: Lab[]; rotate: number; size: number } = { labels: [], rotate: 0, size: 0 }
  let yl: { labels: Lab[]; size: number } = { labels: [], size: 0 }

  const yTicksOf = (s: AnyScale, len: number): { vals: unknown[]; g?: Granularity } => {
    if (isBand(s) || isPoint(s)) return { vals: s.domain() }
    const n = spec.yTickCount ?? Math.max(2, Math.min(10, Math.round(len / 48)))
    if (isTime(s)) { const t = timeTicks(s.domain() as [Date, Date], n, fmt.timeZone); return { vals: t.ticks, g: t.g } }
    return { vals: (s as ScaleLinear<number, number>).ticks(n) }
  }
  const xTicksOf = (s: AnyScale, len: number, guess: number): { vals: unknown[]; g?: Granularity } => {
    if (isBand(s) || isPoint(s)) return { vals: s.domain() }
    const n = spec.xTickCount ?? Math.max(2, Math.min(10, Math.floor(len / (guess + 18))))
    if (isTime(s)) { const t = timeTicks(s.domain() as [Date, Date], n, fmt.timeZone); return { vals: t.ticks, g: t.g } }
    return { vals: (s as ScaleLinear<number, number>).ticks(n) }
  }

  // Band axes thin BEFORE formatting/measuring: at 50k categories, measuring every
  // label was most of the layout time and only ~len/FS of them can ever be shown.
  const bandStep = (s: AnyScale) => (isBand(s) ? s.step() : (s as ScalePoint<string>).step())
  const layoutY = (len: number) => {
    const { vals: all, g } = yTicksOf(y, len)
    const f = fmtFor(y, spec.yFormat, g)
    const band = isBand(y) || isPoint(y)
    const cap = band ? Math.max(40, spec.bandLabelMax ?? W * 0.38) : Infinity
    const step = band ? bandStep(y) : Infinity
    let vals = band && step < FS * 1.4 ? thin(all, Math.ceil((FS * 1.4) / Math.max(step, 0.01))) : all
    // continuous: d3 may return more ticks than asked for on a short axis; keep >= 1.4 lines apart
    if (!band && vals.length > 2) {
      const pitch = Math.abs(posOf(y, vals[1]) - posOf(y, vals[0]))
      if (pitch < FS * 1.4) vals = thin(vals, Math.ceil((FS * 1.4) / Math.max(pitch, 0.01)))
    }
    const labels: Lab[] = vals.map((v) => {
      const full = f(v)
      const text = band ? truncate(full, cap) : full
      return { v, text, full: text !== full ? full : undefined, pos: posOf(y, v) }
    })
    const size = yAxis === 'none' ? 0 : Math.ceil(Math.max(0, ...labels.map((l) => measure(l.text))) + TICK + GAP)
    return { labels, size }
  }
  const layoutX = (len: number, maxBottom: number) => {
    const guess = isTime(x) ? measure('Mmm 2026') : 40
    const { vals: all, g } = xTicksOf(x, len, guess)
    if (xAxis === 'none') return { labels: [] as Lab[], rotate: 0, size: 0 }
    const f = fmtFor(x, spec.xFormat, g)
    const band = isBand(x) || isPoint(x)
    const step = band ? bandStep(x) : Infinity
    const pre = band && step < FS * 1.75 ? Math.ceil((FS * 1.75) / Math.max(step, 0.01)) : 1
    let labels: Lab[] = thin(all, pre).map((v) => ({ v, text: f(v), pos: posOf(x, v) }))
    const widths = labels.map((l) => measure(l.text))
    const maxW = Math.max(0, ...widths)
    if (!band) {
      // continuous: drop ticks until neighbours do not touch
      let k = 1
      while (k < labels.length) {
        const kept = thin(labels, k)
        const ok = kept.every((l, i) => i === 0 || Math.abs(l.pos - kept[i - 1]!.pos) >= (measure(l.text) + measure(kept[i - 1]!.text)) / 2 + 8)
        if (ok) break
        k++
      }
      labels = thin(labels, k)
      return { labels, rotate: 0, size: FS + TICK + GAP + XDROP }
    }
    if (maxW <= step - 6) return { labels, rotate: 0, size: FS + TICK + GAP + XDROP }
    // rotate -45; thin when even rotated labels would collide; truncate to the height budget
    const k = Math.max(1, Math.ceil((FS * 1.75) / Math.max(step * pre, 0.01)))
    labels = thin(labels, k)
    const budget = Math.max(24, maxBottom / Math.SQRT1_2 - FS)
    labels = labels.map((l) => {
      const t = truncate(l.full ?? l.text, budget)
      return { ...l, text: t, full: t !== (l.full ?? l.text) ? l.full ?? l.text : undefined }
    })
    const longest = Math.max(0, ...labels.map((l) => measure(l.text)))
    return { labels, rotate: -45, size: Math.ceil(longest * Math.SQRT1_2 + FS * Math.SQRT1_2 + TICK + GAP) }
  }

  const xTitleH = spec.xTitle && xAxis !== 'none' ? titleH : 0
  const yTitleW = spec.yTitle && yAxis !== 'none' ? titleH : 0
  for (let pass = 0; pass < 3; pass++) {
    const plotH = Math.max(10, H - top - bottom)
    y = makeScale(ySpec, isBand0(ySpec) ? [top, top + plotH] : [top + plotH, top])
    yl = layoutY(plotH)
    const yW = yl.size + yTitleW
    left = Math.max(8, yAxis === 'left' ? yW : 0) + pad.left
    right = Math.max(10, yAxis === 'right' ? yW : 0) + pad.right
    const plotW0 = Math.max(10, W - left - right)
    x = makeScale(xSpec, [left, left + plotW0])
    xl = layoutX(plotW0, H * 0.32)
    // continuous x: first/last labels must fit inside the box
    if (xl.rotate === 0 && xl.labels.length) {
      const first = xl.labels[0]!, last = xl.labels[xl.labels.length - 1]!
      const needL = measure(first.text) / 2 - (first.pos - 0) + 2
      const needR = last.pos + measure(last.text) / 2 + 2 - W
      if (needR > 0) right += needR
      if (needL > 0) left += needL
    } else if (xl.rotate !== 0 && xl.labels.length && xAxis === 'bottom') {
      // bottom labels hang down-left from their tick
      const first = xl.labels[0]!
      // the rotated box reaches left by its length and half its height, both at 45 degrees
      const reach = (measure(first.text) + FS) * Math.SQRT1_2 + 4
      const anchorX = first.pos + (isBand(x) ? x.bandwidth() / 2 : 0)
      if (anchorX - reach < 0) left += reach - anchorX
    } else if (xl.rotate !== 0 && xl.labels.length) {
      // top labels rise up-right from their tick
      const last = xl.labels[xl.labels.length - 1]!
      const reach = last.pos + measure(last.text) * Math.SQRT1_2 + 2 - W
      if (reach > 0) right += reach
    }
    const newTop = xAxis === 'top' ? top0 + xl.size + xTitleH : top0
    const newBottom = (xAxis === 'bottom' ? xl.size + xTitleH : 0) + 6 + pad.bottom
    const settled = Math.abs(newBottom - bottom) < 1 && Math.abs(newTop - top) < 1
    top = newTop
    bottom = newBottom
    if (settled && pass > 0) break
  }
  const plotW = Math.max(10, W - left - right)
  const plotH = Math.max(10, H - top - bottom)
  x = makeScale(xSpec, [left, left + plotW])
  y = makeScale(ySpec, isBand0(ySpec) ? [top, top + plotH] : [top + plotH, top])

  // zoom
  if (spec.zoom && env.zoom && (env.zoom.k !== 1 || env.zoom.x !== 0 || env.zoom.y !== 0)) {
    x = rescale(x, env.zoom.k, env.zoom.x, [left, left + plotW])
    if (env.zoomAxes === 'xy') y = rescale(y, env.zoom.k, env.zoom.y, [top + plotH, top])
  }
  xl = layoutX(plotW, H * 0.32)
  yl = layoutY(plotH)
  const plot: Rect = { x: left, y: top, width: plotW, height: plotH }

  // ----- marks
  const marks: Mark[] = []
  const gx = gridMode === 'x' || gridMode === 'both', gy = gridMode === 'y' || gridMode === 'both'
  const inX = (p: number) => p >= plot.x - 0.5 && p <= plot.x + plot.width + 0.5
  const inY = (p: number) => p >= plot.y - 0.5 && p <= plot.y + plot.height + 0.5
  const xBand = isBand(x) || isPoint(x), yBand = isBand(y) || isPoint(y)
  const xc = (l: Lab) => l.pos + (isBand(x) ? x.bandwidth() / 2 : 0)
  const yc = (l: Lab) => l.pos + (isBand(y) ? y.bandwidth() / 2 : 0)
  if (gy && !yBand) for (const l of yl.labels) if (inY(l.pos)) marks.push({ type: 'line', layer: 'grid', key: `gy:${String(l.v)}`, x1: plot.x, x2: plot.x + plot.width, y1: l.pos, y2: l.pos, stroke: theme.grid, strokeWidth: 1, inert: true })
  if (gx && !xBand) for (const l of xl.labels) if (inX(l.pos)) marks.push({ type: 'line', layer: 'grid', key: `gx:${String(l.v)}`, y1: plot.y, y2: plot.y + plot.height, x1: l.pos, x2: l.pos, stroke: theme.grid, strokeWidth: 1, inert: true })
  // zero line on a value axis that spans 0
  if (!yBand && !isTime(y)) { const d = y.domain() as number[]; if (Math.min(...d) < 0 && Math.max(...d) > 0) { const p = (y as ScaleLinear<number, number>)(0); marks.push({ type: 'line', layer: 'grid', key: 'zero-y', x1: plot.x, x2: plot.x + plot.width, y1: p, y2: p, stroke: theme.ruleStrong, strokeWidth: 1, inert: true }) } }
  if (!xBand && !isTime(x)) { const d = x.domain() as number[]; if (Math.min(...d) < 0 && Math.max(...d) > 0) { const p = (x as ScaleLinear<number, number>)(0); marks.push({ type: 'line', layer: 'grid', key: 'zero-x', y1: plot.y, y2: plot.y + plot.height, x1: p, x2: p, stroke: theme.ruleStrong, strokeWidth: 1, inert: true }) } }

  const label = (key: string, l: Lab, x0: number, y0: number, anchor: TextMark['anchor'], baseline: TextMark['baseline'], rotate?: number): TextMark => ({
    type: 'text', layer: 'axes', key, x: x0, y: y0, text: l.text, full: l.full, anchor, baseline, rotate, size: FS, fill: theme.inkSecondary, className: l.full ? 'oc-trunc' : undefined,
  })
  if (xAxis !== 'none') {
    const yb = xAxis === 'bottom' ? plot.y + plot.height : plot.y
    const dir = xAxis === 'bottom' ? 1 : -1
    marks.push({ type: 'line', layer: 'axes', key: 'x-domain', x1: plot.x, x2: plot.x + plot.width, y1: yb, y2: yb, stroke: theme.axis, strokeWidth: 1, inert: true })
    for (const l of xl.labels) {
      const cx = xc(l)
      if (!inX(cx)) continue
      marks.push({ type: 'line', layer: 'axes', key: `xt:${String(l.v)}`, x1: cx, x2: cx, y1: yb, y2: yb + dir * TICK, stroke: theme.axis, strokeWidth: 1, inert: true })
      if (xl.rotate) marks.push(label(`xl:${String(l.v)}`, l, cx, yb + dir * (TICK + GAP), xAxis === 'bottom' ? 'end' : 'start', 'central', xl.rotate))
      else marks.push(label(`xl:${String(l.v)}`, l, cx, yb + dir * (TICK + GAP + (xAxis === 'bottom' ? XDROP : 0)), 'middle', xAxis === 'bottom' ? 'hanging' : 'alphabetic'))
    }
    if (spec.xTitle) {
      const t = truncate(spec.xTitle, plot.width, SIZES.label)
      marks.push({ type: 'text', layer: 'axes', key: 'x-title', x: plot.x + plot.width / 2, y: xAxis === 'bottom' ? H - 4 : 4 + SIZES.label, text: t, full: t !== spec.xTitle ? spec.xTitle : undefined, anchor: 'middle', baseline: xAxis === 'bottom' ? 'alphabetic' : 'auto', size: SIZES.label, fill: theme.muted })
    }
  }
  if (yAxis !== 'none') {
    const xb = yAxis === 'left' ? plot.x : plot.x + plot.width
    const dir = yAxis === 'left' ? -1 : 1
    if (yBand) marks.push({ type: 'line', layer: 'axes', key: 'y-domain', x1: xb, x2: xb, y1: plot.y, y2: plot.y + plot.height, stroke: theme.axis, strokeWidth: 1, inert: true })
    for (const l of yl.labels) {
      const cy = yc(l)
      if (!inY(cy)) continue
      if (yBand) marks.push({ type: 'line', layer: 'axes', key: `yt:${String(l.v)}`, x1: xb, x2: xb + dir * TICK, y1: cy, y2: cy, stroke: theme.axis, strokeWidth: 1, inert: true })
      marks.push(label(`yl:${String(l.v)}`, l, xb + dir * (TICK + GAP), cy, yAxis === 'left' ? 'end' : 'start', 'central'))
    }
    if (spec.yTitle) {
      const t = truncate(spec.yTitle, plot.height, SIZES.label)
      const tx = yAxis === 'left' ? 4 + SIZES.label / 2 : W - 4 - SIZES.label / 2
      marks.push({ type: 'text', layer: 'axes', key: 'y-title', x: tx, y: plot.y + plot.height / 2, text: t, full: t !== spec.yTitle ? spec.yTitle : undefined, anchor: 'middle', baseline: 'central', rotate: -90, size: SIZES.label, fill: theme.muted })
    }
  }

  const fx = x, fy = y
  return {
    x, y, plot, marks,
    xTicks: xl.labels.map((l) => l.v),
    yTicks: yl.labels.map((l) => l.v),
    px: (v) => posOf(fx, v),
    py: (v) => posOf(fy, v),
    bw: isBand(x) ? x.bandwidth() : 0,
    bh: isBand(y) ? y.bandwidth() : 0,
    invertX: (p) => invert(fx, p),
    invertY: (p) => invert(fy, p),
  }
}

function isBand0(s: ScaleSpec) {
  return s.type === 'band' || s.type === 'point'
}
function applyDomain(s: ScaleSpec, d: [unknown, unknown] | undefined): ScaleSpec {
  if (!d) return s
  if (s.type === 'linear' || s.type === 'symlog' || s.type === 'log') {
    const [a, b] = d
    if (typeof a === 'number' && typeof b === 'number') return { ...s, domain: [a, b] }
  }
  if (s.type === 'time') {
    const [a, b] = d
    const da = a instanceof Date ? a : new Date(a as string), db = b instanceof Date ? b : new Date(b as string)
    if (!Number.isNaN(+da) && !Number.isNaN(+db)) return { ...s, domain: [da, db] }
  }
  return s
}
function thin<T>(arr: T[], k: number): T[] {
  return k <= 1 ? arr : arr.filter((_, i) => i % k === 0)
}
/** Pixel position of a value on any scale (band/point: band start). */
export function posOf(s: AnyScale, v: unknown): number {
  const p = (s as (v: unknown) => number | undefined)(v as never)
  return p === undefined || Number.isNaN(p) ? NaN : p
}
function invert(s: AnyScale, p: number): unknown {
  if (isBand(s) || isPoint(s)) {
    const dom = s.domain()
    const step = s.step()
    const r0 = Math.min(...s.range())
    const i = Math.max(0, Math.min(dom.length - 1, Math.floor((p - r0) / Math.max(step, 1e-9))))
    return dom[i]
  }
  return (s as ScaleLinear<number, number>).invert(p)
}
function rescale(s: AnyScale, k: number, t: number, range: [number, number]): AnyScale {
  if (isBand(s) || isPoint(s)) {
    const c = s.copy()
    c.range([range[0] * k + t, range[1] * k + t])
    return c
  }
  const c = (s as ScaleLinear<number, number>).copy()
  const [r0, r1] = range
  const d0 = (s as ScaleLinear<number, number>).invert((r0 - t) / k)
  const d1 = (s as ScaleLinear<number, number>).invert((r1 - t) / k)
  c.domain([d0, d1])
  return c as AnyScale
}
