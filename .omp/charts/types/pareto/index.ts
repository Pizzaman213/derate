// Pareto chart: causes as bars sorted largest first, with the cumulative share as a
// line on a right-hand 0-100% axis and a reference line at the threshold (80%).
// The second y axis is inherent to the chart: two measures of one ordering.

import { defineChart, phrase, SIZES } from '../../core'
import { collapseLabels, notes } from '../../core/helpers'
import type { Mark } from '../../core'
import { input, options, type ParetoOptions, type ParetoRow } from './schema'

export interface ParetoItem { label: string; value: number; cumulative: number; other?: number }

/** Sorted items with running cumulative share; the tail past maxBars becomes "Other". */
export function paretoItems(data: ParetoRow[], maxBars: number): { items: ParetoItem[]; total: number; duplicates: number; negatives: number; nulls: number; collapsed: number } {
  const { items: raw, duplicates } = collapseLabels(data)
  const negatives = raw.filter((d) => d.value !== null && d.value < 0).length
  const nulls = raw.filter((d) => d.value === null).length
  let ok = raw.filter((d): d is { label: string; value: number } => d.value !== null && d.value >= 0).sort((a, b) => b.value - a.value)
  let collapsed = 0
  let other: { label: string; value: number; other: number } | null = null
  if (ok.length > maxBars) {
    const tail = ok.slice(maxBars - 1)
    collapsed = tail.length
    ok = ok.slice(0, maxBars - 1)
    let name = 'Other', n = 2
    while (ok.some((d) => d.label === name)) name = `Other (${n++})`
    other = { label: name, value: tail.reduce((a, d) => a + d.value, 0), other: collapsed }
  }
  const list: Array<{ label: string; value: number; other?: number }> = other ? [...ok, other] : ok
  const total = list.reduce((a, d) => a + d.value, 0)
  let run = 0
  const items = list.map((d) => {
    run += d.value
    return { ...d, cumulative: total > 0 ? run / total : 0 }
  })
  return { items, total, duplicates, negatives, nulls, collapsed }
}

