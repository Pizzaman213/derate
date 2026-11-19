// Quadrant chart: a scatter split into four quadrants at xMid / yMid (the medians by
// default), with each quadrant named in its corner ("Quick wins") and points labelled
// where the label fits without colliding. Colour and the legend follow the quadrant.

import { defineChart, SIZES, stats, type Mark, type Rect } from '../../core'
import { finiteExtent, notes } from '../../core/helpers'
import { inside, overlaps } from '../_shared/RelationshipB'
import { input, options, type QuadrantOptions, type QuadrantRow } from './schema'

type Q = 'q1' | 'q2' | 'q3' | 'q4'
const QS: Q[] = ['q1', 'q2', 'q3', 'q4']
/** Above this many points no labels are attempted: they could not fit anyway. */
const LABEL_LIMIT = 300

interface Pt { row: QuadrantRow; x: number; y: number; q: Q; key: string }

function prep(rows: QuadrantRow[], o: QuadrantOptions) {
  const seen = new Map<string, number>()
  const pts: Pt[] = []
  let skipped = 0, duplicates = 0
  const ok = rows.filter((r): r is QuadrantRow & { x: number; y: number } => r.x !== null && r.y !== null)
  const xMid = o.xMid ?? (ok.length ? stats.median(ok.map((r) => r.x)) : 0)
  const yMid = o.yMid ?? (ok.length ? stats.median(ok.map((r) => r.y)) : 0)
  for (const r of rows) {
    if (r.x === null || r.y === null) { skipped++; continue }
    const n = seen.get(r.label) ?? 0
    seen.set(r.label, n + 1)
    if (n) duplicates++
    const right = r.x >= xMid, top = r.y >= yMid
    pts.push({ row: r, x: r.x, y: r.y, q: top ? (right ? 'q1' : 'q2') : right ? 'q4' : 'q3', key: n ? `${r.label}#${n}` : r.label })
  }
  return { pts, xMid, yMid, skipped, duplicates }
}

function names(o: QuadrantOptions): Record<Q, string> {
  const xn = o.xTitle ?? 'x', yn = o.yTitle ?? 'y'
  return {
    q1: o.q1 ?? `High ${xn}, high ${yn}`,
    q2: o.q2 ?? `Low ${xn}, high ${yn}`,
    q3: o.q3 ?? `Low ${xn}, low ${yn}`,
    q4: o.q4 ?? `High ${xn}, low ${yn}`,
  }
}

