import { z } from 'zod'
import { input, options as pieOptions } from '../pie/schema'

export { input }

export const options = pieOptions.extend({
  innerRadius: z.number().min(0.3).max(0.85).default(0.6).describe('hole size as a share of the outer radius'),
  centerLabel: z.string().default('Total').describe('caption above the centre total; empty string hides the total'),
})

export type SemiDonutRow = z.output<typeof input>[number]
export type SemiDonutOptions = z.output<typeof options>
