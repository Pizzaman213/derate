import { z } from 'zod'
import { separatorOption, treeInput, type TreeInput } from '../_shared/PartWholeB'

/**
 * A tree { name, value?, children? }. Rows are adapted into one: { path: 'a/b/c', value },
 * { id, parent, value } or { label, value, parent? } -- as a list, CSV text or column arrays.
 */
export const input = treeInput

export const options = z.object({
  separator: separatorOption,
  orientation: z.enum(['horizontal', 'vertical']).default('horizontal').describe('horizontal: levels run left to right; vertical: top to bottom'),
  showValues: z.boolean().default(true).describe('print the value under each name where it fits'),
})

export type IcicleData = TreeInput
export type IcicleOptions = z.output<typeof options>
