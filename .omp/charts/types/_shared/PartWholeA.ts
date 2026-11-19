// Shared by the part-to-whole charts (pie, donut, semi-donut, waffle, funnel, pyramid):
// slice preparation (duplicates, nulls, negatives, zeros, the "Other" tail), the
// largest-remainder allocation, angle layout, and the radial renderer.

import { arc as d3arc } from 'd3-shape'
import { SIZES } from '../../core/theme'
import { collapseLabels, notes } from '../../core/helpers'
import { listPhrase } from '../../core/describe'
import type { DrawContext, DrawResult, LegendItem, Mark, Paint } from '../../core'
import type { Formatters } from '../../core/format'
import type { Palette } from '../../core/theme'

export interface PartRow {
  label: string
  value: number | null
}

/** One drawable part. `index` is the colour/pattern slot, stable across sorting and hiding. */
export interface Slice {
  key: string
  label: string
  value: number
  index: number
  /** Labels folded into "Other". */
  members?: string[]
}

export const OTHER_KEY = '__other'

export interface Prepared {
  /** Positive parts in drawing order ("Other" last). */
  slices: Slice[]
  zeros: number
  negatives: number
  nulls: number
  duplicates: number
  /** How many parts were folded into "Other" (0 = none). */
  folded: number
}

/**
 * Clean rows into positive parts. Duplicate labels are summed, nulls / zeros /
 * negatives left out (and counted for the note), then -- when there are more than
 * `maxSlices` parts -- the smallest are folded into one "Other" part so the total
 * part count is exactly `maxSlices`.
 */
export function prepare(rows: PartRow[], opts: { maxSlices?: number; sort?: 'none' | 'descending' | 'ascending' } = {}): Prepared {
  const { items, duplicates } = collapseLabels(rows)
  let zeros = 0, negatives = 0, nulls = 0
  const pos: Slice[] = []
  items.forEach((d, i) => {
    if (d.value === null || !Number.isFinite(d.value)) nulls++
    else if (d.value < 0) negatives++
    else if (d.value === 0) zeros++
    else pos.push({ key: d.label, label: d.label, value: d.value, index: i })
  })
  let slices = pos
  let folded = 0
  const max = opts.maxSlices
  if (max !== undefined && max >= 2 && pos.length > max) {
    const byValue = [...pos].sort((a, b) => b.value - a.value || a.index - b.index)
    const keep = new Set(byValue.slice(0, max - 1))
    const rest = byValue.slice(max - 1)
    folded = rest.length
    slices = pos.filter((s) => keep.has(s))
    slices.push({ key: OTHER_KEY, label: `Other (${rest.length})`, value: rest.reduce((s, d) => s + d.value, 0), index: -1, members: rest.map((d) => d.label) })
  }
  const sort = opts.sort ?? 'none'
  if (sort !== 'none') {
    const dir = sort === 'descending' ? -1 : 1
    const other = slices.filter((s) => s.key === OTHER_KEY)
    slices = slices.filter((s) => s.key !== OTHER_KEY).sort((a, b) => dir * (a.value - b.value) || a.index - b.index).concat(other)
  }
  return { slices, zeros, negatives, nulls, duplicates, folded }
}

/** The note under the chart: what was summed, folded or left out. */
export function prepNote(p: Prepared, extra: Array<[number, string]> = []): string | undefined {
  return notes([
    [p.duplicates, 'duplicate labels summed'],
    [p.folded, 'smallest parts grouped as Other'],
    [p.negatives, p.negatives === 1 ? 'negative value omitted' : 'negative values omitted'],
    [p.nulls, p.nulls === 1 ? 'missing value omitted' : 'missing values omitted'],
    [p.zeros, p.zeros === 1 ? 'zero value not drawn' : 'zero values not drawn'],
    ...extra,
  ])
}

/** Empty-state message for part-to-whole data: nothing positive to divide up. */
export function emptyMessage(rows: PartRow[]): boolean | string {
  if (!rows.length) return true
  const vals = rows.map((r) => r.value).filter((v): v is number => v !== null && Number.isFinite(v))
  if (!vals.length) return 'No values to show'
  const { items } = collapseLabels(rows)
  if (items.some((d) => d.value !== null && d.value > 0)) return false
  if (vals.every((v) => v === 0)) return 'All values are zero'
  return 'No positive values to show'
}

export const colorOf = (theme: Palette, s: Slice): Paint => (s.key === OTHER_KEY ? theme.neutral : theme.cat(s.index))
export const patternOf = (s: Slice): number | undefined => (s.key === OTHER_KEY ? undefined : s.index % 8)

