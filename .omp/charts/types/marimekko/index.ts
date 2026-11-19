// Marimekko (mosaic): columns whose widths are their share of the grand total, split
// into segments whose heights are each series' share of that column.

import { defineChart, phrase, SIZES } from '../../core'
import type { Mark } from '../../core'
import { notes, uniq } from '../../core/helpers'
import { input, options, type MarimekkoOptions, type MarimekkoRow } from './schema'

export interface MekkoSegment { series: string; value: number; seriesIndex: number }
export interface MekkoColumn { label: string; total: number; segments: MekkoSegment[] }
export interface Mekko {
  columns: MekkoColumn[]
  series: string[]
  total: number
  duplicates: number
  nulls: number
  negatives: number
}

/** Columns and segments after summing duplicates and dropping what has no area. */
export function mekko(rows: MarimekkoRow[], sort: MarimekkoOptions['sort'] = 'none', hidden: ReadonlySet<string> = new Set()): Mekko {
  const series = uniq(rows.map((r) => r.series))
  const cells = new Map<string, Map<string, number>>()
  let duplicates = 0, nulls = 0, negatives = 0
  const seen = new Set<string>()
  for (const r of rows) {
    const k = `${r.label}\u2063${r.series}`
    if (seen.has(k)) duplicates++
    seen.add(k)
    if (!cells.has(r.label)) cells.set(r.label, new Map())
    if (r.value === null) { nulls++; continue }
    if (r.value < 0) { negatives++; continue }
    const col = cells.get(r.label)!
    col.set(r.series, (col.get(r.series) ?? 0) + r.value)
  }
  let columns: MekkoColumn[] = []
  for (const [label, col] of cells) {
    const segments = series
      .map((s, i) => ({ series: s, value: col.get(s) ?? 0, seriesIndex: i }))
      .filter((s) => s.value > 0 && !hidden.has(s.series))
    const total = segments.reduce((a, s) => a + s.value, 0)
    if (total > 0) columns.push({ label, total, segments })
  }
  if (sort !== 'none') columns = [...columns].sort((a, b) => (sort === 'descending' ? b.total - a.total : a.total - b.total))
  return { columns, series, total: columns.reduce((a, c) => a + c.total, 0), duplicates, nulls, negatives }
}

export const marimekko = defineChart<MarimekkoRow[], MarimekkoOptions>({
  id: 'marimekko',
  name: 'Marimekko chart',
  category: 'part-to-whole',
  intents: ['composition', 'compare'],
  summary: 'Columns as wide as their share of the total, split by each series’ share of the column.',
  input,
  options,
  shape: 'rows',
  fields: ['label', 'series', 'value'],
  numberFields: ['value'],
  isEmpty(data) {
    if (data.length === 0) return true
    const m = mekko(data)
    if (m.total > 0) return false
    if (m.negatives > 0) return `No positive values to show (${phrase.count(m.negatives, 'negative value')})`
    return data.some((r) => r.value === 0) ? 'All values are zero' : true
  },
  aspect: 0.6,
  minHeight: 240,
  draw(ctx) {
    const { theme, fmt, options: o } = ctx
    const m = mekko(ctx.data, o.sort, ctx.hidden)
    const fs = SIZES.tick
    const lh = fs * 1.3
    const f = ctx.cartesian({
      x: { type: 'linear', domain: [0, 1], nice: false },
      y: { type: 'linear', domain: [0, 1], nice: false },
      yFormat: (v: number) => fmt.pct(v),
      xAxis: 'none',
      yTitle: o.yTitle,
      grid: 'none',
      pad: { bottom: lh * 2 + 6 },
    })
    const { plot } = f
    const marks: Mark[] = [...f.marks]
    const labels: Mark[] = []
    let x = plot.x
    for (const col of m.columns) {
      const w = m.total > 0 ? (col.total / m.total) * plot.width : 0
      let y = plot.y + plot.height
      for (const s of col.segments) {
        const h = (s.value / col.total) * plot.height
        y -= h
        const fill = theme.cat(s.seriesIndex)
        const inCol = s.value / col.total
        marks.push({
          type: 'rect', role: 'segment', key: `${col.label}\u2063${s.series}`, x, y, width: w, height: h,
          fill, stroke: theme.surface, strokeWidth: Math.min(w, h) < 4 ? 0.25 : 1, pattern: s.seriesIndex, series: s.series,
          datum: { label: col.label, series: s.series, value: s.value, shareOfColumn: inCol, shareOfTotal: s.value / m.total },
          label: `${col.label}, ${s.series}: ${fmt.value(s.value)} (${fmt.pct(inCol)} of ${col.label}, ${fmt.pct(s.value / m.total)} of total)`,
          tooltip: { title: `${col.label} · ${s.series}`, rows: [['Value', fmt.value(s.value)], [`Share of ${col.label}`, fmt.pct(inCol)], ['Share of total', fmt.pct(s.value / m.total)]], color: fill },
        })
        if (o.showValues) {
          const t = fmt.pct(inCol)
          if (h >= fs + 6 && w >= ctx.measure(t, fs) + 8) {
            labels.push({ type: 'text', layer: 'labels', key: `v:${col.label}\u2063${s.series}`, x: x + w / 2, y: y + h / 2, text: t, anchor: 'middle', baseline: 'central', fill: theme.surface, size: fs, inert: true })
          }
        }
      }
      // column name and total under the column, confined to its width
      if (w >= fs * 2.2) {
        const name = ctx.truncate(col.label, w - 4, fs)
        if (name && name !== '…') {
          const top = plot.y + plot.height + 6
          labels.push({ type: 'text', layer: 'labels', key: `c:${col.label}`, x: x + w / 2, y: top, text: name, full: name !== col.label ? col.label : undefined, anchor: 'middle', baseline: 'hanging', fill: theme.ink, size: fs, inert: name === col.label })
          const share = fmt.pct(col.total / m.total)
          if (ctx.measure(share, fs) <= w - 4) labels.push({ type: 'text', layer: 'labels', key: `ct:${col.label}`, x: x + w / 2, y: top + lh, text: share, anchor: 'middle', baseline: 'hanging', fill: theme.muted, size: fs, inert: true })
        }
      }
      x += w
    }
    return {
      marks: [...marks, ...labels],
      plot,
      legend: m.series.length > 1 ? { items: m.series.map((s, i) => ({ key: s, label: s, color: theme.cat(i), shape: 'square' as const, pattern: i })) } : undefined,
      note: notes([[m.negatives, 'negative values omitted'], [m.nulls, 'missing values left out'], [m.duplicates, 'duplicate label/series pairs summed']]),
    }
  },
  describe({ data, options: o, fmt, name }) {
    const m = mekko(data, o.sort)
    if (!m.columns.length) return `${name}, no values`
    const widest = m.columns.reduce((a, c) => (c.total > a.total ? c : a))
    return `${name}, ${phrase.count(m.columns.length, 'column')}, ${phrase.count(m.series.length, 'series', 'series')}, total ${fmt.value(m.total)}, widest ${widest.label} at ${fmt.pct(widest.total / m.total)}`
  },
  table(data, o) {
    const m = mekko(data, o.sort)
    const rows: Array<Array<string | number | null>> = []
    for (const c of m.columns) for (const s of c.segments) rows.push([c.label, s.series, s.value, s.value / c.total, s.value / m.total])
    return { columns: ['Label', 'Series', 'Value', 'Share of column', 'Share of total'], rows }
  },
})

export default marimekko
