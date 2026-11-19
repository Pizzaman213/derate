// Bump chart: how the ranking of several series changes over time. Rank 1 at the top;
// smooth curves between time points; ranks derived from values when not given.

import { defineChart, SIZES, type Formatters, type Mark } from '../../core'
import { notes } from '../../core/helpers'
import { parseDate } from '../../time'
import { bumpPath, placeLabels, pointDateFormatter } from '../_shared/TrendB'
import { input, options, type BumpOptions, type BumpRow } from './schema'

/** Competition ranking ("1, 2, 2, 4"), highest value first; nulls stay unranked. */
export function deriveRanks(values: Array<number | null>): Array<number | null> {
  return values.map((v) => (v === null ? null : 1 + values.filter((w) => w !== null && w > v).length))
}

export interface BumpCell { x: number; series: string; rank: number; value: number | null; derived: boolean; row: BumpRow }
export interface BumpModel {
  /** x points in display order. */
  xs: Array<{ key: string; label: string }>
  /** Series in order of their first rank. */
  series: string[]
  cells: BumpCell[]
  maxRank: number
  duplicates: number
  unranked: number
}

/** Order x, merge duplicates (values summed, best rank kept), derive missing ranks per x. */
export function bumpModel(data: BumpRow[], fmt: Formatters): BumpModel {
  const raw = [...new Set(data.map((r) => r.x))]
  const numeric = raw.every((k) => /^-?\d+(\.\d+)?$/.test(k.trim()))
  // dates (epoch ms written as text included): one x per instant, however it was written
  const parsed = numeric ? null : raw.map((k) => parseDate(/^\d{10,13}$/.test(k.trim()) ? Number(k) : k, { timeZone: fmt.timeZone }))
  const canon = new Map<string, string>(raw.map((k, i) => [k, parsed?.every((d) => d !== null) ? String(+parsed[i]!) : k]))
  const keys = [...new Set(raw.map((k) => canon.get(k)!))]
  let xs: BumpModel['xs']
  if (numeric) xs = [...keys].sort((a, b) => Number(a) - Number(b)).map((k) => ({ key: k, label: k }))
  else if (parsed && parsed.every((d) => d !== null)) {
    const ds = keys.map((k) => new Date(Number(k)))
    const f = pointDateFormatter(ds, fmt)
    xs = keys.map((k, i) => ({ k, d: ds[i]! })).sort((a, b) => +a.d - +b.d).map(({ k, d }) => ({ key: k, label: f(d) }))
  } else xs = keys.map((k) => ({ key: k, label: k }))
  const xi = new Map(raw.map((k) => [k, xs.findIndex((x) => x.key === canon.get(k))]))
  let duplicates = 0, unranked = 0
  const merged = new Map<string, { x: number; series: string; rank: number | null; value: number | null; row: BumpRow }>()
  for (const r of data) {
    const k = `${xi.get(r.x)}\u0000${r.series}`
    const hit = merged.get(k)
    if (!hit) { merged.set(k, { x: xi.get(r.x)!, series: r.series, rank: r.rank, value: r.value, row: r }); continue }
    duplicates++
    hit.value = hit.value === null ? r.value : r.value === null ? hit.value : hit.value + r.value
    hit.rank = hit.rank === null ? r.rank : r.rank === null ? hit.rank : Math.min(hit.rank, r.rank)
  }
  const cells: BumpCell[] = []
  const byX = new Map<number, Array<typeof merged extends Map<string, infer V> ? V : never>>()
  for (const c of merged.values()) byX.set(c.x, [...(byX.get(c.x) ?? []), c])
  for (const [, group] of byX) {
    const allRanked = group.every((c) => c.rank !== null)
    const derived = allRanked ? null : deriveRanks(group.map((c) => c.value))
    group.forEach((c, i) => {
      const fromValue = derived?.[i] ?? null
      const rank = allRanked ? c.rank : fromValue ?? c.rank
      if (rank === null) { unranked++; return }
      cells.push({ x: c.x, series: c.series, rank, value: c.value, derived: !allRanked && fromValue !== null, row: c.row })
    })
  }
  cells.sort((a, b) => a.x - b.x || a.rank - b.rank)
  const series = [...new Set(cells.map((c) => c.series))]
  const maxRank = Math.max(1, ...cells.map((c) => Math.ceil(c.rank)))
  return { xs, series, cells, maxRank, duplicates, unranked }
}

