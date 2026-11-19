import { z } from 'zod'
import { input, options as pieOptions } from '../pie/schema'

export { input }

export const options = pieOptions.extend({
  innerRadius: z.number().min(0.3).max(0.85).default(0.6).describe('hole size as a share of the outer radius'),
  centerLabel: z.string().default('Total').describe('caption under the centre total; empty string hides the total'),
})

export type DonutRow = z.output<typeof input>[number]
export type DonutOptions = z.output<typeof options>
