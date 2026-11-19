// Stacked area chart: Series stacked on each other over time, so the top edge is the total.

import { defineChart } from '../../core'
import { describeStack, drawStack, stackIsEmpty, stackTable } from '../_shared/TrendA'
import { input, options, type Options, type Row } from './schema'

export const stackedArea = defineChart<Row[], Options>({
  id: 'stacked-area',
  name: 'Stacked area chart',
  category: 'trend',
  intents: ['trend', 'change-over-time', 'composition'],
  summary: "Series stacked on each other over time, so the top edge is the total.",
  input,
  options,
  shape: 'rows',
  fields: ['x', 'series', 'y'],
  dateFields: ['x'],
  numberFields: ['y'],
  zoom: 'x',
  isEmpty: (data) => stackIsEmpty(data),
  draw: (ctx) => drawStack(ctx, 'none'),
  describe: ({ data, options: o, fmt, name }) => describeStack(name, data, o, fmt, 'none'),
  table: (data, o, fmt) => stackTable(data, o, fmt),
})

export default stackedArea
