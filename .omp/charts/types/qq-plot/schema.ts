import { z } from 'zod'
import { fields } from '../../core'

/** One observation per row, compared against the normal distribution. */
export const input = fields.rows({
  value: fields.numOrNull('the observation; null rows are skipped (noted)'),
  label: fields.optLabel('optional name of the observation, shown in its tooltip'),
}, 'rows of { value, label? }')

export const options = z.object({
  standardize: z.boolean().default(false).describe('plot z-scores ((value - mean) / sd) instead of raw values'),
  showLine: z.boolean().default(true).describe('reference line through the sample and normal quartiles'),
  xTitle: z.string().default('Normal quantile').describe('title of the theoretical (x) axis'),
  yTitle: z.string().optional().describe('title of the sample (y) axis; default "Sample quantile" ("Sample z-score" when standardized)'),
})

export type QqRow = z.output<typeof input>[number]
export type QqOptions = z.output<typeof options>
