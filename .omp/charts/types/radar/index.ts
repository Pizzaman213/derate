// Radar (spider) chart: one spoke per axis, one polygon per series on a concentric
// grid. A vertex's distance from the centre is linear in its value, from the inner
// bound (0, or the minimum when values go negative) to `max` at the outer ring.

import { scaleLinear } from 'd3-scale'
import { defineChart, phrase, SIZES, type Mark, type Rect } from '../../core'
import { isRecord } from '../../adapters'
import { finiteExtent, notes, uniq } from '../../core/helpers'
import { cellNum, columnNames, isNumericColumn, overlaps } from '../_shared/RelationshipB'
import { input, options, type RadarOptions, type RadarRow } from './schema'

const ONE = 'Value'

/** Wide rows -> { axis, series, value }; long rows pass through. */
function adapt(raw: unknown): unknown {
  if (!Array.isArray(raw) || raw.length === 0 || !raw.every(isRecord)) return raw
  if (raw.some((r) => 'axis' in r)) return raw
  const cols = columnNames(raw)
  const seriesCol = cols.includes('series') ? 'series' : cols.find((c) => !isNumericColumn(raw, c))
  const axes = cols.filter((c) => c !== seriesCol && isNumericColumn(raw, c))
  return raw.flatMap((r, i) => {
    const s = seriesCol !== undefined ? r[seriesCol] : undefined
    const series = s === null || s === undefined || s === '' ? `Series ${i + 1}` : String(s)
    return axes.map((axis) => ({ axis, series, value: cellNum(r, axis) }))
  })
}

interface Model {
  axes: string[]
  series: string[]
  /** series -> axis -> value (summed duplicates; null = missing). */
  values: Map<string, Map<string, number | null>>
  duplicates: number
}

function model(rows: RadarRow[]): Model {
  const axes = uniq(rows.map((r) => r.axis))
  const series = uniq(rows.map((r) => r.series ?? ONE))
  const values = new Map(series.map((s) => [s, new Map<string, number | null>()]))
  let duplicates = 0
  for (const r of rows) {
    const m = values.get(r.series ?? ONE)!
    if (m.has(r.axis)) {
      duplicates++
      const prev = m.get(r.axis)!
      m.set(r.axis, prev === null ? r.value : r.value === null ? prev : prev + r.value)
    } else m.set(r.axis, r.value)
  }
  return { axes, series, values, duplicates }
}

/** Inner and outer bound of the radial scale. */
function domainOf(m: Model, max: number | undefined, levels: number): [number, number] {
  const ext = finiteExtent([...m.values.values()].flatMap((a) => [...a.values()])) ?? [0, 1]
  const lo = Math.min(0, ext[0])
  let hi = max ?? (scaleLinear().domain([lo, Math.max(ext[1], lo)]).nice(levels).domain()[1] as number)
  if (!(hi > lo)) hi = lo + (Math.abs(lo) || 1)
  return [lo, hi]
}

