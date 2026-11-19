import { z } from 'zod'
import { fields } from '../../core'

/** One observation per row. */
export const input = fields.rows({
  group: fields.label('the box this value belongs to'),
  value: fields.numOrNull('the observation; null is dropped (and noted)'),
}, 'rows of { group, value }')

export const options = z.object({
  orientation: z.enum(['vertical', 'horizontal']).default('vertical').describe('vertical boxes over groups on x, or horizontal boxes with groups on y'),
  whisker: z.number().positive().default(1.5).describe('whiskers reach the furthest value within this many IQRs of the box; beyond is an outlier'),
  showMean: z.boolean().default(false).describe('mark the mean with a diamond'),
  points: z.enum(['outliers', 'all', 'none']).default('outliers').describe('dots for outliers only, every value (jittered), or none'),
  sort: z.enum(['none', 'ascending', 'descending']).default('none').describe('group order by median'),
  maxGroups: z.number().int().min(2).default(30).describe('more groups than this: the smallest are pooled into "Other"'),
  xTitle: z.string().optional(),
  yTitle: z.string().optional(),
})

export type BoxRow = z.output<typeof input>[number]
export type BoxOptions = z.output<typeof options>
