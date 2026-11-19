// 100% stacked area chart: Each series' share of the total over time; every x adds up to 100%.

import { defineChart } from '../../core'
import { describeStack, drawStack, stackIsEmpty, stackTable } from '../_shared/TrendA'
import { input, options, type Options, type Row } from './schema'

export const stackedArea100 = defineChart<Row[], Options>({
  id: 'stacked-area-100',
  name: '100% stacked area chart',
  category: 'trend',
  intents: ['trend', 'change-over-time', 'composition'],
  summary: "Each series' share of the total over time; every x adds up to 100%.",
  input,
  options,
  shape: 'rows',
  fields: ['x', 'series', 'y'],
  dateFields: ['x'],
  numberFields: ['y'],
  zoom: 'x',
  isEmpty: (data) => stackIsEmpty(data),
  draw: (ctx) => drawStack(ctx, 'expand'),
  describe: ({ data, options: o, fmt, name }) => describeStack(name, data, o, fmt, 'expand'),
  table: (data, o, fmt) => stackTable(data, o, fmt),
})

export default stackedArea100
