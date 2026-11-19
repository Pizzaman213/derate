import { z } from 'zod'
import { fields } from '../../core'

/** One cause per row. Duplicates are summed; negatives and nulls are left out (noted). */
export const input = fields.rows({
  label: fields.label('cause / category'),
  value: fields.numOrNull('count or cost; must be zero or more'),
}, 'rows of { label, value }')

export const options = z.object({
  maxBars: z.number().int().min(2).default(20).describe('categories beyond this collapse into a final "Other" bar'),
  threshold: z.number().gt(0).lt(1).default(0.8).describe('the reference line on the cumulative axis (0.8 = the 80/20 rule)'),
  showValues: z.boolean().default(false).describe('print each value above its bar'),
  xTitle: z.string().optional(),
  yTitle: z.string().optional(),
  cumulativeTitle: z.string().default('Cumulative %').describe('title of the right-hand cumulative axis'),
})

export type ParetoRow = z.output<typeof input>[number]
export type ParetoOptions = z.output<typeof options>
