import { z } from 'zod'
import { fields } from '../../core'

/** One row per reading; readings sharing a (position, cycle) are averaged. */
export const input = fields.rows({
  date: fields.date('when the reading was taken (read in options.timeZone)'),
  value: fields.numOrNull('the reading; null is skipped'),
}, 'rows of { date, value }')

export const options = z.object({
  period: z.enum(['month-of-year', 'day-of-week', 'quarter']).default('month-of-year').describe('the season: one panel per month, weekday or quarter; each panel runs across years (weeks for day-of-week)'),
  weekStart: z.union([z.literal(0), z.literal(1)]).default(1).describe('day-of-week order and week grouping: 0 = Sunday first, 1 = Monday first'),
  yTitle: z.string().optional(),
})

export type CycleRow = z.output<typeof input>[number]
export type CycleOptions = z.output<typeof options>
