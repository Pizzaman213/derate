import { z } from 'zod'
import { fields } from '../../core'

/** One ring per row, outermost first. Duplicate labels are summed (and noted). */
export const input = fields.rows({
  label: fields.label('ring name, written at the start of the ring'),
  value: fields.numOrNull('arc length; null leaves the ring empty'),
}, 'rows of { label, value }')

export const options = z.object({
  max: z.number().positive().finite().optional().describe('value of a full sweep (e.g. 100 for percentages); default: the largest value'),
  sweep: z.number().min(90).max(270).default(270).describe("degrees of a full ring, clockwise from 12 o'clock; the free top-left quarter holds the labels"),
  sort: z.enum(['none', 'ascending', 'descending']).default('none').describe('ring order, outermost first'),
  showValues: z.boolean().default(true).describe('print each value beside its label'),
  colorBy: z.enum(['category', 'single']).default('category').describe('one colour per ring, or one colour for all (the labels already name the rings)'),
})

export type RadialBarRow = z.output<typeof input>[number]
export type RadialBarOptions = z.output<typeof options>
