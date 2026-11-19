import { z } from 'zod'
import { fields } from '../../core'
import { gapsOption, pointsOption, xField, xTypeOption } from '../_shared/TrendA'

/** Long rows: one row per (x, series). Duplicate (x, series) pairs are summed (and noted). */
export const input = fields.rows({
  x: xField('date (or number) on the x axis'),
  series: fields.label('which line the row belongs to'),
  y: fields.numOrNull('value; null lifts the pen (see gaps)'),
}, 'rows of { x, series, y }')

export const options = z.object({
  curve: z.enum(['linear', 'monotone']).default('linear').describe("'monotone' smooths without overshooting"),
  endLabels: z.boolean().default(true).describe('name each line at its right end when the names fit (the legend stays)'),
  gaps: gapsOption('gap'),
  xType: xTypeOption,
  points: pointsOption,
  xTitle: z.string().optional(),
  yTitle: z.string().optional(),
})

export type MultiLineRow = z.output<typeof input>[number]
export type MultiLineOptions = z.output<typeof options>
