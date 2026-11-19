// Dendrogram: d3.cluster, every leaf on one baseline at the right, elbow links. With a
// `height` on inner nodes (the merge distance of a hierarchical clustering) inner nodes
// sit at their distance on a bottom axis; without it, by depth.

import { cluster as d3cluster } from 'd3-hierarchy'
import { scaleLinear } from 'd3-scale'
import { defineChart, phrase, SIZES } from '../../core'
import { notes } from '../../core/helpers'
import type { Formatters, Mark } from '../../core'
import { adaptTree, branchColor, buildTree, displayName, fittedText, nodeLabel, nodeTooltip, Placer, treeFacts, treeTable, type TNode, type TreeDatum } from '../_shared/FlowB'
import { input, options, type DendrogramInput, type DendrogramOptions } from './schema'

const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v))

/**
 * Merge heights per node: leaves 0 (a collapsed leaf keeps its own), inner nodes their
 * `height`, raised to their tallest child when lower or missing. null: no usable heights.
 */
export function mergeHeights(root: TNode): { h: Map<TNode, number>; raised: number; missing: number } | null {
  if (!root.descendants().some((n) => n.children && typeof n.data.height === 'number')) return null
  const h = new Map<TNode, number>()
  let raised = 0, missing = 0
  root.eachAfter((n) => {
    const own = typeof n.data.height === 'number' ? Math.max(0, n.data.height) : undefined
    if (!n.children) {
      h.set(n, own ?? 0)
      return
    }
    let kid = 0
    for (const c of n.children) kid = Math.max(kid, h.get(c) ?? 0)
    if (own === undefined) missing++
    else if (own < kid) raised++
    h.set(n, Math.max(own ?? kid, kid))
  })
  return (h.get(root) ?? 0) > 0 ? { h, raised, missing } : null
}

function closest(data: TreeDatum, fmt: Formatters): string {
  const { root } = buildTree(data)
  const mh = mergeHeights(root)
  if (!mh) return ''
  let best: TNode | null = null
  for (const n of root.descendants()) if (n.children && (best === null || mh.h.get(n)! < mh.h.get(best)!)) best = n
  if (!best) return ''
  return `, heights up to ${fmt.num(mh.h.get(root)!)}, closest ${phrase.listPhrase(best.children!.map(displayName))} at ${fmt.num(mh.h.get(best)!)}`
}

