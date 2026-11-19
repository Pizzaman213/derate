import { z } from 'zod'
import { fields } from '../../core'
import { isDateLike, parseDate } from '../../time'

/** A category or a date: Dates and epoch numbers (10 or 13 digits) are dates, other numbers become text, null is "(blank)". */
const axisValue = (description: string) =>
  z.preprocess(
    (v) => (v instanceof Date ? v : typeof v === 'number' && isDateLike(v) ? parseDate(v) ?? String(v) : typeof v === 'number' || typeof v === 'boolean' ? String(v) : v === null || v === undefined ? '(blank)' : v),
    z.union([z.date(), z.string()], { invalid_type_error: 'text or a date' }),
  ).describe(description)

/** One cell per row. Rows sharing x and y are combined by `aggregate` (and noted). */
export const input = fields.rows({
  x: axisValue('column: a category, or a date (dates sort chronologically)'),
  y: axisValue('row: a category, or a date'),
  value: fields.numOrNull('cell value; null is drawn hatched'),
}, 'rows of { x, y, value }')

const order = z.enum(['input', 'name', 'value'])

export const options = z.object({
  xOrder: order.default('input').describe('column order for categories: first seen, by name, or by column total (dates are always chronological)'),
  yOrder: order.default('input').describe('row order for categories: first seen, by name, or by row total'),
  aggregate: z.enum(['sum', 'mean', 'max', 'min', 'count']).default('sum').describe('how rows sharing a cell combine'),
  diverging: z.boolean().optional().describe('force (true) or forbid (false) the diverging ramp; default: diverging when values span `center`'),
  center: z.number().default(0).describe('midpoint of the diverging ramp'),
  colorScale: z.enum(['linear', 'log']).default('linear').describe('log compresses outliers (signed, so zero and negatives still work)'),
  showValues: z.boolean().default(true).describe('print values in cells when they fit'),
  xTitle: z.string().optional(),
  yTitle: z.string().optional(),
})

export type HeatmapRow = z.output<typeof input>[number]
export type HeatmapOptions = z.output<typeof options>