export const radar = defineChart<RadarRow[], RadarOptions>({
  id: 'radar',
  name: 'Radar chart',
  category: 'relationship',
  intents: ['relationship', 'correlation', 'compare'],
  summary: 'One polygon per series over spokes for each axis, on a concentric grid.',
  input,
  options,
  shape: 'rows',
  fields: ['axis', 'series', 'value'],
  numberFields: ['value'],
  adapt,
  aspect: 0.8,
  minHeight: 220,
  maxHeight: 640,
  isEmpty: (rows) => (rows.length === 0 ? true : rows.every((r) => r.value === null) ? 'No values to plot' : false),
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    const W = ctx.width, H = ctx.height, FS = SIZES.tick
    const m = model(ctx.data)
    const n = m.axes.length
    const [lo, hi] = domainOf(m, o.max, o.levels)

    // radius: leave room for the axis labels around the outside
    const longest = Math.max(...m.axes.map((a) => ctx.measure(a)))
    const sideRoom = Math.min(longest, W * 0.22, 140)
    const R = Math.max(16, Math.min(H / 2 - FS * 1.6 - 6, W / 2 - sideRoom - 14))
    const cx = W / 2, cy = H / 2
    const angle = (i: number) => -Math.PI / 2 + (2 * Math.PI * i) / n
    const rad = (v: number) => Math.max(0, Math.min(R, (R * (v - lo)) / (hi - lo)))
    const at = (i: number, r: number): [number, number] => [cx + r * Math.cos(angle(i)), cy + r * Math.sin(angle(i))]

    const marks: Mark[] = []
    // grid rings and their values (along the top spoke)
    const ticks = scaleLinear().domain([lo, hi]).ticks(o.levels).filter((t) => t > lo && t < hi)
    // the outer ring is always max; drop a tick that would crowd it
    const tickStep = ticks.length > 1 ? ticks[1]! - ticks[0]! : hi - lo
    if (ticks.length && hi - ticks[ticks.length - 1]! < tickStep * 0.5) ticks.pop()
    const ringVals = [...ticks, hi]
    const showRingLabels = R / ringVals.length >= FS * 1.2
    const ringLabels: Array<{ t: number; y: number }> = []
    for (const t of ringVals) {
      const r = rad(t)
      if (o.grid === 'circle' || n < 3) {
        marks.push({ type: 'circle', layer: 'grid', key: `ring:${t}`, cx, cy, r, fill: 'none', stroke: theme.grid, strokeWidth: 1, inert: true })
      } else {
        const d = m.axes.map((_, i) => { const [x, y] = at(i, r); return `${i ? 'L' : 'M'}${x.toFixed(2)},${y.toFixed(2)}` }).join('') + 'Z'
        marks.push({ type: 'path', layer: 'grid', key: `ring:${t}`, d, fill: 'none', stroke: theme.grid, strokeWidth: 1, inert: true })
      }
      if (showRingLabels) ringLabels.push({ t, y: cy - r })
    }
    // spokes and axis labels (truncated to the room they have, thinned when they collide)
    const kept: Rect[] = []
    m.axes.forEach((a, i) => {
      const [x2, y2] = at(i, R)
      marks.push({ type: 'line', layer: 'grid', key: `sp:${a}`, x1: cx, y1: cy, x2, y2, stroke: theme.grid, strokeWidth: 1, inert: true })
      const c = Math.cos(angle(i)), s = Math.sin(angle(i))
      const [lx, ly] = at(i, R + 6)
      const anchor = c > 0.25 ? 'start' : c < -0.25 ? 'end' : 'middle'
      const baseline = s < -0.35 ? 'alphabetic' : s > 0.35 ? 'hanging' : 'middle'
      const arc = n > 1 ? (2 * Math.PI * (R + 6)) / n - 6 : W
      const room = anchor === 'start' ? W - lx - 2 : anchor === 'end' ? lx - 2 : Math.min(2 * (lx - 2), 2 * (W - lx - 2), Math.max(arc, 30))
      const text = ctx.truncate(a, Math.max(0, room))
      if (!text) return
      const w = ctx.measure(text)
      const bx = anchor === 'start' ? lx : anchor === 'end' ? lx - w : lx - w / 2
      const by = baseline === 'alphabetic' ? ly - FS : baseline === 'hanging' ? ly : ly - FS / 2
      const box = { x: bx, y: by, width: w, height: FS * 1.1 }
      if (kept.some((b) => overlaps(b, box, 2)) || by < 0 || by + FS * 1.1 > H) return
      kept.push(box)
      marks.push({ type: 'text', layer: 'axes', key: `al:${a}`, x: lx, y: ly, text, full: text !== a ? a : undefined, anchor, baseline, size: FS, fill: theme.inkSecondary, className: text !== a ? 'oc-trunc' : undefined })
    })
    // ring values along the top spoke, unless an axis label is already there
    for (const { t, y } of ringLabels) {
      const text = fmt.value(t)
      const box = { x: cx + 4, y: y - FS / 2, width: ctx.measure(text), height: FS }
      if (kept.some((b) => overlaps(b, box, 1))) continue
      marks.push({ type: 'text', layer: 'labels', key: `rv:${t}`, x: cx + 4, y, text, anchor: 'start', baseline: 'middle', size: FS, fill: theme.muted, halo: true, inert: true })
    }
    const fillOpacity = m.series.length <= 2 ? 0.16 : m.series.length <= 4 ? 0.09 : 0.05

    // series: polygon (or broken outline when a vertex is missing) + vertices
    let missing = 0, clamped = 0
    const pr = n > 24 ? 2 : 3
    m.series.forEach((sName, si) => {
      const vals = m.values.get(sName)!
      const color = theme.cat(si)
      const pts = m.axes.map((a, i) => { const v = vals.get(a) ?? null; return v === null ? null : at(i, rad(v)) })
      const ok = pts.filter((p): p is [number, number] => p !== null)
      missing += pts.length - ok.length
      if (!ok.length) return
      const complete = ok.length === n
      let d = ''
      if (complete) d = ok.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(2)},${y.toFixed(2)}`).join('') + 'Z'
      else {
        // join consecutive present vertices only, wrapping round the circle
        for (let i = 0; i < n; i++) {
          const p = pts[i], q = pts[(i + 1) % n]
          if (p && q && n > 1) d += `M${p[0].toFixed(2)},${p[1].toFixed(2)}L${q[0].toFixed(2)},${q[1].toFixed(2)}`
        }
      }
      const xs = ok.map((p) => p[0]), ys = ok.map((p) => p[1])
      const box = { x: Math.min(...xs) - 2, y: Math.min(...ys) - 2, width: Math.max(...xs) - Math.min(...xs) + 4, height: Math.max(...ys) - Math.min(...ys) + 4 }
      const parts = m.axes.map((a) => `${a} ${vals.get(a) == null ? 'missing' : fmt.value(vals.get(a)!)}`)
      if (d) {
        marks.push({
          type: 'path', role: 'area', key: `s:${sName}`, d, fill: complete && o.fill ? color : 'none', fillOpacity, stroke: color, strokeWidth: 2, linejoin: 'round', series: sName,
          cx: box.x + box.width / 2, cy: box.y + box.height / 2, box,
          datum: { series: sName, values: Object.fromEntries(vals) }, label: `${sName}: ${parts.join(', ')}`,
          tooltip: { title: sName, rows: m.axes.map((a): [string, string] => [a, vals.get(a) == null ? '–' : fmt.value(vals.get(a)!)]), color },
        })
      }
      m.axes.forEach((a, i) => {
        const v = vals.get(a)
        if (v == null) return
        if (v > hi) clamped++
        const [x, y] = pts[i]!
        marks.push({
          type: 'circle', role: 'point', key: `p:${sName}:${a}`, cx: x, cy: y, r: pr, fill: color, stroke: theme.surface, strokeWidth: 1, series: sName,
          datum: { axis: a, series: sName, value: v }, label: `${m.series.length > 1 ? `${sName}, ` : ''}${a}: ${fmt.value(v)}`,
          tooltip: { title: a, rows: [[m.series.length > 1 ? sName : 'Value', fmt.value(v)]], color },
        })
      })
    })
    return {
      marks,
      legend: m.series.length > 1 ? { items: m.series.map((s, i) => ({ key: s, label: s, color: theme.cat(i), shape: 'square' as const })) } : undefined,
      note: notes([
        [m.duplicates, 'duplicate axis/series values summed'],
        [missing, `missing ${missing === 1 ? 'value' : 'values'} left out (the outline breaks there)`],
        [clamped, `${clamped === 1 ? 'value' : 'values'} above the max drawn at the outer ring`],
      ]),
      plot: { x: cx - R, y: cy - R, width: 2 * R, height: 2 * R },
    }
  },
  describe({ data, fmt, name }) {
    const m = model(data)
    const items = m.series.flatMap((s) => m.axes.map((a) => ({ label: m.series.length > 1 ? `${s} on ${a}` : a, value: m.values.get(s)!.get(a) ?? null })))
    const who = m.series.length > 1 ? `${phrase.count(m.series.length, 'series', 'series')} over ` : ''
    return `${name}, ${who}${phrase.count(m.axes.length, 'axis', 'axes')} (${phrase.listPhrase(m.axes, 4)}), ${phrase.extremes(items, fmt)}`
  },
  table(data) {
    const m = model(data)
    return { columns: ['Series', ...m.axes], rows: m.series.map((s) => [s, ...m.axes.map((a) => m.values.get(s)!.get(a) ?? null)]) }
  },
})

export default radar
