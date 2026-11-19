// Cumulative flow diagram: how many items sit in each workflow state over time, stacked
// in workflow order with the last state ("Done") at the bottom. Band thickness is WIP in
// that state; the vertical distance between band edges is work in progress, the
// horizontal distance is (approximately) lead time.

import { defineChart, phrase } from '../../core'
import { notes } from '../../core/helpers'
import type { Mark } from '../../core'
import { isRecord, toNumber, unpivot, type Row } from '../../adapters'
import { bandPath, padExtent, sortBy, timeTickCount, zoomFields } from '../_shared/TimelineB'
import { input, options, type FlowOptions, type FlowRow } from './schema'

/** Smallest hover target (px) for a segment on a multi-date chart. */
const MIN_HIT = 6

export interface FlowCell { date: Date; state: string; count: number; y0: number; y1: number }
export interface FlowLayer { state: string; /** Workflow index: colour and pattern. */ index: number; cells: FlowCell[] }
export interface FlowModel {
  dates: Date[]
  /** Every state, in workflow order. */
  states: string[]
  /** Visible states stacked bottom-up: the last workflow state first. */
  layers: FlowLayer[]
  /** Visible total per date (the top of the stack). */
  totals: number[]
  /** counts[date index][state] for every state, hidden or not. */
  counts: Array<Map<string, number>>
  duplicates: number
  nulls: number
  negatives: number
  filled: number
}

/** Workflow order: the `states` option first (those present), then the rest in first-seen order. */
export function workflowOrder(rows: readonly FlowRow[], listed: readonly string[] | undefined): string[] {
  const seen = [...new Set(rows.map((r) => r.state))]
  const present = new Set(seen)
  const head = (listed ?? []).filter((s, i, a) => present.has(s) && a.indexOf(s) === i)
  const inHead = new Set(head)
  return [...head, ...seen.filter((s) => !inHead.has(s))]
}

/** Sum per (date, state), fill missing combinations with 0, and stack the visible states in reverse workflow order. */
export function stackFlow(rows: readonly FlowRow[], listed?: readonly string[], hidden: ReadonlySet<string> = new Set()): FlowModel {
  const states = workflowOrder(rows, listed)
  const byDate = new Map<number, Map<string, number>>()
  const seenPair = new Set<string>()
  let duplicates = 0, nulls = 0, negatives = 0
  for (const r of sortBy(rows, (x) => +x.date)) {
    const t = +r.date
    let m = byDate.get(t)
    if (!m) byDate.set(t, (m = new Map()))
    const pair = `${t}\u0000${r.state}`
    if (seenPair.has(pair)) duplicates++
    seenPair.add(pair)
    let c = r.count
    if (c === null) { nulls++; c = 0 }
    if (c < 0) { negatives++; c = 0 }
    m.set(r.state, (m.get(r.state) ?? 0) + c)
  }
  const ts = [...byDate.keys()].sort((a, b) => a - b)
  let filled = 0
  const counts = ts.map((t) => {
    const m = byDate.get(t)!
    for (const s of states) if (!m.has(s)) { filled++; m.set(s, 0) }
    return m
  })
  const dates = ts.map((t) => new Date(t))
  const visible = states.map((state, index) => ({ state, index })).filter((s) => !hidden.has(s.state)).reverse()
  const base = ts.map(() => 0)
  const layers: FlowLayer[] = visible.map(({ state, index }) => ({
    state, index,
    cells: ts.map((_, i) => {
      const count = counts[i]!.get(state) ?? 0
      const cell = { date: dates[i]!, state, count, y0: base[i]!, y1: base[i]! + count }
      base[i] = cell.y1
      return cell
    }),
  }))
  return { dates, states, layers, totals: base, counts, duplicates, nulls, negatives, filled }
}

/**
 * Wide input (one row per date, a numeric column per state) becomes long rows. Runs after
 * the field mapping, so `{ fields: { date: 'Day' } }` works and 'Day' is not taken for a state.
 */
function adapt(raw: unknown, o: FlowOptions & { fields?: Record<string, string> }): unknown {
  if (!Array.isArray(raw) || raw.length === 0 || !raw.every(isRecord)) return raw
  const rows = raw as Row[]
  if (rows.some((r) => Object.hasOwn(r, 'state') || Object.hasOwn(r, 'count'))) return raw
  const skip = new Set(['date', ...Object.values(o.fields ?? {})])
  const cols = [...new Set(rows.flatMap((r) => Object.keys(r)))].filter((k) => !skip.has(k))
  const numeric = cols.filter((k) => rows.every((r) => r[k] === null || r[k] === undefined || r[k] === '' || toNumber(r[k]) !== null))
  if (!numeric.length) return raw
  return unpivot(rows, { id: ['date'], key: 'state', value: 'count', columns: numeric })
}

