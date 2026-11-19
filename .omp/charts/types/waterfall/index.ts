// Waterfall chart: how a starting value becomes an ending value through a run of
// increases and decreases. Each change floats from the previous running total; totals
// stand on zero. A 'total' row is always drawn at the running total at that point.

import { defineChart, phrase, SIZES } from '../../core'
import { notes } from '../../core/helpers'
import type { LegendItem, Mark, Paint } from '../../core'
import { signed, uniqueKeys } from '../_shared/ComparisonB'
import { input, options, type WaterfallOptions, type WaterfallRow } from './schema'

export interface Step {
  key: string
  label: string
  kind: 'delta' | 'total'
  /** The change (deltas) or the running total (totals); null for a missing delta. */
  value: number | null
  /** Running total before and after this step; a total spans 0 -> running total. */
  from: number
  to: number
  /** A total appended by `showTotal`, not in the input. */
  appended: boolean
  /** A total row whose given value disagreed with the running total. */
  given?: number
}

/**
 * The running-total arithmetic. A total row that comes before any change sets the
 * opening balance from its value; every later total equals the running total.
 */
export function waterfallSteps(rows: WaterfallRow[], o: Pick<WaterfallOptions, 'showTotal' | 'totalLabel'>): Step[] {
  const keys = uniqueKeys([...rows.map((d) => d.label), o.totalLabel])
  const steps: Step[] = []
  let run = 0, started = false
  rows.forEach((d, i) => {
    const key = keys[i]!
    if (d.kind === 'total') {
      if (!started && d.value !== null) run = d.value
      started = true
      const given = d.value !== null && d.value !== run ? d.value : undefined
      steps.push({ key, label: d.label, kind: 'total', value: run, from: 0, to: run, appended: false, given })
      return
    }
    started = true
    const from = run
    if (d.value !== null) run += d.value
    steps.push({ key, label: d.label, kind: 'delta', value: d.value, from, to: run, appended: false })
  })
  const last = steps[steps.length - 1]
  if (o.showTotal && last && last.kind !== 'total') {
    const key = rows.some((d) => d.label === o.totalLabel) ? keys[rows.length]! : o.totalLabel
    steps.push({ key, label: o.totalLabel, kind: 'total', value: run, from: 0, to: run, appended: true })
  }
  return steps
}

const LEGEND: Array<['increase' | 'decrease' | 'total', string]> = [['increase', 'Increase'], ['decrease', 'Decrease'], ['total', 'Total']]

