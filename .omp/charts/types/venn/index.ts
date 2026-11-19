// Venn diagram of two or three sets. Circle areas are exactly proportional to the set
// sizes and each pairwise overlap is exact: the centre distance is solved by bisection on
// the circle-intersection (lens) area. With three sets the three pairwise distances fix
// the triangle, so the three-way region is whatever that triangle produces -- noted when
// it is visibly off.

import { intersectionArea } from '@upsetjs/venn.js'
import { defineChart, type Formatters } from '../../core'
import { adaptAreas, areaKey, distanceForOverlap, drawSetDiagram, setsDescribe, setsEmpty, setsTable, type Circle, type DiagramLayout, type SetModel } from '../_shared/SetCharts'
import { input, options, type VennOptions, type VennRow } from './schema'

/** Exact radii and pairwise distances; the triple region is a consequence of the triangle. */
function vennLayout(m: SetModel, fmt: Formatters): DiagramLayout {
  const names = m.names
  const r = names.map((n) => Math.sqrt(m.sizes.get(n)! / Math.PI))
  const dist = (i: number, j: number) => {
    const o = m.sizes.get(areaKey([names[i]!, names[j]!])) ?? 0
    // disjoint sets: tangent is exact, a small gap reads as "no overlap"
    return o > 0 ? distanceForOverlap(r[i]!, r[j]!, o) : r[i]! + r[j]! + 0.12 * Math.min(r[i]!, r[j]!)
  }
  const at = (i: number, x: number, y: number): Circle => ({ set: names[i]!, x, y, r: r[i]! })
  if (names.length === 1) return { circles: [at(0, 0, 0)] }
  const dAB = dist(0, 1)
  if (names.length === 2) return { circles: [at(0, 0, 0), at(1, dAB, 0)] }

  const dAC = dist(0, 2), dBC = dist(1, 2)
  let x: number, y: number
  if (dAB < 1e-9) {
    x = dAC
    y = 0
  } else {
    x = (dAB * dAB + dAC * dAC - dBC * dBC) / (2 * dAB)
    y = Math.sqrt(Math.max(0, dAC * dAC - x * x))
  }
  const circles = [at(0, 0, 0), at(1, dAB, 0), at(2, x, y)]
  const said: string[] = []
  // the three distances need not form a triangle (inconsistent pairwise sizes)
  if (Math.abs(Math.hypot(x - dAB, y) - dBC) > 0.01 * (r[1]! + r[2]!) || Math.abs(Math.hypot(x, y) - dAC) > 0.01 * (r[0]! + r[2]!)) said.push('the pairwise overlaps cannot all be drawn exactly together, so one is approximate')
  const given = m.sizes.get(areaKey(names)) ?? 0
  const drawn = intersectionArea(circles.map((c) => ({ x: c.x, y: c.y, radius: c.r, setid: c.set })))
  const smallest = Math.min(...names.map((n) => m.sizes.get(n)!))
  if (Math.abs(drawn - given) > 0.05 * smallest) said.push(`the three-way overlap is drawn at about ${fmt.value(drawn)} for ${fmt.value(given)} (only set and pairwise areas are exact)`)
  return { circles, note: said.join('; ') || undefined }
}

export const venn = defineChart<VennRow[], VennOptions>({
  id: 'venn',
  name: 'Venn diagram',
  category: 'sets',
  intents: ['sets', 'compare'],
  summary: 'Two or three overlapping circles with areas proportional to set and overlap sizes.',
  input,
  options,
  shape: 'object',
  fields: ['sets', 'size'],
  numberFields: ['size'],
  aspect: 0.7,
  minHeight: 220,
  adapt: (raw, opts) => adaptAreas(raw, opts),
  isEmpty: (data) => setsEmpty(data),
  draw: (ctx) => drawSetDiagram(ctx, vennLayout),
  describe: ({ data, fmt, name }) => setsDescribe(data, fmt, name),
  table: (data) => setsTable(data),
})

export default venn
