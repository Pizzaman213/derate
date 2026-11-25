// The users chart's rows, wall markers and measured points.
//
//   node src/tabs/models/usersChart.check.mjs

import { load, report } from '../../check/harness.mjs'

const M = await load(import.meta.url, './usersChart.ts')
const { check, done } = report()

const resp = {
  bandwidth_gbps: 273,
  basis: 'live',
  caveat: 'A ceiling from memory bandwidth alone.',
  series: [
    { context: 8192, wall_users: 12, points: [
      { users: 1, ceiling_tps_each: 40, ceiling_tps_total: 40, queued: false },
      { users: 12, ceiling_tps_each: 30, ceiling_tps_total: 360, queued: false },
      { users: 16, ceiling_tps_each: 28, ceiling_tps_total: 336, queued: true },
    ] },
    { context: 32768, wall_users: 0, points: [{ users: 1, ceiling_tps_each: 35, ceiling_tps_total: 35, queued: true }] },
  ],
}
const bench = { points: [
  { users: 1, tps_each_mean: 38.5, ttft_ms_p50: 90, tps_total: 38.5, requests: 2, errors: 0 },
  { users: 4, tps_each_mean: null, ttft_ms_p50: null, tps_total: null, requests: 8, errors: 8 },
] }

const d = M.toChartRows(resp, ['BF16'], bench)
check(d.rows.filter((r) => r.series === 'BF16').map((r) => `${r.x}:${r.y}`).join(',') === '1:40,12:30,16:28', 'one row per point, y is tok/s each')
check(d.rows.some((r) => r.series === '32k'), 'an unnamed series falls back to its context')
check(d.verticalMarkers.length === 1 && d.verticalMarkers[0].x === 12 && d.verticalMarkers[0].series === 'BF16', 'a wall marker per series with a wall above 0')
const measured = d.rows.filter((r) => r.series === M.MEASURED_SERIES)
check(measured.length === 1 && measured[0].y === 38.5, 'measured points without a rate are dropped')
check(d.pointsOnly.join() === M.MEASURED_SERIES, 'the measured series is drawn as dots')
check(M.toChartRows(resp, ['a', 'b'], null).pointsOnly.length === 0, 'no benchmark, no dots series')

const marked = M.toChartRows(resp, ['BF16'], null, { x: 4, series: 'BF16' })
check(
  marked.verticalMarkers.some((m) => m.x === 4 && m.series === 'BF16' && m.label === M.settingLabel(4) && m.label === 'your setting: 4'),
  'the your-setting marker sits at seqs on the picked series',
)
check(M.toChartRows(resp, ['BF16'], null).verticalMarkers.every((m) => !m.label.startsWith('your setting')), 'no setting, no marker')
// The setting on the wall itself: drawn at one x, so one label, both facts --
// two labels there were drawn over each other into an unreadable smear.
const onWall = M.toChartRows(resp, ['BF16'], null, { x: 12, series: 'BF16' }).verticalMarkers.filter((m) => m.x === 12)
check(onWall.length === 1 && onWall[0].label === 'BF16: KV full at 12 · your setting: 12', 'a setting on the wall shares its label')
check(M.ctxLabel(8192) === '8k' && M.ctxLabel(6656) === '6.5k', 'a context that is not whole k keeps its half')

const ttftResp = {
  ...resp,
  series: [
    { context: 8192, prompt_tokens: 8192, wall_users: 2, points: [
      { users: 1, ceiling_tps_each: 40, ceiling_tps_total: 40, queued: false, ttft_floor_s: 0.5 },
      { users: 2, ceiling_tps_each: 38, ceiling_tps_total: 76, queued: false, ttft_floor_s: 1.0 },
      { users: 4, ceiling_tps_each: 30, ceiling_tps_total: 120, queued: true, ttft_floor_s: null },
    ] },
  ],
}
const t = M.toTtftRows(ttftResp, ['8k'], bench)
check(t.rows.filter((r) => r.series === '8k').map((r) => `${r.x}:${r.y}`).join(',') === '1:0.5,2:1,4:null', 'ttft rows are the floor in seconds; a null floor stays a gap')
const tm = t.rows.filter((r) => r.series === M.MEASURED_SERIES)
check(tm.length === 1 && tm[0].x === 1 && tm[0].y === 0.09, 'measured first token is p50 ms / 1000, nulls dropped')
check(t.pointsOnly.join() === M.MEASURED_SERIES, 'measured first token is dots')
check(t.verticalMarkers.length === 1 && t.verticalMarkers[0].x === 2, 'ttft keeps the wall marker')

check(M.presetContexts(131072).join() === '8192,32768,65536,131072', 'presets: every preset within the window')
check(M.presetContexts(2097152).map(M.ctxLabel).join() === '8k,32k,64k,128k,256k,512k,1M,2M', 'presets: 8k, then 32k doubling, in k then M')
check(M.presetContexts(40960).join() === '8192,32768', 'presets: presets only -- a window that is not one is not added')
check(M.comparedContexts(9728, M.presetContexts(40960)).join() === '8192,9728,32768', 'compared: the one in use and its nearest neighbours in doublings')
check(M.comparedContexts(32768, M.presetContexts(262144)).join() === '8192,32768,65536,131072', 'compared: at most four, the current one kept')

check(M.contextStops(40960).map(M.ctxLabel).join() === '1k,1.5k,2k,3k,4k,6k,8k,12k,16k,24k,32k,40k', 'stops: 1k, x1.5, x2 ladder, the window last')
check(M.contextStops(1536).join() === '1024,1536', 'stops: a window on a half-step is not repeated')
check(M.contextStops(512).join() === '512', 'stops: a window under 1k is the only stop')
check(M.contextStops(null).at(-1) === 2097152, 'stops: no window runs to the largest preset')
const st = M.contextStops(131072)
check(st[M.nearestStop(st, 74752)] === 65536, 'nearest: a planner pick sits on the nearest stop in doublings')
check(st[M.nearestStop(st, 49152)] === 49152, 'nearest: a value on a stop is that stop')
check(M.USER_STOPS[M.nearestStop(M.USER_STOPS, 1000)] === 256, 'nearest: past the last stop clamps to it')
check(M.nearestStop(M.USER_STOPS, 0) === 0 && M.nearestStop(M.USER_STOPS, NaN) === 0, 'nearest: no positive value sits on the first stop')
check(M.ctxLabel(16) === '16' && M.ctxLabel(1023) === '1023' && M.ctxLabel(1024) === '1k', 'label: under 1k is a plain count, never "0.0k"')
const between = M.withValue(M.contextStops(131072), 5120)
check(between.includes(5120) && between[M.nearestStop(between, 5120) - 1] === 4096 && between[M.nearestStop(between, 5120) + 1] === 6144,
  'withValue: a value between stops is its own stop, with its neighbours either side')
check(M.withValue(M.USER_STOPS, 16) === M.USER_STOPS && M.withValue(M.USER_STOPS, 0) === M.USER_STOPS,
  'withValue: a value already a stop, or none, leaves the stops as they are')
check(between.every((v) => M.withValue(between, v).length === between.length),
  'withValue: any stop picked from the committed list keeps the list the same length')

done()
