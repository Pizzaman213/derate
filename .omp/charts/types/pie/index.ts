// Pie chart: slices of one whole, largest first from 12 o'clock.

import { defineChart } from '../../core'
import { describeParts, drawRadial, emptyMessage, partsTable } from '../_shared/PartWholeA'
import { z } from 'zod'
import { fields } from '../../core'

/** One slice per row. Duplicate labels are summed; nulls, zeros and negatives are left out (noted). */
export const input = fields.rows({
  label: fields.label('slice name'),
  value: fields.numOrNull('slice size; must be positive to be drawn'),
}, 'rows of { label, value }')

export const options = z.object({
  maxSlices: z.number().int().min(2).max(24).default(6).describe('above this many slices the smallest are grouped as "Other"'),
  sort: z.enum(['none', 'descending', 'ascending']).default('descending').describe('slice order, clockwise from 12 o\'clock ("Other" stays last)'),
  labels: z.enum(['auto', 'outside', 'inside', 'none']).default('auto').describe('auto: names outside with leader lines when there is room, else shares inside the slices'),
})

export type PieRow = z.output<typeof input>[number]
export type PieOptions = z.output<typeof options>

export const pie = defineChart<PieRow[], PieOptions>({
  id: 'pie',
  name: 'Pie chart',
  category: 'part-to-whole',
  intents: ['composition'],
  summary: 'Slices showing how a handful of parts make up one whole.',
  input,
  options,
  shape: 'rows',
  fields: ['label', 'value'],
  numberFields: ['value'],
  aspect: 0.75,
  minHeight: 220,
  isEmpty: (data) => emptyMessage(data),
  draw: (ctx) => drawRadial(ctx, { inner: 0, half: false, labels: ctx.options.labels }),
  describe: ({ data, options: o, fmt, name }) => describeParts(name, data, fmt, { maxSlices: o.maxSlices, noun: 'slice' }),
  table: (data) => partsTable(data),
})

export default pie
