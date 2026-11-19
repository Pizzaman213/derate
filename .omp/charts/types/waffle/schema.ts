import { z } from 'zod'
import { fields } from '../../core'

/** One part per row. Duplicate labels are summed; nulls, zeros and negatives are left out (noted). */
export const input = fields.rows({
  label: fields.label('part name'),
  value: fields.numOrNull('part size; must be positive to get squares'),
}, 'rows of { label, value }')

export const options = z.object({
  cells: z.number().int().min(4).max(10000).default(100).describe('squares in the grid; 100 makes each square 1%'),
  columns: z.number().int().min(1).max(200).optional().describe('squares per row (default: the square root of cells, rounded)'),
  maxSlices: z.number().int().min(2).max(24).default(8).describe('above this many parts the smallest are grouped as "Other"'),
  sort: z.enum(['none', 'descending', 'ascending']).default('descending').describe('part order, filling row by row from the top left ("Other" stays last)'),
})

export type WaffleRow = z.output<typeof input>[number]
export type WaffleOptions = z.output<typeof options>