export const waterfall = defineChart<WaterfallRow[], WaterfallOptions>({
  id: 'waterfall',
  name: 'Waterfall chart',
  category: 'comparison',
  intents: ['compare', 'change-over-time'],
  summary: 'Floating bars that walk a running total from a start to an end through increases and decreases.',
  input,
  options,
  shape: 'rows',
  fields: ['label', 'value', 'kind'],
  numberFields: ['value'],
  minHeight: 180,
  isEmpty: (data) => data.length === 0 || data.every((d) => d.value === null),
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    const steps = waterfallSteps(ctx.data, o)
    const labelOf = new Map(steps.map((s) => [s.key, s.label]))
    const paint = (s: Step): Paint => (s.kind === 'total' ? theme.neutral : (s.value ?? 0) >= 0 ? theme.positive : theme.negative)
    const text = (s: Step) => (s.kind === 'total' ? fmt.value(s.to) : signed(s.value ?? 0, fmt.value))
    let lo = 0, hi = 0
    for (const s of steps) lo = Math.min(lo, s.from, s.to), hi = Math.max(hi, s.from, s.to)
    const labelRoom = o.showValues ? SIZES.tick + 6 : 0
    const f = ctx.cartesian({
      x: { type: 'band', domain: steps.map((s) => s.key), padding: 0.25 },
      y: { type: 'linear', domain: [lo, hi], zero: true },
      xFormat: (k: string) => labelOf.get(k) ?? k,
      xTitle: o.xTitle,
      yTitle: o.yTitle,
      pad: { top: labelRoom, bottom: lo < 0 ? labelRoom : 0 },
    })
    const marks: Mark[] = [...f.marks]
    const widest = Math.max(0, ...steps.map((s) => ctx.measure(text(s))))
    const showVals = o.showValues && f.bw >= widest + 2
    let prev: { x: number; level: number } | null = null
    for (const s of steps) {
      if (s.kind === 'delta' && s.value === null) continue
      const x = f.px(s.key)
      const y0 = f.py(s.from), y1 = f.py(s.to)
      const flat = Math.abs(y1 - y0) < 1
      const fill = paint(s)
      if (o.connectors && prev) {
        marks.push({ type: 'line', layer: 'grid', key: `k:${s.key}`, x1: prev.x, x2: x, y1: f.py(prev.level), y2: f.py(prev.level), stroke: theme.axis, strokeWidth: 1, dash: '3 2', inert: true })
      }
      const rows: Array<[string, string]> = s.kind === 'total'
        ? [['Running total', fmt.value(s.to)]]
        : [['Change', signed(s.value ?? 0, fmt.value)], ['Running total', `${fmt.value(s.from)} → ${fmt.value(s.to)}`]]
      if (s.given !== undefined) rows.push(['Given', fmt.value(s.given)])
      const what = s.kind === 'total' ? `total ${fmt.value(s.to)}` : `${signed(s.value ?? 0, fmt.value)}, running total ${fmt.value(s.to)}`
      marks.push({
        type: 'rect', role: 'bar', key: s.key, series: s.kind === 'total' ? 'total' : (s.value ?? 0) >= 0 ? 'increase' : 'decrease',
        x, width: Math.max(0.5, f.bw), y: flat ? (y0 + y1) / 2 - 0.5 : Math.min(y0, y1), height: flat ? 1 : Math.abs(y1 - y0),
        rx: Math.min(2, f.bw / 4), fill, pattern: s.kind === 'total' ? 0 : (s.value ?? 0) >= 0 ? 3 : 1,
        datum: s, label: `${s.label}: ${what}`, tooltip: { title: s.label, rows, color: fill },
      })
      if (showVals) {
        // above the bar's top for rises and positive totals, below its bottom otherwise
        const up = s.kind === 'total' ? s.to >= 0 : (s.value ?? 0) >= 0
        const top = Math.min(y0, y1), bottom = Math.max(y0, y1)
        marks.push({ type: 'text', layer: 'labels', key: `v:${s.key}`, x: x + f.bw / 2, y: up ? top - 4 : bottom + 4, text: text(s), anchor: 'middle', baseline: up ? 'alphabetic' : 'hanging', size: SIZES.tick, fill: theme.inkSecondary, inert: true })
      }
      prev = { x: x + f.bw, level: s.to }
    }
    const present = new Set(steps.filter((s) => s.kind === 'total' || s.value !== null).map((s) => (s.kind === 'total' ? 'total' : (s.value ?? 0) >= 0 ? 'increase' : 'decrease')))
    const legend = {
      // hiding a step would silently change every running total after it
      toggle: false,
      items: LEGEND.filter(([k]) => present.has(k)).map(([k, label]): LegendItem => ({ key: k, label, color: k === 'total' ? theme.neutral : k === 'increase' ? theme.positive : theme.negative })),
    }
    const missing = steps.filter((s) => s.kind === 'delta' && s.value === null).length
    const mismatched = steps.filter((s) => s.given !== undefined).length
    const repeats = ctx.data.length - new Set(ctx.data.map((d) => d.label)).size
    return {
      marks, legend, plot: f.plot,
      note: notes([[missing, 'missing changes treated as no change'], [mismatched, 'totals differed from the running total and were drawn at it'], [repeats, 'repeated labels kept as separate steps']]),
    }
  },
  describe({ data, options: o, fmt, name }) {
    const steps = waterfallSteps(data, o)
    const deltas = steps.filter((s): s is Step & { value: number } => s.kind === 'delta' && s.value !== null)
    const first = steps[0]
    const start = first?.kind === 'total' ? first.to : 0
    const end = steps[steps.length - 1]?.to ?? 0
    type Change = Step & { value: number }
    const up = deltas.reduce<Change | null>((m, s) => (s.value > 0 && (!m || s.value > m.value) ? s : m), null)
    const down = deltas.reduce<Change | null>((m, s) => (s.value < 0 && (!m || s.value < m.value) ? s : m), null)
    const parts = [`${name}, ${phrase.count(deltas.length, 'change')}, from ${fmt.value(start)} to ${fmt.value(end)} (net ${signed(end - start, fmt.value)})`]
    if (up) parts.push(`largest increase ${up.label} ${signed(up.value, fmt.value)}`)
    if (down) parts.push(`largest decrease ${down.label} ${signed(down.value, fmt.value)}`)
    return parts.join(', ')
  },
  table(data, o) {
    return {
      columns: ['Label', 'Kind', 'Change', 'Running total'],
      rows: waterfallSteps(data, o).map((s) => [s.label, s.kind, s.kind === 'total' ? null : s.value, s.to]),
    }
  },
})

export default waterfall