export function legendOf(theme: Palette, slices: Slice[], label: (s: Slice) => string = (s) => s.label): { items: LegendItem[]; toggle: boolean } {
  return {
    items: slices.map((s) => ({ key: s.key, label: label(s), color: colorOf(theme, s), shape: 'square', pattern: patternOf(s) })),
    toggle: slices.length > 1,
  }
}

/**
 * Largest-remainder (Hamilton) allocation of `total` whole units to `values`:
 * floor every exact quota, then hand the leftover units to the largest remainders
 * (ties: larger value, then earlier position). The result always sums to `total`
 * when any value is positive.
 */
export function largestRemainder(values: number[], total: number): number[] {
  const sum = values.reduce((s, v) => s + (v > 0 ? v : 0), 0)
  if (sum <= 0 || total <= 0) return values.map(() => 0)
  const quotas = values.map((v) => (v > 0 ? (v / sum) * total : 0))
  const out = quotas.map((q) => Math.floor(q))
  let left = total - out.reduce((s, v) => s + v, 0)
  const order = quotas
    .map((q, i) => ({ i, r: q - Math.floor(q), v: values[i]! }))
    .filter((d) => d.v > 0)
    .sort((a, b) => b.r - a.r || b.v - a.v || a.i - b.i)
  for (let k = 0; left > 0 && order.length; k = (k + 1) % order.length, left--) out[order[k]!.i]!++
  return out
}

export interface Angle {
  start: number
  end: number
  share: number
}

/** Angles (radians, clockwise from 12 o'clock) for values over `span` starting at `start`. */
export function arcAngles(values: number[], start = 0, span = Math.PI * 2): Angle[] {
  const sum = values.reduce((s, v) => s + v, 0)
  let a = start
  return values.map((v) => {
    const share = sum > 0 ? v / sum : 0
    const out = { start: a, end: a + share * span, share }
    a = out.end
    return out
  })
}

/** Point at angle (clockwise from 12 o'clock) and radius, relative to the centre. */
export const polar = (a: number, r: number): [number, number] => [Math.sin(a) * r, -Math.cos(a) * r]

interface SideLabel {
  key: string
  text: string
  name: string
  pct: string
  side: -1 | 1
  ay: number
  mid: number
  value: number
  y: number
}

/** Spread labels on one side so they never overlap, dropping the smallest when they cannot all fit. */
export function spreadLabels<T extends { y: number; value: number }>(items: T[], lineH: number, top: number, bottom: number): T[] {
  const cap = Math.max(0, Math.floor((bottom - top) / lineH) + 1)
  let keep = items
  if (keep.length > cap) {
    const big = new Set([...keep].sort((a, b) => b.value - a.value).slice(0, cap))
    keep = keep.filter((d) => big.has(d))
  }
  keep = [...keep].sort((a, b) => a.y - b.y)
  for (let i = 0; i < keep.length; i++) keep[i]!.y = Math.max(keep[i]!.y, i ? keep[i - 1]!.y + lineH : top)
  for (let i = keep.length - 1; i >= 0; i--) keep[i]!.y = Math.min(keep[i]!.y, i < keep.length - 1 ? keep[i + 1]!.y - lineH : bottom)
  return keep
}

export interface RadialSpec {
  /** Inner radius as a share of the outer radius (0 = pie). */
  inner: number
  /** Half circle (180°, flat side down) instead of a full turn. */
  half: boolean
  /** 'auto': names outside with leader lines when there is room, else shares inside. */
  labels: 'auto' | 'outside' | 'inside' | 'none'
  /** Caption under the centre total (holes only); empty string hides the total. */
  centerLabel?: string
}

export interface RadialOptions {
  maxSlices: number
  sort: 'none' | 'descending' | 'ascending'
  labels: 'auto' | 'outside' | 'inside' | 'none'
}

