// Multi-line chart: several series over the same x, one line each.

import { defineChart } from '../../core'
import { CURVES, describeTrend, drawLines, trendTable, yNoun } from '../_shared/TrendA'
import { z } from 'zod'
import { fields } from '../../core'
import { gapsOption, pointsOption, xField, xTypeOption } from '../_shared/TrendA'

/** Long rows: one row per (x, series). Duplicate (x, series) pairs are summed (and noted). */
export const input = fields.rows({
  x: xField('date (or number) on the x axis'),
  series: fields.label('which line the row belongs to'),
  y: fields.numOrNull('value; null lifts the pen (see gaps)'),
}, 'rows of { x, series, y }')

export const options = z.object({
  curve: z.enum(['linear', 'monotone']).default('linear').describe("'monotone' smooths without overshooting"),
  endLabels: z.boolean().default(true).describe('name each line at its right end when the names fit (the legend stays)'),
  gaps: gapsOption('gap'),
  xType: xTypeOption,
  points: pointsOption,
  xTitle: z.string().optional(),
  yTitle: z.string().optional(),
})

export type MultiLineRow = z.output<typeof input>[number]
export type MultiLineOptions = z.output<typeof options>

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
