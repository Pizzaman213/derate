import { z } from 'zod'
import { areasInput } from '../_shared/SetCharts'

/**
 * Up to eight sets and their overlaps, in venn.js's convention: `size` is inclusive (the
 * row for 'A,B' counts everything in both A and B). Overlaps that are not listed are empty.
 */
export const input = areasInput(8, (n) =>
  `an Euler diagram shows at most 8 sets, got ${n}: circles cannot show that many overlaps readably, use 'upset'`)

export const options = z.object({})

export type EulerRow = z.output<typeof input>[number]
export type EulerOptions = z.output<typeof options>
