import { z } from 'zod'
import { separatorOption, treeInput, type TreeInput } from '../_shared/PartWholeB'

/**
 * A tree { name, value?, children? }. Rows are adapted into one: { path: 'a/b/c', value },
 * { id, parent, value } or { label, value, parent? } -- as a list, CSV text or column arrays.
 */
export const input = treeInput

export const options = z.object({
  separator: separatorOption,
  labels: z.boolean().default(true).describe('name the arcs that have room for it'),
})

export type SunburstData = TreeInput
export type SunburstOptions = z.output<typeof options>