export const pareto = defineChart<ParetoRow[], ParetoOptions>({
  id: 'pareto',
  name: 'Pareto chart',
  category: 'comparison',
  intents: ['rank', 'compare'],
  summary: 'Causes as bars sorted largest first, with the cumulative share as a line against an 80% reference.',
  input,
  options,
  shape: 'rows',
  fields: ['label', 'value'],
  numberFields: ['value'],
  isEmpty(data) {
    if (!data.length) return true
    return data.some((r) => r.value !== null && r.value > 0) ? false : 'All values are zero'
  },
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    const p = paretoItems(ctx.data, o.maxBars)
    const { items } = p
    const max = Math.max(0, ...items.map((d) => d.value))
    const tick = 4, gap = 4
    const titleW = o.cumulativeTitle ? SIZES.label + 6 : 0
    const rightW = ctx.measure(fmt.pct(1)) + tick + gap + titleW
    const f = ctx.cartesian({
      x: { type: 'band', domain: items.map((d) => d.label), padding: 0.15 },
      y: { type: 'linear', domain: [0, max], zero: true },
      xTitle: o.xTitle,
      yTitle: o.yTitle,
      pad: { top: o.showValues ? 14 : 0, right: rightW },
    })
    const { plot } = f
    const right = plot.x + plot.width
    const cy = (c: number) => plot.y + plot.height * (1 - c)
    const marks: Mark[] = [...f.marks]
    // right-hand cumulative axis, 0-100% in 20% steps
    marks.push({ type: 'line', layer: 'axes', key: 'y2-domain', x1: right, x2: right, y1: plot.y, y2: plot.y + plot.height, stroke: theme.axis, strokeWidth: 1, inert: true })
    const stepPct = plot.height < 120 ? 0.5 : plot.height < 220 ? 0.25 : 0.2
    for (let c = 0; c <= 1 + 1e-9; c += stepPct) {
      const y = cy(c)
      marks.push({ type: 'line', layer: 'axes', key: `y2t:${c.toFixed(2)}`, x1: right, x2: right + tick, y1: y, y2: y, stroke: theme.axis, strokeWidth: 1, inert: true })
      marks.push({ type: 'text', layer: 'axes', key: `y2l:${c.toFixed(2)}`, x: right + tick + gap, y, text: fmt.pct(c), anchor: 'start', baseline: 'central', size: SIZES.tick, fill: theme.inkSecondary })
    }
    if (o.cumulativeTitle) {
      const t = ctx.truncate(o.cumulativeTitle, plot.height, SIZES.label)
      marks.push({ type: 'text', layer: 'axes', key: 'y2-title', x: ctx.width - 4 - SIZES.label / 2, y: plot.y + plot.height / 2, text: t, full: t !== o.cumulativeTitle ? o.cumulativeTitle : undefined, anchor: 'middle', baseline: 'central', rotate: 90, size: SIZES.label, fill: theme.muted })
    }
    // threshold reference
    const ty = cy(o.threshold)
    marks.push({ type: 'line', layer: 'grid', key: 'threshold', x1: plot.x, x2: right, y1: ty, y2: ty, stroke: theme.muted, strokeWidth: 1.5, dash: '5 4', inert: true })
    const y0 = f.py(0)
    const bar = theme.cat(0), line = theme.cat(1)
    const showVals = o.showValues && f.bw >= ctx.measure(fmt.value(max)) * 0.9
    items.forEach((d, i) => {
      const x = f.px(d.label), yv = f.py(d.value)
      const name = d.other ? `${d.label} (${d.other} categories)` : d.label
      const rows: Array<[string, string]> = [['Value', fmt.value(d.value)], ['Share', fmt.pct(p.total > 0 ? d.value / p.total : 0)], ['Cumulative', fmt.pct(d.cumulative)]]
      marks.push({
        type: 'rect', role: 'bar', key: d.label, series: 'value', x, width: Math.max(0.5, f.bw), y: d.value === 0 ? y0 - 0.5 : yv, height: d.value === 0 ? 1 : y0 - yv,
        rx: Math.min(2, f.bw / 4), fill: d.other ? theme.neutral : bar, pattern: 0,
        datum: d, label: `${i + 1}. ${name}: ${fmt.value(d.value)}, cumulative ${fmt.pct(d.cumulative)}`, tooltip: { title: name, rows, color: bar },
      })
      if (showVals) marks.push({ type: 'text', layer: 'labels', key: `v:${d.label}`, x: x + f.bw / 2, y: yv - 4, text: fmt.value(d.value), anchor: 'middle', baseline: 'alphabetic', fill: theme.ink, inert: true })
    })
    const pts = items.map((d) => [f.px(d.label) + f.bw / 2, cy(d.cumulative)] as const)
    if (pts.length) {
      marks.push({ type: 'path', layer: 'labels', key: 'cumulative', d: `M${pts.map(([x, y]) => `${x},${y}`).join('L')}`, fill: 'none', stroke: line, strokeWidth: 2, linejoin: 'round', inert: true })
      const r = Math.max(1.5, Math.min(4, f.bw / 4))
      items.forEach((d, i) => {
        const [x, y] = pts[i]!
        marks.push({
          type: 'circle', layer: 'labels', role: 'point', key: `cum:${d.label}`, series: 'cumulative', cx: x, cy: y, r, fill: line, stroke: theme.surface, strokeWidth: 1,
          datum: { label: d.label, cumulative: d.cumulative }, label: `${d.label}: cumulative ${fmt.pct(d.cumulative)}`,
          tooltip: { title: d.label, rows: [['Cumulative', fmt.pct(d.cumulative)]], color: line },
        })
      })
    }
    return {
      marks,
      plot,
      legend: { items: [
        { key: 'value', label: o.yTitle ?? 'Value', color: bar, shape: 'square', pattern: 0 },
        { key: 'cumulative', label: o.cumulativeTitle || 'Cumulative %', color: line, shape: 'line' },
        { key: 'threshold', label: `${fmt.pct(o.threshold)} threshold`, color: theme.muted, shape: 'dash' },
      ], toggle: false },
      note: notes([
        [p.collapsed, 'smallest categories combined into Other'],
        [p.duplicates, 'duplicate labels summed'],
        [p.negatives, 'negative values omitted'],
        [p.nulls, 'missing values omitted'],
      ]),
    }
  },
  describe({ data, options: o, fmt, name }) {
    const p = paretoItems(data, o.maxBars ?? 20)
    const th = o.threshold ?? 0.8
    const k = p.items.findIndex((d) => d.cumulative >= th - 1e-12) + 1
    const top = p.items[0]
    return `${name}, ${phrase.count(p.items.length, 'category', 'categories')}, largest ${top ? `${top.label} at ${fmt.value(top.value)}` : 'none'}, ${phrase.count(k, 'category', 'categories')} reach ${fmt.pct(th)} of the total`
  },
  table(data, o) {
    const p = paretoItems(data, o.maxBars ?? 20)
    return { columns: ['Label', 'Value', 'Cumulative share'], rows: p.items.map((d) => [d.label, d.value, d.cumulative]) }
  },
})

export default pareto
