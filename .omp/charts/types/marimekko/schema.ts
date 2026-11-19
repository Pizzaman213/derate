import { z } from 'zod'
import { fields } from '../../core'

/** One segment per row: column `label`, stack `series`, `value`. Repeated (label, series) pairs are summed. */
export const input = fields.rows({
  label: fields.label('column (x category); its width is its share of the grand total'),
  series: fields.label('segment within the column; its height is its share of the column'),
  value: fields.numOrNull('segment size; negatives and nulls are left out and noted'),
}, 'rows of { label, series, value }')

export const options = z.object({
  sort: z.enum(['none', 'descending', 'ascending']).default('none').describe('column order by column total'),
  showValues: z.boolean().default(true).describe("print each segment's share of its column where it fits"),
  yTitle: z.string().optional(),
})

export type MarimekkoRow = z.output<typeof input>[number]
export type MarimekkoOptions = z.output<typeof options>
