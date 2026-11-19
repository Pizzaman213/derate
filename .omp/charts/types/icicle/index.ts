// Icicle: a hierarchy as adjacent bands. One band per level; each node's extent along
// the band is its share of its parent's (rectangular partition).

import { hierarchy, partition, type HierarchyRectangularNode } from 'd3-hierarchy'
import { defineChart, SIZES } from '../../core'
import type { Mark } from '../../core'
import { boxLabels, branchLegend, cleanTree, describeTree, emptyMessage, nodeFill, nodeMarkProps, toTree, treeNote, treeTable, type PNode } from '../_shared/PartWholeB'
import { input, options, type IcicleData, type IcicleOptions } from './schema'

export interface IcicleRect { node: HierarchyRectangularNode<PNode>; x: number; y: number; width: number; height: number }

/** Partition of a cleaned tree into width x height; horizontal puts levels in columns, vertical in rows. */
export function icicleLayout(root: PNode, width: number, height: number, orientation: 'horizontal' | 'vertical'): IcicleRect[] {
  const h = hierarchy<PNode>(root, (n) => n.children).sum((n) => (n.children.length ? 0 : n.value))
  const horizontal = orientation === 'horizontal'
  // partition's x is breadth, y is depth
  const laid = partition<PNode>().size(horizontal ? [height, width] : [width, height])(h)
  return laid.descendants().map((node) => horizontal
    ? { node, x: node.y0, y: node.x0, width: node.y1 - node.y0, height: node.x1 - node.x0 }
    : { node, x: node.x0, y: node.y0, width: node.x1 - node.x0, height: node.y1 - node.y0 })
}

export const icicle = defineChart<IcicleData, IcicleOptions>({
  id: 'icicle',
  name: 'Icicle chart',
  category: 'part-to-whole',
  intents: ['composition', 'hierarchy'],
  summary: 'Adjacent bands, one per level of a hierarchy: each node spans its share of its parent.',
  input,
  options,
  shape: 'object',
  fields: ['path', 'id', 'parent', 'label', 'value'],
  numberFields: ['value'],
  adapt: (raw, o) => toTree(raw, o),
  isEmpty: (data) => emptyMessage(cleanTree(data)),
  aspect: 0.6,
  minHeight: 220,
  draw(ctx) {
    const { theme, fmt, options: o } = ctx
    const c = cleanTree(ctx.data, ctx.hidden)
    const legend = branchLegend(c, theme)
    const plot = { x: 0, y: 0, width: ctx.width, height: ctx.height }
    const root = c.root
    if (!root) return { marks: [], legend, note: treeNote(c), plot }
    const marks: Mark[] = []
    const labels: Mark[] = []
    for (const r of icicleLayout(root, ctx.width, ctx.height, o.orientation)) {
      const n = r.node.data
      if (r.width <= 0 || r.height <= 0) continue
      const fill = nodeFill(theme, n)
      marks.push({
        type: 'rect', role: 'node', key: n.id, x: r.x, y: r.y, width: r.width, height: r.height,
        // a separator on a band thinner than itself would paint the band in the surface colour
        fill, stroke: Math.min(r.width, r.height) < 2 ? undefined : theme.surface, strokeWidth: Math.min(r.width, r.height) < 4 ? 0.5 : 1, pattern: n.branch >= 0 ? n.branch : undefined,
        ...nodeMarkProps(n, root, fmt, fill),
      })
      labels.push(...boxLabels({
        key: n.id, x: r.x, y: r.y, width: r.width, height: r.height, name: n.name, value: o.showValues ? fmt.value(n.value) : '',
        fill: n.branch < 0 && n.children.length ? theme.ink : theme.surface, size: SIZES.tick, measure: ctx.measure, truncate: ctx.truncate, weight: n.depth <= 1 ? 600 : 400,
      }))
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

export default icicle
