import { z } from 'zod'
import { fields } from '../../core'
import { gapsOption, pointsOption, xField, xTypeOption } from '../_shared/TrendA'

/** Long rows: one row per (x, series). Missing pairs count as zero; duplicates are summed (noted). */
export const input = fields.rows({
  x: xField('date (or number) on the x axis'),
  series: fields.label('which layer the row belongs to'),
  y: fields.numOrNull('non-negative value; null follows gaps, negatives are drawn as zero (noted)'),
}, 'rows of { x, series, y }')

export const options = z.object({
  curve: z.enum(['linear', 'monotone']).default('monotone').describe("'monotone' smooths without overshooting"),
  gaps: gapsOption('zero'),
  xType: xTypeOption,
  points: pointsOption.default('never'),
  xTitle: z.string().optional(),
  yTitle: z.string().optional(),
})

export type Row = z.output<typeof input>[number]
export type Options = z.output<typeof options>
