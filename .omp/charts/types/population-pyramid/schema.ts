import { z } from 'zod'
import { fields } from '../../core'

/** One row per age band and side. Repeated (age, sex) pairs are summed (noted). */
export const input = fields.rows({
  age: fields.label('age band, e.g. "0–4" or "85+"'),
  sex: fields.label("which side the bar goes on: 'male' | 'female', or any two groups"),
  value: fields.numOrNull('count (or share) in that band; null leaves a gap, negatives are omitted (both noted)'),
}, 'rows of { age, sex, value }')

export const options = z.object({
  left: z.string().optional().describe("group drawn on the left; default: the male group when there is one, else the first seen"),
  right: z.string().optional().describe("group drawn on the right; default: the female group when there is one, else the second seen"),
  percentages: z.boolean().default(false).describe('show each bar as a share of the whole population (both sides)'),
  order: z.enum(['youngest-bottom', 'as-given']).default('youngest-bottom').describe('youngest-bottom: bands ordered by their first number with the youngest at the bottom; as-given: first-seen band at the top'),
  valueTitle: z.string().optional().describe('title of the value axis'),
  ageTitle: z.string().optional().describe('title of the age axis'),
})

export type PyramidRow = z.output<typeof input>[number]
export type PyramidOptions = z.output<typeof options>
