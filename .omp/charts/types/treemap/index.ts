// Treemap: a hierarchy as nested rectangles, leaf area proportional to value
// (squarified), coloured by first-level branch.

import { hierarchy, treemap as d3treemap, treemapSquarify, type HierarchyRectangularNode } from 'd3-hierarchy'
import { defineChart, SIZES } from '../../core'
import type { Mark, Rect } from '../../core'
import { boxLabels, branchLegend, cleanTree, describeTree, emptyMessage, nodeFill, nodeMarkProps, toTree, treeNote, treeTable, truncateBold, type PNode } from '../_shared/PartWholeB'
import { input, options, type TreemapData, type TreemapOptions } from './schema'

/** Squarified layout of a cleaned tree in a width x height box (no padding, so area stays proportional). */
export function treemapLayout(root: PNode, width: number, height: number): HierarchyRectangularNode<PNode> {
  const h = hierarchy<PNode>(root, (n) => n.children).sum((n) => (n.children.length ? 0 : n.value))
  return d3treemap<PNode>().tile(treemapSquarify).size([width, height]).round(false)(h)
}

const overlaps = (a: Rect, b: Rect) => a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height

export const treemap = defineChart<TreemapData, TreemapOptions>({
  id: 'treemap',
  name: 'Treemap',
  category: 'part-to-whole',
  intents: ['composition', 'hierarchy'],
  summary: 'Nested rectangles whose areas are proportional to the values of a hierarchy.',
  input,
  options,
  shape: 'object',
  fields: ['path', 'id', 'parent', 'label', 'value'],
  numberFields: ['value'],
  adapt: (raw, o) => toTree(raw, o),
  isEmpty: (data) => emptyMessage(cleanTree(data)),
  aspect: 0.62,
  minHeight: 220,
  draw(ctx) {
    const { theme, fmt, options: o } = ctx
    const c = cleanTree(ctx.data, ctx.hidden)
    const legend = branchLegend(c, theme)
    const plot = { x: 0, y: 0, width: ctx.width, height: ctx.height }
    const root = c.root
    if (!root) return { marks: [], legend, note: treeNote(c), plot }
    const laid = treemapLayout(root, ctx.width, ctx.height)
    const marks: Mark[] = []
    const deep = c.maxDepth > 1
    const lh = SIZES.label * 1.25

    // first-level group outlines and names (trees deeper than one level)
    const blocks = new Map<number, Rect>()
    if (deep) {
      for (const g of laid.children ?? []) {
        if (!g.children) continue
        const w = g.x1 - g.x0, ht = g.y1 - g.y0
        marks.push({ type: 'rect', layer: 'labels', key: `g:${g.data.id}`, role: 'group', x: g.x0, y: g.y0, width: w, height: ht, fill: 'none', stroke: theme.surface, strokeWidth: 3, inert: true })
        if (!o.groupLabels || w < 48 || ht < lh * 2 + 8) continue
        const text = truncateBold(g.data.name, w - 12, SIZES.label, ctx)
        if (!text || text === '…') continue
        marks.push({ type: 'text', layer: 'labels', key: `gl:${g.data.id}`, x: g.x0 + 6, y: g.y0 + 6, text, full: text !== g.data.name ? g.data.name : undefined, baseline: 'hanging', fill: theme.surface, size: SIZES.label, weight: 700, inert: text === g.data.name })
        blocks.set(g.data.branch, { x: g.x0, y: g.y0, width: ctx.measure(text, SIZES.label, 700) + 12, height: lh + 6 })
      }
    }

    for (const leaf of laid.leaves()) {
      const n = leaf.data
      const w = leaf.x1 - leaf.x0, ht = leaf.y1 - leaf.y0
      const fill = deep && n.branch >= 0 ? theme.cat(n.branch) : nodeFill(theme, n)
      marks.push({
        type: 'rect', role: 'cell', key: n.id, x: leaf.x0, y: leaf.y0, width: w, height: ht,
        fill, stroke: theme.surface, strokeWidth: Math.min(w, ht) < 4 ? 0.5 : 1, pattern: n.branch >= 0 ? n.branch : 0,
        ...nodeMarkProps(n, root, fmt, fill),
      })
      // labels start below the group name when they would collide with it
      let y = leaf.y0, height = ht
      const block = blocks.get(n.branch)
      if (block && overlaps(block, { x: leaf.x0, y: leaf.y0, width: w, height: lh * 2 + 10 })) {
        y = Math.max(y, block.y + block.height - 4)
        height = leaf.y1 - y
      }
      marks.push(...boxLabels({ key: n.id, x: leaf.x0, y, width: w, height, name: n.name, value: o.showValues ? fmt.value(n.value) : '', fill: theme.surface, size: SIZES.tick, measure: ctx.measure, truncate: ctx.truncate, weight: deep ? 400 : 600 }))
    }
    return { marks, legend, note: treeNote(c), plot }
  },
  describe({ data, fmt, name }) {
    return describeTree(name, cleanTree(data), fmt)
  },
  table(data) {
    return treeTable(cleanTree(data))
  },
})

export default treemap
