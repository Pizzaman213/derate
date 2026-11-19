import { z } from 'zod'
import { fields } from '../../core'

/** One line per row, from its start value to its end value. */
export const input = fields.rows({
  label: fields.label('what the line is (a country, a product)'),
  start: fields.numOrNull('value at the first time point'),
  end: fields.numOrNull('value at the second time point'),
}, 'rows of { label, start, end }')

export const options = z.object({
  startLabel: z.string().default('Start').describe('heading over the left axis, e.g. "2020"'),
  endLabel: z.string().default('End').describe('heading over the right axis, e.g. "2024"'),
  colorBy: z.enum(['direction', 'category']).default('direction').describe('rises/falls in the positive/negative colours, or one colour per line'),
  showValues: z.boolean().default(true).describe('print the value beside each label'),
})

export type SlopeRow = z.output<typeof input>[number]
export type SlopeOptions = z.output<typeof options>
