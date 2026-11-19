// Multi-line chart: several series over the same x, one line each.

import { defineChart } from '../../core'
import { CURVES, describeTrend, drawLines, trendTable, yNoun } from '../_shared/TrendA'
import { input, options, type MultiLineOptions, type MultiLineRow } from './schema'

export const multiLine = defineChart<MultiLineRow[], MultiLineOptions>({
  id: 'multi-line',
  name: 'Multi-line chart',
  category: 'trend',
  intents: ['trend', 'change-over-time', 'compare'],
  summary: 'Several series over time, one line each, named at their ends.',
  input,
  options,
  shape: 'rows',
  fields: ['x', 'series', 'y'],
  dateFields: ['x'],
  numberFields: ['y'],
  zoom: 'x',
  hover: 'nearest-x',
  isEmpty: (data) => data.length === 0,
  draw: (ctx) => drawLines(ctx, { curve: CURVES[ctx.options.curve], area: false, endLabels: ctx.options.endLabels }),
  describe: ({ data, options: o, fmt, name }) => {
    const n = new Set(data.map((r) => r.series)).size
    const noun = yNoun(o)
    return describeTrend({ name, rows: data, getX: (r) => r.x, getY: (r) => r.y, getSeries: (r) => r.series, xType: o.xType, fmt, what: noun ? `${noun}, ${n} series` : undefined })
  },
  table: (data, o, fmt) => trendTable(data, o.xType, fmt, true),
})

export default multiLine
