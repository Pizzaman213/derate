// Linear progress: one horizontal track per item, filled to value / max, with its label
// and percentage. A missing value is a dashed, empty track -- never a 0% bar -- so
// "not reported" and "nothing done" cannot be confused. Tracks can be cut into steps
// or filled with stacked parts.

import { defineChart, phrase, SIZES, type Formatters, type LegendItem, type Mark } from '../../core'
import { notes, uniq } from '../../core/helpers'
import { adaptObjects, moreRow, rowLayout, uniqueKeys } from '../_shared/KpiCharts'
import { input, options, type ProgressItem, type ProgressOptions } from './schema'

/** The value a bar shows: its own, else the sum of its parts; null when missing. */
export function progressValue(d: ProgressItem): number | null {
  if (d.value !== null) return d.value
  if (Array.isArray(d.segments) && d.segments.length) return d.segments.reduce((s, p) => s + p.value, 0)
  return null
}

/** Filled share of the track in [0, 1]; null when missing. */
export function progressShare(d: ProgressItem): number | null {
  const v = progressValue(d)
  return v === null ? null : Math.min(1, Math.max(0, v / d.max))
}

function readout(d: ProgressItem, mode: ProgressOptions['showValues'], fmt: Formatters): string {
  const v = progressValue(d)
  if (v === null) return 'No data'
  const f = v / d.max
  const pct = fmt.pct(f, Math.abs(f) > 0 && Math.abs(f) < 0.1 ? 1 : 0)
  const abs = `${fmt.value(v)} / ${fmt.value(d.max)}`
  return mode === 'percent' ? pct : mode === 'value' ? abs : `${abs} · ${pct}`
}

const nameOf = (d: ProgressItem, i: number, n: number) => d.label ?? (n > 1 ? `Bar ${i + 1}` : 'Progress')

