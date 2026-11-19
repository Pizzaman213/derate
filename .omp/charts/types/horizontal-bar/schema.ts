import { z } from 'zod'
import { fields } from '../../core'

/** One horizontal bar per row. Duplicate labels are summed (and noted under the chart). */
export const input = fields.rows({
  label: fields.label('category on the y axis, read horizontally'),
  value: fields.numOrNull('bar length; null leaves a gap'),
}, 'rows of { label, value }')

export const options = z.object({
  sort: z.enum(['none', 'ascending', 'descending']).default('none').describe('bar order, top to bottom'),
  showValues: z.boolean().default(false).describe('print each value at the end of its bar'),
  colorBy: z.enum(['single', 'category']).default('single').describe('one colour, or one per category'),
  labelWidth: z.number().positive().optional().describe('max px for category labels (default 38% of the width)'),
  xTitle: z.string().optional(),
  yTitle: z.string().optional(),
})

export type HBarRow = z.output<typeof input>[number]
export type HBarOptions = z.output<typeof options>
