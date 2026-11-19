import { z } from 'zod'
import { fields } from '../../core'
import { objectList, optText } from '../_shared/KpiCharts'

const yOrNull = z.preprocess((v) => (v === undefined || v === '' ? null : v), z.number({ invalid_type_error: 'number' }).finite().nullable())
const point = z.object({ x: z.union([z.number(), z.string(), z.date()]).describe('position: number, date or ISO string'), y: yOrNull })

/** One tile per object; a single object is one tile. */
export const item = z.object({
  label: fields.label('what the number is'),
  value: fields.numOrNull('the current value; null shows a dash'),
  previous: fields.numOrNull('the comparison value; the tile shows the change from it'),
  unit: optText('unit printed after the value, e.g. "ms" or "%"'),
  format: optText("this tile's number format: auto | si | currency | percent | integer | number | a d3-format specifier"),
  series: z.union([z.array(yOrNull), z.array(point)]).optional().describe('history for the sparkline: numbers, or { x, y } points (sorted by x)'),
  goodDirection: z.enum(['up', 'down']).default('up').describe("which way is good: 'down' for latency, cost, errors"),
}).passthrough()

export const input = objectList(item, '{ label, value, previous?, unit?, format?, series?, goodDirection? } or a list of them')

export const options = z.object({
  compareLabel: z.string().default('vs previous').describe('the words after the change, e.g. "vs last week"'),
})

export type TileItem = z.output<typeof item>
export type TileOptions = z.output<typeof options>
