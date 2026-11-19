import { z } from 'zod'
import { fields } from '../../core'

/** One row per combination of stage categories (or per respondent), with an optional weight. */
export const input = fields.rows({
  value: fields.numOrNull('weight of the row; without a value column every row counts 1'),
}, 'rows of { <stage 1>, <stage 2>, ..., value }')

export const options = z.object({
  columns: z.array(z.string()).optional().describe('the stage columns, left to right (default: every column except value, in order)'),
  maxCategories: z.number().int().min(2).default(12).describe('categories kept per stage; the smallest rest are grouped as "Other"'),
  nodeSort: z.enum(['input', 'size', 'name']).default('input').describe('order of the categories within a stage'),
  showValues: z.boolean().default(false).describe('print each category total after its label'),
})

export type AlluvialRow = z.output<typeof input>[number]
export type AlluvialOptions = z.output<typeof options>
