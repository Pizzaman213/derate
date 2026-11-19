import { z } from 'zod'
import { fields } from '../../core'

/** One lollipop per row. Duplicate labels are summed (and noted under the chart). */
export const input = fields.rows({
  label: fields.label('category'),
  value: fields.numOrNull('dot position; null leaves the row blank'),
}, 'rows of { label, value }')

export const options = z.object({
  orientation: z.enum(['horizontal', 'vertical']).default('horizontal').describe('horizontal: categories down the side (long names read); vertical: categories along the bottom'),
  sort: z.enum(['none', 'ascending', 'descending']).default('none').describe('row order'),
  baseline: z.number().finite().default(0).describe('value the stems grow from'),
  showValues: z.boolean().default(false).describe('print each value beside its dot'),
  xTitle: z.string().optional(),
  yTitle: z.string().optional(),
})

export type LollipopRow = z.output<typeof input>[number]
export type LollipopOptions = z.output<typeof options>
