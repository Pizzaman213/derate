// Circle packing: d3.pack, one circle per node, nested inside its parent; a leaf's area
// is proportional to its value and a group's circle encloses its leaves. Leaves without
// a positive value cannot have an area: they are left out and counted in the note.

import { pack } from 'd3-hierarchy'
import { defineChart, phrase, SIZES } from '../../core'
import { notes } from '../../core/helpers'
import type { LegendSpec, Mark } from '../../core'
import { adaptTree, buildTree, displayName, fittedText, nodeLabel, nodeTooltip, Placer, treeTable, type TNode, type TreeDatum } from '../_shared/FlowB'
import { input, options, type CirclePackingInput, type CirclePackingOptions } from './schema'

export interface Positive {
  tree: TreeDatum | null
  leaves: number
  nulls: number
  negatives: number
  zeros: number
}

/** The tree with every leaf that has no positive value removed (and groups left empty by that). */
export function positiveTree(data: TreeDatum): Positive {
  let leaves = 0, nulls = 0, negatives = 0, zeros = 0
  const walk = (n: TreeDatum): TreeDatum | null => {
    if (n.children && n.children.length) {
      const kids = n.children.map(walk).filter((c): c is TreeDatum => c !== null)
      return kids.length ? { ...n, children: kids } : null
    }
    const v = n.value
    if (v === undefined || v === null) nulls++
    else if (v < 0) negatives++
    else if (v === 0) zeros++
    else {
      leaves++
      return n
    }
    return null
  }
  return { tree: walk(data), leaves, nulls, negatives, zeros }
}

const leafValue = (d: TreeDatum) => (d.children && d.children.length ? 0 : Math.max(0, d.value ?? 0))

