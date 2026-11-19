import { z } from 'zod'
import { fields } from '../../core'

/** One dot per row, on its category's line; `series` colours dots and adds a legend. */
export const input = fields.rows({
  label: fields.label('category (one line per category)'),
  series: fields.optLabel('series: one colour per series'),
  value: fields.numOrNull('dot position; null is skipped'),
}, 'rows of { label, value, series? }')

export const options = z.object({
  sort: z.enum(['none', 'ascending', 'descending']).default('none').describe('category order, by the mean of each category\'s values'),
  zero: z.boolean().default(false).describe('force zero into the value axis (off: the axis spans the data, which is the point of a dot plot)'),
  guides: z.boolean().default(true).describe('faint dotted guide across each category line'),
  xTitle: z.string().optional(),
  yTitle: z.string().optional(),
})

export type DotPlotRow = z.output<typeof input>[number]
export type DotPlotOptions = z.output<typeof options>
