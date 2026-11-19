// Line chart: one value over continuous time (or any numeric x).

import { defineChart } from '../../core'
import { CURVES, describeTrend, drawLines, trendTable, yNoun } from '../_shared/TrendA'
import { input, options, type LineOptions, type LineRow } from './schema'

export const line = defineChart<LineRow[], LineOptions>({
  id: 'line',
  name: 'Line chart',
  category: 'trend',
  intents: ['trend', 'change-over-time'],
  summary: 'One value over time, as a line through its points.',
  input,
  options,
  shape: 'rows',
  fields: ['x', 'y'],
  dateFields: ['x'],
  numberFields: ['y'],
  zoom: 'x',
  hover: 'nearest-x',
  isEmpty: (data) => data.length === 0,
  draw: (ctx) => drawLines(ctx, { curve: CURVES[ctx.options.curve], area: false, endLabels: false }),
  describe: ({ data, options: o, fmt, name }) => describeTrend({ name, rows: data, getX: (r) => r.x, getY: (r) => r.y, xType: o.xType, fmt, what: yNoun(o) }),
  table: (data, o, fmt) => trendTable(data, o.xType, fmt, false),
})

export default line
