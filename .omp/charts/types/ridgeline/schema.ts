import { z } from 'zod'
import { fields } from '../../core'
import { bandwidthOption, focusOption } from '../_shared/DistributionA'

/** One observation per row. */
export const input = fields.rows({
  group: fields.label('the ridge this value belongs to'),
  value: fields.numOrNull('the observation; null is dropped (and noted)'),
}, 'rows of { group, value }')

export const options = z.object({
  bandwidth: bandwidthOption,
  overlap: z.number().min(0).max(4).default(1.2).describe('how far the tallest ridge rises into the rows above, in rows (0: no overlap)'),
  scale: z.enum(['shared', 'each']).default('shared').describe("'shared': one density scale, so peaks compare; 'each': every ridge as tall as the tallest"),
  colorBy: z.enum(['single', 'group']).default('single').describe('one colour, or one per group'),
  focus: focusOption,
  sort: z.enum(['none', 'ascending', 'descending']).default('none').describe('row order by median (ascending: lowest median at the top)'),
  maxGroups: z.number().int().min(2).default(30).describe('more groups than this: the smallest are pooled into "Other"'),
  xTitle: z.string().optional(),
})

export type RidgeRow = z.output<typeof input>[number]
export type RidgeOptions = z.output<typeof options>
