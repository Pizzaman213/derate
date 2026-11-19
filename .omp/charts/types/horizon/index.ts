// Horizon chart: each series is a short row; the value range is cut into `bands` slices
// that are folded down onto the baseline and layered in darker shades, so a large range
// reads in a fraction of the height. Negatives fold the same way in the other hue.

import { defineChart, SIZES, type Formatters, type Mark } from '../../core'
import { notes } from '../../core/helpers'
import { perRender, pointDateFormatter } from '../_shared/TrendB'
import { input, options, type HorizonOptions, type HorizonRow } from './schema'

/** How full each band is for |v|: band i holds the slice [i·max/bands, (i+1)·max/bands]. */
export function fold(v: number, bands: number, max: number): number[] {
  const size = max / bands
  return Array.from({ length: bands }, (_, i) => Math.min(1, Math.max(0, Math.abs(v) / size - i)))
}

interface Pt { t: number; v: number | null; row: HorizonRow; dupes: number }
interface Series { name: string; pts: Pt[] }

/** Series in first-seen order, points sorted by time; repeated (series, x) readings averaged. */
export function horizonSeries(data: HorizonRow[]): Series[] {
  const map = new Map<string, Map<number, Pt & { n: number; sum: number }>>()
  for (const row of data) {
    const k = row.series ?? ''
    const byT = map.get(k) ?? new Map<number, Pt & { n: number; sum: number }>()
    map.set(k, byT)
    const t = +row.x, hit = byT.get(t)
    if (!hit) { byT.set(t, { t, v: row.y, row, dupes: 0, n: row.y === null ? 0 : 1, sum: row.y ?? 0 }); continue }
    hit.dupes++
    if (row.y !== null) { hit.n++; hit.sum += row.y; hit.v = hit.sum / hit.n }
  }
  return [...map].map(([name, byT]) => ({ name, pts: [...byT.values()].sort((a, b) => a.t - b.t) }))
}

/**
 * Points of one unbroken run with extra points wherever the value crosses zero or a band
 * edge, so folding the straight segments between them is exact.
 */
function densify(run: Array<{ t: number; v: number }>, edges: number[]): Array<{ t: number; v: number }> {
  const out: Array<{ t: number; v: number }> = []
  run.forEach((b, i) => {
    const a = run[i - 1]
    if (a && a.v !== b.v) {
      const cuts = edges.map((e) => (e - a.v) / (b.v - a.v)).filter((f) => f > 0 && f < 1).sort((x, y) => x - y)
      for (const f of cuts) out.push({ t: a.t + f * (b.t - a.t), v: a.v + f * (b.v - a.v) })
    }
    out.push(b)
  })
  return out
}

/** One hover strip: a reading, its neighbours' times (the strip reaches halfway to them) and its text. */
interface Strip { t: number; v: number; k: number; prev?: number; next?: number; row: HorizonRow; label: string; title: string; value: string }
interface SeriesModel { name: string; runs: Array<Array<{ t: number; v: number }>>; strips: Strip[] }

const seriesOf = perRender((data: HorizonRow[], _o: HorizonOptions) => horizonSeries(data))

