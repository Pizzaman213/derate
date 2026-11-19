import { z } from 'zod'
import { fields } from '../../core'

/** One level per row, apex first. Duplicate labels are summed; nulls, zeros and negatives are left out (noted). */
export const input = fields.rows({
  label: fields.label('level name'),
  value: fields.numOrNull('level size; the band area is proportional to it'),
}, 'rows of { label, value }, apex first')

export const options = z.object({
  order: z.enum(['apex-first', 'base-first']).default('apex-first').describe('whether the first row is the top (apex) or the bottom (base) band'),
})

export type PyramidRow = z.output<typeof input>[number]
export type PyramidOptions = z.output<typeof options>
