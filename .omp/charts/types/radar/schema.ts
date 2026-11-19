import { z } from 'zod'
import { fields } from '../../core'

/** One row per (axis, series) value. Wide rows ({ series, <axis1>, <axis2>... }) are unpivoted into this by `adapt`. */
export const input = fields.rows({
  axis: fields.label('the spoke this value sits on'),
  series: fields.optLabel('the polygon it belongs to (default: one series)'),
  value: fields.numOrNull('distance from the centre; null leaves the vertex out'),
}, 'rows of { axis, series, value }, or wide rows of { series, <axis>: number ... }')

export const options = z.object({
  max: z.number().finite().optional().describe('value at the outer ring (default: the data maximum, rounded up to a nice number)'),
  levels: z.number().int().min(1).max(10).default(4).describe('number of concentric grid rings'),
  grid: z.enum(['polygon', 'circle']).default('polygon').describe('grid ring shape'),
  fill: z.boolean().default(true).describe('fill each polygon (lightly) as well as outlining it'),
})

export type RadarRow = z.output<typeof input>[number]
export type RadarOptions = z.output<typeof options>
