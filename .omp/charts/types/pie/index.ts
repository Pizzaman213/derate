// Pie chart: slices of one whole, largest first from 12 o'clock.

import { defineChart } from '../../core'
import { describeParts, drawRadial, emptyMessage, partsTable } from '../_shared/PartWholeA'
import { input, options, type PieOptions, type PieRow } from './schema'

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