export const circlePacking = defineChart<CirclePackingInput, CirclePackingOptions>({
  id: 'circle-packing',
  name: 'Circle packing',
  category: 'flow-hierarchy',
  intents: ['hierarchy', 'composition'],
  summary: 'Nested circles: each group encloses its members, each leaf sized by its value.',
  input,
  options,
  shape: 'object',
  fields: ['id', 'parent', 'name', 'value', 'path'],
  numberFields: ['value'],
  adapt: (raw, o) => adaptTree(raw, o),
  isEmpty(d) {
    if (!d) return true
    const p = positiveTree(d)
    if (p.tree) return false
    return p.zeros > 0 && p.nulls === 0 && p.negatives === 0 ? 'All values are zero' : 'No positive values to size the circles'
  },
  aspect: 1,
  minHeight: 280,
  maxHeight: 760,
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    const p = ctx.data ? positiveTree(ctx.data) : null
    if (!p?.tree) return { marks: [] }
    const W = ctx.width, H = ctx.height
    const b = buildTree(p.tree, { maxDepth: o.maxDepth, sum: leafValue, sort: (x, y) => (y.value ?? 0) - (x.value ?? 0), exclude: (t) => ctx.hidden.has(t.id ?? '') })
    const total = b.root.value ?? 0
    const groups = b.top.length
    const legendOn = o.colorBy === 'branch' && groups >= 2 && groups <= 12
    const legend: LegendSpec | undefined = legendOn ? { items: b.top.map((t, i) => ({ key: t.id ?? '', label: displayName(t), color: theme.cat(i), shape: 'circle' as const })) } : undefined
    const note = notes([
      [p.nulls, 'leaves without a value omitted'],
      [p.negatives, 'negative values omitted'],
      [p.zeros, 'zero values omitted'],
      [b.duplicates, 'duplicate sibling names kept as separate nodes'],
      [b.collapsed, `nodes below depth ${o.maxDepth ?? 0} collapsed`],
    ])
    if (total <= 0) return { marks: [], legend, note }

    const nodes = b.root.descendants()
    const large = nodes.length > 1500
    const root = pack<TreeDatum>()
      .size([W - 2, H - 2])
      .padding(large ? 1 : 3)(b.root)
    let maxD = 0
    for (const n of nodes) if (n.depth > maxD) maxD = n.depth
    const topOf = (n: TNode): TNode | null => {
      let t: TNode = n
      while (t.parent && t.parent.parent) t = t.parent
      return t.parent ? t : null
    }
    const colorOf = (n: TNode) => {
      if (o.colorBy === 'depth') return theme.seq(maxD ? 0.25 + (0.75 * n.depth) / maxD : 0.6)
      const i = b.branch.get(n) ?? -1
      return i < 0 ? (n.children ? theme.muted : theme.cat(0)) : theme.cat(i)
    }

    const marks: Mark[] = []
    for (const n of root.descendants()) {
      const color = colorOf(n)
      const hidden = b.hiddenBelow.get(n) ?? 0
      const isLeaf = !n.children
      const fill = !n.parent && !isLeaf
        ? theme.mix(theme.surface, theme.muted, 0.06)
        : isLeaf
          ? o.colorBy === 'depth' ? color : theme.mix(theme.surface, color, 0.72)
          : theme.mix(theme.surface, color, o.colorBy === 'depth' ? 0.35 : Math.min(0.4, 0.1 + 0.07 * (n.depth - 1)))
      const stroke = isLeaf ? (hidden ? color : theme.surface) : theme.mix(theme.surface, n.parent ? color : theme.muted, 0.45)
      const share = total > 0 ? (n.value ?? 0) / total : 0
      const t = topOf(n)
      marks.push({
        type: 'circle', role: 'node', key: `n:${n.id}`, cx: n.x + 1, cy: n.y + 1, r: Math.max(0.5, n.r),
        fill, stroke, strokeWidth: hidden ? 1.5 : isLeaf ? (large ? 0 : 0.75) : 1, dash: hidden ? '3 2' : undefined,
        series: t && legendOn ? t.id : undefined,
        pattern: t && legendOn && isLeaf ? b.branch.get(n) : undefined,
        datum: n.data, label: `${nodeLabel(n, fmt, hidden, n.value)}${n.parent ? ` (${fmt.pct(share, 1)})` : ''}`,
        tooltip: nodeTooltip(n, fmt, color, hidden, n.value, n.parent ? [['Share of total', fmt.pct(share, 1)]] : []),
      })
    }

    // labels: groups along their top edge, leaves centred (name, then value) when the circle has room
    const fs = SIZES.tick, lh = fs + 3
    const minText = ctx.measure('Ab…', fs)
    const placer = new Placer()
    const chord = (r: number, off: number) => (off >= r ? 0 : 2 * Math.sqrt(r * r - off * off))
    const ordered = [...root.descendants()].sort((a, c) => a.depth - c.depth || c.r - a.r)
    for (const n of ordered) {
      const cx = n.x + 1, cy = n.y + 1
      const hidden = b.hiddenBelow.get(n) ?? 0
      const full = `${displayName(n)}${hidden ? ` (+${fmt.integer(hidden)})` : ''}`
      if (n.children) {
        if (!n.parent || n.r < 34) continue
        const y = cy - n.r + 10 + lh / 2
        const shown = ctx.truncate(full, chord(n.r, n.r - 10) - 6, fs)
        if (!shown || ctx.measure(shown, fs) < Math.min(minText, ctx.measure(full, fs))) continue
        const w = ctx.measure(shown, fs)
        if (!placer.place({ x0: cx - w / 2, x1: cx + w / 2, y0: y - lh / 2, y1: y + lh / 2 })) continue
        marks.push(fittedText({ type: 'text', layer: 'labels', key: `t:${n.id}`, x: cx, y, anchor: 'middle', baseline: 'central', fill: theme.ink, size: fs, weight: 600, halo: true }, full, shown))
        continue
      }
      if (n.r < 12) continue
      const two = o.showValues && n.r >= 24
      const room = chord(n.r, two ? lh : lh / 2) - 6
      const shown = ctx.truncate(full, room, fs)
      if (!shown || ctx.measure(shown, fs) < Math.min(minText, ctx.measure(full, fs))) continue
      const w = ctx.measure(shown, fs)
      const ny = two ? cy - lh / 2 : cy
      if (!placer.place({ x0: cx - w / 2, x1: cx + w / 2, y0: ny - lh / 2, y1: ny + lh / 2 })) continue
      marks.push(fittedText({ type: 'text', layer: 'labels', key: `t:${n.id}`, x: cx, y: ny, anchor: 'middle', baseline: 'central', fill: theme.ink, size: fs, weight: 500, halo: true }, full, shown))
      if (two) {
        const v = fmt.value(n.value ?? 0)
        const vw = ctx.measure(v, fs)
        if (vw <= room && placer.place({ x0: cx - vw / 2, x1: cx + vw / 2, y0: cy, y1: cy + lh })) {
          marks.push({ type: 'text', layer: 'labels', key: `v:${n.id}`, x: cx, y: cy + lh / 2, text: v, anchor: 'middle', baseline: 'central', fill: theme.inkSecondary, size: fs, halo: true })
        }
      }
    }
    return { marks, legend, note }
  },
  describe({ data, fmt, name }) {
    const p = data ? positiveTree(data) : null
    if (!p?.tree) return `${name}, no positive values`
    const { root } = buildTree(p.tree, { sum: leafValue })
    const total = root.value ?? 0
    const leaves = root.leaves()
    let big = leaves[0]!
    for (const l of leaves) if ((l.value ?? 0) > (big.value ?? 0)) big = l
    const groups = root.children?.length ?? 0
    return `${name}, ${phrase.count(leaves.length, 'leaf', 'leaves')}${groups > 1 ? ` in ${phrase.count(groups, 'group')}` : ''}, total ${fmt.value(total)}, largest ${displayName(big)} at ${fmt.value(big.value ?? 0)} (${fmt.pct(total ? (big.value ?? 0) / total : 0, 0)})`
  },
  table: (data) => treeTable(data),
})

export default circlePacking
