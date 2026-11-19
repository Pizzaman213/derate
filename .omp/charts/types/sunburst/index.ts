// Sunburst: a hierarchy as concentric rings. The root is the centre disc; each ring is
// one level, and every node's angle is its share of its parent's angle.

import { hierarchy, partition, type HierarchyRectangularNode } from 'd3-hierarchy'
import { defineChart, SIZES } from '../../core'
import type { Mark, Rect } from '../../core'
import { branchLegend, cleanTree, describeTree, emptyMessage, nodeFill, nodeMarkProps, toTree, treeNote, treeTable, truncateBold, type PNode } from '../_shared/PartWholeB'
import { input, options, type SunburstData, type SunburstOptions } from './schema'

const TAU = Math.PI * 2

/** Partition of a cleaned tree: x0..x1 is the angle (radians, clockwise from 12 o'clock), y0..y1 the level (root 0..1). */
export function sunburstLayout(root: PNode): HierarchyRectangularNode<PNode> {
  const h = hierarchy<PNode>(root, (n) => n.children).sum((n) => (n.children.length ? 0 : n.value))
  return partition<PNode>().size([TAU, h.height + 1])(h)
}

const r2 = (n: number) => Math.round(n * 100) / 100

/** An annular sector (or a full ring / disc) in absolute coordinates. */
export function sectorPath(cx: number, cy: number, a0: number, a1: number, r0: number, r1: number): string {
  const p = (a: number, r: number) => `${r2(cx + r * Math.sin(a))},${r2(cy - r * Math.cos(a))}`
  if (a1 - a0 >= TAU - 1e-6) {
    const outer = `M${p(0, r1)}A${r2(r1)},${r2(r1)} 0 1 1 ${p(Math.PI, r1)}A${r2(r1)},${r2(r1)} 0 1 1 ${p(0, r1)}Z`
    return r0 > 0 ? `${outer}M${p(0, r0)}A${r2(r0)},${r2(r0)} 0 1 0 ${p(Math.PI, r0)}A${r2(r0)},${r2(r0)} 0 1 0 ${p(0, r0)}Z` : outer
  }
  const large = a1 - a0 > Math.PI ? 1 : 0
  const head = `M${p(a0, r1)}A${r2(r1)},${r2(r1)} 0 ${large} 1 ${p(a1, r1)}`
  return r0 > 0 ? `${head}L${p(a1, r0)}A${r2(r0)},${r2(r0)} 0 ${large} 0 ${p(a0, r0)}Z` : `${head}L${p(a0, 0)}Z`
}

function sectorBox(cx: number, cy: number, a0: number, a1: number, r0: number, r1: number): Rect {
  if (a1 - a0 >= TAU - 1e-6) return { x: cx - r1, y: cy - r1, width: r1 * 2, height: r1 * 2 }
  const xs: number[] = [], ys: number[] = []
  const add = (a: number, r: number) => { xs.push(cx + r * Math.sin(a)); ys.push(cy - r * Math.cos(a)) }
  for (const r of [r0, r1]) { add(a0, r); add(a1, r) }
  // the extreme points of the outer arc at each quarter turn it crosses
  for (let q = Math.ceil(a0 / (Math.PI / 2)) * (Math.PI / 2); q < a1; q += Math.PI / 2) add(q, r1)
  const x = Math.min(...xs), y = Math.min(...ys)
  return { x, y, width: Math.max(2, Math.max(...xs) - x), height: Math.max(2, Math.max(...ys) - y) }
}

