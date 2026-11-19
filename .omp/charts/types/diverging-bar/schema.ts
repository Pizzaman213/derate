import { z } from 'zod'
import { fields } from '../../core'

/** One bar per row, extending left or right of the midpoint by its signed value. */
export const input = fields.rows({
  label: fields.label('category on the y axis'),
  value: fields.numOrNull('signed value; bars run right of the midpoint when above it, left when below; null leaves a gap'),
}, 'rows of { label, value }')

export const options = z.object({
  midpoint: z.number().default(0).describe('the centre line bars diverge from (a target, a scale midpoint such as 3 on a 1-5 Likert scale)'),
  sort: z.enum(['none', 'ascending', 'descending']).default('none').describe('bar order, top to bottom'),
  symmetric: z.boolean().default(true).describe('equal room either side of the midpoint, so left and right lengths compare fairly'),
  showValues: z.boolean().default(false).describe('print each value at the end of its bar'),
  aboveLabel: z.string().optional().describe('legend text for bars above the midpoint (default "Above <midpoint>", or "Positive")'),
  belowLabel: z.string().optional().describe('legend text for bars below the midpoint (default "Below <midpoint>", or "Negative")'),
  xTitle: z.string().optional(),
  yTitle: z.string().optional(),
})

export type DivergingRow = z.output<typeof input>[number]
export type DivergingOptions = z.output<typeof options>
