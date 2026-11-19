import { z } from 'zod'
import { separatorOption, treeInput, type TreeInput } from '../_shared/PartWholeB'

/**
 * A tree { name, value?, children? }. Rows are adapted into one: { path: 'a/b/c', value },
 * { id, parent, value } or { label, value, parent? } -- as a list, CSV text or column arrays.
 */
export const input = treeInput

export const options = z.object({
  separator: separatorOption,
  showValues: z.boolean().default(true).describe('print the value under each name where it fits'),
  groupLabels: z.boolean().default(true).describe('name each first-level group in its top-left corner (trees deeper than one level)'),
})

export type TreemapData = TreeInput
export type TreemapOptions = z.output<typeof options>
