import { z } from 'zod'
import { fields } from '../../core'
import { bandwidthOption, focusOption } from '../_shared/DistributionA'

/** One observation per row; a plain list of numbers is accepted too (see adapt). */
export const input = fields.rows({
  value: fields.numOrNull('the observation; null is dropped (and noted)'),
  series: fields.optLabel('optional series: one curve each'),
}, 'rows of { value, series? }, or a list of numbers')

export const options = z.object({
  bandwidth: bandwidthOption,
  kernel: z.enum(['gaussian', 'epanechnikov']).default('gaussian').describe('smoothing kernel'),
  fill: z.boolean().default(true).describe('fill the area under each curve'),
  rug: z.boolean().default(false).describe('tick marks for the observations along the x axis (up to 2,000 values)'),
  xTitle: z.string().optional(),
  focus: focusOption,
  yTitle: z.string().optional(),
})

export type DensityRow = z.output<typeof input>[number]
export type DensityOptions = z.output<typeof options>
