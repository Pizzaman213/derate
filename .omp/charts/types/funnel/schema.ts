import { z } from 'zod'
import { fields } from '../../core'

/** One stage per row, in funnel order. Duplicate labels are summed; nulls and negatives are left out (noted). */
export const input = fields.rows({
  label: fields.label('stage name'),
  value: fields.numOrNull('how many reached this stage'),
}, 'rows of { label, value } in stage order')

export const options = z.object({
  showConversion: z.enum(['both', 'first', 'previous', 'none']).default('both').describe('conversion shown beside each stage: from the first stage, from the previous one, both or none'),
})

export type FunnelRow = z.output<typeof input>[number]
export type FunnelOptions = z.output<typeof options>
