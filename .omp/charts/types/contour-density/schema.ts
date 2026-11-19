import { z } from 'zod'
import { fields } from '../../core'

/** One point per row; the chart estimates their density and draws its contours. */
export const input = fields.rows({
  x: fields.numOrNull('horizontal position'),
  y: fields.numOrNull('vertical position'),
}, 'rows of { x, y }')

export const options = z.object({
  bandwidth: z.number().min(2).max(80).default(20).describe('kernel bandwidth in px: larger is smoother'),
  thresholds: z.number().int().min(2).max(40).default(10).describe('approximate number of contour levels (d3 picks round density values)'),
  showPoints: z.enum(['auto', 'always', 'never']).default('auto').describe('the raw points over the contours; auto: up to 2,000 points'),
  xTitle: z.string().optional(),
  yTitle: z.string().optional(),
})

export type ContourRow = z.output<typeof input>[number]
export type ContourOptions = z.output<typeof options>
