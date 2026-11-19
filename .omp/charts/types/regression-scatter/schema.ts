import { z } from 'zod'
import { fields } from '../../core'

/** One point per row; rows missing x or y are skipped (and noted). */
export const input = fields.rows({
  x: fields.numOrNull('predictor, on the horizontal axis'),
  y: fields.numOrNull('response, on the vertical axis'),
  label: fields.optLabel('point name, shown in the tooltip'),
}, 'rows of { x, y, label? }')

export const options = z.object({
  degree: z.union([z.literal(1), z.literal(2), z.literal(3)]).default(1).describe('1: straight line (least squares); 2 or 3: quadratic or cubic polynomial'),
  showEquation: z.boolean().default(true).describe('the fitted equation and R² in the emptiest corner of the plot'),
  band: z.boolean().default(true).describe('95% confidence band around the fitted mean'),
  xTitle: z.string().optional(),
  yTitle: z.string().optional(),
})

export type RegressionRow = z.output<typeof input>[number]
export type RegressionOptions = z.output<typeof options>
