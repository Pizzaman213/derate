import type { HistoryEnvelope } from '../../api/types'
import type { TelemetryPoint } from '../../state/useTelemetry'

// The arithmetic behind every chart, kept out of React and out of uPlot so
// that `chart.check.mjs` can run it in node. There is no UI test runner here,
// so the rule is the same as layout.ts and routes.ts: the part that can be
// wrong silently is the part that gets verified.
//
// Three things in this file are the reason it exists at all:
//
//   1. `align` NEVER interpolates. A timestamp one series has and another
//      does not yields a null in the second, and uPlot lifts the pen on a
//      null. This is the same promise `useTelemetry.push` makes about the
//      live ring and `history.nodeSeries` makes about the archive, and it now
//      survives being handed to a third-party renderer.
//
//   2. `yRange` refuses to auto-fit a bounded quantity. The old hand-rolled
//      chart scaled every series to its own min..max, so a memory series that
//      sat between 61% and 63% filled the full height of the box and read as
//      an event. A percentage is drawn against 0..100 because that is what a
//      percentage MEANS; a rate is drawn from zero because the ratio between
//      two moments is the thing a rate chart is read for.
//
//   3. `gapSpans` turns the envelope's known holes into geometry. A flat line
//      has two causes -- the machine was idle, or the rows were never
//      collected -- and until now the difference was a paragraph underneath
//      rather than a mark on the drawing.

/** How a series' vertical axis should be chosen. Inferred from the unit by
 *  `kindFor` so the existing call sites get the fix without being rewritten,
 *  overridable per chart where the unit is not enough. */
export type YKind = 'percent' | 'rate' | 'span'

export interface Bounds {
  mn: number
  mx: number
  /** Real samples behind the line, not points in the window: a window that has
   *  been running ten seconds says ten, and a window that is half holes says
   *  how many rows actually arrived. */
  n: number
  last: number | null
}

export interface Range {
  min: number
  max: number
}

/** uPlot's `AlignedData`: one ascending x column, then one column per series,
 *  every column the same length. */
export type Columns = [number[], ...(number | null)[][]]

export interface GapSpan {
  from: number
  to: number
  reason: string
}

function real(v: number | null | undefined): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

/** Min, max, count and latest across every REAL value. Nulls are skipped here
 *  and only here -- they are still points, they simply have no magnitude to
 *  contribute to an extent. Returns null when nothing was ever measured, which
 *  the caller renders as "no samples yet" rather than as a flat line at zero. */
export function bounds(points: TelemetryPoint[]): Bounds | null {
  let mn = Infinity
  let mx = -Infinity
  let n = 0
  let last: number | null = null
  for (const p of points) {
    const v = real(p.v)
    if (v == null) continue
    if (v < mn) mn = v
    if (v > mx) mx = v
    n += 1
    last = v
  }
  if (n === 0) return null
  return { mn, mx, n, last }
}

/** Merges N series onto one strictly-ascending timestamp column.
 *
 *  uPlot requires a single shared, ascending x and will misdraw rather than
 *  complain if handed anything else, so the ascent is produced here rather
 *  than assumed of the caller. The archive returns rows in whatever order the
 *  query gave them and the live ring appends, so neither is trusted.
 *
 *  A timestamp that one series carries and another does not becomes a null in
 *  the second -- never a carried-forward value and never an interpolation.
 *  "Sampled and had nothing to say" and "never sampled" both draw as the hole
 *  they are; which of the two it was is the envelope's job to say, not the
 *  line's. */
export function align(series: TelemetryPoint[][]): Columns {
  const stamps = new Set<number>()
  const byTime: Map<number, number | null>[] = []
  for (const s of series) {
    const m = new Map<number, number | null>()
    for (const p of s) {
      if (!Number.isFinite(p.t)) continue
      stamps.add(p.t)
      // Last write wins on a duplicate timestamp. Two rows at one instant is
      // not a thing the archive should produce, but a duplicated x makes uPlot
      // draw backwards, so it is collapsed rather than passed through.
      m.set(p.t, real(p.v))
    }
    byTime.push(m)
  }
  const xs = [...stamps].sort((a, b) => a - b)
  const cols = byTime.map((m) => xs.map((t) => (m.has(t) ? m.get(t)! : null)))
  return [xs, ...cols] as Columns
}

