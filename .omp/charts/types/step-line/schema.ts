import { z } from 'zod'
import { fields } from '../../core'
import { gapsOption, pointsOption, xField, xTypeOption } from '../_shared/TrendA'

/** One row per (x, series); `series` is optional (one line when absent). */
export const input = fields.rows({
  x: xField('date (or number) on the x axis'),
  y: fields.numOrNull('value held until the next change; null lifts the pen'),
  series: fields.optLabel('optional: one stepped line per series'),
}, 'rows of { x, y, series? }')

export const options = z.object({
  step: z.enum(['after', 'before', 'center']).default('after').describe("where the value changes: at its own x ('after'), at the previous x ('before'), or halfway ('center')"),
  gaps: gapsOption('gap'),
  xType: xTypeOption,
  points: pointsOption,
  xTitle: z.string().optional(),
  yTitle: z.string().optional(),
})

export type StepLineRow = z.output<typeof input>[number]
export type StepLineOptions = z.output<typeof options>
