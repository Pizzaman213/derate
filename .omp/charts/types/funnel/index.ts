// Funnel chart: stages in order, centred bars proportional to how many reached
// each one, with conversion from the first stage and from the previous one.

import { defineChart, SIZES } from '../../core'
import type { Mark } from '../../core'
import { collapseLabels, notes } from '../../core/helpers'
import { legendOf, patternOf, type Slice } from '../_shared/PartWholeA'
import { input, options, type FunnelOptions, type FunnelRow } from './schema'

export interface Stage extends Slice {
  /** value / first visible stage (null when the first is zero). */
  fromFirst: number | null
  /** value / previous visible stage (null for the first stage or after a zero). */
  fromPrev: number | null
}

/** Conversion ratios through ordered stage values. */
export function conversions(values: number[]): Array<{ fromFirst: number | null; fromPrev: number | null }> {
  const first = values[0] ?? 0
  return values.map((v, i) => ({
    fromFirst: first > 0 ? v / first : null,
    fromPrev: i > 0 && values[i - 1]! > 0 ? v / values[i - 1]! : null,
  }))
}

/** Stages with values (duplicates summed); negatives and nulls left out and counted. */
function stagesOf(rows: FunnelRow[]): { stages: Slice[]; duplicates: number; negatives: number; nulls: number } {
  const { items, duplicates } = collapseLabels(rows)
  let negatives = 0, nulls = 0
  const stages: Slice[] = []
  items.forEach((d, i) => {
    if (d.value === null) nulls++
    else if (d.value < 0) negatives++
    else stages.push({ key: d.label, label: d.label, value: d.value, index: i })
  })
  return { stages, duplicates, negatives, nulls }
}

const MAX_LEGEND = 12

