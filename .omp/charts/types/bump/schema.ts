import { z } from 'zod'
import { fields } from '../../core'

/** One row per (x, series): a rank, or a value the rank is derived from. */
export const input = fields.rows({
  x: fields.label('time point: a date, a year or an ordinal label ("Week 1")'),
  series: fields.label('the thing being ranked'),
  rank: fields.numOrNull('1 = first; derived from value when missing'),
  value: fields.numOrNull('ranked per x, highest first, when rank is missing'),
}, 'rows of { x, series, rank? | value? }')

export const options = z.object({
  showRanks: z.boolean().default(true).describe('print the rank inside each point when it fits'),
})

export type BumpRow = z.output<typeof input>[number]
export type BumpOptions = z.output<typeof options>
