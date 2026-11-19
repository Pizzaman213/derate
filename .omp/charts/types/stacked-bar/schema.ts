import { z } from 'zod'
import { orientation, seriesInput } from '../_shared/ComparisonA'

/** Long rows: one segment per (label, series). Negatives stack below zero; duplicates are summed. */
export const input = seriesInput

export const options = z.object({
  orientation,
  showTotals: z.boolean().default(false).describe('print each bar\'s net total past the end of its stack'),
  xTitle: z.string().optional(),
  yTitle: z.string().optional(),
})

export type StackedRow = z.output<typeof input>[number]
export type StackedOptions = z.output<typeof options>