export const funnel = defineChart<FunnelRow[], FunnelOptions>({
  id: 'funnel',
  name: 'Funnel chart',
  category: 'part-to-whole',
  intents: ['composition', 'flow'],
  summary: 'Ordered stages narrowing as items drop out, with conversion rates.',
  input,
  options,
  shape: 'rows',
  fields: ['label', 'value'],
  numberFields: ['value'],
  aspect: 0.65,
  minHeight: 220,
  isEmpty(data) {
    if (!data.length) return true
    const { stages } = stagesOf(data)
    if (!stages.length) return 'No non-negative values to show'
    return stages.every((s) => s.value === 0) ? 'All values are zero' : false
  },
  draw(ctx) {
    const { theme, fmt, options: o, width: W, height: H } = ctx
    const { stages: all, duplicates, negatives, nulls } = stagesOf(ctx.data)
    const vis = all.filter((s) => !ctx.hidden.has(s.key))
    const conv = conversions(vis.map((s) => s.value))
    const note = notes([[duplicates, 'duplicate stages summed'], [negatives, negatives === 1 ? 'negative value omitted' : 'negative values omitted'], [nulls, nulls === 1 ? 'missing value omitted' : 'missing values omitted']])
    const legend = all.length > 1 && all.length <= MAX_LEGEND ? legendOf(theme, all) : undefined
    const marks: Mark[] = []
    if (!vis.length) {
      marks.push({ type: 'text', layer: 'labels', key: 'hidden-all', x: W / 2, y: H / 2, text: ctx.truncate('Every stage is hidden; use the legend to show one', W - 8, SIZES.label), anchor: 'middle', baseline: 'central', fill: theme.muted, size: SIZES.label })
      return { marks, legend, note }
    }
    const fs = SIZES.label, fsT = SIZES.tick
    const n = vis.length
    const rowH = H / n
    const barH = Math.max(1, rowH * (n > 1 ? 0.7 : 0.5))
    const showFirst = o.showConversion === 'both' || o.showConversion === 'first'
    const showPrev = o.showConversion === 'both' || o.showConversion === 'previous'
    const labelW = Math.min(Math.max(...vis.map((s) => ctx.measure(s.label, fs))), W * 0.28)
    const rightTexts = vis.flatMap((_, i) => [showFirst ? `${fmt.pct(conv[i]!.fromFirst, 1)} of first` : '', showPrev && i ? `${fmt.pct(conv[i]!.fromPrev, 1)} of previous` : ''])
    // one stage converts from nothing: no conversion column
    // measured bold: the "of first" line is drawn at weight 600
    const rightW = o.showConversion === 'none' || n === 1 ? 0 : Math.min(Math.max(...rightTexts.map((t) => ctx.measure(t, fsT, 600))), W * 0.26)
    const gutter = 8
    const cx0 = labelW + gutter, cw = Math.max(10, W - cx0 - (rightW ? rightW + gutter : 0))
    const mid = cx0 + cw / 2
    const maxV = Math.max(...vis.map((s) => s.value)) || 1
    // label thinning: every k-th row when rows are shorter than a line of text
    const k = Math.max(1, Math.ceil((fs + 3) / rowH))
    const lines = rowH >= fsT * 2 + 6 && showFirst && showPrev ? 2 : 1
    const widths = vis.map((s) => Math.max(1, (cw * s.value) / maxV))
    vis.forEach((s, i) => {
      const w = widths[i]!
      const y = i * rowH + (rowH - barH) / 2
      // text row centre, kept a half line inside the box when rows are thinner than text
      const cy = Math.min(H - fs / 2 - 2, Math.max(fs / 2 + 2, y + barH / 2))
      const fill = theme.cat(s.index)
      const c = conv[i]!
      if (i < n - 1 && rowH - barH >= 2) {
        const w2 = widths[i + 1]!, y2 = (i + 1) * rowH + (rowH - barH) / 2
        marks.push({ type: 'path', layer: 'back', key: `neck:${s.key}`, d: `M${mid - w / 2},${y + barH}L${mid + w / 2},${y + barH}L${mid + w2 / 2},${y2}L${mid - w2 / 2},${y2}Z`, fill: theme.track, inert: true })
      }
      const pf = fmt.pct(c.fromFirst, 1), pp = fmt.pct(c.fromPrev, 1)
      const rowsT: Array<[string, string]> = [['Value', fmt.value(s.value)], ['Of first stage', pf]]
      if (i) rowsT.push(['Of previous stage', pp])
      marks.push({
        type: 'rect', role: 'stage', key: s.key, series: s.key, x: mid - w / 2, y, width: w, height: barH, rx: Math.min(2, barH / 4), fill, pattern: patternOf(s),
        datum: { label: s.label, value: s.value, fromFirst: c.fromFirst, fromPrev: c.fromPrev },
        label: `${s.label}: ${fmt.value(s.value)}, ${pf} of first${i ? `, ${pp} of previous` : ''}`,
        tooltip: { title: s.label, rows: rowsT, color: fill },
      })
      if (i % k !== 0) return
      const name = ctx.truncate(s.label, labelW, fs)
      marks.push({ type: 'text', layer: 'labels', key: `l:${s.key}`, x: labelW, y: cy, text: name, full: name !== s.label ? s.label : undefined, anchor: 'end', baseline: 'central', fill: theme.ink, size: fs })
      const vt = fmt.value(s.value)
      const vw = ctx.measure(vt, fsT, 600)
      if (barH >= fsT + 2 && vw + 8 <= w) marks.push({ type: 'text', layer: 'labels', key: `v:${s.key}`, x: mid, y: cy, text: vt, anchor: 'middle', baseline: 'central', fill: theme.ink, size: fsT, weight: 600, halo: true, inert: true })
      else if (barH >= fsT && vw + 4 <= (cw - w) / 2) marks.push({ type: 'text', layer: 'labels', key: `v:${s.key}`, x: mid + w / 2 + 4, y: cy, text: vt, anchor: 'start', baseline: 'central', fill: theme.ink, size: fsT, inert: true })
      if (!rightW) return
      const rx = W - rightW
      // "of previous" shortens to "of prev." before it would be cut with an ellipsis
      const prevText = ctx.measure(`${pp} of previous`, fsT) <= rightW ? `${pp} of previous` : `${pp} of prev.`
      const a = showFirst ? `${pf} of first` : i ? prevText : ''
      const b = lines === 2 && i ? prevText : ''
      const ay = b ? cy - (fsT + 2) / 2 : cy
      if (a) {
        // ctx.truncate measures regular weight: shrink its budget by the bold/regular ratio
        const t = ctx.truncate(a, (rightW * ctx.measure(a, fsT)) / ctx.measure(a, fsT, 600), fsT)
        marks.push({ type: 'text', layer: 'labels', key: `cf:${s.key}`, x: rx, y: ay, text: t, full: t !== a ? a : undefined, anchor: 'start', baseline: 'central', fill: theme.ink, size: fsT, weight: 600 })
      }
      if (b) {
        const t = ctx.truncate(b, rightW, fsT)
        marks.push({ type: 'text', layer: 'labels', key: `cp:${s.key}`, x: rx, y: ay + fsT + 2, text: t, full: t !== b ? b : undefined, anchor: 'start', baseline: 'central', fill: theme.muted, size: fsT })
      }
    })
    return { marks, legend, note }
  },
  describe({ data, fmt, name }) {
    const { stages } = stagesOf(data)
    if (!stages.length) return `${name}, no stages`
    const first = stages[0]!, last = stages[stages.length - 1]!
    const head = `${name}, ${stages.length} ${stages.length === 1 ? 'stage' : 'stages'}`
    if (stages.length === 1) return `${head}, ${first.label} at ${fmt.value(first.value)}`
    const conv = conversions(stages.map((s) => s.value))
    let drop = -1, worst = Infinity
    conv.forEach((c, i) => { if (c.fromPrev !== null && c.fromPrev < worst) { worst = c.fromPrev; drop = i } })
    const overall = conv[conv.length - 1]!.fromFirst
    const tail = drop > 0 ? `, biggest drop ${stages[drop - 1]!.label} to ${stages[drop]!.label} keeping ${fmt.pct(worst, 1)}` : ''
    return `${head} from ${first.label} at ${fmt.value(first.value)} to ${last.label} at ${fmt.value(last.value)}, ${fmt.pct(overall, 1)} overall conversion${tail}`
  },
  table(data) {
    const { stages } = stagesOf(data)
    const conv = conversions(stages.map((s) => s.value))
    return { columns: ['Stage', 'Value', 'Of first', 'Of previous'], rows: stages.map((s, i) => [s.label, s.value, conv[i]!.fromFirst, conv[i]!.fromPrev]) }
  },
})

export default funnel
