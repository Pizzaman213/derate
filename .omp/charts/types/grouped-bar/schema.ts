import { z } from 'zod'
import { orientation, seriesInput } from '../_shared/ComparisonA'

/** Long rows: one bar per (label, series). Duplicate pairs are summed (and noted). */
export const input = seriesInput

export const options = z.object({
  orientation,
  showValues: z.boolean().default(false).describe('print each value at the end of its bar (dropped when bars are too thin)'),
  groupPadding: z.number().min(0).max(0.8).default(0.2).describe('gap between category groups, as a share of the step'),
  xTitle: z.string().optional(),
  yTitle: z.string().optional(),
})

export type GroupedRow = z.output<typeof input>[number]
export type GroupedOptions = z.output<typeof options>