export const cumulativeFlow = defineChart<FlowRow[], FlowOptions>({
  id: 'cumulative-flow',
  name: 'Cumulative flow diagram',
  category: 'timeline',
  intents: ['timeline', 'schedule', 'trend'],
  summary: 'Items in each workflow state over time, stacked in workflow order.',
  input,
  options,
  shape: 'rows',
  fields: ['date', 'state', 'count'],
  dateFields: ['date'],
  numberFields: ['count'],
  zoom: 'x',
  adapt,
  isEmpty: (data) => data.length === 0 || (data.every((r) => !r.count) ? 'No items in any state' : false),
  draw(ctx) {
    const { theme, options: o, fmt, hidden } = ctx
    const m = stackFlow(ctx.data, o.states, hidden)
    const ts = m.dates.map((d) => +d)
    const max = Math.max(0, ...m.totals)
    const dom = padExtent(ts[0]!, ts[ts.length - 1]!, ctx.width, ctx.measure)
    const f = ctx.cartesian({
      x: { type: 'time', domain: [new Date(dom[0]), new Date(dom[1])] },
      y: { type: 'linear', domain: [0, max || 1], zero: true },
      xTitle: o.xTitle,
      yTitle: o.yTitle,
      zoom: true,
      xTickCount: timeTickCount(m.dates.length, ctx.width),
    })
    const marks: Mark[] = [...f.marks]
    const xs = m.dates.map((d) => f.px(d))
    const single = m.dates.length === 1
    // hit columns: from halfway to the previous date to halfway to the next
    const colW = single ? Math.min(48, f.plot.width / 3) : 0
    const edges = xs.map((x, i) => single
      ? [x - colW / 2, x + colW / 2] as const
      : [i === 0 ? x - (xs[1]! - x) / 2 : (xs[i - 1]! + x) / 2, i === xs.length - 1 ? x + (x - xs[i - 1]!) / 2 : (x + xs[i + 1]!) / 2] as const)

    for (const layer of m.layers) {
      const fill = theme.cat(layer.index)
      if (!single) {
        const d = bandPath(layer.cells.map((c, i) => ({ x: xs[i]!, y0: f.py(c.y0), y1: f.py(c.y1) })))
        marks.push({ type: 'path', key: `band:${layer.state}`, d, fill, fillOpacity: 0.88, stroke: theme.surface, strokeWidth: 0.75, series: layer.state, pattern: layer.index, inert: true })
      }
      layer.cells.forEach((c, i) => {
        const [a, b] = edges[i]!
        let top = f.py(c.y1), bottom = f.py(c.y0)
        // multi-date segments are invisible hit targets over the bands: a state with no
        // (or almost no) items still gets a hoverable strip on its boundary
        if (!single && bottom - top < MIN_HIT) {
          const mid = (top + bottom) / 2
          top = mid - MIN_HIT / 2
          bottom = mid + MIN_HIT / 2
        }
        const dt = fmt.dateTime(c.date)
        const rows: Array<[string, string]> = m.states.filter((s) => !hidden.has(s)).map((s) => [s, fmt.value(m.counts[i]!.get(s) ?? 0)])
        rows.push(['Total', fmt.value(m.totals[i]!)])
        marks.push({
          type: 'rect', role: 'segment', key: `seg:${layer.state}:${ts[i]}`, x: a, width: Math.max(0.5, b - a), y: top, height: Math.max(0, bottom - top),
          fill, fillOpacity: single ? 0.88 : 0, stroke: single ? theme.surface : undefined, strokeWidth: single ? 0.75 : undefined, pattern: single ? layer.index : undefined,
          series: layer.state, datum: c,
          label: `${dt}: ${layer.state} ${fmt.value(c.count)} of ${fmt.value(m.totals[i]!)}`,
          tooltip: { title: `${dt} · ${layer.state}`, rows, color: fill },
        })
      })
    }
    // band names at the right edge, where the band is tall enough to hold one
    if (o.labels && m.layers.length > 1) {
      const li = m.dates.length - 1
      const right = (single ? edges[0]![1] + 6 : xs[li]! - 6)
      const room = single ? f.plot.x + f.plot.width - right : Math.min(f.plot.width * 0.4, 140)
      for (const layer of m.layers) {
        const c = layer.cells[li]!
        const top = f.py(c.y1), bottom = f.py(c.y0)
        if (bottom - top < ctx.measure('M') * 1.6 || room < 30) continue
        const t = ctx.truncate(layer.state, room)
        if (!t || t === '…') continue
        marks.push({ type: 'text', layer: 'labels', key: `bl:${layer.state}`, x: right, y: (top + bottom) / 2, text: t, full: t !== layer.state ? layer.state : undefined, className: t !== layer.state ? 'oc-trunc' : undefined, anchor: single ? 'start' : 'end', baseline: 'central', fill: theme.ink, halo: true, series: layer.state })
      }
    }
    return {
      marks,
      legend: { items: m.states.map((s, i) => ({ key: s, label: s, color: theme.cat(i), shape: 'square' as const, pattern: i })) },
      note: notes([
        [m.duplicates, 'repeated date/state pairs summed'],
        [m.nulls, 'missing counts taken as 0'],
        [m.negatives, 'negative counts taken as 0'],
        [m.filled, `state${m.filled === 1 ? '' : 's'} absent on a date taken as 0`],
      ]),
      ...zoomFields(f, o.zoom),
    }
  },
  describe({ data, options: o, fmt, name }) {
    const m = stackFlow(data, o.states)
    const n = m.dates.length
    const first = 0, last = n - 1
    const parts = [
      name,
      `${phrase.count(m.states.length, 'state')} (${phrase.listPhrase(m.states, 4)})`,
      `${phrase.count(n, 'date')} ${n === 1 ? 'on' : 'from'} ${phrase.dateSpan(m.dates, fmt)}`,
      n === 1 ? `${fmt.value(m.totals[last])} items` : `${fmt.value(m.totals[first])} to ${fmt.value(m.totals[last])} items in total`,
    ]
    const done = m.states[m.states.length - 1]
    if (done !== undefined && m.states.length > 1 && n > 1) parts.push(`${done} ${fmt.value(m.counts[first]!.get(done))} to ${fmt.value(m.counts[last]!.get(done))}`)
    return parts.join(', ')
  },
  table(data, o, fmt) {
    const m = stackFlow(data, o.states)
    return {
      columns: ['Date', ...m.states, 'Total'],
      rows: m.dates.map((d, i) => [fmt.dateTime(d), ...m.states.map((s) => m.counts[i]!.get(s) ?? 0), m.totals[i]!]),
    }
  },
})

export default cumulativeFlow
