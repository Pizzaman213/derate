import { z } from 'zod'
import { separatorOption, treeInput, type TreeDatum } from '../_shared/FlowB'

/** A tree whose leaves carry `value` (circle area); inner values are the sums of their leaves. */
export const input = treeInput

export const options = z.object({
  colorBy: z.enum(['branch', 'depth']).default('branch').describe('one hue per top-level group, or a sequential ramp by depth'),
  showValues: z.boolean().default(true).describe('print the value under the name in circles big enough'),
  maxDepth: z.number().int().min(1).optional().describe('collapse everything below this depth (root = 0); a collapsed circle keeps its total'),
  separator: separatorOption,
})

export type CirclePackingInput = TreeDatum | null
export type CirclePackingOptions = z.output<typeof options>
