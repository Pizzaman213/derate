// Waffle chart: a grid of squares (100 by default), each part getting its
// largest-remainder share of them, so the counts always add up to the grid.

import { defineChart, SIZES } from '../../core'
import type { Mark } from '../../core'
import { colorOf, describeParts, emptyMessage, largestRemainder, legendOf, partsTable, patternOf, prepare, prepNote } from '../_shared/PartWholeA'
import { listPhrase } from '../../core/describe'
import { input, options, type WaffleOptions, type WaffleRow } from './schema'

export const waffle = defineChart<WaffleRow[], WaffleOptions>({
  id: 'waffle',
  name: 'Waffle chart',
  category: 'part-to-whole',
  intents: ['composition'],
  summary: 'A grid of squares coloured by part: shares you can count.',
  input,
  options,
  shape: 'rows',
  fields: ['label', 'value'],
  numberFields: ['value'],
  aspect: 0.7,
  minHeight: 200,
  isEmpty: (data) => emptyMessage(data),
  draw(ctx) {
    const { theme, fmt, options: o, width: W, height: H } = ctx
    const prep = prepare(ctx.data, { maxSlices: o.maxSlices, sort: o.sort })
    const vis = prep.slices.filter((s) => !ctx.hidden.has(s.key))
    const total = vis.reduce((s, d) => s + d.value, 0)
    const counts = largestRemainder(vis.map((s) => s.value), o.cells)
    const share = new Map(vis.map((s) => [s.key, s.value / total]))
    const legend = legendOf(theme, prep.slices, (s) => (share.has(s.key) ? `${s.label} ${fmt.pct(share.get(s.key)!, share.get(s.key)! < 0.1 ? 1 : 0)}` : s.label))
    const tooSmall = counts.filter((c) => c === 0).length
    const note = prepNote(prep, [[tooSmall, tooSmall === 1 ? 'part too small for a square' : 'parts too small for a square']])
    const marks: Mark[] = []
    if (!vis.length) {
      marks.push({ type: 'text', layer: 'labels', key: 'hidden-all', x: W / 2, y: H / 2, text: ctx.truncate('Every part is hidden; use the legend to show one', W - 8, SIZES.label), anchor: 'middle', baseline: 'central', fill: theme.muted, size: SIZES.label })
      return { marks, legend, note }
    }
    const cols = Math.min(o.cells, o.columns ?? Math.max(1, Math.round(Math.sqrt(o.cells))))
    const rows = Math.ceil(o.cells / cols)
    const pad = 2
    const step = Math.max(1, Math.min((W - 2 * pad) / cols, (H - 2 * pad) / rows))
    const gap = step >= 8 ? Math.max(1, step * 0.12) : step >= 3 ? 0.5 : 0
    const size = step - gap
    const x0 = (W - step * cols + gap) / 2
    const y0 = (H - step * rows + gap) / 2
    let cell = 0
    vis.forEach((s, i) => {
      const n = counts[i]!
      const fill = colorOf(theme, s)
      const sh = share.get(s.key)!
      const pctText = fmt.pct(sh, sh < 0.1 ? 1 : 0)
      const datum = { label: s.label, value: s.value, share: sh, cells: n }
      const label = `${s.label}: ${fmt.value(s.value)} (${pctText}), ${n} of ${o.cells} squares`
      const rowsT: Array<[string, string]> = [['Value', fmt.value(s.value)], ['Share', pctText], ['Squares', `${n} of ${o.cells}`]]
      if (s.members) rowsT.push(['Includes', listPhrase(s.members, 4)])
      for (let j = 0; j < n; j++, cell++) {
        const c = cell % cols, r = Math.floor(cell / cols)
        marks.push({
          type: 'rect', role: 'cell', key: `${s.key}#${j}`, series: s.key, x: x0 + c * step, y: y0 + r * step, width: size, height: size,
          rx: size >= 8 ? Math.min(2, size / 6) : 0, fill, pattern: patternOf(s), datum, label, tooltip: { title: s.label, rows: rowsT, color: fill },
        })
      }
    })
    return { marks, legend, note }
  },
  describe({ data, options: o, fmt, name }) {
    const p = prepare(data, { maxSlices: o.maxSlices })
    const counts = largestRemainder(p.slices.map((s) => s.value), o.cells)
    const base = describeParts(name, data, fmt, { maxSlices: o.maxSlices })
    const top = p.slices.map((s, i) => ({ s, n: counts[i]! })).sort((a, b) => b.n - a.n)[0]
    return top ? `${base}, ${top.s.label} fills ${top.n} of ${o.cells} squares` : base
  },
  table: (data) => partsTable(data),
})

export default waffle
