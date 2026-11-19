import { z } from 'zod'
import { fields } from '../../core'

/** One row per date: the work still remaining on it. */
export const input = fields.rows({
  date: fields.date('the day (or instant) of the reading'),
  remaining: fields.numOrNull('work remaining at that date (points, hours, tasks); null leaves a gap'),
  ideal: fields.numOrNull('the planned remaining work; when no row has one, the ideal line runs straight from the first reading to zero at the end'),
  scopeChange: fields.numOrNull('work added (+) or removed (−) on that date; flagged along the top'),
  note: fields.optLabel('what happened on that date ("Payments epic added"); shown on the flag'),
}, 'rows of { date, remaining, ideal?, scopeChange?, note? }')

export const options = z.object({
  end: z.union([z.string(), z.number(), z.date()]).optional().describe('the sprint/release end date: the ideal line reaches zero there and the x axis runs to it (default: the last date)'),
  area: z.boolean().default(true).describe('shade the area under the remaining line'),
  xTitle: z.string().optional(),
  yTitle: z.string().optional(),
})

export type BurndownRow = z.output<typeof input>[number]
export type BurndownOptions = z.output<typeof options>
