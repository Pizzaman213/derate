// Dumbbell chart: two values per category (before/after) as a hollow and a filled dot
// joined by a line, coloured by the direction of the change. Data marks are the two
// endpoints: a complete row counts 2, a row with one value missing counts 1.

import { defineChart, phrase } from '../../core'
import { finiteExtent, notes } from '../../core/helpers'
import type { LegendItem, Mark, Paint } from '../../core'
import { signed, uniqueKeys } from '../_shared/ComparisonB'
import { input, options, type DumbbellOptions, type DumbbellRow } from './schema'

type Direction = 'increase' | 'decrease' | 'unchanged'
const DIRECTIONS: Array<[Direction, string]> = [['increase', 'Increase'], ['decrease', 'Decrease'], ['unchanged', 'No change']]

const directionOf = (d: DumbbellRow): Direction | null =>
  d.start === null || d.end === null ? null : d.end > d.start ? 'increase' : d.end < d.start ? 'decrease' : 'unchanged'

/** Rows in display order: descending by the chosen measure, rows without it last (stable). */
function ordered(rows: DumbbellRow[], sort: DumbbellOptions['sort']): DumbbellRow[] {
  if (sort === 'none') return rows
  const by = (d: DumbbellRow) => (sort === 'start' ? d.start : sort === 'end' ? d.end : d.start === null || d.end === null ? null : d.end - d.start)
  return [...rows].sort((a, b) => {
    const va = by(a), vb = by(b)
    return va === null ? (vb === null ? 0 : 1) : vb === null ? -1 : vb - va
  })
}

export const dumbbell = defineChart<DumbbellRow[], DumbbellOptions>({
  id: 'dumbbell',
  name: 'Dumbbell chart',
  category: 'comparison',
  intents: ['compare', 'rank', 'deviation', 'change-over-time'],
  summary: 'Two values per category joined by a line, coloured by the direction of the change.',
  input,
  options,
  shape: 'rows',
  fields: ['label', 'start', 'end'],
  numberFields: ['start', 'end'],
  minHeight: 160,
  isEmpty: (data) => data.length === 0 || data.every((d) => d.start === null && d.end === null),
  draw(ctx) {
    const { theme, options: o, fmt, hidden } = ctx
    const rows = ordered(ctx.data, o.sort)
    const keys = uniqueKeys(rows.map((d) => d.label))
    const labelOf = new Map(keys.map((k, i) => [k, rows[i]!.label]))
    const good = theme.positive, bad = theme.negative
    const color: Record<Direction, Paint> = { increase: o.higherIsBetter ? good : bad, decrease: o.higherIsBetter ? bad : good, unchanged: theme.neutral }
    const ext = finiteExtent(rows.flatMap((d) => [d.start, d.end])) ?? [0, 1]
    let [lo, hi] = o.zero ? [Math.min(0, ext[0]), Math.max(0, ext[1])] : ext
    const span = hi - lo || Math.abs(hi) * 0.2 || 1
    if (!(o.zero && lo === 0)) lo -= span * 0.02
    if (!(o.zero && hi === 0)) hi += span * 0.02
    const f = ctx.cartesian({
      y: { type: 'band', domain: keys, padding: 0 },
      x: { type: 'linear', domain: [lo, hi], zero: o.zero },
      yFormat: (k: string) => labelOf.get(k) ?? k,
      xTitle: o.xTitle,
      yTitle: o.yTitle,
    })
    const marks: Mark[] = [...f.marks]
    const r = Math.max(2, Math.min(5.5, f.bh * 0.3))
    const stroke = Math.max(1, Math.min(3, r * 0.6))
    rows.forEach((d, i) => {
      const key = keys[i]!
      const dir = directionOf(d)
      if (dir && hidden.has(dir)) return
      const y = f.py(key) + f.bh / 2
      const paint = dir ? color[dir] : theme.neutral
      const change = d.start !== null && d.end !== null ? d.end - d.start : null
      const rowsTip: Array<[string, string]> = [[o.startLabel, d.start === null ? '—' : fmt.value(d.start)], [o.endLabel, d.end === null ? '—' : fmt.value(d.end)]]
      if (change !== null) rowsTip.push(['Change', signed(change, fmt.value)])
      const tooltip = { title: d.label, rows: rowsTip, color: paint }
      const tail = change === null ? '' : `, ${signed(change, fmt.value)} from ${o.startLabel}`
      if (d.start !== null && d.end !== null && Math.abs(f.px(d.end) - f.px(d.start)) > r * 2) {
        const x1 = f.px(d.start), x2 = f.px(d.end), s = Math.sign(x2 - x1)
        marks.push({ type: 'line', key: `c:${key}`, x1: x1 + s * r, x2: x2 - s * r, y1: y, y2: y, stroke: paint, strokeWidth: stroke, inert: true })
      }
      if (d.start !== null) {
        marks.push({
          type: 'circle', role: 'point', key: `s:${key}`, series: dir ?? undefined, cx: f.px(d.start), cy: y, r: Math.max(1.5, r - stroke / 2),
          fill: theme.surface, stroke: paint, strokeWidth: stroke,
          datum: { label: d.label, which: 'start', value: d.start }, label: `${d.label}, ${o.startLabel}: ${fmt.value(d.start)}`, tooltip,
        })
      }
      if (d.end !== null) {
        marks.push({
          type: 'circle', role: 'point', key: `e:${key}`, series: dir ?? undefined, cx: f.px(d.end), cy: y, r,
          fill: paint, stroke: theme.surface, strokeWidth: r >= 3 ? 1 : 0,
          datum: { label: d.label, which: 'end', value: d.end }, label: `${d.label}, ${o.endLabel}: ${fmt.value(d.end)}${tail}`, tooltip,
        })
      }
    })
    const present = new Set(rows.map(directionOf))
    const legend = {
      title: `○ ${o.startLabel} → ● ${o.endLabel}`,
      items: DIRECTIONS.filter(([k]) => present.has(k)).map(([k, label]): LegendItem => ({ key: k, label, color: color[k], shape: 'line' })),
    }
    const partial = rows.filter((d) => (d.start === null) !== (d.end === null)).length
    const blank = rows.filter((d) => d.start === null && d.end === null).length
    return {
      marks, legend, plot: f.plot,
      note: notes([[rows.length - new Set(rows.map((d) => d.label)).size, 'repeated labels drawn as separate rows'], [partial, 'rows missing one value (one dot drawn)'], [blank, 'rows with no values left blank']]),
    }
  },
  describe({ data, options: o, fmt, name }) {
    const up = data.filter((d) => directionOf(d) === 'increase').length
    const down = data.filter((d) => directionOf(d) === 'decrease').length
    let big: { label: string; change: number } | null = null
    for (const d of data) {
      if (d.start === null || d.end === null) continue
      const change = d.end - d.start
      if (!big || Math.abs(change) > Math.abs(big.change)) big = { label: d.label, change }
    }
    const notable = big && big.change !== 0 ? `, largest change ${big.label} ${signed(big.change, fmt.value)}` : ''
    return `${name}, ${phrase.count(data.length, 'row')}, ${o.startLabel} to ${o.endLabel}, ${up} up, ${down} down${notable}`
  },
  table(data, o) {
    return {
      columns: ['Label', o.startLabel, o.endLabel, 'Change'],
      rows: data.map((d) => [d.label, d.start, d.end, d.start === null || d.end === null ? null : d.end - d.start]),
    }
  },
})

export default dumbbell