/** Pie, donut and semi-donut all draw through here. */
export function drawRadial(ctx: DrawContext<PartRow[], RadialOptions & Record<string, unknown>>, spec: RadialSpec): DrawResult {
  const { theme, fmt, width: W, height: H, options: o } = ctx
  const prep = prepare(ctx.data, { maxSlices: o.maxSlices, sort: o.sort })
  const vis = prep.slices.filter((s) => !ctx.hidden.has(s.key))
  const total = vis.reduce((s, d) => s + d.value, 0)
  const legend = legendOf(theme, prep.slices)
  const note = prepNote(prep)
  const marks: Mark[] = []
  if (!vis.length) {
    marks.push({ type: 'text', layer: 'labels', key: 'hidden-all', x: W / 2, y: H / 2, text: ctx.truncate('Every part is hidden; use the legend to show one', W - 8, SIZES.label), anchor: 'middle', baseline: 'central', fill: theme.muted, size: SIZES.label })
    return { marks, legend, note }
  }
  const start = spec.half ? -Math.PI / 2 : 0
  const span = spec.half ? Math.PI : Math.PI * 2
  const angles = arcAngles(vis.map((s) => s.value), start, span)
  const labelSize = SIZES.label
  const lineH = labelSize + 4
  const textOf = (s: Slice, a: Angle) => `${s.label} ${fmt.pct(a.share, a.share < 0.1 ? 1 : 0)}`
  const widest = Math.max(...vis.map((s, i) => ctx.measure(textOf(s, angles[i]!), labelSize)))
  const pad = 4
  // radius candidates: with an outside label column, and without
  const fitR = (side: number) => (spec.half ? Math.min((W - 2 * side) / 2 - pad, H - 2 * pad - (side ? lineH / 2 : 0)) : Math.min((W - 2 * side) / 2 - pad, H / 2 - pad - (side ? lineH / 2 : 0)))
  const sideW = Math.min(widest, Math.max(70, W * 0.24)) + 26
  let outside = o.labels === 'outside' || (o.labels === 'auto' && W >= 400)
  let r = outside ? fitR(sideW) : fitR(0)
  if (outside && o.labels === 'auto' && (r < Math.min(W, H) * 0.28 || r < 60)) {
    outside = false
    r = fitR(0)
  }
  r = Math.max(8, r)
  const ri = r * spec.inner
  const cx = W / 2
  const cy = spec.half ? (H + r) / 2 : H / 2
  const tr = `translate(${cx},${cy})`
  const gen = d3arc<Angle>().innerRadius(ri).outerRadius(r).startAngle((d) => d.start).endAngle((d) => d.end).cornerRadius(vis.length > 1 ? Math.min(2, (r - ri) / 8) : 0)
  const sideLabels: SideLabel[] = []
  vis.forEach((s, i) => {
    const a = angles[i]!
    const fill = colorOf(theme, s)
    const [mx, my] = polar((a.start + a.end) / 2, ri + (r - ri) * (spec.inner > 0 ? 0.5 : 0.62))
    const m = (a.start + a.end) / 2
    const pctText = fmt.pct(a.share, a.share < 0.1 ? 1 : 0)
    const extra: Array<[string, string]> = s.members ? [['Includes', listPhrase(s.members, 4)]] : []
    marks.push({
      type: 'path', role: 'arc', key: s.key, series: s.key, d: gen(a) ?? '', transform: tr, cx: mx, cy: my,
      box: { x: mx - 8, y: my - 8, width: 16, height: 16 },
      fill, stroke: theme.surface, strokeWidth: vis.length > 1 ? 1 : 0, pattern: patternOf(s),
      datum: { label: s.label, value: s.value, share: a.share },
      label: `${s.label}: ${fmt.value(s.value)} (${pctText})`,
      tooltip: { title: s.label, rows: [['Value', fmt.value(s.value)], ['Share', pctText], ...extra], color: fill },
    })
    if (o.labels === 'none') return
    if (outside) {
      const [ax, ay] = polar(m, r)
      sideLabels.push({ key: s.key, text: textOf(s, a), name: s.label, pct: pctText, side: ax < -0.001 ? -1 : 1, ay, mid: m, value: s.value, y: ay })
    } else {
      // share inside the slice when the arc and ring have room for it
      const tw = ctx.measure(pctText, SIZES.tick)
      const rr = Math.hypot(mx, my)
      const arcLen = (a.end - a.start) * rr
      if (arcLen >= tw + 6 && (spec.inner === 0 || r - ri >= SIZES.tick + 4) && (a.end - a.start) < Math.PI * 2 - 1e-9) {
        marks.push({ type: 'text', layer: 'labels', key: `p:${s.key}`, x: mx, y: my, transform: tr, text: pctText, anchor: 'middle', baseline: 'central', fill: theme.ink, size: SIZES.tick, weight: 600, halo: true, inert: true })
      } else if ((a.end - a.start) >= Math.PI * 2 - 1e-9 && spec.inner === 0) {
        marks.push({ type: 'text', layer: 'labels', key: `p:${s.key}`, x: 0, y: 0, transform: tr, text: pctText, anchor: 'middle', baseline: 'central', fill: theme.ink, size: SIZES.tick, weight: 600, halo: true, inert: true })
      }
    }
  })
  if (outside && sideLabels.length) {
    const topY = -cy + pad + lineH / 2
    const bottomY = spec.half ? -lineH / 2 : H / 2 - pad - lineH / 2
    const colW = (W / 2) - r - 22 - pad
    for (const side of [-1, 1] as const) {
      const placed = spreadLabels(sideLabels.filter((l) => l.side === side), lineH, topY, bottomY)
      for (const l of placed) {
        const [ex, ey] = polar(l.mid, r + 8)
        const tx = side * (r + 16)
        // the name gives way before the share does
        const text = ctx.measure(l.text, labelSize) <= colW ? l.text : `${ctx.truncate(l.name, colW - ctx.measure(` ${l.pct}`, labelSize), labelSize)} ${l.pct}`.trimStart()
        marks.push({ type: 'path', layer: 'labels', key: `ll:${l.key}`, transform: tr, d: `M${polar(l.mid, r + 2).join(',')}L${ex},${ey}L${tx},${l.y}`, stroke: theme.muted, strokeWidth: 1, fill: 'none', inert: true })
        marks.push({ type: 'text', layer: 'labels', key: `lt:${l.key}`, transform: tr, x: tx + side * 4, y: l.y, text, full: text !== l.text ? l.text : undefined, anchor: side < 0 ? 'end' : 'start', baseline: 'central', fill: theme.ink, size: labelSize })
      }
    }
  }
  if (spec.inner > 0 && spec.centerLabel !== '') {
    const room = ri * (spec.half ? 1.6 : 1.5)
    const valueText = fmt.value(total)
    let size = SIZES.value
    while (size > SIZES.label && ctx.measure(valueText, size, 600) > room) size -= 1
    const caption = spec.centerLabel ?? 'Total'
    const lift = spec.half ? Math.ceil(size * 0.3) : 0
    const capFits = ri >= (spec.half ? lift + size + SIZES.tick + 8 : (size + SIZES.tick) / 2 + 6)
    const cap = capFits ? ctx.truncate(caption, room, SIZES.tick) : ''
    if (ctx.measure(valueText, size, 600) <= room) {
      if (spec.half) {
        marks.push({ type: 'text', layer: 'labels', key: 'centre:v', transform: tr, x: 0, y: -lift, text: valueText, anchor: 'middle', baseline: 'alphabetic', fill: theme.ink, size, weight: 600, inert: true })
        if (cap) marks.push({ type: 'text', layer: 'labels', key: 'centre:c', transform: tr, x: 0, y: -lift - size - 4, text: cap, full: cap !== caption ? caption : undefined, anchor: 'middle', baseline: 'alphabetic', fill: theme.muted, size: SIZES.tick })
      } else {
        const off = cap ? (SIZES.tick + 2) / 2 : 0
        marks.push({ type: 'text', layer: 'labels', key: 'centre:v', transform: tr, x: 0, y: -off, text: valueText, anchor: 'middle', baseline: 'central', fill: theme.ink, size, weight: 600, inert: true })
        if (cap) marks.push({ type: 'text', layer: 'labels', key: 'centre:c', transform: tr, x: 0, y: size / 2 - off + 4 + SIZES.tick / 2, text: cap, full: cap !== caption ? caption : undefined, anchor: 'middle', baseline: 'central', fill: theme.muted, size: SIZES.tick })
      }
    }
  }
  return { marks, legend, note }
}

