import { z } from 'zod'
import { fields } from '../../core'

/** One circle per row, dodged along the value axis so none overlap. */
export const input = fields.rows({
  value: fields.numOrNull('position on the value axis; null rows are skipped (noted)'),
  group: fields.optLabel('optional group: one swarm per group'),
  label: fields.optLabel('optional name of the point, shown in its tooltip'),
}, 'rows of { value, group?, label? }')

export const options = z.object({
  orientation: z.enum(['horizontal', 'vertical']).default('horizontal').describe('horizontal: values along x, groups stacked; vertical: values up y, groups side by side'),
  radius: z.number().positive().max(40).optional().describe('circle radius in px; default fits the swarm into its band'),
  scale: z.enum(['linear', 'log']).default('linear').describe('value scale; log skips values <= 0 (noted)'),
  valueTitle: z.string().optional().describe('title of the value axis'),
  groupTitle: z.string().optional().describe('title of the group axis'),
})

export type BeeswarmRow = z.output<typeof input>[number]
export type BeeswarmOptions = z.output<typeof options>
