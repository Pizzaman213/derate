import { z } from 'zod'
import { fields } from '../../core'

/** One row per (x, series) point. */
export const input = fields.rows({
  x: fields.date('time of the reading'),
  y: fields.numOrNull('value; positives and negatives fold into separate colours; null leaves a gap'),
  series: fields.optLabel('one horizon row per series'),
}, 'rows of { x, y, series? }')

export const options = z.object({
  bands: z.number().int().min(1).max(6).default(3).describe('how many times the value range folds; more bands = flatter rows'),
  mode: z.enum(['offset', 'mirror']).default('offset').describe('negatives hang from the top (offset) or are mirrored up from the baseline'),
  max: z.number().positive().optional().describe('the value one full fold stack represents (default: the largest |y|); share it across charts to compare'),
})

export type HorizonRow = z.output<typeof input>[number]
export type HorizonOptions = z.output<typeof options>
