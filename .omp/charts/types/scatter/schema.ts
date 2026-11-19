import { z } from 'zod'
import { fields } from '../../core'

/** One point per row; rows missing x or y are skipped (and noted). */
export const input = fields.rows({
  x: fields.numOrNull('horizontal position'),
  y: fields.numOrNull('vertical position'),
  series: fields.optLabel('group: one colour and legend entry per series'),
  label: fields.optLabel('point name, shown in the tooltip and as a direct label when there is room'),
}, 'rows of { x, y, series?, label? }')

export const options = z.object({
  xScale: z.enum(['linear', 'log']).default('linear').describe('x axis scale; log drops values <= 0 (noted)'),
  yScale: z.enum(['linear', 'log']).default('linear').describe('y axis scale; log drops values <= 0 (noted)'),
  pointRadius: z.number().positive().max(20).optional().describe('px; default shrinks as the point count grows'),
  labels: z.enum(['auto', 'none']).default('auto').describe('auto: direct labels from the label field when there are at most 60 labelled points; labels that would collide are left to the tooltip'),
  xTitle: z.string().optional(),
  yTitle: z.string().optional(),
})

export type ScatterRow = z.output<typeof input>[number]
export type ScatterOptions = z.output<typeof options>
