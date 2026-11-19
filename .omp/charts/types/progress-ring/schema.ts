import { z } from 'zod'
import { fields } from '../../core'
import { numDefault, objectList, optText } from '../_shared/KpiCharts'

/** One ring per object; a single object is one ring. */
export const item = z.object({
  value: fields.numOrNull('progress so far; null draws a dashed empty ring'),
  max: numDefault(100, 'the value that completes the ring'),
  label: optText('what is measured'),
}).passthrough().superRefine((d, c) => {
  if (!(d.max > 0)) c.addIssue({ code: z.ZodIssueCode.custom, path: ['max'], message: `max must be greater than 0, got ${d.max}` })
})

export const input = objectList(item, '{ value, max?, label? } or a list of them')

export const options = z.object({
  layout: z.enum(['side-by-side', 'concentric']).default('side-by-side').describe("'side-by-side': one ring each with its % in the middle; 'concentric': nested rings with a labelled key beside them"),
})

export type RingItem = z.output<typeof item>
export type RingOptions = z.output<typeof options>