export const dendrogram = defineChart<DendrogramInput, DendrogramOptions>({
  id: 'dendrogram',
  name: 'Dendrogram',
  category: 'flow-hierarchy',
  intents: ['hierarchy', 'network'],
  summary: 'A cluster tree with every leaf aligned; inner nodes placed by merge distance or by depth.',
  input,
  options,
  shape: 'object',
  fields: ['id', 'parent', 'name', 'value', 'height', 'path'],
  numberFields: ['value', 'height'],
  adapt: (raw, o) => adaptTree(raw, o),
  isEmpty: (d) => d === null,
  aspect: 0.62,
  minHeight: 240,
  maxHeight: 760,
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    if (!ctx.data) return { marks: [] }
    const W = ctx.width, H = ctx.height
    const b = buildTree(ctx.data, { maxDepth: o.maxDepth })
    const root = b.root
    const nodes = root.descendants()
    const leaves = root.leaves()
    const fs = SIZES.tick, lh = fs + 4
    const mh = o.useHeight ? mergeHeights(root) : null
    const maxH = mh ? mh.h.get(root)! : 0
    const textOf = (n: TNode) => {
      const hidden = b.hiddenBelow.get(n) ?? 0
      return `${displayName(n)}${hidden ? ` (+${fmt.integer(hidden)})` : ''}`
    }

    d3cluster<TreeDatum>().size([1, 1]).separation(() => 1)(root)
    let labelW = 0
    for (const n of leaves) labelW = Math.max(labelW, ctx.measure(textOf(n), fs))
    labelW = Math.min(labelW, W * 0.4)
    const axisH = mh ? fs + 10 + (o.distanceTitle ? lh + 2 : 0) : 0
    const x0 = leaves.length === 1 && !root.children ? Math.max(10, (W - labelW) / 2 - 10) : 10
    const x1 = root.children ? W - labelW - 14 : x0
    const y0 = lh / 2 + 4
    const y1 = H - lh / 2 - 4 - axisH
    const px = (n: TNode) => (mh ? x0 + (1 - mh.h.get(n)! / maxH) * (x1 - x0) : x0 + (n.y ?? 0) * (x1 - x0))
    const py = (n: TNode) => y0 + (n.x ?? 0.5) * (y1 - y0)
    const spacing = leaves.length > 1 ? (y1 - y0) / (leaves.length - 1) : 20
    const r = clamp(spacing * 0.36, 1.25, 4)
    const large = nodes.length > 2000

    const marks: Mark[] = []
    for (const n of nodes) {
      if (!n.parent) continue
      const c = o.colorBy === 'branch' ? branchColor(theme, b, n, 'branch') : theme.axis
      marks.push({ type: 'path', layer: 'back', key: `l:${n.id}`, d: `M${px(n.parent)},${py(n.parent)}V${py(n)}H${px(n)}`, stroke: c, strokeOpacity: o.colorBy === 'branch' ? 0.7 : 0.85, strokeWidth: large ? 0.6 : 1.25, fill: 'none', inert: true })
    }
    for (const n of nodes) {
      const color = branchColor(theme, b, n, o.colorBy)
      const hidden = b.hiddenBelow.get(n) ?? 0
      const hv = mh?.h.get(n)
      const extra: Array<[string, string]> = hv !== undefined && (n.children || hidden) ? [['Height', fmt.num(hv)]] : []
      if (n.children || hidden) extra.push(['Leaves', fmt.integer(hidden ? hiddenLeaves(n.data) : n.leaves().length)])
      const style = hidden
        ? { r: r + 1.5, fill: theme.mix(theme.surface, color, 0.35), stroke: color, strokeWidth: 2 }
        : n.children
          ? { r: Math.max(1.25, r * 0.75), fill: color }
          : { r, fill: theme.surface, stroke: color, strokeWidth: large ? 1 : 1.5 }
      const label = nodeLabel(n, fmt, hidden) + (hv !== undefined && n.children ? `, merged at ${fmt.num(hv)}` : '')
      marks.push({ type: 'circle', role: 'node', key: `n:${n.id}`, cx: px(n), cy: py(n), ...style, datum: n.data, label, tooltip: nodeTooltip(n, fmt, color, hidden, n.data.value, extra) })
    }

    // labels: leaves to the right (dropped where they would collide), named inner nodes above their link
    const placer = new Placer()
    const minText = ctx.measure('Ab…', fs)
    for (const n of leaves) {
      const full = textOf(n)
      const x = px(n) + (b.hiddenBelow.has(n) ? r + 1.5 : r) + 4, y = py(n)
      const shown = ctx.truncate(full, W - x - 4, fs)
      if (!shown || ctx.measure(shown, fs) < Math.min(minText, ctx.measure(full, fs))) continue
      if (!placer.place({ x0: x, x1: x + ctx.measure(shown, fs), y0: y - lh / 2, y1: y + lh / 2 })) continue
      marks.push(fittedText({ type: 'text', layer: 'labels', key: `t:${n.id}`, x, y, anchor: 'start', baseline: 'central', fill: theme.inkSecondary, size: fs, halo: true }, full, shown))
    }
    for (const n of nodes) {
      // merges named only by their row id (c12) are identified in the tooltip, not on the chart
      if (!n.children || !n.data.name || n.data.name === n.data.id) continue
      const full = textOf(n)
      const x = px(n), y = py(n) - 4
      const room = n.parent ? x - px(n.parent) - 8 : W - x - 8
      const shown = ctx.truncate(full, room, fs)
      if (!shown || ctx.measure(shown, fs) < Math.min(minText, ctx.measure(full, fs))) continue
      const w = ctx.measure(shown, fs)
      const box = n.parent ? { x0: x - 4 - w, x1: x - 4, y0: y - lh, y1: y } : { x0: x + 4, x1: x + 4 + w, y0: y - lh, y1: y }
      if (box.y0 < 0 || !placer.place(box)) continue
      marks.push(fittedText({ type: 'text', layer: 'labels', key: `t:${n.id}`, x: n.parent ? x - 4 : x + 4, y, anchor: n.parent ? 'end' : 'start', baseline: 'alphabetic', fill: theme.ink, size: fs, weight: 500, halo: true }, full, shown))
    }

    // distance axis
    if (mh) {
      const ay = y1 + lh / 2 + 4
      const s = scaleLinear().domain([maxH, 0]).range([x0, x1])
      marks.push({ type: 'line', layer: 'axes', key: 'ax:base', x1: x0, y1: ay, x2: x1, y2: ay, stroke: theme.axis, strokeWidth: 1, inert: true })
      const ticks = s.ticks(Math.max(2, Math.floor((x1 - x0) / 70)))
      const tw = Math.max(...ticks.map((t) => ctx.measure(fmt.num(t), fs)))
      const pitch = ticks.length > 1 ? Math.abs(s(ticks[1]!) - s(ticks[0]!)) : Infinity
      const every = Math.max(1, Math.ceil((tw + 10) / pitch))
      ticks.forEach((t, i) => {
        if (i % every) return
        const x = s(t), text = fmt.num(t), w = ctx.measure(text, fs)
        const tx = clamp(x, w / 2 + 2, W - w / 2 - 2)
        marks.push({ type: 'line', layer: 'axes', key: `ax:t:${t}`, x1: x, y1: ay, x2: x, y2: ay + 4, stroke: theme.axis, strokeWidth: 1, inert: true })
        marks.push({ type: 'text', layer: 'axes', key: `ax:l:${t}`, x: tx, y: ay + 5, text, anchor: 'middle', baseline: 'hanging', fill: theme.muted, size: fs, inert: true })
      })
      if (o.distanceTitle) {
        const full = o.distanceTitle, shown = ctx.truncate(full, x1 - x0, fs)
        marks.push(fittedText({ type: 'text', layer: 'axes', key: 'ax:title', x: (x0 + x1) / 2, y: ay + fs + 9, anchor: 'middle', baseline: 'hanging', fill: theme.inkSecondary, size: fs, weight: 500 }, full, shown))
      }
    }
    return {
      marks,
      note: notes([
        [b.duplicates, 'duplicate sibling names kept as separate nodes'],
        [b.collapsed, `nodes below depth ${o.maxDepth ?? 0} collapsed`],
        [mh?.raised ?? 0, "merge heights raised to their children's"],
        [mh?.missing ?? 0, "inner nodes without a height placed at their children's"],
      ]),
    }
  },
  describe({ data, options: o, fmt, name }) {
    if (!data) return `${name}, no leaves`
    const f = treeFacts(data)
    const merges = f.nodes - f.leaves
    const tail = o.useHeight ? closest(data, fmt) : ''
    return `${name}, ${phrase.count(f.leaves, 'leaf', 'leaves')} joined in ${phrase.count(merges, 'merge')}${tail || `, ${phrase.count(f.levels, 'level')} under ${f.root || 'the root'}`}`
  },
  table: (data) => treeTable(data),
})

/** Leaves under a datum (for collapsed nodes, whose children were cut from the layout). */
function hiddenLeaves(d: TreeDatum): number {
  return d.children && d.children.length ? d.children.reduce((s, c) => s + hiddenLeaves(c), 0) : 1
}

export default dendrogram
