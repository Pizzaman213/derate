import { z } from 'zod'
import { fields } from '../../core'

/** One point per row, jittered across its group's band. */
export const input = fields.rows({
  group: fields.label('the group (one strip per group, first-seen order)'),
  value: fields.numOrNull('position on the value axis; null rows are skipped (noted)'),
}, 'rows of { group, value }')

export const options = z.object({
  orientation: z.enum(['vertical', 'horizontal']).default('vertical').describe('vertical: groups along x, values up y; horizontal: groups down y, values along x'),
  jitter: z.number().min(0).max(1).default(0.6).describe('share of the band the points spread across (0 = a single line)'),
  radius: z.number().positive().max(20).optional().describe('point radius in px; default from the point count'),
  showMedian: z.boolean().default(true).describe('a rule at each group median'),
  valueTitle: z.string().optional().describe('title of the value axis'),
  groupTitle: z.string().optional().describe('title of the group axis'),
})

export type StripRow = z.output<typeof input>[number]
export type StripOptions = z.output<typeof options>
