import { z } from 'zod'
import { fields } from '../../core'

/** One observation per row; a plain list of numbers is accepted too (see adapt). */
export const input = fields.rows({
  value: fields.numOrNull('the observation; null is dropped (and noted)'),
  series: fields.optLabel('optional series: overlaid or one panel each'),
}, 'rows of { value, series? }, or a list of numbers')

export const options = z.object({
  bins: z.object({
    count: z.number().int().positive().max(20000).optional().describe('target bin count (rounded to nice edges)'),
    thresholds: z.array(z.number().finite()).optional().describe('explicit inner bin edges'),
    method: z.enum(['sturges', 'freedman-diaconis', 'scott']).optional().describe('rule for the bin count (default sturges)'),
    domain: z.tuple([z.number().finite(), z.number().finite()]).optional().describe('fix the binned range; values outside are left out (and noted)'),
  }).default({}).describe('how values are binned'),
  layout: z.enum(['overlay', 'facet']).default('overlay').describe('several series: semi-transparent overlay, or one panel each with shared scales'),
  normalize: z.enum(['count', 'percent', 'density']).default('count').describe('bar height: count, share of the series, or density (area 1)'),
  xTitle: z.string().optional(),
  yTitle: z.string().optional(),
})

export type HistogramRow = z.output<typeof input>[number]
export type HistogramOptions = z.output<typeof options>
