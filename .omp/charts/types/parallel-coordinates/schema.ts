import { z } from 'zod'
import { fields } from '../../core'

/** One polyline per row; every other numeric column is an axis. */
export const input = fields.rows({
  series: fields.optLabel('group: colours the line, toggled from the legend'),
  label: fields.optLabel('name of the row, for tooltips and announcements'),
}, 'rows of numeric columns, with an optional series and label')

export const options = z.object({
  columns: z.array(z.string()).optional().describe('numeric columns to use as axes, in order (default: every numeric column, up to 12)'),
  zero: z.boolean().default(false).describe('start every axis at zero instead of at its minimum'),
})

export type PcRow = z.output<typeof input>[number]
export type PcOptions = z.output<typeof options>
