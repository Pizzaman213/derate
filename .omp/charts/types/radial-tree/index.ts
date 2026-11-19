// Radial tidy tree: d3.tree laid out in polar coordinates -- the root in the centre,
// each depth a ring. Leaf labels point outward along their ray and are truncated to the
// distance left to the chart's edge; inner labels point inward within their ring.

import { tree as d3tree } from 'd3-hierarchy'
import { defineChart, phrase, SIZES } from '../../core'
import { notes } from '../../core/helpers'
import type { Mark } from '../../core'
import { adaptTree, branchColor, buildTree, displayName, fittedText, nodeLabel, nodeTooltip, treeFacts, treeTable, type TNode } from '../_shared/FlowB'
import { input, options, type RadialTreeInput, type RadialTreeOptions } from './schema'

const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v))

interface Candidate {
  n: TNode
  band: number
  angle: number
  inner: number
  room: number
  outward: boolean
}

export const radialTree = defineChart<RadialTreeInput, RadialTreeOptions>({
  id: 'radial-tree',
  name: 'Radial tree',
  category: 'flow-hierarchy',
  intents: ['hierarchy', 'network'],
  summary: 'A tidy tree wrapped around its root: each depth is a ring, leaves face outward.',
  input,
  options,
  shape: 'object',
  fields: ['id', 'parent', 'name', 'value', 'path'],
  numberFields: ['value'],
  adapt: (raw, o) => adaptTree(raw, o),
  isEmpty: (d) => d === null,
  aspect: 1,
  minHeight: 280,
  maxHeight: 760,
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    if (!ctx.data) return { marks: [] }
    const W = ctx.width, H = ctx.height
    const cx = W / 2, cy = H / 2
    const b = buildTree(ctx.data, { maxDepth: o.maxDepth })
    const root = b.root
    const nodes = root.descendants()
    const fs = SIZES.tick, lh = fs + 3
    let maxD = 0
    for (const n of nodes) if (n.depth > maxD) maxD = n.depth
    const textOf = (n: TNode) => {
      const hidden = b.hiddenBelow.get(n) ?? 0
      return `${displayName(n)}${hidden ? ` (+${fmt.integer(hidden)})` : ''}`
    }

    // ring radius: leave room for the outermost labels (85th percentile, capped)
    const R0 = Math.min(W, H) / 2 - 4
    const deepW = nodes.filter((n) => n.depth === maxD && maxD > 0).map((n) => ctx.measure(textOf(n), fs)).sort((a, c) => a - c)
    const typical = deepW.length ? deepW[Math.min(deepW.length - 1, Math.floor(deepW.length * 0.85))]! : 0
    const R = maxD === 0 ? 0 : Math.max(R0 * 0.4, R0 - clamp(typical + 14, 16, R0 * 0.42))
    const ring = maxD ? R / maxD : 0

    d3tree<typeof root.data>()
      .size([2 * Math.PI, 1])
      .separation((a, c) => (a.parent === c.parent ? 1 : 2) / Math.max(1, a.depth))(root)
    const polar = (a: number, rad: number): [number, number] => [cx + rad * Math.sin(a), cy - rad * Math.cos(a)]
    const radOf = (n: TNode) => (n.y ?? 0) * R
    const angOf = (n: TNode) => n.x ?? 0

    // node size from the tightest angular spacing at the outer ring
    let minArc = Infinity
    const perDepth: TNode[][] = []
    for (const n of nodes) (perDepth[n.depth] ??= []).push(n)
    for (const row of perDepth) {
      if (!row || row.length < 2) continue
      row.sort((a, c) => angOf(a) - angOf(c))
      for (let i = 1; i < row.length; i++) minArc = Math.min(minArc, (angOf(row[i]!) - angOf(row[i - 1]!)) * radOf(row[i]!))
    }
    const r = clamp(Number.isFinite(minArc) ? minArc * 0.36 : 4.5, 1.5, 4.5)
    const large = nodes.length > 2000

    const marks: Mark[] = []
    for (const n of nodes) {
      if (!n.parent) continue
      const a0 = angOf(n.parent), r0 = radOf(n.parent), a1 = angOf(n), r1 = radOf(n)
      const m = (r0 + r1) / 2
      const [sx, sy] = polar(a0, r0), [c1x, c1y] = polar(a0, m), [c2x, c2y] = polar(a1, m), [tx, ty] = polar(a1, r1)
      const c = o.colorBy === 'branch' ? branchColor(theme, b, n, 'branch') : theme.axis
      marks.push({ type: 'path', layer: 'back', key: `l:${n.id}`, d: `M${sx},${sy}C${c1x},${c1y} ${c2x},${c2y} ${tx},${ty}`, stroke: c, strokeOpacity: o.colorBy === 'branch' ? 0.55 : 0.75, strokeWidth: large ? 0.6 : 1.25, fill: 'none', inert: true })
    }
    for (const n of nodes) {
      const [x, y] = polar(angOf(n), radOf(n))
      const color = branchColor(theme, b, n, o.colorBy)
      const hidden = b.hiddenBelow.get(n) ?? 0
      const style = hidden
        ? { r: r + 1.5, fill: theme.mix(theme.surface, color, 0.35), stroke: color, strokeWidth: 2 }
        : n.children
          ? { r, fill: color, stroke: theme.surface, strokeWidth: large ? 0 : 1 }
          : { r, fill: theme.surface, stroke: color, strokeWidth: large ? 1 : 1.5 }
      marks.push({ type: 'circle', role: 'node', key: `n:${n.id}`, cx: x, cy: y, ...style, datum: n.data, label: nodeLabel(n, fmt, hidden), tooltip: nodeTooltip(n, fmt, color, hidden) })
    }

    // root label: horizontal, under the root
    const minText = ctx.measure('Ab…', fs)
    const rootFull = textOf(root)
    const rootRoom = maxD ? Math.min(W - 8, Math.max(ring * 1.4, 40)) : W - 8
    const rootShown = ctx.truncate(rootFull, rootRoom, fs)
    const rootHalf = ctx.measure(rootShown, fs) / 2
    const rr0 = r + 3
    marks.push(fittedText({ type: 'text', layer: 'labels', key: `t:${root.id}`, x: cx, y: cy + rr0 + 1, anchor: 'middle', baseline: 'hanging', fill: theme.ink, size: fs, weight: 600, halo: true }, rootFull, rootShown))
    const rootReach = Math.hypot(rootHalf, rr0 + lh) + 3

    // ray budget: distance from (x, y) along (dx, dy) to the box edge, less half a line
    const toEdge = (x: number, y: number, dx: number, dy: number) => {
      let t = Infinity
      if (dx > 1e-9) t = Math.min(t, (W - 2 - x) / dx)
      if (dx < -1e-9) t = Math.min(t, (2 - x) / dx)
      if (dy > 1e-9) t = Math.min(t, (H - 2 - y) / dy)
      if (dy < -1e-9) t = Math.min(t, (2 - y) / dy)
      // the line's thickness reaches past the ray by lh/2 across it
      return t - (lh / 2) * Math.max(Math.abs(dx), Math.abs(dy)) * Math.min(Math.abs(dx), Math.abs(dy)) * 2 - 2
    }

    const cands: Candidate[] = []
    for (const n of nodes) {
      if (n.depth === 0) continue
      const rr = (b.hiddenBelow.has(n) ? r + 1.5 : r) + 3
      const rad = radOf(n), a = angOf(n)
      if (n.children) {
        // inward, within ring band n.depth (and clear of the root label)
        const innerLimit = n.depth === 1 ? rootReach : radOf(n.parent!) + r + 3
        cands.push({ n, band: n.depth, angle: a, inner: innerLimit, room: rad - rr - innerLimit, outward: false })
      } else {
        const [sx, sy] = polar(a, rad + rr)
        const edge = toEdge(sx, sy, Math.sin(a), -Math.cos(a))
        const room = n.depth === maxD ? edge : Math.min(edge, ring - 2 * rr)
        cands.push({ n, band: n.depth === maxD ? maxD + 1 : n.depth + 1, angle: a, inner: rad + rr, room, outward: true })
      }
    }
    // per band, in angle order: keep a label only when its neighbour is far enough away across the ray
    const bands = new Map<number, Candidate[]>()
    for (const c of cands) {
      const list = bands.get(c.band)
      if (list) list.push(c)
      else bands.set(c.band, [c])
    }
    for (const list of bands.values()) {
      list.sort((p, q) => p.angle - q.angle)
      const kept: Candidate[] = []
      for (const c of list) {
        const prev = kept[kept.length - 1]
        const near = Math.min(c.inner, prev?.inner ?? Infinity)
        if (prev && (c.angle - prev.angle) * near < lh * 0.95) continue
        const first = kept[0]
        if (first && first !== c && (first.angle + 2 * Math.PI - c.angle) * Math.min(c.inner, first.inner) < lh * 0.95) continue
        kept.push(c)
      }
      for (const c of kept) {
        const full = textOf(c.n)
        const shown = ctx.truncate(full, Math.min(c.room, 260), fs)
        if (!shown || ctx.measure(shown, fs) < Math.min(minText, ctx.measure(full, fs))) continue
        const rr = (b.hiddenBelow.has(c.n) ? r + 1.5 : r) + 3
        const [x, y] = polar(c.angle, radOf(c.n) + (c.outward ? rr : -rr))
        const deg = (c.angle * 180) / Math.PI - 90
        const flip = c.angle > Math.PI
        const anchor = c.outward !== flip ? 'start' : 'end'
        marks.push(fittedText({ type: 'text', layer: 'labels', key: `t:${c.n.id}`, x, y, rotate: flip ? deg - 180 : deg, anchor, baseline: 'central', fill: theme.inkSecondary, size: fs, weight: c.outward ? 400 : 500, halo: true }, full, shown))
      }
    }
    return {
      marks,
      note: notes([
        [b.duplicates, 'duplicate sibling names kept as separate nodes'],
        [b.collapsed, `nodes below depth ${o.maxDepth ?? 0} collapsed`],
      ]),
    }
  },
  describe({ data, name }) {
    if (!data) return `${name}, no nodes`
    const f = treeFacts(data)
    return `${name}, ${phrase.count(f.nodes, 'node')} in ${phrase.count(f.levels, 'ring')} around ${f.root}${f.top.length ? `, ${phrase.count(f.top.length, 'branch', 'branches')} (${phrase.listPhrase(f.top)})` : ''}, ${phrase.count(f.leaves, 'leaf', 'leaves')}`
  },
  table: (data) => treeTable(data),
})

export default radialTree