/** Everything that depends on the data and options alone: grouping, folding edges, text. */
const horizonModel = perRender((data: HorizonRow[], o: HorizonOptions, fmt: Formatters) => {
  const all = seriesOf(data, o)
  const series = all.filter((s) => s.pts.some((p) => p.v !== null))
  let lo = Infinity, hi = -Infinity, maxAbs = 0
  for (const s of series) for (const p of s.pts) {
    lo = Math.min(lo, p.t); hi = Math.max(hi, p.t)
    if (p.v !== null) maxAbs = Math.max(maxAbs, Math.abs(p.v))
  }
  const max = o.max ?? (maxAbs || 1)
  const bands = o.bands, size = max / bands
  const edges = [0, ...Array.from({ length: bands - 1 }, (_, k) => [(k + 1) * size, -(k + 1) * size]).flat()]
  const fmtDate = pointDateFormatter(series.flatMap((s) => s.pts.map((p) => new Date(p.t))), fmt)
  let clipped = 0
  const rows: SeriesModel[] = series.map((s) => {
    // unbroken runs, densified at every band edge so folding them is exact
    const runs: Array<Array<{ t: number; v: number }>> = []
    let run: Array<{ t: number; v: number }> = []
    for (const p of s.pts) {
      if (p.v === null) { if (run.length) runs.push(run); run = []; continue }
      if (Math.abs(p.v) > max) clipped++
      run.push({ t: p.t, v: Math.max(-max, Math.min(max, p.v)) })
    }
    if (run.length) runs.push(run)
    const strips: Strip[] = []
    s.pts.forEach((p, k) => {
      if (p.v === null) return
      const when = fmtDate(new Date(p.t))
      const value = fmt.value(p.v)
      strips.push({ t: p.t, v: p.v, k, prev: s.pts[k - 1]?.t, next: s.pts[k + 1]?.t, row: p.row, value, title: s.name ? `${s.name} · ${when}` : when, label: `${s.name ? `${s.name}, ` : ''}${when}: ${value}` })
    })
    return { name: s.name, runs: runs.map((r) => densify(r, edges)), strips }
  })
  return {
    rows, lo, hi, maxAbs, max, bands, size, clipped,
    named: !(series.length === 1 && series[0]!.name === ''),
    nulls: series.reduce((n, s) => n + s.pts.filter((p) => p.v === null).length, 0),
    dupes: all.reduce((n, s) => n + s.pts.reduce((k, p) => k + p.dupes, 0), 0),
    hasNeg: maxAbs > 0 && series.some((s) => s.pts.some((p) => (p.v ?? 0) < 0)),
  }
})

