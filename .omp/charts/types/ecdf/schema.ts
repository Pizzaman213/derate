import { z } from 'zod'
import { fields } from '../../core'

/** One observation per row; one cumulative curve per series. */
export const input = fields.rows({
  value: fields.numOrNull('the observation; null rows are skipped (noted)'),
  series: fields.optLabel('optional series: one curve per series'),
}, 'rows of { value, series? }')

export const options = z.object({
  showPoints: z.enum(['auto', 'always', 'never']).default('auto').describe('dots at each step; auto shows them up to 60 steps per curve (steps stay hoverable either way)'),
  scale: z.enum(['linear', 'log']).default('linear').describe('value scale; log skips values <= 0 (noted)'),
  valueTitle: z.string().optional().describe('title of the value (x) axis'),
  yTitle: z.string().default('Cumulative share').describe('title of the cumulative (y) axis'),
})

export type EcdfRow = z.output<typeof input>[number]
export type EcdfOptions = z.output<typeof options>
