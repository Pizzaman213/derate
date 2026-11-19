// Donut chart: a pie with a hole, and the total in the hole.

import { defineChart } from '../../core'
import { describeParts, drawRadial, emptyMessage, partsTable } from '../_shared/PartWholeA'
import { z } from 'zod'
import { input, options as pieOptions } from '../pie/index'

export { input }

export const options = pieOptions.extend({
  innerRadius: z.number().min(0.3).max(0.85).default(0.6).describe('hole size as a share of the outer radius'),
  centerLabel: z.string().default('Total').describe('caption under the centre total; empty string hides the total'),
})

export type DonutRow = z.output<typeof input>[number]
export type DonutOptions = z.output<typeof options>

export const donut = defineChart<DonutRow[], DonutOptions>({
  id: 'donut',
  name: 'Donut chart',
  category: 'part-to-whole',
  intents: ['composition'],
  summary: 'A ring of slices around the total: parts of one whole.',
  input,
  options,
  shape: 'rows',
  fields: ['label', 'value'],
  numberFields: ['value'],
  aspect: 0.75,
  minHeight: 220,
  isEmpty: (data) => emptyMessage(data),
  draw: (ctx) => drawRadial(ctx, { inner: ctx.options.innerRadius, half: false, labels: ctx.options.labels, centerLabel: ctx.options.centerLabel }),
  describe: ({ data, options: o, fmt, name }) => describeParts(name, data, fmt, { maxSlices: o.maxSlices, noun: 'slice' }),
  table: (data) => partsTable(data),
})

export default donut
