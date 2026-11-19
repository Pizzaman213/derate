import { z } from 'zod'
import { separatorOption, treeInput, type TreeDatum } from '../_shared/FlowB'

/** People and who they report to: rows of { id, parent, name, title? } (or a nested tree). */
export const input = treeInput

export const options = z.object({
  colorBy: z.enum(['single', 'branch']).default('branch').describe("one colour, or one per top-level report's organisation"),
  separator: separatorOption,
})

export type OrgChartInput = TreeDatum | null
export type OrgChartOptions = z.output<typeof options>
