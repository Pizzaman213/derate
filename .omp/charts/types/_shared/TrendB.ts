import { df } from '../../core/format'
// Helpers shared by the trend charts (sparkline, slope, bump, horizon, candlestick,
// ohlc, calendar-heatmap, cycle-plot): path builders, label nudging, date spans.

import { z } from 'zod'
import { fields, phrase, type DateGranularity, type DrawContext, type DrawResult, type Formatters, type Mark, type TableModel } from '../../core'

export type Pt = readonly [number, number]

const r2 = (v: number) => Math.round(v * 100) / 100

/** Polyline through points; a null breaks the line (gap). */
export function linePath(points: Array<Pt | null>): string {
  let d = '', pen = false
  for (const p of points) {
    if (!p) { pen = false; continue }
    d += `${pen ? 'L' : 'M'}${r2(p[0])},${r2(p[1])}`
    pen = true
  }
  return d
}

/** Smooth "bump" curve: cubic segments with horizontal tangents at every point; null breaks it. */
export function bumpPath(points: Array<Pt | null>): string {
  let d = ''
  let prev: Pt | null = null
  for (const p of points) {
    if (!p) { prev = null; continue }
    if (!prev) d += `M${r2(p[0])},${r2(p[1])}`
    else {
      const mx = (prev[0] + p[0]) / 2
      d += `C${r2(mx)},${r2(prev[1])} ${r2(mx)},${r2(p[1])} ${r2(p[0])},${r2(p[1])}`
    }
    prev = p
  }
  return d
}

/**
 * Move label positions apart so neighbours are at least `gap` apart, staying inside
 * [lo, hi] and keeping their order. Overlapping labels are merged into clusters centred
 * on their wanted positions. Returns null when n labels cannot fit at all.
 */
export function nudge(want: number[], gap: number, lo: number, hi: number): number[] | null {
  const n = want.length
  if (!n) return []
  if ((n - 1) * gap > hi - lo + 1e-6) return null
  const order = want.map((_, i) => i).sort((a, b) => want[a]! - want[b]! || a - b)
  interface G { first: number; n: number; sum: number }
  const groups: G[] = []
  const top = (g: G) => Math.min(hi - (g.n - 1) * gap, Math.max(lo, g.sum / g.n))
  order.forEach((idx, j) => {
    groups.push({ first: j, n: 1, sum: want[idx]! })
    while (groups.length > 1) {
      const b = groups[groups.length - 1]!, a = groups[groups.length - 2]!
      if (top(a) + a.n * gap <= top(b) + 1e-9) break
      a.sum += b.sum - b.n * a.n * gap
      a.n += b.n
      groups.pop()
    }
  })
  const out = new Array<number>(n)
  for (const g of groups) {
    const t = top(g)
    for (let k = 0; k < g.n; k++) out[order[g.first + k]!] = t + k * gap
  }
  return out
}

/**
 * Pick which labels to show and where: the highest-priority labels that fit at
 * `gap` spacing in [lo, hi], nudged apart. Returns position by index; dropped
 * labels are absent.
 */
export function placeLabels(want: number[], priority: number[], gap: number, lo: number, hi: number): Map<number, number> {
  const cap = Math.max(0, Math.floor((hi - lo) / Math.max(gap, 1e-6)) + 1)
  let chosen = want.map((_, i) => i)
  if (chosen.length > cap) chosen = chosen.sort((a, b) => priority[b]! - priority[a]! || a - b).slice(0, cap)
  const pos = nudge(chosen.map((i) => want[i]!), gap, lo, hi) ?? chosen.map((i) => want[i]!)
  return new Map(chosen.map((i, k) => [i, pos[k]!]))
}

/** Date label with the year included ("Mar 3, 2026"). */
export function dayLabel(d: Date, locale: string, timeZone: string): string {
  return dayFormat(locale, timeZone).format(d)
}

/** The formatter behind `dayLabel`: look it up once per draw, not once per mark. */
export function dayFormat(locale: string, timeZone: string): Intl.DateTimeFormat {
  return df(locale, { timeZone, year: 'numeric', month: 'short', day: 'numeric' })
}

/** Signed change as text: "+12" / "−3". */
export function signed(v: number, f: (n: number) => string): string {
  return v > 0 ? `+${f(v)}` : v < 0 ? `−${f(-v)}` : f(0)
}

/**
 * Memoises `make(data, options, ...)` on the identity of the data and options. One render
 * draws twice (again once the legend and note are measured), then describes and tables,
 * all with the same validated data and parsed options -- and core re-validates into new
 * objects on every update, so identity is exactly the right key. Anything derived from
 * those two alone (grouping, sorting, time-zone reads, formatted text) belongs in here.
 */
export function perRender<D extends object, O, A extends unknown[], T>(make: (data: D, o: O, ...rest: A) => T): (data: D, o: O, ...rest: A) => T {
  const cache = new WeakMap<D, { o: O; v: T }>()
  return (data, o, ...rest) => {
    const hit = cache.get(data)
    if (hit && hit.o === o) return hit.v
    const v = make(data, o, ...rest)
    cache.set(data, { o, v })
    return v
  }
}

