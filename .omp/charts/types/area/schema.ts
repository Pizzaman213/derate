import { z } from 'zod'
import { fields } from '../../core'
import { gapsOption, pointsOption, xField, xTypeOption } from '../_shared/TrendA'

/** One point per row, filled down to zero. Duplicate x's are summed (and noted). */
export const input = fields.rows({
  x: xField('date (or number) on the x axis'),
  y: fields.numOrNull('value; null leaves a gap in the fill (see gaps)'),
}, 'rows of { x, y }')

export const options = z.object({
  curve: z.enum(['linear', 'monotone']).default('linear').describe("'monotone' smooths without overshooting"),
  gaps: gapsOption('gap'),
  xType: xTypeOption,
  points: pointsOption,
  xTitle: z.string().optional(),
  yTitle: z.string().optional(),
})

export type AreaRow = z.output<typeof input>[number]
export type AreaOptions = z.output<typeof options>