/** The default axis rule for a unit. `%` is bounded by definition, `°C` has no
 *  meaningful zero (a chart of core temperature drawn from 0 is 60% empty),
 *  and everything else in this product -- W, tok/s, ms, reqs -- is a rate or a
 *  count, where zero is the only honest floor. */
export function kindFor(unit: string): YKind {
  if (unit === '%') return 'percent'
  if (unit === '°C') return 'span'
  return 'rate'
}

/** The narrowest range a `span` axis is allowed to show, in that axis' own
 *  units. Without a floor, a thermally boring hour of 47.0-47.4 degrees is
 *  stretched to the full height of the box and reads as a thermal event. */
const SPAN_FLOOR = 10

/** Headroom above the top sample so the peak is not welded to the frame. */
const PAD = 0.08

export function yRange(kind: YKind, b: Bounds | null): Range {
  if (kind === 'percent') return { min: 0, max: 100 }
  if (!b) return { min: 0, max: 1 }

  if (kind === 'rate') {
    // A rate that has only ever been zero still needs a height, or uPlot
    // divides by a zero-span scale. One unit, so the flat line sits on the
    // floor where it belongs rather than through the middle of the box.
    if (b.mx <= 0) return { min: 0, max: 1 }
    return { min: 0, max: b.mx * (1 + PAD) }
  }

  const span = b.mx - b.mn
  if (span >= SPAN_FLOOR) {
    const pad = span * PAD
    return { min: b.mn - pad, max: b.mx + pad }
  }
  const mid = (b.mn + b.mx) / 2
  return { min: mid - SPAN_FLOOR / 2, max: mid + SPAN_FLOOR / 2 }
}

/** The envelope's known holes, clipped to what the chart is actually drawing.
 *
 *  A gap wholly outside the window is dropped rather than clamped to its edge:
 *  a marker hard against the frame reads as "the data stops here", which is
 *  the claim `truncated` makes and this one does not. */
export function gapSpans(
  env: HistoryEnvelope | null | undefined,
  x0: number,
  x1: number,
): GapSpan[] {
  if (!env || !env.gaps || x1 <= x0) return []
  const out: GapSpan[] = []
  for (const g of env.gaps) {
    const from = Math.max(g.from_ts, x0)
    const to = Math.min(g.to_ts, x1)
    if (!(to > from)) continue
    out.push({ from, to, reason: g.reason })
  }
  return out.sort((a, b) => a.from - b.from)
}

/** Which gap, if any, an instant falls inside. Drives the hover readout: over
 *  a hole the chart says why the rows are missing instead of showing the
 *  nearest value from the far side of it. */
export function gapAt(spans: GapSpan[], t: number): GapSpan | null {
  for (const s of spans) {
    if (t >= s.from && t <= s.to) return s
  }
  return null
}

/** A cheap content signature for a series, used as a memo key.
 *
 *  The arrays here are rebuilt on every render -- `nodeSeries` and `depSeries`
 *  map fresh over their rows each time -- so array identity says nothing about
 *  whether the DATA moved. Realigning on identity would rebuild the columns
 *  and push a full canvas redraw on every mouse move across the crosshair.
 *
 *  What it assumes, and why that holds here: a series either grows at the end
 *  (the live ring appends and trims by time) or is replaced wholesale (a
 *  history refetch returns a new window). It does NOT detect a value edited in
 *  the middle of an otherwise identical window, because nothing in this
 *  product does that -- the archive is append-only and the ring is a queue.
 *  `chart.check.mjs` pins the cases that must change the key. */
export function seriesKey(points: TelemetryPoint[]): string {
  const n = points.length
  if (n === 0) return '0'
  const first = points[0]!
  const last = points[n - 1]!
  return `${n}:${first.t}:${first.v ?? 'x'}:${last.t}:${last.v ?? 'x'}`
}