/** Formats one point's date at the precision its sampling interval calls for. */
export function pointDateFormatter(dates: Date[], fmt: { locale: string; timeZone: string; date: (d: Date, g: DateGranularity) => string; dateTime: (d: Date) => string }): (d: Date) => string {
  const ts = dates.map((d) => +d).sort((a, b) => a - b)
  const gaps = ts.slice(1).map((t, i) => t - ts[i]!).filter((g) => g > 0).sort((a, b) => a - b)
  const step = gaps[Math.floor(gaps.length / 2)] ?? 864e5
  if (step >= 360 * 864e5) return (d) => fmt.date(d, 'year')
  if (step >= 28 * 864e5) return (d) => fmt.date(d, 'month')
  if (step >= 864e5) { const f = dayFormat(fmt.locale, fmt.timeZone); return (d) => f.format(d) }
  return (d) => fmt.dateTime(d)
}
// ---------------------------------------------------------------- OHLC (candlestick, ohlc)

/** Price rows; high must cover open and close, low must sit under them. */
export const ohlcInput = fields.rows({
  date: fields.date('session date'),
  open: fields.num('opening price'),
  high: fields.num('highest price; at least max(open, close)'),
  low: fields.num('lowest price; at most min(open, close)'),
  close: fields.num('closing price'),
  volume: fields.numOrNull('traded volume; drawn as bars under the prices'),
}, 'rows of { date, open, high, low, close, volume? }').superRefine((rows, ctx) => {
  rows.forEach((r, i) => {
    const top = Math.max(r.open, r.close), bottom = Math.min(r.open, r.close)
    if (r.high < top) ctx.addIssue({ code: 'custom', path: [i, 'high'], message: `must be at least max(open, close) = ${top}, got ${r.high}` })
    if (r.low > bottom) ctx.addIssue({ code: 'custom', path: [i, 'low'], message: `must be at most min(open, close) = ${bottom}, got ${r.low}` })
  })
})

export const ohlcOptions = z.object({
  volume: z.boolean().default(true).describe('volume bars under the prices (when the data has volume)'),
  yTitle: z.string().optional(),
})

export type OhlcRow = z.output<typeof ohlcInput>[number]
export type OhlcOptions = z.output<typeof ohlcOptions>

/** Rows in time order (stable for equal dates). */
export function ohlcSorted(data: OhlcRow[]): OhlcRow[] {
  return data.map((r, i) => ({ r, i })).sort((a, b) => +a.r.date - +b.r.date || a.i - b.i).map((x) => x.r)
}

export interface OhlcGeometry {
  row: OhlcRow
  key: string
  x: number
  /** Body / tick width. */
  w: number
  open: number
  high: number
  low: number
  close: number
  up: boolean
  color: string
  label: string
  tooltip: NonNullable<Mark['tooltip']>
}

/** Per session, in time order: its key and the text its mark carries. */
interface OhlcSession { row: OhlcRow; key: string; up: boolean; label: string; tooltipRows: Array<[string, string]>; when: string }
interface OhlcModel { rows: OhlcRow[]; fd: (d: Date) => string; sessions: OhlcSession[]; lo: number; hi: number; maxVol: number; ups: number; dupes: number }

