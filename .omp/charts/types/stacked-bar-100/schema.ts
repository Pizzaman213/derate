import { z } from 'zod'
import { orientation, seriesInput } from '../_shared/ComparisonA'

/** Long rows: one segment per (label, series); each bar is that category's mix, summing to 100%. */
export const input = seriesInput

export const options = z.object({
  orientation,
  showValues: z.boolean().default(false).describe('print each share inside its segment when it fits'),
  xTitle: z.string().optional(),
  yTitle: z.string().optional(),
})

export type Stacked100Row = z.output<typeof input>[number]
export type Stacked100Options = z.output<typeof options>
