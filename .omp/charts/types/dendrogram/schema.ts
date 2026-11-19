import { z } from 'zod'
import { separatorOption, treeInput, type TreeDatum } from '../_shared/FlowB'

/** A tree; inner nodes may carry `height` (the merge distance of a hierarchical clustering). */
export const input = treeInput

export const options = z.object({
  useHeight: z.boolean().default(true).describe("place inner nodes by their 'height' (merge distance) when the data has it; false: by depth"),
  distanceTitle: z.string().optional().describe('title under the distance axis'),
  maxDepth: z.number().int().min(1).optional().describe('collapse everything below this depth (root = 0)'),
  colorBy: z.enum(['single', 'branch']).default('single').describe('one colour, or one per top-level cluster'),
  separator: separatorOption,
})

export type DendrogramInput = TreeDatum | null
export type DendrogramOptions = z.output<typeof options>
