import { z } from 'zod'
import { fields } from '../../core'

/** One row per date: work completed so far and the total scope at that date. */
export const input = fields.rows({
  date: fields.date('the day (or instant) of the reading'),
  completed: fields.numOrNull('work completed so far (cumulative); null leaves a gap'),
  scope: fields.numOrNull('total scope at that date (completed + remaining); null leaves a gap'),
  note: fields.optLabel('what happened on that date ("Beta shipped"); flagged along the top'),
}, 'rows of { date, completed, scope, note? }')

export const options = z.object({
  projection: z.boolean().default(true).describe('project the completion date: a least-squares line through completed work, extended to the latest scope'),
  projectionWindow: z.number().int().min(2).optional().describe('fit only the last N readings (default: all of them)'),
  area: z.boolean().default(true).describe('shade the area under the completed line'),
  xTitle: z.string().optional(),
  yTitle: z.string().optional(),
})

export type BurnupRow = z.output<typeof input>[number]
export type BurnupOptions = z.output<typeof options>