export const sunburst = defineChart<SunburstData, SunburstOptions>({
  id: 'sunburst',
  name: 'Sunburst',
  category: 'part-to-whole',
  intents: ['composition', 'hierarchy'],
  summary: 'Concentric rings of a hierarchy: each arc spans its share of its parent.',
  input,
  options,
  shape: 'object',
  fields: ['path', 'id', 'parent', 'label', 'value'],
  numberFields: ['value'],
  adapt: (raw, o) => toTree(raw, o),
  isEmpty: (data) => emptyMessage(cleanTree(data)),
  aspect: 1,
  minHeight: 220,
  maxHeight: 560,
  draw(ctx) {
    const { theme, fmt, options: o } = ctx
    const c = cleanTree(ctx.data, ctx.hidden)
    const legend = branchLegend(c, theme)
    const plot = { x: 0, y: 0, width: ctx.width, height: ctx.height }
    const root = c.root
    if (!root) return { marks: [], legend, note: treeNote(c), plot }
    const laid = sunburstLayout(root)
    const cx = ctx.width / 2, cy = ctx.height / 2
    const R = Math.max(10, Math.min(ctx.width, ctx.height) / 2 - 2)
    const ring = R / (laid.height + 1)
    const fs = SIZES.tick
    const marks: Mark[] = []
    const labels: Mark[] = []

    for (const node of laid.descendants()) {
      const n = node.data
      const a0 = node.x0, a1 = node.x1
      const r0 = node.y0 * ring, r1 = node.y1 * ring
      if (a1 - a0 <= 0) continue
      const fill = nodeFill(theme, n)
      const am = (a0 + a1) / 2, rm = node.depth === 0 ? 0 : (r0 + r1) / 2
      const thin = (a1 - a0) * r1 < 3
      marks.push({
        type: 'path', role: 'arc', key: n.id, d: sectorPath(cx, cy, a0, a1, r0, r1),
        cx: cx + rm * Math.sin(am), cy: cy - rm * Math.cos(am), box: sectorBox(cx, cy, a0, a1, r0, r1),
        fill, stroke: (a1 - a0) * r1 < 2 ? undefined : theme.surface, strokeWidth: thin ? 0.5 : 1, pattern: n.branch >= 0 ? n.branch : undefined,
        ...nodeMarkProps(n, root, fmt, fill),
      })
      if (!o.labels) continue
      const ink = n.branch < 0 && n.children.length ? theme.ink : theme.surface
      if (node.depth === 0) {
        // centre: the root's name and total
        const w = r1 * 1.6
        const name = truncateBold(n.name, w, SIZES.label, ctx, 600)
        const total = fmt.value(n.value)
        if (r1 >= SIZES.label * 1.6 && name && name !== '…') {
          const two = r1 >= SIZES.label * 2.4 && ctx.measure(total, SIZES.label) <= w
          labels.push({ type: 'text', layer: 'labels', key: `l:${n.id}`, x: cx, y: two ? cy - SIZES.label * 0.65 : cy, text: name, full: name !== n.name ? n.name : undefined, anchor: 'middle', baseline: 'central', fill: ink, size: SIZES.label, weight: 600, inert: name === n.name })
          if (two) labels.push({ type: 'text', layer: 'labels', key: `v:${n.id}`, x: cx, y: cy + SIZES.label * 0.75, text: total, anchor: 'middle', baseline: 'central', fill: ink, size: SIZES.label, inert: true })
        }
        continue
      }
      const x = cx + rm * Math.sin(am), y = cy - rm * Math.cos(am)
      const span = a1 - a0
      const depthLen = r1 - r0
      // straight text along the tangent at mid-radius: its ends must stay inside the outer
      // edge (chord) and inside the sector's sides (angle)
      const outerHalf = Math.sqrt(Math.max(0, (r1 - 2) ** 2 - (rm + fs / 2) ** 2))
      const sideHalf = span / 2 >= Math.PI / 2 ? Infinity : (rm - fs / 2) * Math.tan(span / 2) - 3
      const tangentRoom = 2 * Math.min(outerHalf, sideHalf)
      // along the arc where it is wide, otherwise along the radius
      const tangential = depthLen >= fs * 1.5 && tangentRoom >= Math.min(ctx.measure(n.name, fs), 60)
      const room = tangential ? tangentRoom : depthLen - 8
      const across = tangential ? depthLen : span * (r0 + 4)
      if (across < fs * 1.25 || room < fs * 2) continue
      const text = ctx.truncate(n.name, room, fs)
      if (!text || text === '…' || ([...text].length < 3 && text !== n.name)) continue
      let deg = tangential ? (am * 180) / Math.PI : (am * 180) / Math.PI - 90
      if (tangential ? am > Math.PI / 2 && am < (3 * Math.PI) / 2 : am > Math.PI) deg += 180
      labels.push({ type: 'text', layer: 'labels', key: `l:${n.id}`, x, y, text, full: text !== n.name ? n.name : undefined, anchor: 'middle', baseline: 'central', rotate: r2(deg), fill: ink, size: fs, inert: text === n.name })
    }
    return { marks: [...marks, ...labels], legend, note: treeNote(c), plot }
  },
  describe({ data, fmt, name }) {
    return describeTree(name, cleanTree(data), fmt)
  },
  table(data) {
    return treeTable(cleanTree(data))
  },
})

export default sunburst
