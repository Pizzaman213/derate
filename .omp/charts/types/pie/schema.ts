import { z } from 'zod'
import { fields } from '../../core'

/** One slice per row. Duplicate labels are summed; nulls, zeros and negatives are left out (noted). */
export const input = fields.rows({
  label: fields.label('slice name'),
  value: fields.numOrNull('slice size; must be positive to be drawn'),
}, 'rows of { label, value }')

export const options = z.object({
  maxSlices: z.number().int().min(2).max(24).default(6).describe('above this many slices the smallest are grouped as "Other"'),
  sort: z.enum(['none', 'descending', 'ascending']).default('descending').describe('slice order, clockwise from 12 o\'clock ("Other" stays last)'),
  labels: z.enum(['auto', 'outside', 'inside', 'none']).default('auto').describe('auto: names outside with leader lines when there is room, else shares inside the slices'),
})

export type PieRow = z.output<typeof input>[number]
export type PieOptions = z.output<typeof options>
