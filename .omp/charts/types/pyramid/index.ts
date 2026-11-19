// Pyramid chart: a triangle cut into horizontal bands, one per level, each band's
// AREA proportional to its value (heights are not: a band near the apex is taller
// than one of the same value near the base, because the triangle is narrower there).

import { defineChart, SIZES } from '../../core'
import type { Mark } from '../../core'
import { colorOf, describeParts, emptyMessage, legendOf, partsTable, patternOf, prepare, prepNote, spreadLabels } from '../_shared/PartWholeA'
import { input, options, type PyramidOptions, type PyramidRow } from './schema'

export interface Band {
  /** Distance from the apex, top and bottom edge, in units of the triangle height. */
  t0: number
  t1: number
  share: number
}

/**
 * Bands of a unit triangle (apex at t = 0, base at t = 1). The area above depth t is
 * t², so a band ending at cumulative share c ends at t = √c: areas are exactly the shares.
 */
export function pyramidBands(values: number[]): Band[] {
  const sum = values.reduce((s, v) => s + v, 0)
  let c = 0
  return values.map((v) => {
    const share = sum > 0 ? v / sum : 0
    const t0 = Math.sqrt(c)
    c = Math.min(1, c + share)
    return { t0, t1: Math.sqrt(c), share }
  })
}

const MAX_LEGEND = 12

export const pyramid = defineChart<PyramidRow[], PyramidOptions>({
  id: 'pyramid',
  name: 'Pyramid chart',
  category: 'part-to-whole',
  intents: ['composition'],
  summary: 'A triangle sliced into levels whose areas are proportional to their values.',
  input,
  options,
  shape: 'rows',
  fields: ['label', 'value'],
  numberFields: ['value'],
  aspect: 0.7,
  minHeight: 220,
  isEmpty: (data) => emptyMessage(data),
  draw(ctx) {
    const { theme, fmt, options: o, width: W, height: H } = ctx
    const prep = prepare(o.order === 'base-first' ? [...ctx.data].reverse() : ctx.data)
    const vis = prep.slices.filter((s) => !ctx.hidden.has(s.key))
    const legend = prep.slices.length > 1 && prep.slices.length <= MAX_LEGEND ? legendOf(theme, prep.slices) : undefined
    const note = prepNote(prep)
    const marks: Mark[] = []
    if (!vis.length) {
      marks.push({ type: 'text', layer: 'labels', key: 'hidden-all', x: W / 2, y: H / 2, text: ctx.truncate('Every level is hidden; use the legend to show one', W - 8, SIZES.label), anchor: 'middle', baseline: 'central', fill: theme.muted, size: SIZES.label })
      return { marks, legend, note }
    }
    const fs = SIZES.label, lineH = fs + 4, pad = 4
    const bands = pyramidBands(vis.map((s) => s.value))
    const texts = vis.map((s, i) => `${s.label} ${fmt.pct(bands[i]!.share, bands[i]!.share < 0.1 ? 1 : 0)}`)
    const colW = Math.min(Math.max(...texts.map((t) => ctx.measure(t, fs))), W * 0.45)
    const th = H - 2 * pad
    // the base is as wide as the space left of the label column allows, at most 1.3x the height
    const base = Math.max(10, Math.min(W - colW - 24 - 2 * pad, th * 1.3))
    const ax = pad + base / 2, top = pad
    const half = (t: number) => (t * base) / 2
    const labels: Array<{ key: string; text: string; y: number; value: number; ex: number; ey: number }> = []
    vis.forEach((s, i) => {
      const b = bands[i]!
      const y0 = top + b.t0 * th, y1 = top + b.t1 * th
      const fill = colorOf(theme, s)
      const pctText = fmt.pct(b.share, b.share < 0.1 ? 1 : 0)
      const ym = (y0 + y1) / 2
      marks.push({
        type: 'path', role: 'band', key: s.key, series: s.key,
        d: `M${ax - half(b.t0)},${y0}L${ax + half(b.t0)},${y0}L${ax + half(b.t1)},${y1}L${ax - half(b.t1)},${y1}Z`,
        cx: ax, cy: ym, box: { x: ax - half(b.t1), y: y0, width: 2 * half(b.t1), height: Math.max(1, y1 - y0) },
        fill, stroke: theme.surface, strokeWidth: vis.length > 1 ? 1 : 0, pattern: patternOf(s),
        datum: { label: s.label, value: s.value, share: b.share },
        label: `${s.label}: ${fmt.value(s.value)} (${pctText})`,
        tooltip: { title: s.label, rows: [['Value', fmt.value(s.value)], ['Share of area', pctText]], color: fill },
      })
      const tm = (b.t0 + b.t1) / 2
      labels.push({ key: s.key, text: texts[i]!, y: ym, value: s.value, ex: ax + half(tm), ey: ym })
    })
    const lx = ax + base / 2 + 20
    for (const l of spreadLabels(labels, lineH, pad + lineH / 2, H - pad - lineH / 2)) {
      const t = ctx.truncate(l.text, W - lx - 4 - pad, fs)
      marks.push({ type: 'path', layer: 'labels', key: `ll:${l.key}`, d: `M${l.ex + 2},${l.ey}L${lx - 8},${l.ey}L${lx - 2},${l.y}`, stroke: theme.muted, strokeWidth: 1, fill: 'none', inert: true })
      marks.push({ type: 'text', layer: 'labels', key: `lt:${l.key}`, x: lx, y: l.y, text: t, full: t !== l.text ? l.text : undefined, anchor: 'start', baseline: 'central', fill: theme.ink, size: fs })
    }
    return { marks, legend, note }
  },
  describe: ({ data, options: o, fmt, name }) => describeParts(name, o.order === 'base-first' ? [...data].reverse() : data, fmt, { noun: 'level' }),
  table: (data) => partsTable(data),
})

export default pyramid
