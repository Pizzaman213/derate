import { z } from 'zod'
import { fields } from '../../core'

/** One bubble per row; the bubble's area is proportional to `size`. */
export const input = fields.rows({
  x: fields.numOrNull('horizontal position'),
  y: fields.numOrNull('vertical position'),
  size: fields.numOrNull('bubble area (proportional); null or negative omits the bubble, 0 draws an empty ring'),
  series: fields.optLabel('group: one colour and legend entry per series'),
  label: fields.optLabel('bubble name: tooltip, and a direct label when there is room'),
}, 'rows of { x, y, size, series?, label? }')

export const options = z.object({
  maxRadius: z.number().positive().max(80).default(28).describe('px radius of the largest bubble (shrunk on small charts)'),
  xScale: z.enum(['linear', 'log']).default('linear').describe('x axis scale; log drops values <= 0 (noted)'),
  yScale: z.enum(['linear', 'log']).default('linear').describe('y axis scale; log drops values <= 0 (noted)'),
  sizeLegend: z.boolean().default(true).describe('nested-circle key for the size encoding, top right'),
  labels: z.enum(['auto', 'none']).default('auto').describe('auto: label from the label field inside bubbles that fit it, otherwise beside them when there is room'),
  xTitle: z.string().optional(),
  yTitle: z.string().optional(),
  sizeTitle: z.string().optional().describe('what size measures; titles the size key and tooltips'),
})

export type BubbleRow = z.output<typeof input>[number]
export type BubbleOptions = z.output<typeof options>