/** Sorting, keys and every session's label and tooltip, once per render rather than per pass. */
const ohlcModel = perRender((data: OhlcRow[], _o: OhlcOptions, fmt: Formatters): OhlcModel => {
  const rows = ohlcSorted(data)
  const fd = pointDateFormatter(rows.map((r) => r.date), fmt)
  let lo = Infinity, hi = -Infinity, maxVol = 0, ups = 0
  const seen = new Map<number, number>()
  const sessions = rows.map((r): OhlcSession => {
    lo = Math.min(lo, r.low); hi = Math.max(hi, r.high)
    if (r.volume !== null && r.volume > 0) maxVol = Math.max(maxVol, r.volume)
    const up = r.close >= r.open
    if (up) ups++
    const t = +r.date
    const k = seen.get(t) ?? 0
    seen.set(t, k + 1)
    const when = fd(r.date)
    const change = r.close - r.open
    const open = fmt.value(r.open), high = fmt.value(r.high), low = fmt.value(r.low), close = fmt.value(r.close)
    const tooltipRows: Array<[string, string]> = [['Open', open], ['High', high], ['Low', low], ['Close', close], ['Change', `${signed(change, fmt.value)} (${r.open ? fmt.pct(change / Math.abs(r.open), 1) : '–'})`]]
    if (r.volume !== null) tooltipRows.push(['Volume', fmt.si(r.volume)])
    return { row: r, key: `${t}${k ? `#${k}` : ''}`, up, when, tooltipRows, label: `${when}: open ${open}, high ${high}, low ${low}, close ${close}${up ? '' : ' (down)'}` }
  })
  return { rows, fd, sessions, lo, hi, maxVol, ups, dupes: rows.length - seen.size }
})

/**
 * The shared frame for price charts: a zoomable time axis, a price axis whose lower
 * fifth holds the volume bars, the up/down legend and notes. `mark` turns one session's
 * pixel geometry into its data mark.
 */
export function drawOhlc(ctx: DrawContext<OhlcRow[], OhlcOptions>, mark: (g: OhlcGeometry) => Mark): DrawResult {
  const { theme, fmt, options: o, hidden } = ctx
  const model = ohlcModel(ctx.data, o, fmt)
  const { rows, sessions, maxVol, ups, dupes } = model
  let { lo, hi } = model
  const showVol = o.volume && maxVol > 0
  if (lo === hi) { lo -= Math.abs(lo) * 0.05 || 1; hi += Math.abs(hi) * 0.05 || 1 }
  const pad = (hi - lo) * 0.04
  const yLo = showVol ? lo - (hi - lo) * 0.3 : lo - pad
  const t0 = +rows[0]!.date, t1 = +rows[rows.length - 1]!.date
  const half = rows.length > 1 ? (t1 - t0) / (rows.length - 1) / 2 : 864e5 / 2
  const f = ctx.cartesian({
    x: { type: 'time', domain: [new Date(t0 - half), new Date(t1 + half)] },
    y: { type: 'linear', domain: [yLo, hi + pad], zero: false },
    yTitle: o.yTitle,
    // core picks a coarser step than the span holds on narrow plots (zero ticks); ask for at least 4
    xTickCount: Math.max(4, Math.floor(ctx.width / 110)),
    zoom: true,
  })
  // no price ticks inside the volume strip
  const priceFloor = f.py(lo) + 1
  const marks: Mark[] = f.marks.filter((m) => !(m.key && /^(gy|yl):/.test(m.key) && ((m.type === 'line' && m.y1 > priceFloor) || (m.type === 'text' && m.y > priceFloor))))
  const xs = rows.map((r) => f.px(r.date))
  let step = Infinity
  for (let i = 1; i < xs.length; i++) if (xs[i]! - xs[i - 1]! > 0.01) step = Math.min(step, xs[i]! - xs[i - 1]!)
  if (!Number.isFinite(step)) step = f.plot.width / 3
  const w = Math.max(1, Math.min(24, step * 0.7))
  const volH = f.plot.height * 0.18, volBase = f.plot.y + f.plot.height
  const volFill = { up: theme.mix(theme.positive, theme.surface, 0.55), down: theme.mix(theme.negative, theme.surface, 0.55) }
  sessions.forEach((s, i) => {
    const r = s.row
    const series = s.up ? 'up' : 'down'
    const color = s.up ? theme.positive : theme.negative
    if (showVol && r.volume !== null && r.volume > 0 && !hidden.has(series)) {
      const vh = Math.max(1, (r.volume / maxVol) * volH)
      marks.push({ type: 'rect', key: `vol:${s.key}`, role: 'volume', x: xs[i]! - w / 2, y: volBase - vh, width: w, height: vh, fill: volFill[series], inert: true })
    }
    const m = mark({
      row: r, key: s.key, x: xs[i]!, w, open: f.py(r.open), high: f.py(r.high), low: f.py(r.low), close: f.py(r.close), up: s.up, color,
      label: s.label,
      tooltip: { title: s.when, rows: s.tooltipRows, color },
    })
    m.series = series
    marks.push(m)
  })
  return {
    marks,
    plot: f.plot,
    clip: f.plot,
    invertX: f.invertX,
    legend: ups > 0 && ups < rows.length ? { items: [{ key: 'up', label: 'Up (close ≥ open)', color: theme.positive, shape: 'square' }, { key: 'down', label: 'Down', color: theme.negative, shape: 'square' }] } : undefined,
    note: dupes ? `${dupes} repeated dates drawn side by side at the same time` : undefined,
  }
}

/** "Candlestick chart, 20 sessions, Jan 2, 2026 to Jan 29, 2026, opened 100, closed 112 (+12%), high 118 on …, low 95 on …" */
export function describeOhlc(data: OhlcRow[], o: OhlcOptions, fmt: Formatters, name: string): string {
  const { rows, fd } = ohlcModel(data, o, fmt)
  const first = rows[0]!, last = rows[rows.length - 1]!
  let hi = first, lo = first
  for (const r of rows) { if (r.high > hi.high) hi = r; if (r.low < lo.low) lo = r }
  const change = last.close - first.open
  const pct = first.open ? ` (${change >= 0 ? '+' : '−'}${fmt.pct(Math.abs(change / first.open), 1)})` : ''
  const span = rows.length > 1 ? `${fd(first.date)} to ${fd(last.date)}` : fd(first.date)
  return `${name}, ${phrase.count(rows.length, 'session')}, ${span}, opened ${fmt.value(first.open)}, closed ${fmt.value(last.close)}${pct}, high ${fmt.value(hi.high)} on ${fd(hi.date)}, low ${fmt.value(lo.low)} on ${fd(lo.date)}`
}

export function tableOhlc(data: OhlcRow[], o: OhlcOptions, fmt: Formatters): TableModel {
  const { sessions } = ohlcModel(data, o, fmt)
  return { columns: ['Date', 'Open', 'High', 'Low', 'Close', 'Volume'], rows: sessions.map(({ row: r, when }) => [when, r.open, r.high, r.low, r.close, r.volume]) }
}
