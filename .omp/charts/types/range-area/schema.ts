import { z } from 'zod'
import { fields } from '../../core'
import { gapsOption, pointsOption, xField, xTypeOption } from '../_shared/TrendA'

/** One row per x: the band's low and high, and an optional centre value (mean, median, forecast). */
export const input = fields.rows({
  x: xField('date (or number) on the x axis'),
  low: fields.numOrNull('bottom of the band; null leaves a gap'),
  high: fields.numOrNull('top of the band; must be >= low'),
  y: fields.numOrNull('optional centre line (mean, median, forecast)'),
}, 'rows of { x, low, high, y? }').superRefine((rows, c) => {
  rows.forEach((r, i) => {
    if (r.low !== null && r.high !== null && r.high < r.low) {
      c.addIssue({ code: z.ZodIssueCode.too_small, type: 'number', minimum: r.low, inclusive: true, path: [i, 'high'], message: `must be >= low (${r.low})` })
    }
  })
})

export const options = z.object({
  curve: z.enum(['linear', 'monotone']).default('linear').describe("'monotone' smooths without overshooting"),
  gaps: gapsOption('gap'),
  xType: xTypeOption,
  points: pointsOption,
  bandLabel: z.string().default('Range').describe("what the band is, for tooltips and the legend ('95% CI', 'Min–max')"),
  lineLabel: z.string().default('Value').describe('what the centre line is'),
  xTitle: z.string().optional(),
  yTitle: z.string().optional(),
})

export type RangeRow = z.output<typeof input>[number]
export type RangeOptions = z.output<typeof options>
