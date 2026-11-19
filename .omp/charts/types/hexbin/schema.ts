import { z } from 'zod'
import { fields } from '../../core'

/** One point per row; points are counted into hexagonal bins. */
export const input = fields.rows({
  x: fields.numOrNull('horizontal position'),
  y: fields.numOrNull('vertical position'),
}, 'rows of { x, y }')

export const options = z.object({
  radius: z.number().min(3).max(60).optional().describe('hexagon radius in px; default scales with the plot (about 1/40 of its width, 5-16px)'),
  colorScale: z.enum(['linear', 'log']).default('linear').describe('count -> colour; log keeps sparse bins distinguishable when a few are very dense'),
  xTitle: z.string().optional(),
  yTitle: z.string().optional(),
})

export type HexbinRow = z.output<typeof input>[number]
export type HexbinOptions = z.output<typeof options>
