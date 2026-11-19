// Lollipop chart: a thin stem from a baseline to a dot, one per category. Reads like a
// bar chart with less ink, which keeps many near-equal values legible.

import { defineChart, phrase, SIZES } from '../../core'
import { collapseLabels, finiteExtent, notes, sortItems } from '../../core/helpers'
import type { Mark } from '../../core'
import { signed } from '../_shared/ComparisonB'
import { input, options, type LollipopOptions, type LollipopRow } from './schema'

export const lollipop = defineChart<LollipopRow[], LollipopOptions>({
  id: 'lollipop',
  name: 'Lollipop chart',
  category: 'comparison',
  intents: ['compare', 'rank'],
  summary: 'A stem and a dot per category: a lighter-ink bar chart for comparing and ranking one value.',
  input,
  options,
  shape: 'rows',
  fields: ['label', 'value'],
  numberFields: ['value'],
  minHeight: 160,
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    const { items: raw, duplicates } = collapseLabels(ctx.data)
    const items = sortItems(raw, o.sort)
    const base = o.baseline
    const ext = finiteExtent(items.map((d) => d.value)) ?? [base, base + 1]
    // room for the dot's radius past the data extremes (the baseline end stays flush)
    const span = Math.max(base, ext[1]) - Math.min(base, ext[0]) || 1
    const lo = ext[0] < base ? ext[0] - span * 0.04 : base
    const hi = ext[1] > base ? ext[1] + span * 0.04 : lo === base ? base + 1 : base
    const horizontal = o.orientation === 'horizontal'
    const valueW = o.showValues ? Math.max(ctx.measure(fmt.value(ext[0])), ctx.measure(fmt.value(ext[1]))) + 10 : 0
    const valueH = o.showValues ? SIZES.tick + 8 : 0
    const labels = items.map((d) => d.label)
    const value = { type: 'linear' as const, domain: [lo, hi] as [number, number], zero: false }
    const band = { type: 'band' as const, domain: labels, padding: 0 }
    const f = horizontal
      ? ctx.cartesian({ y: band, x: value, xTitle: o.xTitle, yTitle: o.yTitle, pad: { right: ext[1] > base ? valueW : 0, left: ext[0] < base ? valueW : 0 } })
      : ctx.cartesian({ x: band, y: value, xTitle: o.xTitle, yTitle: o.yTitle, pad: { top: ext[1] > base ? valueH : 0, bottom: ext[0] < base ? valueH : 0 } })
    const thick = horizontal ? f.bh : f.bw
    const r = Math.max(1.5, Math.min(5.5, thick * 0.32))
    const color = theme.cat(0)
    const stem = theme.mix(color, theme.surface, 0.3)
    const marks: Mark[] = [...f.marks]
    const vPos = (v: number) => (horizontal ? f.px(v) : f.py(v))
    const b = vPos(base)
    if (base !== 0) {
      const p = f.plot
      marks.push(horizontal
        ? { type: 'line', layer: 'grid', key: 'baseline', x1: b, x2: b, y1: p.y, y2: p.y + p.height, stroke: theme.ruleStrong, strokeWidth: 1, inert: true }
        : { type: 'line', layer: 'grid', key: 'baseline', x1: p.x, x2: p.x + p.width, y1: b, y2: b, stroke: theme.ruleStrong, strokeWidth: 1, inert: true })
    }
    const showVals = o.showValues && (horizontal ? thick >= SIZES.tick : thick >= ctx.measure(fmt.value(ext[1])) * 0.9)
    items.forEach((d) => {
      if (d.value === null) return
      const c = (horizontal ? f.py(d.label) : f.px(d.label)) + thick / 2
      const v = vPos(d.value)
      const up = d.value >= base
      const text = `${d.label}: ${fmt.value(d.value)}`
      if (Math.abs(v - b) > r) {
        marks.push(horizontal
          ? { type: 'line', key: `s:${d.label}`, x1: b, x2: v + (up ? -r : r), y1: c, y2: c, stroke: stem, strokeWidth: Math.min(2, Math.max(1, r / 2.5)), inert: true }
          : { type: 'line', key: `s:${d.label}`, x1: c, x2: c, y1: b, y2: v + (up ? r : -r), stroke: stem, strokeWidth: Math.min(2, Math.max(1, r / 2.5)), inert: true })
      }
      const rows: Array<[string, string]> = [['Value', fmt.value(d.value)]]
      if (base !== 0) rows.push(['From baseline', signed(d.value - base, fmt.value)])
      marks.push({
        type: 'circle', role: 'point', key: d.label, cx: horizontal ? v : c, cy: horizontal ? c : v, r,
        fill: color, datum: d, label: text, tooltip: { title: d.label, rows, color },
      })
      if (showVals) {
        const t = fmt.value(d.value)
        marks.push(horizontal
          ? { type: 'text', layer: 'labels', key: `v:${d.label}`, x: up ? v + r + 4 : v - r - 4, y: c, text: t, anchor: up ? 'start' : 'end', baseline: 'central', size: SIZES.tick, fill: theme.inkSecondary, inert: true }
          : { type: 'text', layer: 'labels', key: `v:${d.label}`, x: c, y: up ? v - r - 4 : v + r + 4, text: t, anchor: 'middle', baseline: up ? 'alphabetic' : 'hanging', size: SIZES.tick, fill: theme.inkSecondary, inert: true })
      }
    })
    const nulls = items.filter((d) => d.value === null).length
    return { marks, plot: f.plot, note: notes([[duplicates, 'duplicate labels summed'], [nulls, 'missing values left blank']]) }
  },
  describe({ data, fmt, name }) {
    const { items } = collapseLabels(data)
    return `${name}, ${phrase.count(items.length, 'item')}, ${phrase.extremes(items, fmt)}`
  },
  table(data) {
    const { items } = collapseLabels(data)
    return { columns: ['Label', 'Value'], rows: items.map((d) => [d.label, d.value]) }
  },
})

export default lollipop
