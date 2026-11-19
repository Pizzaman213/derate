import { z } from 'zod'
import { fields } from '../../core'

/** One row per observation; every other numeric column is a variable. */
export const input = fields.rows({
  series: fields.optLabel('group: colours the points and stacks the histograms'),
  label: fields.optLabel('name of the observation, for tooltips'),
}, 'rows of numeric columns, with an optional series and label')

export const options = z.object({
  columns: z.array(z.string()).optional().describe('numeric columns to plot, in order (default: the first 6 numeric columns)'),
  bins: z.number().int().min(2).max(60).default(10).describe('approximate histogram bins on the diagonal'),
})

export type SplomRow = z.output<typeof input>[number]
export type SplomOptions = z.output<typeof options>
