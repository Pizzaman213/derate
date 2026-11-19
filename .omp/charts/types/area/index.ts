// Area chart: one value over time, filled to the zero line to stress volume.

import { defineChart } from '../../core'
import { CURVES, describeTrend, drawLines, trendTable, yNoun } from '../_shared/TrendA'
import { input, options, type AreaOptions, type AreaRow } from './schema'

export const area = defineChart<AreaRow[], AreaOptions>({
  id: 'area',
  name: 'Area chart',
  category: 'trend',
  intents: ['trend', 'change-over-time'],
  summary: 'One value over time, filled down to zero to show volume.',
  input,
  options,
  shape: 'rows',
  fields: ['x', 'y'],
  dateFields: ['x'],
  numberFields: ['y'],
  zoom: 'x',
  hover: 'nearest-x',
  isEmpty: (data) => data.length === 0,
  draw: (ctx) => drawLines(ctx, { curve: CURVES[ctx.options.curve], area: true, endLabels: false }),
  describe: ({ data, options: o, fmt, name }) => describeTrend({ name, rows: data, getX: (r) => r.x, getY: (r) => r.y, xType: o.xType, fmt, what: yNoun(o) }),
  table: (data, o, fmt) => trendTable(data, o.xType, fmt, false),
})

export default area
