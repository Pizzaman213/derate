import { z } from 'zod'
import { separatorOption, treeInput, type TreeDatum } from '../_shared/FlowB'

/** A tree: nested object, id/parent rows or path rows (see the README). */
export const input = treeInput

export const options = z.object({
  maxDepth: z.number().int().min(1).optional().describe('collapse everything below this depth (root = 0); collapsed nodes show how many they hide'),
  colorBy: z.enum(['single', 'branch']).default('branch').describe('one colour, or one per top-level branch'),
  separator: separatorOption,
})

export type RadialTreeInput = TreeDatum | null
export type RadialTreeOptions = z.output<typeof options>
