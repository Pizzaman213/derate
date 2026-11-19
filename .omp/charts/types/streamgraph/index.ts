// Streamgraph: Series flowing around a centre line over time; shape shows rise and fall, not exact values.

import { defineChart } from '../../core'
import { describeStack, drawStack, stackIsEmpty, stackTable } from '../_shared/TrendA'
import { input, options, type Options, type Row } from './schema'

export const streamgraph = defineChart<Row[], Options>({
  id: 'streamgraph',
  name: 'Streamgraph',
  category: 'trend',
  intents: ['trend', 'change-over-time', 'composition'],
  summary: "Series flowing around a centre line over time; shape shows rise and fall, not exact values.",
  input,
  options,
  shape: 'rows',
  fields: ['x', 'series', 'y'],
  dateFields: ['x'],
  numberFields: ['y'],
  zoom: 'x',
  isEmpty: (data) => stackIsEmpty(data),
  draw: (ctx) => drawStack(ctx, 'wiggle'),
  describe: ({ data, options: o, fmt, name }) => describeStack(name, data, o, fmt, 'wiggle'),
  table: (data, o, fmt) => stackTable(data, o, fmt),
})

export default streamgraph
