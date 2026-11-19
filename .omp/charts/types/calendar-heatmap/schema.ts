import { z } from 'zod'
import { fields } from '../../core'

/** One row per day; several rows on the same day are summed. */
export const input = fields.rows({
  date: fields.date('the day (read in options.timeZone)'),
  value: fields.numOrNull('the day\'s value; null shows an empty day'),
}, 'rows of { date, value }')

export const options = z.object({
  weekStart: z.union([z.literal(0), z.literal(1)]).default(0).describe('first row: 0 = Sunday (GitHub), 1 = Monday (ISO)'),
})

export type CalendarRow = z.output<typeof input>[number]
export type CalendarOptions = z.output<typeof options>
