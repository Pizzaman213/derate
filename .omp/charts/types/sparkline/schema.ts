import { z } from 'zod'
import { fields } from '../../core'

const INDEX = /^-?\d{1,5}(\.\d+)?$/

/** x is a date, or a plain number (an index / ordinal position); absent means row order. */
const x = z.preprocess(
  (v) => (v === null || v === '' ? undefined : typeof v === 'string' && INDEX.test(v.trim()) ? Number(v) : v),
  z.union([z.number().finite(), fields.date()]).optional(),
).describe('date or numeric index; omit to use row order')

/** One point per row. */
export const input = fields.rows({
  x,
  y: fields.numOrNull('value; null leaves a gap'),
}, 'rows of { x?, y }')

export const options = z.object({
  showMinMax: z.boolean().default(true).describe('dots on the lowest and highest points'),
  showLastValue: z.boolean().default(false).describe('print the last value at the right end'),
  area: z.boolean().default(false).describe('fill under the line'),
})

export type SparklineRow = z.output<typeof input>[number]
export type SparklineOptions = z.output<typeof options>