export const bump = defineChart<BumpRow[], BumpOptions>({
  id: 'bump',
  name: 'Bump chart',
  category: 'trend',
  intents: ['trend', 'change-over-time', 'rank', 'compare'],
  summary: 'Rank of each series at each time point, joined by smooth lines; 1 at the top.',
  input,
  options,
  shape: 'rows',
  fields: ['x', 'series', 'rank', 'value'],
  numberFields: ['rank', 'value'],
  aspect: 0.6,
  minHeight: 240,
  isEmpty: (data) => (data.length === 0 ? true : data.every((r) => r.rank === null && r.value === null) ? 'No row has a rank or a value' : false),
  draw(ctx) {
    const { theme, fmt, options: o, hidden } = ctx
    const m = bumpModel(ctx.data, fmt)
    const colorIdx = new Map(m.series.map((s, i) => [s, i]))
    const last = m.xs.length - 1
    const endSeries = m.series.filter((s) => m.cells.some((c) => c.series === s && c.x === last))
    const labelW = Math.min(ctx.width * 0.28, Math.max(0, ...endSeries.map((s) => ctx.measure(s))) + 10)
    const ranks = Array.from({ length: m.maxRank }, (_, i) => String(i + 1))
    const f = ctx.cartesian({
      x: { type: 'point', domain: m.xs.map((x) => x.key), padding: 0.3 },
      y: { type: 'point', domain: ranks, padding: 0.5 },
      xFormat: ((k: string) => m.xs.find((x) => x.key === k)?.label ?? k) as (v: never) => string,
      yFormat: ((r: string) => `#${r}`) as (v: never) => string,
      grid: 'none',
      pad: { right: labelW, left: 4 },
    })
    const step = Math.min(f.plot.height / Math.max(1, m.maxRank), f.plot.width / Math.max(1, m.xs.length))
    const r = Math.max(2, Math.min(9, step * 0.3))
    const py = (rank: number) => {
      const lo = Math.floor(rank), a = f.py(String(Math.max(1, lo)))
      return rank === lo ? a : a + (rank - lo) * (f.py(String(Math.min(m.maxRank, lo + 1))) - a)
    }
    const px = (x: number) => f.px(m.xs[x]!.key)
    const marks: Mark[] = [...f.marks]
    for (const x of m.xs) marks.push({ type: 'line', layer: 'grid', key: `gx:${x.key}`, x1: f.px(x.key), x2: f.px(x.key), y1: f.plot.y, y2: f.plot.y + f.plot.height, stroke: theme.grid, strokeWidth: 1, inert: true })
    const bySeries = new Map<string, BumpCell[]>()
    for (const c of m.cells) bySeries.set(c.series, [...(bySeries.get(c.series) ?? []), c])
    const width = Math.max(1.5, Math.min(4, r * 0.6))
    for (const s of m.series) {
      if (hidden.has(s)) continue
      const cs = bySeries.get(s)!
      const pts = m.xs.map((_, i) => { const c = cs.find((d) => d.x === i); return c ? ([px(i), py(c.rank)] as const) : null })
      marks.push({ type: 'path', key: `line:${s}`, role: 'line', series: s, d: bumpPath(pts), fill: 'none', stroke: theme.cat(colorIdx.get(s)!), strokeWidth: width, linecap: 'round', inert: true })
    }
    const showRanks = o.showRanks && r >= 7
    for (const c of m.cells) {
      const color = theme.cat(colorIdx.get(c.series)!)
      const at = m.xs[c.x]!.label
      const rows: Array<[string, string]> = [['Rank', `#${c.rank}`]]
      if (c.value !== null) rows.push(['Value', fmt.value(c.value)])
      marks.push({
        type: 'circle', role: 'point', key: `p:${c.series}:${m.xs[c.x]!.key}`, series: c.series, cx: px(c.x), cy: py(c.rank), r, fill: color, stroke: theme.surface, strokeWidth: 1.5,
        datum: c.row, label: `${c.series}, ${at}: rank ${c.rank}${c.value !== null ? ` (${fmt.value(c.value)})` : ''}`, tooltip: { title: `${c.series} · ${at}`, rows, color },
      })
      if (showRanks && !hidden.has(c.series) && Number.isInteger(c.rank) && c.rank < 100) marks.push({ type: 'text', layer: 'labels', key: `rk:${c.series}:${m.xs[c.x]!.key}`, x: px(c.x), y: py(c.rank), text: String(c.rank), anchor: 'middle', baseline: 'central', size: Math.min(SIZES.tick, r * 1.2), fill: theme.surface, weight: 600, inert: true })
    }
    // end labels for series present at the last time point
    const ends = endSeries.filter((s) => !hidden.has(s)).map((s) => ({ s, rank: bySeries.get(s)!.find((c) => c.x === last)!.rank }))
    const placed = placeLabels(ends.map((e) => py(e.rank)), ends.map((e) => -e.rank), SIZES.tick + 2, f.plot.y, f.plot.y + f.plot.height)
    const lx = px(last) + r + 6
    for (const [i, y] of placed) {
      const e = ends[i]!
      const t = ctx.truncate(e.s, ctx.width - lx - 2)
      marks.push({ type: 'text', layer: 'labels', key: `end:${e.s}`, role: 'end-label', x: lx, y, text: t, full: t !== e.s ? e.s : undefined, anchor: 'start', baseline: 'central', fill: theme.cat(colorIdx.get(e.s)!), size: SIZES.tick, className: t !== e.s ? 'oc-trunc' : undefined })
    }
    return {
      marks,
      plot: f.plot,
      legend: m.series.length > 1 && m.series.length <= 12 ? { items: m.series.map((s) => ({ key: s, label: s, color: theme.cat(colorIdx.get(s)!), shape: 'line' as const })) } : undefined,
      note: notes([[m.duplicates, 'duplicate rows merged (values summed)'], [m.unranked, 'rows without a rank or value left out'], [ends.length - placed.size, 'end labels hidden for space']]),
    }
  },
  describe({ data, fmt, name }) {
    const m = bumpModel(data, fmt)
    const first = m.xs[0]!, last = m.xs[m.xs.length - 1]!
    const leaderAt = (x: number) => m.cells.find((c) => c.x === x && c.rank === Math.min(...m.cells.filter((d) => d.x === x).map((d) => d.rank)))?.series ?? 'nobody'
    const span = m.xs.length > 1 ? `${first.label} to ${last.label}` : first.label
    let best = { s: '', d: 0 }
    for (const s of m.series) {
      const cs = m.cells.filter((c) => c.series === s)
      if (cs.length < 2) continue
      const d = cs[0]!.rank - cs[cs.length - 1]!.rank
      if (d > best.d) best = { s, d }
    }
    const climb = best.s ? `, biggest climb ${best.s} (up ${best.d} ${best.d === 1 ? 'place' : 'places'})` : ''
    return `${name}, ${m.series.length} series over ${m.xs.length} ${m.xs.length === 1 ? 'point' : 'points'}, ${span}, ${leaderAt(m.xs.length - 1)} ranked first at the end${climb}`
  },
  table(data, _o, fmt) {
    const m = bumpModel(data, fmt)
    return { columns: ['X', 'Series', 'Rank', 'Value'], rows: m.cells.map((c) => [m.xs[c.x]!.label, c.series, c.rank, c.value]) }
  },
})

export default bump
