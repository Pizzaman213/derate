import { z } from 'zod'
import { fields } from '../../core'

/** One bar per row. Duplicate labels are summed (and noted under the chart). */
export const input = fields.rows({
  label: fields.label('category on the x axis'),
  value: fields.numOrNull('bar height; null leaves a gap'),
}, 'rows of { label, value }')

export const options = z.object({
  sort: z.enum(['none', 'ascending', 'descending']).default('none').describe('bar order'),
  showValues: z.boolean().default(false).describe('print each value above its bar'),
  colorBy: z.enum(['single', 'category']).default('single').describe('one colour, or one per category (needs a reason: categories are not ordinal)'),
  xTitle: z.string().optional(),
  yTitle: z.string().optional(),
})

export type BarRow = z.output<typeof input>[number]
export type BarOptions = z.output<typeof options>
