// Radial bar chart: concentric arcs, one ring per category, each swept clockwise from
// 12 o'clock in proportion to its value (a full sweep = `max`). The rings start in the
// free top-left quarter, which is where their labels sit.

import { arc as d3arc, type DefaultArcObject } from 'd3-shape'
import { defineChart, phrase, SIZES } from '../../core'
import { collapseLabels, notes, sortItems } from '../../core/helpers'
import type { Mark, Rect } from '../../core'
import { input, options, type RadialBarOptions, type RadialBarRow } from './schema'

/** Bounding box of an annular sector starting at 12 o'clock (angles clockwise, radians). */
function sectorBox(inner: number, outer: number, end: number): Rect {
  const pts: Array<[number, number]> = []
  const steps = Math.max(2, Math.ceil(end / (Math.PI / 8)))
  for (let i = 0; i <= steps; i++) {
    const a = (end * i) / steps
    pts.push([outer * Math.sin(a), -outer * Math.cos(a)], [inner * Math.sin(a), -inner * Math.cos(a)])
  }
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1])
  const x = Math.min(...xs), y = Math.min(...ys)
  return { x, y, width: Math.max(2, Math.max(...xs) - x), height: Math.max(2, Math.max(...ys) - y) }
}

export const radialBar = defineChart<RadialBarRow[], RadialBarOptions>({
  id: 'radial-bar',
  name: 'Radial bar chart',
  category: 'comparison',
  intents: ['compare', 'rank'],
  summary: 'Concentric arcs, one ring per category, each swept in proportion to its value.',
  input,
  options,
  shape: 'rows',
  fields: ['label', 'value'],
  numberFields: ['value'],
  aspect: 0.62,
  minHeight: 160,
  isEmpty(data) {
    const values = collapseLabels(data).items.flatMap((d) => (d.value === null ? [] : [d.value]))
    if (!values.length) return true
    return values.every((v) => v < 0) ? 'All values are negative: nothing to sweep' : false
  },
  draw(ctx) {
    const { theme, options: o, fmt, width: W, height: H } = ctx
    const FS = SIZES.tick
    const { items: raw, duplicates } = collapseLabels(ctx.data)
    const negatives = raw.filter((d) => d.value !== null && d.value < 0).length
    const items = sortItems(raw.filter((d) => d.value === null || d.value >= 0), o.sort)
    const max = o.max ?? Math.max(...items.map((d) => d.value ?? 0), 0)
    const full = (o.sweep * Math.PI) / 180
    // geometry: the circle fits the height; a ring pitch of at least 6px and 4.5% of the
    // radius (about 17 rings at most) -- past that, rings are slivers nobody can tell apart
    const R = Math.max(20, Math.min(H / 2 - 4, (W - 8) / 2))
    const r0 = R * 0.22
    const fit = Math.max(1, Math.floor((R - r0) / Math.max(6, R * 0.045)))
    const shown = items.slice(0, fit)
    const step = (R - r0) / shown.length
    const thick = step * (shown.length === 1 ? 0.5 : 0.78)
    // labels every ring when they fit its pitch, else every k-th
    const every = Math.max(1, Math.ceil((FS * 1.3) / step))
    const valueText = (v: number | null) => (v === null ? '—' : fmt.value(v))
    const valueW = o.showValues ? Math.max(...shown.map((d) => ctx.measure(valueText(d.value)))) : 0
    const wanted = Math.max(...shown.map((d) => ctx.measure(d.label))) + (o.showValues ? valueW + 6 : 0)
    // labels run left from just before 12 o'clock; they may reach past the circle when there is room
    const labelW = Math.min(wanted, Math.max(R - 6, W - 8 - R - 6))
    const left = Math.max(R, labelW + 6)
    const cx = 4 + left + Math.max(0, (W - 8 - left - R) / 2)
    const cy = H / 2
    const at = `translate(${cx},${cy})`
    const marks: Mark[] = []
    const gen = d3arc<DefaultArcObject>().cornerRadius(Math.min(thick / 2, 3))
    let clamped = 0
    shown.forEach((d, i) => {
      const outer = R - i * step, inner = outer - thick, mid = (outer + inner) / 2
      const track = gen({ innerRadius: inner, outerRadius: outer, startAngle: 0, endAngle: full }) ?? ''
      marks.push({ type: 'path', layer: 'back', key: `t:${d.label}`, d: track, fill: theme.track, transform: at, inert: true })
      if (i % every === 0) {
        const vt = valueText(d.value)
        const budget = labelW - (o.showValues ? valueW + 6 : 0)
        const name = ctx.truncate(d.label, Math.max(12, budget))
        const nameX = o.showValues ? -6 - valueW - 6 : -6
        marks.push({ type: 'text', layer: 'labels', key: `l:${d.label}`, x: nameX, y: -mid, text: name, full: name !== d.label ? d.label : undefined, className: name !== d.label ? 'oc-trunc' : undefined, anchor: 'end', baseline: 'central', size: FS, fill: theme.ink, transform: at })
        if (o.showValues) marks.push({ type: 'text', layer: 'labels', key: `v:${d.label}`, x: -6, y: -mid, text: vt, anchor: 'end', baseline: 'central', size: FS, fill: theme.inkSecondary, transform: at, inert: true })
      }
      if (d.value === null) return
      if (d.value > max) clamped++
      const share = max > 0 ? Math.min(1, d.value / max) : 0
      // a zero still gets a sliver, so it can be hovered and focused
      const end = Math.max(share * full, 1.5 / mid)
      const sector: DefaultArcObject = { innerRadius: inner, outerRadius: outer, startAngle: 0, endAngle: end }
      const path = gen(sector) ?? ''
      const c = gen.centroid(sector)
      const fill = o.colorBy === 'category' ? theme.cat(i) : theme.cat(0)
      const pct = max > 0 ? `${Math.round((d.value / max) * 100)}% of ${fmt.value(max)}` : ''
      const rows: Array<[string, string]> = [['Value', fmt.value(d.value)]]
      if (o.max !== undefined) rows.push(['Share of max', pct])
      marks.push({
        type: 'path', role: 'arc', key: d.label, d: path, fill, pattern: o.colorBy === 'category' ? i : undefined, transform: at,
        cx: c[0], cy: c[1], box: sectorBox(inner, outer, end),
        datum: d, label: `${d.label}: ${fmt.value(d.value)}${o.max !== undefined ? ` (${pct})` : ''}`, tooltip: { title: d.label, rows, color: fill },
      })
    })
    const nulls = shown.filter((d) => d.value === null).length
    return {
      marks,
      note: notes([
        [duplicates, 'duplicate labels summed'],
        [negatives, 'negative values omitted'],
        [clamped, `values above the max (${fmt.value(max)}) drawn as a full ring`],
        [items.length - shown.length, `more rings did not fit (all ${items.length} are in the table)`],
        [nulls, 'missing values left as empty rings'],
      ]),
    }
  },
  describe({ data, options: o, fmt, name }) {
    const { items } = collapseLabels(data)
    const ok = items.filter((d) => d.value === null || d.value >= 0)
    const of = o.max !== undefined ? `, out of ${fmt.value(o.max)}` : ''
    return `${name}, ${phrase.count(ok.length, 'ring')}, ${phrase.extremes(ok, fmt)}${of}`
  },
  table(data) {
    const { items } = collapseLabels(data)
    return { columns: ['Label', 'Value'], rows: items.map((d) => [d.label, d.value]) }
  },
})

export default radialBar