export const linearProgress = defineChart<ProgressItem[], ProgressOptions>({
  id: 'linear-progress',
  name: 'Progress bar',
  category: 'kpi',
  intents: ['kpi'],
  summary: 'Horizontal progress bars with labels and percentages; a missing value is a dashed empty track.',
  input,
  options,
  shape: 'object',
  fields: ['label', 'value', 'max'],
  adapt: adaptObjects,
  aspect: 0.3,
  minHeight: 160,
  maxHeight: 480,
  isEmpty: (data) => data.length === 0,
  draw(ctx) {
    const { theme, fmt, options: o } = ctx
    const items = ctx.data
    const n = items.length
    const keys = uniqueKeys(items.map((d, i) => d.label ?? `p${i + 1}`))
    const names = items.map((d, i) => nameOf(d, i, n))
    const texts = items.map((d) => readout(d, o.showValues, fmt))
    const anyLabel = items.some((d) => d.label)
    const L = rowLayout(ctx, anyLabel ? names : items.map(() => ''), texts, { labelSize: SIZES.label, valueSize: SIZES.label })
    const marks: Mark[] = []
    // parts share one colour per part label across bars, in first-seen order
    const partNames = uniq(items.flatMap((d) => (Array.isArray(d.segments) ? d.segments.map((p) => p.label) : [])))
    const partIndex = new Map(partNames.map((p, j) => [p, j]))
    for (let i = 0; i < L.shown; i++) {
      const d = items[i]!, key = keys[i]!, name = names[i]!
      const y0 = L.top + i * L.rowH
      const ty = L.stacked ? y0 + L.rowH - L.trackH - 4 : y0 + (L.rowH - L.trackH) / 2
      const textY = L.stacked ? ty - 6 : y0 + L.rowH / 2
      const tx = L.trackX, tw = L.trackW, th = L.trackH
      const rx = Math.min(3, th / 2)
      const v = progressValue(d)
      const share = progressShare(d)
      const missing = v === null
      const rowsTip: Array<[string, string]> = missing ? [['Value', 'no data'], ['Goal', fmt.value(d.max)]] : [['Progress', readout(d, 'percent', fmt)], ['Value', fmt.value(v)], ['Goal', fmt.value(d.max)]]
      // track
      marks.push(missing
        ? { type: 'rect', key: `${key}:track`, layer: 'back', x: tx + 0.75, y: ty + 0.75, width: Math.max(0, tw - 1.5), height: Math.max(0, th - 1.5), rx, fill: 'none', stroke: theme.muted, strokeWidth: 1.5, dash: '4 3', inert: true }
        : { type: 'rect', key: `${key}:track`, layer: 'back', x: tx, y: ty, width: tw, height: th, rx, fill: theme.track, inert: true })
      const parts = Array.isArray(d.segments) && d.segments.length ? d.segments : null
      if (parts && !missing) {
        // stacked parts; hidden ones (legend) close up
        let x = tx
        parts.forEach((p, j) => {
          if (ctx.hidden.has(p.label)) return
          const pw = Math.max(0, Math.min(tx + tw - x, (Math.max(0, p.value) / d.max) * tw))
          const ci = partIndex.get(p.label) ?? j
          const paint = theme.cat(ci)
          marks.push({
            type: 'rect', role: 'segment', key: `${key}:part:${p.label}#${j}`, series: p.label, x, y: ty, width: Math.max(pw, p.value > 0 ? 1 : 0), height: th, rx: Math.min(rx, pw / 2), fill: paint, pattern: ci,
            datum: { ...p, bar: name }, label: `${name}, ${p.label}: ${fmt.value(p.value)} (${fmt.pct(p.value / d.max)})`,
            tooltip: { title: name, rows: [[p.label, fmt.value(p.value)], ['Share of goal', fmt.pct(p.value / d.max)]], color: paint },
          })
          x += pw
        })
      } else {
        if (!missing && share !== null && share > 0) {
          marks.push({ type: 'rect', key: `${key}:fill`, x: tx, y: ty, width: Math.max(th, share * tw), height: th, rx, fill: v !== null && v > d.max ? theme.mix(theme.cat(0), theme.ink, 0.3) : theme.cat(0), inert: true })
        }
        // the whole track answers hover; the fill above it is inert
        marks.push({
          type: 'rect', role: 'progress', key: `${key}:value`, x: tx, y: ty - 3, width: tw, height: th + 6, fill: 'transparent',
          datum: d, label: missing ? `${name}: no data` : `${name}: ${readout(d, 'both', fmt)}`, tooltip: { title: name, rows: rowsTip, color: theme.cat(0) },
        })
      }
      // steps
      if (typeof d.segments === 'number' && d.segments > 1) {
        for (let k = 1; k < d.segments; k++) {
          const sx = tx + (k / d.segments) * tw
          marks.push({ type: 'line', key: `${key}:step:${k}`, layer: 'labels', x1: sx, x2: sx, y1: ty - 0.5, y2: ty + th + 0.5, stroke: theme.surface, strokeWidth: 2, inert: true })
        }
      }
      // label and value
      if (anyLabel) {
        const lt = ctx.truncate(name, L.labelW, SIZES.label)
        if (lt) marks.push({ type: 'text', key: `${key}:label`, layer: 'labels', x: L.labelX, y: textY, text: lt, full: lt !== name ? name : undefined, anchor: 'start', baseline: L.stacked ? 'alphabetic' : 'central', size: SIZES.label, fill: theme.inkSecondary })
      }
      const t = texts[i]!
      marks.push({ type: 'text', key: `${key}:readout`, layer: 'labels', x: L.valueX, y: textY, text: t, anchor: 'end', baseline: L.stacked ? 'alphabetic' : 'central', size: SIZES.label, weight: missing ? 400 : 600, fill: missing ? theme.muted : theme.ink, inert: true })
    }
    marks.push(...moreRow(ctx, L, 'bars'))
    const nulls = items.filter((d) => progressValue(d) === null).length
    const over = items.filter((d) => { const v = progressValue(d); return v !== null && v > d.max }).length
    const negatives = items.filter((d) => { const v = progressValue(d); return v !== null && v < 0 }).length
    const legendItems: LegendItem[] = partNames.map((p, j) => ({ key: p, label: p, color: theme.cat(j), shape: 'square', pattern: j }))
    return {
      marks,
      legend: legendItems.length > 1 ? { items: legendItems } : undefined,
      note: notes([
        [nulls, nulls === 1 ? 'value missing (dashed track)' : 'values missing (dashed tracks)'],
        [over, over === 1 ? 'bar past its goal, drawn full' : 'bars past their goal, drawn full'],
        [negatives, negatives === 1 ? 'negative value drawn empty' : 'negative values drawn empty'],
      ]),
    }
  },
  describe({ data, fmt, name }) {
    if (data.length === 1) {
      const d = data[0]!
      const v = progressValue(d)
      return v === null ? `${name}, ${d.label ?? 'progress'}, no data` : `${name}, ${d.label ?? 'progress'} at ${readout(d, 'percent', fmt)}, ${fmt.value(v)} of ${fmt.value(d.max)}`
    }
    const items = data.map((d, i) => { const v = progressValue(d); return { label: nameOf(d, i, data.length), value: v === null ? null : v / d.max } })
    const missing = items.filter((d) => d.value === null).length
    return `${name}, ${phrase.count(data.length, 'bar')}, ${phrase.extremes(items, fmt, (v) => fmt.pct(v))}${missing ? `, ${missing} with no data` : ''}`
  },
  table(data) {
    return {
      columns: ['Label', 'Value', 'Max', 'Fraction'],
      rows: data.map((d, i) => { const v = progressValue(d); return [nameOf(d, i, data.length), v, d.max, v === null ? null : v / d.max] }),
    }
  },
})

export default linearProgress
