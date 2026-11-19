import { z } from 'zod'
import { fields } from '../../core'

/** One step per row, connected in input order; a row missing x or y breaks the path. */
export const input = fields.rows({
  x: fields.numOrNull('horizontal position'),
  y: fields.numOrNull('vertical position'),
  label: fields.optLabel('step name, usually the period ("2019"): labels the ends and the tooltip'),
}, 'rows of { x, y, label? }, in order')

export const options = z.object({
  curve: z.enum(['linear', 'smooth']).default('linear').describe('straight segments, or a Catmull-Rom curve through every point'),
  arrows: z.boolean().default(true).describe('direction arrows on the segments that are long enough to carry one'),
  labels: z.enum(['ends', 'auto', 'none']).default('auto').describe('ends: first and last step; auto: the ends, then every other step that has room'),
  xTitle: z.string().optional(),
  yTitle: z.string().optional(),
})

export type ConnectedRow = z.output<typeof input>[number]
export type ConnectedOptions = z.output<typeof options>
