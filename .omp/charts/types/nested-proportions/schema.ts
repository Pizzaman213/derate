import { z } from 'zod'
import { separatorOption, treeInput, type TreeInput } from '../_shared/PartWholeB'

/**
 * A tree { name, value?, children? } where each child is a part of its parent. Rows are
 * adapted into one: { label, value, parent? } (no parent column: each row is a part of the
 * row before it), { id, parent, value } or { path: 'a/b', value }.
 */
export const input = treeInput

export const options = z.object({
  separator: separatorOption,
  maxParts: z.number().int().min(2).max(24).default(8).describe('parts shown per whole; smaller ones are grouped as "Other"'),
})

export type NestedData = TreeInput
export type NestedOptions = z.output<typeof options>