/** "<Name>, 5 parts totalling 1,035, largest Bravo at 33%, smallest Delta at 9%". */
export function describeParts(name: string, rows: PartRow[], fmt: Formatters, opts: { maxSlices?: number; noun?: string } = {}): string {
  const p = prepare(rows, { maxSlices: opts.maxSlices })
  const noun = opts.noun ?? 'part'
  const total = p.slices.reduce((s, d) => s + d.value, 0)
  if (!p.slices.length) return `${name}, no positive values`
  const sorted = [...p.slices].sort((a, b) => b.value - a.value)
  const hi = sorted[0]!, lo = sorted[sorted.length - 1]!
  const n = p.slices.length
  const head = `${name}, ${n} ${n === 1 ? noun : `${noun}s`} totalling ${fmt.value(total)}`
  if (n === 1) return `${head}, all ${hi.label}`
  return `${head}, largest ${hi.label} at ${fmt.pct(hi.value / total)}, smallest ${lo.label} at ${fmt.pct(lo.value / total, 1)}`
}

/** Table for label/value parts: the cleaned rows, with each one's share of the positive total. */
export function partsTable(rows: PartRow[]): { columns: string[]; rows: Array<Array<string | number | null>> } {
  const { items } = collapseLabels(rows)
  const total = items.reduce((s, d) => s + (d.value !== null && d.value > 0 ? d.value : 0), 0)
  return {
    columns: ['Label', 'Value', 'Share'],
    rows: items.map((d) => [d.label, d.value, d.value !== null && d.value > 0 && total > 0 ? d.value / total : null]),
  }
}
