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

/** A context for a chip, a series name or a sentence: "M" from 1,048,576
 *  tokens up, else "k". Whole when it is one, else one decimal: 6656 is
 *  "6.5k", where rounding said "7k" -- a context that is not the one on the
 *  chip beside it. */
export const ctxLabel = (n: number) => {
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

/** The contexts people ask for: 8k, then 32k doubling to 2M. */
const CONTEXT_PRESETS = [8192, 32768, 65536, 131072, 262144, 524288, 1048576, 2097152]

/** The context chips: the presets within the model's native window, and
 *  nothing else. Planner's-pick is a separate chip. */
export function contextChips(native: number | null): number[] {
  const cap = native ?? Infinity
  return CONTEXT_PRESETS.filter((c) => c <= cap)
}

/** The contexts the By context and First token tabs draw: the server takes at
 *  most four, so the one in use plus the three chips nearest it (in doublings,
 *  so 16k is as near 8k as 4k is), sorted. */
export function comparedContexts(current: number | null, chips: number[]): number[] {
  if (current == null) return chips.slice(0, 4)
  const near = [...new Set([current, ...chips])].sort(
    (a, b) => Math.abs(Math.log2(a / current)) - Math.abs(Math.log2(b / current)) || a - b,
  )
  return near.slice(0, 4).sort((a, b) => a - b)
}
