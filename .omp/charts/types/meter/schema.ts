import { z } from 'zod'
import { fields } from '../../core'
import { numDefault, objectList, optText } from '../_shared/KpiCharts'

/** One meter per object, with HTML <meter> semantics (and its defaults: min 0, max 1). */
export const item = z.object({
  value: fields.numOrNull('the measurement; null draws a dashed empty meter'),
  min: numDefault(0, 'lower bound (default 0, as in HTML)'),
  max: numDefault(1, 'upper bound (default 1, as in HTML)'),
  low: fields.numOrNull('top of the low zone (default min)'),
  high: fields.numOrNull('bottom of the high zone (default max)'),
  optimum: fields.numOrNull('the best value; the zone it falls in is the good one (default the midpoint)'),
  label: optText('what is measured'),
  unit: optText('unit printed after the value'),
}).passthrough().superRefine((d, c) => {
  if (!(d.max > d.min)) c.addIssue({ code: z.ZodIssueCode.custom, path: ['max'], message: `max (${d.max}) must be greater than min (${d.min})` })
})

export const input = objectList(item, '{ value, min?, max?, low?, high?, optimum?, label? } or a list of them')

export const options = z.object({
  showZone: z.boolean().default(true).describe('print the reading\'s zone ("optimum", "suboptimum", "poor") after the value'),
})

export type MeterItem = z.output<typeof item>
export type MeterOptions = z.output<typeof options>
