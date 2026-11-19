// Bullet graph (Stephen Few): one measure as a bar, its target as a tick, against
// qualitative bands (poor / satisfactory / good) -- a gauge in a strip. One bullet per
// row, stacked; each gets its own axis unless `scale: 'shared'`.

import { scaleLinear, type ScaleLinear } from 'd3-scale'
import { defineChart, phrase, SIZES } from '../../core'
import { finiteExtent, notes } from '../../core/helpers'
import type { Mark } from '../../core'
import { uniqueKeys } from '../_shared/ComparisonB'
import { input, options, type BulletOptions, type BulletRow } from './schema'

const TICK = 4, GAP = 6

/** The row's value domain: zero, the value, the target and every band edge. */
const extentOf = (d: BulletRow): [number, number] => {
  const e = finiteExtent([0, d.value, d.target, ...(d.ranges ?? [])])!
  return e[0] === e[1] ? [e[0], e[0] + 1] : e
}

export const bullet = defineChart<BulletRow[], BulletOptions>({
  id: 'bullet',
  name: 'Bullet chart',
  category: 'comparison',
  intents: ['compare', 'kpi'],
  summary: 'Few\'s bullet graph: a measure bar, a target tick and qualitative bands, one per row.',
  input,
  options,
  shape: 'rows',
  fields: ['label', 'value', 'target', 'ranges'],
  numberFields: ['value', 'target'],
  minHeight: 80,
  isEmpty: (data) => data.length === 0 || data.every((d) => d.value === null && d.target === null && !d.ranges?.length),
  draw(ctx) {
    const { theme, options: o, fmt, width: W, height: H } = ctx
    const FS = SIZES.tick, LS = SIZES.label
    const shared = o.scale === 'shared'
    const axisH = FS + TICK + 3
    // rows that fit: a bullet needs ~12px, plus its own axis when scales are independent
    const minRow = 12 + (shared ? 0 : axisH) + 6
    const avail = H - (shared ? axisH + 4 : 0)
    const shown = ctx.data.slice(0, Math.max(1, Math.floor(avail / minRow)))
    const keys = uniqueKeys(shown.map((d) => d.label))
    const rowH = avail / shown.length
    const bh = Math.max(8, Math.min(30, rowH - (shared ? 0 : axisH) - 10))
    const sub = o.showValues && bh >= LS + FS + 2
    const subText = (d: BulletRow) => (d.value === null ? '—' : fmt.value(d.value)) + (d.target === null ? '' : ` / ${fmt.value(d.target)}`)
    // label column: as wide as the longest label (capped), then the bullets
    const labelW = Math.min(W * 0.32, Math.max(32, ...shown.map((d) => Math.max(ctx.measure(d.label, LS), sub ? ctx.measure(subText(d)) : 0))))
    // measured widths run short for scripts the measuring font lacks (Hebrew, CJK): leave slack
    const plotX = Math.ceil(labelW * 1.1 + 4 + 10)
    const domains = shown.map(extentOf)
    const sharedDomain = finiteExtent(domains.flat()) ?? [0, 1]
    // the last tick label is centred on the plot's right edge: leave it half its width
    const lastTick = (dom: [number, number]) => {
      const s = scaleLinear().domain(dom).nice()
      return ctx.measure(fmt.value(s.domain()[1]!)) / 2 + 2
    }
    const right = Math.max(8, ...(shared ? [lastTick(sharedDomain)] : domains.map(lastTick)))
    const plotW = Math.max(20, W - plotX - right)
    const marks: Mark[] = []
    const ticksFor = (scale: ScaleLinear<number, number>, y: number, keyPrefix: string) => {
      const vals = scale.ticks(Math.max(2, Math.min(8, Math.floor(plotW / 56))))
      let lastEnd = -Infinity
      for (const v of vals) {
        const x = scale(v), t = fmt.value(v), w = ctx.measure(t)
        marks.push({ type: 'line', layer: 'axes', key: `${keyPrefix}t:${v}`, x1: x, x2: x, y1: y, y2: y + TICK, stroke: theme.axis, strokeWidth: 1, inert: true })
        if (x - w / 2 < lastEnd + 6) continue
        lastEnd = x + w / 2
        marks.push({ type: 'text', layer: 'axes', key: `${keyPrefix}l:${v}`, x, y: y + TICK + 2, text: t, anchor: 'middle', baseline: 'hanging', size: FS, fill: theme.inkSecondary, inert: true })
      }
    }
    const sharedScale = scaleLinear().domain(sharedDomain).nice().range([plotX, plotX + plotW])
    let measured = 0
    shown.forEach((d, i) => {
      const key = keys[i]!
      const scale = shared ? sharedScale : scaleLinear().domain(domains[i]!).nice().range([plotX, plotX + plotW])
      const [d0, d1] = scale.domain() as [number, number]
      const top = i * rowH + (rowH - bh - (shared ? 0 : axisH)) / 2
      const cy = top + bh / 2
      // qualitative bands: darkest first (the lowest band reads as "poor"), track when none are given.
      // grid -> track alone is too faint on light themes, so the first band leans toward muted ink.
      const shade = (j: number, k: number) => (k === 1 || j === k - 1 ? theme.track : j === 0 ? theme.mix(theme.grid, theme.muted, 0.3) : theme.mix(theme.grid, theme.track, j / (k - 1)))
      const ranges = [...(d.ranges ?? [])].sort((a, b) => a - b)
      if (ranges.length) {
        let from = Math.min(0, d0)
        ranges.forEach((r, j) => {
          const x0 = scale(from), x1 = scale(Math.min(r, d1))
          if (x1 > x0) marks.push({ type: 'rect', layer: 'back', key: `b:${key}:${j}`, x: x0, y: top, width: x1 - x0, height: bh, fill: shade(j, ranges.length), inert: true })
          from = r
        })
      } else {
        marks.push({ type: 'rect', layer: 'back', key: `b:${key}:0`, x: plotX, y: top, width: plotW, height: bh, fill: theme.track, inert: true })
      }
      if (d0 < 0) marks.push({ type: 'line', layer: 'grid', key: `z:${key}`, x1: scale(0), x2: scale(0), y1: top, y2: top + bh, stroke: theme.ruleStrong, strokeWidth: 1, inert: true })
      const name = ctx.truncate(d.label, labelW, LS)
      const labelY = sub ? cy - 1 : cy
      marks.push({ type: 'text', layer: 'labels', key: `n:${key}`, x: plotX - 10, y: labelY, text: name, full: name !== d.label ? d.label : undefined, className: name !== d.label ? 'oc-trunc' : undefined, anchor: 'end', baseline: sub ? 'alphabetic' : 'central', size: LS, fill: theme.ink })
      if (sub) marks.push({ type: 'text', layer: 'labels', key: `s:${key}`, x: plotX - 10, y: cy + 2, text: ctx.truncate(subText(d), labelW), anchor: 'end', baseline: 'hanging', size: FS, fill: theme.muted, inert: true })
      if (!shared) ticksFor(scale, top + bh, `a:${key}:`)
      const band = ranges.findIndex((r) => d.value !== null && d.value <= r)
      const tip: Array<[string, string]> = [['Value', d.value === null ? '—' : fmt.value(d.value)]]
      if (d.target !== null) tip.push(['Target', fmt.value(d.target)])
      if (d.target !== null && d.value !== null) tip.push(['vs target', `${d.value - d.target > 0 ? '+' : ''}${fmt.value(d.value - d.target)}`])
      if (ranges.length) tip.push(['Bands', ranges.map((r) => fmt.value(r)).join(' / ')])
      if (d.value !== null) {
        measured++
        const x0 = scale(0), x1 = scale(d.value)
        const mh = Math.max(3, bh / 3)
        const vs = d.target === null ? '' : `, target ${fmt.value(d.target)}`
        const where = ranges.length ? `, ${band < 0 ? 'above every band' : `band ${band + 1} of ${ranges.length}`}` : ''
        marks.push({
          type: 'rect', role: 'bar', key: `m:${key}`, x: Math.min(x0, x1), y: cy - mh / 2, width: Math.max(1, Math.abs(x1 - x0)), height: mh,
          fill: theme.cat(0), datum: d, label: `${d.label}: ${fmt.value(d.value)}${vs}${where}`, tooltip: { title: d.label, rows: tip, color: theme.cat(0) },
        })
      }
      if (d.target !== null) {
        const tx = scale(d.target)
        marks.push({ type: 'line', layer: 'labels', key: `t:${key}`, x1: tx, x2: tx, y1: cy - bh * 0.36, y2: cy + bh * 0.36, stroke: theme.ink, strokeWidth: Math.max(2, Math.min(3, bh / 9)), inert: true })
      }
    })
    if (shared) {
      const y = shown.length * rowH + 4
      marks.push({ type: 'line', layer: 'axes', key: 'axis', x1: plotX, x2: plotX + plotW, y1: y, y2: y, stroke: theme.axis, strokeWidth: 1, inert: true })
      ticksFor(sharedScale, y, 'a:')
    }
    const hiddenRows = ctx.data.length - shown.length
    const noValue = shown.length - measured
    return {
      marks,
      plot: { x: plotX, y: 0, width: plotW, height: H },
      note: notes([[hiddenRows, `more rows did not fit (all ${ctx.data.length} are in the table)`], [noValue, 'rows without a value (bands and target only)']]),
    }
  },
  describe({ data, fmt, name }) {
    const withTarget = data.filter((d): d is BulletRow & { value: number; target: number } => d.value !== null && d.target !== null)
    const met = withTarget.filter((d) => d.value >= d.target).length
    let worst: (BulletRow & { value: number; target: number }) | null = null
    for (const d of withTarget) if (d.value < d.target && (!worst || d.value / Math.abs(d.target || 1) < worst.value / Math.abs(worst.target || 1))) worst = d
    const parts = [`${name}, ${phrase.count(data.length, 'measure')}`]
    if (withTarget.length) parts.push(`${met} of ${withTarget.length} at or above target`)
    else parts.push(phrase.extremes(data, fmt))
    if (worst) parts.push(`furthest below ${worst.label} at ${fmt.value(worst.value)} against ${fmt.value(worst.target)}`)
    return parts.join(', ')
  },
  table(data) {
    return {
      columns: ['Label', 'Value', 'Target', 'Ranges'],
      rows: data.map((d) => [d.label, d.value, d.target, d.ranges?.length ? d.ranges.join(' / ') : null]),
    }
  },
})

export default bullet
