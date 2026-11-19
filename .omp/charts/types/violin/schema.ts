import { z } from 'zod'
import { fields } from '../../core'
import { bandwidthOption, focusOption } from '../_shared/DistributionA'

/** One observation per row. */
export const input = fields.rows({
  group: fields.label('the violin this value belongs to'),
  value: fields.numOrNull('the observation; null is dropped (and noted)'),
}, 'rows of { group, value }')

export const options = z.object({
  bandwidth: bandwidthOption,
  scale: z.enum(['area', 'width', 'count']).default('area').describe("'area': one density scale, every violin has the same area; 'width': every violin as wide as the band; 'count': area grows with n"),
  inner: z.enum(['box', 'quartiles', 'none']).default('box').describe('inside each violin: a slim box plot, dashed quartile lines, or nothing'),
  trim: z.boolean().default(false).describe('end each violin at its smallest and largest value instead of letting the tails run on'),
  focus: focusOption,
  sort: z.enum(['none', 'ascending', 'descending']).default('none').describe('group order by median'),
  maxGroups: z.number().int().min(2).default(24).describe('more groups than this: the smallest are pooled into "Other"'),
  xTitle: z.string().optional(),
  yTitle: z.string().optional(),
})

export type ViolinRow = z.output<typeof input>[number]
export type ViolinOptions = z.output<typeof options>
