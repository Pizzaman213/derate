// Semi-donut: a half ring (180°, flat side down) with the total in the hollow.

import { defineChart } from '../../core'
import { describeParts, drawRadial, emptyMessage, partsTable } from '../_shared/PartWholeA'
import { input, options, type SemiDonutOptions, type SemiDonutRow } from './schema'

export const semiDonut = defineChart<SemiDonutRow[], SemiDonutOptions>({
  id: 'semi-donut',
  name: 'Semi-donut chart',
  category: 'part-to-whole',
  intents: ['composition'],
  summary: 'A half ring of parts with the total underneath: compact composition.',
  input,
  options,
  shape: 'rows',
  fields: ['label', 'value'],
  numberFields: ['value'],
  aspect: 0.6,
  minHeight: 180,
  isEmpty: (data) => emptyMessage(data),
  draw: (ctx) => drawRadial(ctx, { inner: ctx.options.innerRadius, half: true, labels: ctx.options.labels, centerLabel: ctx.options.centerLabel }),
  describe: ({ data, options: o, fmt, name }) => describeParts(name, data, fmt, { maxSlices: o.maxSlices, noun: 'part' }),
  table: (data) => partsTable(data),
})

export default semiDonut
