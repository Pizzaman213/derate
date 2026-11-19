// Euler diagram: up to eight sets as circles, laid out by @upsetjs/venn.js (greedy initial
// layout refined by Nelder-Mead on the pairwise-overlap loss, then normalised so disjoint
// clusters pack together). Circle areas are exact; overlaps are a best fit, so the chart
// says when the fit is visibly off. Only the pure layout functions are used -- no DOM.

import { circleOverlap, greedyLayout, normalizeSolution, venn as solve } from '@upsetjs/venn.js'
import { defineChart } from '../../core'
import { adaptAreas, areaKey, drawSetDiagram, setsDescribe, setsEmpty, setsTable, type DiagramLayout, type SetModel } from '../_shared/SetCharts'
import { input, options, type EulerOptions, type EulerRow } from './schema'

/** Share of the total set area by which drawn pairwise overlaps miss the requested ones. */
export function layoutLoss(m: SetModel, circles: DiagramLayout['circles']): number {
  let miss = 0, total = 0
  for (const n of m.names) total += m.sizes.get(n)!
  for (let i = 0; i < circles.length; i++) {
    for (let j = i + 1; j < circles.length; j++) {
      const a = circles[i]!, b = circles[j]!
      const want = m.sizes.get(areaKey([a.set, b.set])) ?? 0
      miss += Math.abs(circleOverlap(a.r, b.r, Math.hypot(a.x - b.x, a.y - b.y)) - want)
    }
  }
  return total > 0 ? miss / total : 0
}

function eulerLayout(m: SetModel): DiagramLayout {
  if (m.names.length === 1) return { circles: [{ set: m.names[0]!, x: 0, y: 0, r: Math.sqrt(m.sizes.get(m.names[0]!)! / Math.PI) }] }
  // greedy initial layout: venn.js's default switches to a randomly restarted MDS at 8+ areas,
  // and a chart must lay out the same way every time
  const solution = normalizeSolution(solve(m.areas.filter((a) => a.size > 0), { initialLayout: greedyLayout }), Math.PI / 2)
  const circles = m.names.map((n) => ({ set: n, x: solution[n]!.x, y: solution[n]!.y, r: solution[n]!.radius }))
  const loss = layoutLoss(m, circles)
  return { circles, note: loss > 0.05 ? `approximate layout: drawn overlaps are off by ${Math.round(loss * 100)}% of the total area (exact counts in the table, or use 'upset')` : undefined }
}

export const euler = defineChart<EulerRow[], EulerOptions>({
  id: 'euler',
  name: 'Euler diagram',
  category: 'sets',
  intents: ['sets', 'compare'],
  summary: 'Up to eight sets as circles sized by count, overlapping where they share members.',
  input,
  options,
  shape: 'object',
  fields: ['sets', 'size'],
  numberFields: ['size'],
  aspect: 0.7,
  minHeight: 220,
  adapt: (raw, opts) => adaptAreas(raw, opts),
  isEmpty: (data) => setsEmpty(data),
  draw: (ctx) => drawSetDiagram(ctx, eulerLayout),
  describe: ({ data, fmt, name }) => setsDescribe(data, fmt, name),
  table: (data) => setsTable(data),
})

export default euler
