import { z } from 'zod'
import { fields } from '../../core'
import { numDefault, objectList, optText } from '../_shared/KpiCharts'

const band = z.object({
  to: fields.num('upper end of the band (bands run from the previous band, or min)'),
  status: z.enum(['good', 'warning', 'bad']).describe('colour: theme positive / warning / negative'),
})

/** One gauge per object; a single object is one gauge. */
export const item = z.object({
  value: fields.numOrNull('the reading; null draws an empty dial'),
  min: numDefault(0, 'start of the scale'),
  max: numDefault(100, 'end of the scale'),
  target: fields.numOrNull('optional target tick'),
  bands: z.array(band).optional().describe('coloured ranges, e.g. [{ to: 60, status: "good" }, { to: 85, status: "warning" }, { to: 100, status: "bad" }]'),
  label: optText('what is measured'),
  unit: optText('unit printed after the value, e.g. "%" or "°C"'),
}).passthrough().superRefine((d, c) => {
  if (!(d.max > d.min)) c.addIssue({ code: z.ZodIssueCode.custom, path: ['max'], message: `max (${d.max}) must be greater than min (${d.min})` })
})

export const input = objectList(item, '{ value, min?, max?, target?, bands?, label?, unit? } or a list of them')

export const options = z.object({
  display: z.enum(['fill', 'needle']).default('fill').describe("'fill': the arc fills to the value; 'needle': a needle over the full banded arc"),
})

export type GaugeItem = z.output<typeof item>
export type GaugeBand = z.output<typeof band>
export type GaugeOptions = z.output<typeof options>