export const horizon = defineChart<HorizonRow[], HorizonOptions>({
  id: 'horizon',
  name: 'Horizon chart',
  category: 'trend',
  intents: ['trend', 'change-over-time', 'compare'],
  summary: 'Compact rows of folded, layered area bands: many time series in little height.',
  input,
  options,
  shape: 'rows',
  fields: ['x', 'y', 'series'],
  dateFields: ['x'],
  numberFields: ['y'],
  zoom: 'x',
  aspect: 0.45,
  minHeight: 120,
  isEmpty: (data) => data.length === 0 || data.every((r) => r.y === null),
  draw(ctx) {
    const { theme, fmt, options: o } = ctx
    const { rows, lo, hi, max, bands, size, clipped, named, nulls, dupes, hasNeg } = horizonModel(ctx.data, o, fmt)
    const f = ctx.cartesian({
      x: { type: 'time', domain: [new Date(lo), new Date(hi)] },
      y: { type: 'band', domain: rows.map((s) => s.name || ' '), padding: rows.length > 1 ? 0.12 : 0, paddingOuter: 0 },
      yAxis: named ? 'left' : 'none',
      grid: 'none',
      zoom: true,
      bandLabelMax: ctx.width * 0.25,
    })
    const marks: Mark[] = [...f.marks]
    const pos = (t: number) => f.px(new Date(t))
    const bandFill = (sign: number, i: number) => {
      const t = 0.5 + sign * 0.5 * ((i + 1) / bands)
      return theme.div(sign > 0 ? Math.max(t, 0.62) : Math.min(t, 0.38))
    }
    const cellUp = theme.div(0.85), cellDown = theme.div(0.15)
    for (const s of rows) {
      const y0 = f.py(s.name || ' '), h = f.bh, base = y0 + h
      marks.push({ type: 'rect', layer: 'back', key: `bg:${s.name}`, x: f.plot.x, y: y0, width: f.plot.width, height: h, fill: theme.track, fillOpacity: 0.35, inert: true })
      // each run's x positions once: a lone reading becomes a 2px sliver so it is not invisible
      const runs = s.runs.map((r) => {
        const xs = r.length === 1 ? [pos(r[0]!.t) - 1, pos(r[0]!.t) + 1] : r.map((p) => pos(p.t))
        const vs = r.length === 1 ? [r[0]!.v, r[0]!.v] : r.map((p) => p.v)
        return { x: xs.map((x) => x.toFixed(2)), vs }
      })
      for (const sign of [1, -1] as const) {
        const offset = sign < 0 && o.mode === 'offset'
        const from = offset ? y0 : base, dir = offset ? 1 : -1
        const from2 = from.toFixed(2)
        for (let i = 0; i < bands; i++) {
          let d = ''
          for (const { x, vs } of runs) {
            // how full band i is at each point (see `fold`)
            let any = false
            const ys = vs.map((v) => {
              const y = sign * v > 0 ? Math.min(1, Math.max(0, Math.abs(v) / size - i)) : 0
              if (y > 0) any = true
              return y
            })
            if (!any) continue
            d += `M${x[0]},${from2}`
            for (let k = 0; k < x.length; k++) d += `L${x[k]},${(from + dir * ys[k]! * h).toFixed(2)}`
            d += `L${x[x.length - 1]},${from2}Z`
          }
          if (d) marks.push({ type: 'path', key: `band:${s.name}:${sign}:${i}`, role: 'band', d, fill: bandFill(sign, i), inert: true })
        }
      }
      // hover targets: one strip per reading, reaching halfway to its neighbours
      for (const st of s.strips) {
        const x = pos(st.t)
        const x0 = st.prev !== undefined ? (pos(st.prev) + x) / 2 : x - 2
        const x1 = st.next !== undefined ? (pos(st.next) + x) / 2 : x + 2
        marks.push({
          type: 'rect', role: 'cell', key: `c:${s.name}:${st.t}:${st.k}`, x: x0, y: y0, width: Math.max(0.5, x1 - x0), height: h, fill: theme.ink, fillOpacity: 0,
          datum: st.row, label: st.label, tooltip: { title: st.title, rows: [['Value', st.value]], color: st.v >= 0 ? cellUp : cellDown },
        })
      }
      // baseline
      marks.push({ type: 'line', layer: 'grid', key: `base:${s.name}`, x1: f.plot.x, x2: f.plot.x + f.plot.width, y1: base, y2: base, stroke: theme.axis, strokeWidth: 0.5, inert: true })
    }
    return {
      marks,
      plot: f.plot,
      clip: f.plot,
      invertX: f.invertX,
      legend: { title: `${bands} ${bands === 1 ? 'band' : 'bands'} of ${fmt.value(size)}`, gradient: hasNeg ? { stops: theme.divStops(), min: fmt.value(-max), mid: '0', max: fmt.value(max) } : { stops: theme.divStops().slice(Math.floor(theme.divStops().length / 2)), min: '0', max: fmt.value(max) } },
      note: notes([[nulls, 'missing values left as gaps'], [dupes, 'repeated readings averaged'], [clipped, `values beyond ±${fmt.value(max)} clipped to the top band`]]),
    }
  },
  describe({ data, fmt, options: o, name }) {
    const series = seriesOf(data, o).filter((s) => s.pts.some((p) => p.v !== null))
    const pts = series.flatMap((s) => s.pts.filter((p) => p.v !== null).map((p) => ({ s: s.name, t: p.t, v: p.v! })))
    let hi = pts[0]!, lo = pts[0]!, first = Infinity, last = -Infinity
    for (const p of pts) { if (p.v > hi.v) hi = p; if (p.v < lo.v) lo = p; first = Math.min(first, p.t); last = Math.max(last, p.t) }
    const fd = pointDateFormatter(pts.map((p) => new Date(p.t)), fmt)
    const span = pts.length > 1 ? `${fd(new Date(first))} to ${fd(new Date(last))}` : fd(new Date(hi.t))
    const who = (p: typeof hi) => `${p.s ? `${p.s} ` : ''}at ${fmt.value(p.v)} on ${fd(new Date(p.t))}`
    const max = o.max ?? Math.max(Math.abs(hi.v), Math.abs(lo.v))
    return `${name}, ${series.length} series, ${span}, ${o.bands} bands of ${fmt.value((max || 1) / o.bands)}, highest ${who(hi)}, lowest ${who(lo)}`
  },
  table(data, o, fmt) {
    const fd = pointDateFormatter(data.map((r) => r.x), fmt)
    return { columns: ['Series', 'X', 'Value'], rows: seriesOf(data, o).flatMap((s) => s.pts.map((p) => [s.name, fd(new Date(p.t)), p.v])) }
  },
})

export default horizon
