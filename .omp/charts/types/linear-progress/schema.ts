import { z } from 'zod'
import { fields } from '../../core'
import { numDefault, objectList, optText } from '../_shared/KpiCharts'

const part = z.object({
  label: fields.label('what this part of the progress is'),
  value: fields.num('its amount'),
})

/** One bar per object; a single object is one bar. */
export const item = z.object({
  value: fields.numOrNull('progress so far; null (missing) draws a dashed empty track, distinct from 0. With `segments` as parts, defaults to their sum'),
  max: numDefault(100, 'the value that fills the track'),
  label: optText('what is measured'),
  segments: z.union([
    z.number({ invalid_type_error: 'number' }).int().min(1).max(100),
    z.array(part),
  ]).optional().describe('a step count (the track is divided into that many steps) or parts [{ label, value }] stacked in order'),
}).passthrough().superRefine((d, c) => {
  if (!(d.max > 0)) c.addIssue({ code: z.ZodIssueCode.custom, path: ['max'], message: `max must be greater than 0, got ${d.max}` })
})

export const input = objectList(item, '{ value, max?, label?, segments? } or a list of them')

export const options = z.object({
  showValues: z.enum(['percent', 'value', 'both']).default('percent').describe("what the number at the right end says: '68%', '34 / 50', or both"),
})

export type ProgressItem = z.output<typeof item>
export type ProgressPart = z.output<typeof part>
export type ProgressOptions = z.output<typeof options>