export const quadrant = defineChart<QuadrantRow[], QuadrantOptions>({
  id: 'quadrant',
  name: 'Quadrant chart',
  category: 'relationship',
  intents: ['relationship', 'correlation', 'rank'],
  summary: 'A scatter split into four named quadrants at a threshold on each axis.',
  input,
  options,
  shape: 'rows',
  fields: ['label', 'x', 'y'],
  numberFields: ['x', 'y'],
  zoom: 'xy',
  aspect: 0.7,
  minHeight: 240,
  isEmpty: (rows) => (rows.length === 0 ? true : rows.every((r) => r.x === null || r.y === null) ? 'No rows have both x and y' : false),
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    const FS = SIZES.tick, LS = SIZES.label
    const { pts, xMid, yMid, skipped, duplicates } = prep(ctx.data, o)
    const qn = names(o)
    const color = (q: Q) => (o.colorBy === 'single' ? theme.cat(0) : theme.cat(QS.indexOf(q)))
    // domains always include the split, so all four quadrants exist on screen
    const pad = ([a, b]: [number, number], frac: number): [number, number] => { const d = (b - a) * frac || Math.abs(a) * 0.1 || 1; return [a - d, b + d] }
    const xd = pad(finiteExtent([...pts.map((p) => p.x), xMid]) ?? [0, 1], 0.06)
    // keep a band free at the top and bottom of the plot for the corner names
    const band = Math.min(0.3, (LS + 12) / Math.max(60, ctx.height - 60))
    const yd = pad(finiteExtent([...pts.map((p) => p.y), yMid]) ?? [0, 1], band / (1 - 2 * band))
    const f = ctx.cartesian({ x: { type: 'linear', domain: xd, zero: false }, y: { type: 'linear', domain: yd, zero: false }, grid: 'both', xTitle: o.xTitle, yTitle: o.yTitle, zoom: true })
    const P = f.plot
    const clampX = (v: number) => Math.max(P.x, Math.min(P.x + P.width, v))
    const clampY = (v: number) => Math.max(P.y, Math.min(P.y + P.height, v))
    const mx = clampX(f.px(xMid)), my = clampY(f.py(yMid))
    const marks: Mark[] = [...f.marks]

    // quadrant tints, split lines, corner names
    const regions: Record<Q, Rect> = {
      q1: { x: mx, y: P.y, width: P.x + P.width - mx, height: my - P.y },
      q2: { x: P.x, y: P.y, width: mx - P.x, height: my - P.y },
      q3: { x: P.x, y: my, width: mx - P.x, height: P.y + P.height - my },
      q4: { x: mx, y: my, width: P.x + P.width - mx, height: P.y + P.height - my },
    }
    const used: Rect[] = []
    for (const q of QS) {
      const r = regions[q]
      if (o.colorBy === 'quadrant') marks.push({ type: 'rect', layer: 'back', key: `bg:${q}`, ...r, fill: color(q), fillOpacity: 0.06, inert: true })
      const right = q === 'q1' || q === 'q4', top = q === 'q1' || q === 'q2'
      const room = Math.max(r.width - 12, P.width / 2 - 12)
      const text = ctx.truncate(qn[q], room, LS)
      if (!text) continue
      const w = ctx.measure(text, LS, 600)
      const x = right ? P.x + P.width - 6 : P.x + 6
      const y = top ? P.y + 6 : P.y + P.height - 6
      used.push({ x: right ? x - w : x, y: top ? y : y - LS, width: w, height: LS * 1.1 })
      marks.push({ type: 'text', layer: 'labels', key: `qn:${q}`, x, y, text, full: text !== qn[q] ? qn[q] : undefined, anchor: right ? 'end' : 'start', baseline: top ? 'hanging' : 'alphabetic', size: LS, weight: 600, fill: o.colorBy === 'quadrant' ? color(q) : theme.inkSecondary, halo: true, inert: text === qn[q], className: text !== qn[q] ? 'oc-trunc' : undefined })
    }
    if (mx > P.x && mx < P.x + P.width) marks.push({ type: 'line', layer: 'grid', key: 'mid-x', x1: mx, x2: mx, y1: P.y, y2: P.y + P.height, stroke: theme.ruleStrong, strokeWidth: 1.5, dash: '5 4', inert: true })
    if (my > P.y && my < P.y + P.height) marks.push({ type: 'line', layer: 'grid', key: 'mid-y', x1: P.x, x2: P.x + P.width, y1: my, y2: my, stroke: theme.ruleStrong, strokeWidth: 1.5, dash: '5 4', inert: true })

    // points
    const n = pts.length
    const r = n <= 60 ? 5 : n <= 500 ? 3.5 : 2
    const xName = o.xTitle ?? 'x', yName = o.yTitle ?? 'y'
    const placed = pts.map((p) => ({ p, cx: f.px(p.x), cy: f.py(p.y) }))
    for (const { p, cx, cy } of placed) {
      const c = color(p.q)
      marks.push({
        type: 'circle', role: 'point', key: `p:${p.key}`, cx, cy, r, fill: c, fillOpacity: n > 500 ? 0.6 : 0.85, stroke: theme.surface, strokeWidth: n > 500 ? 0 : 1, series: p.q,
        datum: p.row, label: `${p.row.label}: ${xName} ${fmt.value(p.x)}, ${yName} ${fmt.value(p.y)}, ${qn[p.q]}`,
        tooltip: { title: p.row.label, rows: [[xName, fmt.value(p.x)], [yName, fmt.value(p.y)], ['Quadrant', qn[p.q]]], color: c },
      })
    }

    // labels: the most extreme points first, four candidate spots each, never over another label, a point or a corner name
    if (o.showLabels && n <= LABEL_LIMIT) {
      const visible = placed.filter(({ p, cx, cy }) => !ctx.hidden.has(p.q) && cx >= P.x && cx <= P.x + P.width && cy >= P.y && cy <= P.y + P.height)
      const dots = visible.map(({ cx, cy }) => ({ x: cx - r, y: cy - r, width: 2 * r, height: 2 * r }))
      const sx = Math.max(1e-9, P.width / 2), sy = Math.max(1e-9, P.height / 2)
      const order = [...visible].sort((a, b) => Math.hypot((b.cx - mx) / sx, (b.cy - my) / sy) - Math.hypot((a.cx - mx) / sx, (a.cy - my) / sy))
      for (const { p, cx, cy } of order) {
        const text = ctx.truncate(p.row.label, 140)
        if (!text) continue
        const w = ctx.measure(text), h = FS * 1.15, g = r + 3
        const spots: Array<{ x: number; y: number; anchor: 'start' | 'end' | 'middle'; box: Rect }> = [
          { x: cx + g, y: cy, anchor: 'start', box: { x: cx + g, y: cy - h / 2, width: w, height: h } },
          { x: cx - g, y: cy, anchor: 'end', box: { x: cx - g - w, y: cy - h / 2, width: w, height: h } },
          { x: cx, y: cy - g - h / 2, anchor: 'middle', box: { x: cx - w / 2, y: cy - g - h, width: w, height: h } },
          { x: cx, y: cy + g + h / 2, anchor: 'middle', box: { x: cx - w / 2, y: cy + g, width: w, height: h } },
        ]
        const spot = spots.find((s) => inside(s.box, P) && !used.some((u) => overlaps(u, s.box, 1)) && !dots.some((d) => overlaps(d, s.box, 0)))
        if (!spot) continue
        used.push(spot.box)
        marks.push({ type: 'text', layer: 'labels', key: `pl:${p.key}`, x: spot.x, y: spot.y, text, full: text !== p.row.label ? p.row.label : undefined, anchor: spot.anchor, baseline: 'central', size: FS, fill: theme.inkSecondary, halo: true, inert: text === p.row.label, series: p.q, className: text !== p.row.label ? 'oc-trunc' : undefined })
      }
    }
    const counts = QS.map((q) => pts.filter((p) => p.q === q).length)
    return {
      marks,
      legend: o.colorBy === 'quadrant' ? { items: QS.map((q, i) => ({ key: q, label: `${qn[q]} (${counts[i]})`, color: color(q), shape: 'circle' as const })) } : undefined,
      clip: P,
      plot: P,
      invertX: f.invertX,
      invertY: f.invertY,
      note: notes([[skipped, `${skipped === 1 ? 'row' : 'rows'} without both x and y left out`], [duplicates, `duplicate ${duplicates === 1 ? 'label' : 'labels'} kept as separate points`]]),
    }
  },
  describe({ data, options: o, fmt, name }) {
    const { pts, xMid, yMid } = prep(data, o)
    const qn = names(o)
    const how = o.xMid === undefined && o.yMid === undefined ? ' (medians)' : ''
    const counts = QS.map((q) => ({ q, n: pts.filter((p) => p.q === q).length }))
    const top = counts.reduce((a, b) => (b.n > a.n ? b : a))
    return `${name}, ${pts.length} ${pts.length === 1 ? 'item' : 'items'}, split at ${o.xTitle ?? 'x'} ${fmt.value(xMid)} and ${o.yTitle ?? 'y'} ${fmt.value(yMid)}${how}, most in ${qn[top.q]} (${top.n})`
  },
  table(data, o) {
    const { pts } = prep(data, o)
    const qn = names(o)
    const quadOf = new Map(pts.map((p) => [p.row, qn[p.q]]))
    return { columns: ['Label', o.xTitle ?? 'x', o.yTitle ?? 'y', 'Quadrant'], rows: data.map((r) => [r.label, r.x, r.y, quadOf.get(r) ?? null]) }
  },
})

export default quadrant
