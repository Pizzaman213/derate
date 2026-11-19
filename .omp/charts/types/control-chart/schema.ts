import { z } from 'zod'
import { fields } from '../../core'
import { toNumber } from '../../adapters'
import { parseDate } from '../../time/parse'

/** x: a date, or a number (sample number / sequence index); numeric strings become numbers. */
const xValue = (description: string) =>
  z.preprocess((v) => {
    if (v === null || v === undefined || v === '') return null
    if (typeof v === 'string') return toNumber(v) ?? parseDate(v) ?? v
    return v
  }, z.custom<number | Date>((v) => (typeof v === 'number' ? Number.isFinite(v) : v instanceof Date && !Number.isNaN(+v)), { message: 'must be a date or a number' }).nullable())
    .optional().transform((v) => v ?? null).describe(description)

/** One measurement per row, in time order (rows are sorted by x). */
export const input = fields.rows({
  x: xValue('when the measurement was taken: a date, or a sample number. Omit it everywhere to use the row order'),
  value: fields.numOrNull('the measurement; null is skipped (noted)'),
  label: fields.optLabel('a name for the measurement ("Batch 14"), used in tooltips and announcements'),
}, 'rows of { x?, value, label? }')

export const options = z.object({
  method: z.enum(['individuals', 'sigma']).default('individuals').describe("how sigma is estimated: 'individuals' (XmR: mean moving range / 1.128, the standard for one value per period) or 'sigma' (sample standard deviation)"),
  sigmas: z.number().positive().max(6).default(3).describe('control limits at mean ± sigmas × sigma'),
  zones: z.boolean().default(false).describe('shade the 1σ, 2σ and 3σ zones (C, B, A) around the mean'),
  line: z.boolean().default(true).describe('connect the measurements with a line'),
  xTitle: z.string().optional(),
  yTitle: z.string().optional(),
})

export type ControlRow = z.output<typeof input>[number]
export type ControlOptions = z.output<typeof options>
