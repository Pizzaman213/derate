// Step line: values that hold until they change (prices, rates, plan tiers, stock levels).

import { defineChart } from '../../core'
import { CURVES, describeTrend, drawLines, trendTable, yNoun } from '../_shared/TrendA'
import { input, options, type StepLineOptions, type StepLineRow } from './schema'

export const stepLine = defineChart<StepLineRow[], StepLineOptions>({
  id: 'step-line',
  name: 'Step line chart',
  category: 'trend',
  intents: ['trend', 'change-over-time'],
  summary: 'Values that hold until they change, drawn as horizontal steps.',
  input,
  options,
  shape: 'rows',
  fields: ['x', 'y', 'series'],
  dateFields: ['x'],
  numberFields: ['y'],
  zoom: 'x',
  hover: 'nearest-x',
  isEmpty: (data) => data.length === 0,
  draw: (ctx) => drawLines(ctx, { curve: CURVES[`step-${ctx.options.step}`], area: false, endLabels: true }),
  describe: ({ data, options: o, fmt, name }) => describeTrend({ name, rows: data, getX: (r) => r.x, getY: (r) => r.y, getSeries: (r) => r.series ?? '', xType: o.xType, fmt, what: yNoun(o) }),
  table: (data, o, fmt) => trendTable(data, o.xType, fmt, data.some((r) => r.series !== undefined)),
})

export default stepLine
