import { z } from 'zod'
import { fields } from '../../core'

/** A square matrix of flows: matrix[i][j] = flow from names[i] to names[j]. */
export const input = z.object({
  names: z.array(fields.label('group name')).describe('one name per row/column'),
  matrix: z.array(z.array(fields.numOrNull('flow from the row group to the column group'))).describe('n x n flows, row = from, column = to'),
}).superRefine((d, ctx) => {
  if (d.matrix.length !== d.names.length)
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ['matrix'], message: `has ${d.matrix.length} rows but there are ${d.names.length} names; it must be square, one row and one column per name` })
  d.matrix.forEach((row, i) => {
    if (row.length !== d.names.length)
      ctx.addIssue({ code: z.ZodIssueCode.custom, path: ['matrix', i], message: `has ${row.length} values but there are ${d.names.length} names` })
  })
}).describe('{ names: string[], matrix: number[][] }')

export const options = z.object({
  sort: z.enum(['input', 'descending', 'ascending']).default('input').describe('order of the groups around the circle'),
  padAngle: z.number().min(0).max(0.2).optional().describe('gap between groups in radians (default: shrinks with the number of groups)'),
})

export type ChordInput = z.output<typeof input>
export type ChordOptions = z.output<typeof options>
