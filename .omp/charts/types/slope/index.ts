// Slope chart: each item is a line from its value at one time point (left axis) to its
// value at a second (right axis); labels at both ends are nudged apart so they never collide.

import { defineChart, SIZES, type Mark } from '../../core'
import { finiteExtent, notes } from '../../core/helpers'
import { placeLabels, signed } from '../_shared/TrendB'
import { input, options, type SlopeOptions, type SlopeRow } from './schema'

type Dir = 'up' | 'down' | 'flat'
const DIR_LABEL: Record<Dir, string> = { up: 'Increase', down: 'Decrease', flat: 'No change' }
const dirOf = (r: { start: number; end: number }): Dir => (r.end > r.start ? 'up' : r.end < r.start ? 'down' : 'flat')

interface Item { row: SlopeRow; label: string; start: number; end: number; key: string; dir: Dir; i: number }

/** Complete rows (both ends present), with a stable key per row; repeated labels stay distinct. */
export function slopeItems(data: SlopeRow[]): { items: Item[]; skipped: number; duplicates: number } {
  const seen = new Map<string, number>()
  const items: Item[] = []
  let skipped = 0, duplicates = 0
  data.forEach((row, i) => {
    const k = seen.get(row.label) ?? 0
    seen.set(row.label, k + 1)
    if (k > 0) duplicates++
    if (row.start === null || row.end === null) { skipped++; return }
    const it = { row, label: row.label, start: row.start, end: row.end, key: k ? `${row.label}#${k + 1}` : row.label, i }
    items.push({ ...it, dir: dirOf(it) })
  })
  return { items, skipped, duplicates }
}

