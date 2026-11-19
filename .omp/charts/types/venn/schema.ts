import { z } from 'zod'
import { areasInput } from '../_shared/SetCharts'

/**
 * Two or three sets and their overlaps, in venn.js's convention: `size` is inclusive (the
 * row for 'A,B' counts everything in both A and B, whatever else it is in). Overlaps that
 * are not listed are empty.
 */
export const input = areasInput(3, (n, names) =>
  `a Venn diagram shows at most 3 sets, got ${n} (${names.slice(0, 6).join(', ')}${names.length > 6 ? ', …' : ''}): use 'upset' for exact overlaps of many sets, or 'euler' for an approximate area diagram of up to 8`)

export const options = z.object({})

export type VennRow = z.output<typeof input>[number]
export type VennOptions = z.output<typeof options>
