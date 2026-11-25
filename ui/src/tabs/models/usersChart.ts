/** The "How many users" chart's data: `POST /api/plan/concurrency` series as
 *  the chart pack's `multi-line` long rows, a dashed marker at each series'
 *  KV wall, and a benchmark's measured points as a dots-only series.
 *
 *  y is tok/s PER USER -- the bandwidth ceiling at that many users, the same
 *  number the Verdict card shows at one. Pure, so `usersChart.check.mjs`
 *  can pin it. */

import type { BenchRecord, ConcurrencyResponse } from '../../api/types'

export interface UsersChartRow {
  x: number
  series: string
  y: number | null
}

export interface UsersChartData {
  rows: UsersChartRow[]
  verticalMarkers: { x: number; series: string; label: string }[]
  pointsOnly: string[]
}

export const MEASURED_SERIES = 'measured'

/** Where the operator's own concurrency sits, drawn as a marker on the
 *  picked series. */
export interface SettingMarker {
  x: number
  series: string
}

export const settingLabel = (seqs: number) => `your setting: ${seqs}`

/** A context for a slider label, a series name or a sentence: "M" from
 *  1,048,576 tokens up, else "k". Whole when it is one, else one decimal:
 *  6656 is "6.5k", where rounding said "7k" -- a context that is not the one
 *  the slider set. */
export const ctxLabel = (n: number) => {
  if (n < 1024) return String(n)
  const [unit, size] = n >= 1048576 ? ['M', 1048576] : ['k', 1024]
  return n % size === 0 ? `${n / size}${unit}` : `${(n / size).toFixed(1)}${unit}`
}

/** Markers drawn at one x are drawn on top of each other, so their labels
 *  are joined into the first one there instead. */
function addMarker(markers: UsersChartData['verticalMarkers'], m: UsersChartData['verticalMarkers'][number]) {
  const same = markers.find((o) => o.x === m.x)
  if (same) same.label = `${same.label} · ${m.label}`
  else markers.push(m)
}

/** `names[i]` labels `resp.series[i]`; a missing name falls back to the
 *  series' context in k. */
export function toChartRows(
  resp: ConcurrencyResponse,
  names: string[],
  bench: BenchRecord | null,
  setting: SettingMarker | null = null,
): UsersChartData {
  const rows: UsersChartRow[] = []
  const verticalMarkers: UsersChartData['verticalMarkers'] = []
  resp.series.forEach((s, i) => {
    const series = names[i] ?? ctxLabel(s.context)
    for (const p of s.points) rows.push({ x: p.users, series, y: p.ceiling_tps_each })
    if (s.wall_users > 0) {
      addMarker(verticalMarkers, { x: s.wall_users, series, label: `${series}: KV full at ${s.wall_users}` })
    }
  })
  if (setting) addMarker(verticalMarkers, { x: setting.x, series: setting.series, label: settingLabel(setting.x) })
  const pointsOnly: string[] = []
  const measured = (bench?.points ?? []).filter((p) => p.tps_each_mean != null)
  if (measured.length) {
    pointsOnly.push(MEASURED_SERIES)
    for (const p of measured) rows.push({ x: p.users, series: MEASURED_SERIES, y: p.tps_each_mean })
  }
  return { rows, verticalMarkers, pointsOnly }
}

/** The First token tab: y is `ttft_floor_s`, seconds, a floor. A null floor
 *  (past the wall, or no compute record) stays null so the pen lifts. The
 *  benchmark's median first token, in seconds, is the dots series. */
export function toTtftRows(
  resp: ConcurrencyResponse,
  names: string[],
  bench: BenchRecord | null,
): UsersChartData {
  const rows: UsersChartRow[] = []
  const verticalMarkers: UsersChartData['verticalMarkers'] = []
  resp.series.forEach((s, i) => {
    const series = names[i] ?? ctxLabel(s.context)
    for (const p of s.points) rows.push({ x: p.users, series, y: p.ttft_floor_s })
    if (s.wall_users > 0) {
      addMarker(verticalMarkers, { x: s.wall_users, series, label: `${series}: KV full at ${s.wall_users}` })
    }
  })
  const pointsOnly: string[] = []
  const measured = (bench?.points ?? []).filter((p) => p.ttft_ms_p50 != null)
  if (measured.length) {
    pointsOnly.push(MEASURED_SERIES)
    for (const p of measured) rows.push({ x: p.users, series: MEASURED_SERIES, y: (p.ttft_ms_p50 as number) / 1000 })
  }
  return { rows, verticalMarkers, pointsOnly }
}

/** The largest context any control here offers when the model names no window. */
const MAX_CONTEXT = 2097152

/** The contexts people ask for: 8k, then 32k doubling to `MAX_CONTEXT`. */
const CONTEXT_PRESETS = [8192, 32768, 65536, 131072, 262144, 524288, 1048576, MAX_CONTEXT]

/** How far apart two sizes are in doublings, so 16k is as near 8k as 4k is. */
const doublings = (a: number, b: number) => Math.abs(Math.log2(a / b))

/** The presets within the model's native window, and nothing else: the
 *  candidates `comparedContexts` picks the chart's series from. */
export function presetContexts(native: number | null): number[] {
  const cap = native ?? Infinity
  return CONTEXT_PRESETS.filter((c) => c <= cap)
}

/** The contexts the By context and First token tabs draw: the server takes at
 *  most four, so the one in use plus the three presets nearest it (in
 *  doublings), sorted. */
export function comparedContexts(current: number | null, presets: number[]): number[] {
  if (current == null) return presets.slice(0, 4)
  const near = [...new Set([current, ...presets])].sort((a, b) => doublings(a, current) - doublings(b, current) || a - b)
  return near.slice(0, 4).sort((a, b) => a - b)
}

/** The context slider's stops: 1k, then each power of two and the halfway
 *  point above it (2k, 3k, 4k, 6k, 8k, 12k ...), up to the model's window,
 *  which is always the last stop even when it is not on that ladder. */
export function contextStops(native: number | null): number[] {
  const cap = native ?? MAX_CONTEXT
  const out: number[] = []
  for (let p = 1024; p < cap; p *= 2) {
    out.push(p)
    if (p * 1.5 < cap) out.push(p * 1.5)
  }
  out.push(cap)
  return out
}

/** The users slider's stops: every count to 16, then coarser. 256 is where
 *  this slider stops offering, a UI choice; the plan itself takes any count. */
export const USER_STOPS = [
  1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 20, 24, 32, 40, 48, 64, 96, 128, 192, 256,
]

/** The index of the stop a value sits on, or of the nearest one in doublings
 *  when it is between two (a planner-picked context rarely lands on a stop).
 *  A value of 0 or less has no distance and sits on the first stop. */
export function nearestStop(stops: number[], value: number): number {
  if (!(value > 0)) return 0
  let best = 0
  for (let i = 1; i < stops.length; i++) {
    if (doublings(stops[i]!, value) < doublings(stops[best]!, value)) best = i
  }
  return best
}

/** `stops` with `value` inserted in order, so a planner pick between two stops
 *  is a stop of its own: the thumb sits on it exactly and one arrow press goes
 *  to the stop next to it rather than past it. Pass the COMMITTED value, never
 *  an in-gesture one, or the track changes length under the pointer. */
export function withValue(stops: number[], value: number): number[] {
  if (!(value > 0) || stops.includes(value)) return stops
  return [...stops, value].sort((a, b) => a - b)
}