export const slope = defineChart<SlopeRow[], SlopeOptions>({
  id: 'slope',
  name: 'Slope chart',
  category: 'trend',
  intents: ['trend', 'change-over-time', 'compare', 'rank'],
  summary: 'Lines between two time points, one per item, labelled at both ends.',
  input,
  options,
  shape: 'rows',
  fields: ['label', 'start', 'end'],
  numberFields: ['start', 'end'],
  aspect: 0.75,
  minHeight: 220,
  maxHeight: 640,
  isEmpty: (data) => (data.length === 0 ? true : slopeItems(data).items.length === 0 ? 'No row has both a start and an end value' : false),
  draw(ctx) {
    const { theme, options: o, fmt, width: W, height: H, hidden } = ctx
    const { items, skipped, duplicates } = slopeItems(ctx.data)
    const FS = SIZES.tick, gap = FS + 3, dot = 3.5
    const headH = SIZES.label + 10
    const top = headH + gap / 2, bottom = H - gap / 2 - 2
    // label column width: what the labels need, capped so the lines keep at least ~30% of the width
    const valW = o.showValues ? Math.max(...items.flatMap((d) => [ctx.measure(fmt.value(d.start)), ctx.measure(fmt.value(d.end))])) + 6 : 0
    const nameW = Math.max(...items.slice(0, 400).map((d) => ctx.measure(d.label)))
    const side = Math.max(Math.min(nameW + valW, W * 0.34), Math.min(W * 0.2, 60))
    const xl = side + dot + 6, xr = W - side - dot - 6
    const ext = finiteExtent(items.flatMap((d) => [d.start, d.end]))!
    let [lo, hi] = ext
    if (lo === hi) { lo -= Math.abs(lo) * 0.1 || 1; hi += Math.abs(hi) * 0.1 || 1 }
    const py = (v: number) => bottom - ((v - lo) / (hi - lo)) * (bottom - top)
    const colorOf = (d: Item) => (o.colorBy === 'category' ? theme.cat(d.i) : d.dir === 'up' ? theme.positive : d.dir === 'down' ? theme.negative : theme.neutral)
    const series = (d: Item) => (o.colorBy === 'direction' ? d.dir : undefined)
    const visible = items.filter((d) => !series(d) || !hidden.has(series(d)!))

    const marks: Mark[] = []
    const head = (key: string, text: string, x: number, anchor: 'start' | 'middle' | 'end') => {
      const t = ctx.truncate(text, side + 20, SIZES.label)
      marks.push({ type: 'text', layer: 'axes', key, x, y: SIZES.label + 2, text: t, full: t !== text ? text : undefined, anchor, baseline: 'alphabetic', size: SIZES.label, weight: 600, fill: theme.ink })
    }
    head('h:start', o.startLabel, xl, 'middle')
    head('h:end', o.endLabel, xr, 'middle')
    for (const [k, x] of [['a:start', xl], ['a:end', xr]] as const) marks.push({ type: 'line', layer: 'grid', key: k, x1: x, x2: x, y1: headH, y2: H - 2, stroke: theme.axis, strokeWidth: 1, inert: true })

    for (const d of items) {
      const c = colorOf(d), y1 = py(d.start), y2 = py(d.end)
      const change = signed(d.end - d.start, fmt.value)
      marks.push({
        type: 'line', role: 'slope', key: d.key, x1: xl, y1, x2: xr, y2, stroke: c, strokeWidth: 2, linecap: 'round', series: series(d),
        datum: d.row, label: `${d.label}: ${fmt.value(d.start)} to ${fmt.value(d.end)} (${change})`,
        tooltip: { title: d.label, rows: [[o.startLabel, fmt.value(d.start)], [o.endLabel, fmt.value(d.end)], ['Change', change]], color: c },
      })
    }
    const dense = items.length > 300
    for (const d of visible) {
      if (dense) break
      const c = colorOf(d)
      marks.push({ type: 'circle', layer: 'labels', key: `ds:${d.key}`, cx: xl, cy: py(d.start), r: dot, fill: c, stroke: theme.surface, strokeWidth: 1, inert: true })
      marks.push({ type: 'circle', layer: 'labels', key: `de:${d.key}`, cx: xr, cy: py(d.end), r: dot, fill: c, stroke: theme.surface, strokeWidth: 1, inert: true })
    }
    // labels: biggest movers win when they cannot all fit
    const prio = visible.map((d) => Math.abs(d.end - d.start))
    const lo2 = headH + FS / 2 + 2, hi2 = H - FS / 2 - 2
    const place = (which: 'start' | 'end') => {
      const at = placeLabels(visible.map((d) => py(d[which])), prio, gap, lo2, hi2)
      for (const [i, y] of at) {
        const d = visible[i]!
        const v = fmt.value(d[which])
        const room = side - (o.showValues ? ctx.measure(v) + 6 : 0)
        const name = ctx.truncate(d.label, Math.max(12, room))
        const text = o.showValues ? (which === 'start' ? `${name}  ${v}` : `${v}  ${name}`) : name
        const full = name !== d.label ? (o.showValues ? (which === 'start' ? `${d.label}  ${v}` : `${v}  ${d.label}`) : d.label) : undefined
        marks.push({
          type: 'text', layer: 'labels', key: `l${which}:${d.key}`, role: 'slope-label', x: which === 'start' ? xl - dot - 6 : xr + dot + 6, y,
          text, full, anchor: which === 'start' ? 'end' : 'start', baseline: 'central', fill: theme.inkSecondary, size: FS, className: full ? 'oc-trunc' : undefined,
        })
      }
      return at.size
    }
    const shown = Math.min(place('start'), place('end'))
    const dirs = (['up', 'down', 'flat'] as const).filter((k) => items.some((d) => d.dir === k))
    return {
      marks,
      legend: o.colorBy === 'direction' && dirs.length > 1 ? { items: dirs.map((k) => ({ key: k, label: DIR_LABEL[k], color: k === 'up' ? theme.positive : k === 'down' ? theme.negative : theme.neutral, shape: 'line' as const })) } : undefined,
      note: notes([[skipped, 'rows without both values left out'], [duplicates, 'repeated labels drawn as separate lines'], [visible.length - shown, 'labels hidden for space (hover a line)']]),
    }
  },
  describe({ data, options: o, fmt, name }) {
    const { items } = slopeItems(data)
    const up = items.filter((d) => d.dir === 'up'), down = items.filter((d) => d.dir === 'down')
    const parts = [`${name}, ${items.length} ${items.length === 1 ? 'item' : 'items'}, ${o.startLabel} to ${o.endLabel}`, `${up.length} rose and ${down.length} fell`]
    const best = up.reduce<Item | null>((a, d) => (!a || d.end - d.start > a.end - a.start ? d : a), null)
    const worst = down.reduce<Item | null>((a, d) => (!a || d.end - d.start < a.end - a.start ? d : a), null)
    if (best) parts.push(`biggest rise ${best.label} (${signed(best.end - best.start, fmt.value)})`)
    if (worst) parts.push(`biggest fall ${worst.label} (${signed(worst.end - worst.start, fmt.value)})`)
    return parts.join(', ')
  },
  table(data, o) {
    return { columns: ['Label', o.startLabel, o.endLabel, 'Change'], rows: data.map((r) => [r.label, r.start, r.end, r.start === null || r.end === null ? null : r.end - r.start]) }
  },
})

export default slope
