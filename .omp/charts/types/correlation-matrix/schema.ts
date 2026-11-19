import { z } from 'zod'

/**
 * Canonical form: a square matrix of coefficients in [-1, 1] with its variable names.
 * Wide rows (one numeric column per variable), CSV text and column arrays are turned
 * into this by `adapt`, which computes the coefficients with `options.method`.
 */
export const input = z.object({
  names: z.array(z.string({ invalid_type_error: 'text' })).describe('variable names, one per row/column of the matrix'),
  matrix: z.array(z.array(z.number({ invalid_type_error: 'number' }).min(-1).max(1).nullable())).describe('square matrix of coefficients in [-1, 1]; null = not defined'),
  /** How the coefficients were obtained. */
  method: z.enum(['pearson', 'spearman', 'given']).default('given'),
  /** Rows the coefficients were computed from (null when the matrix was given). */
  rows: z.number().int().nonnegative().nullable().default(null),
  /** Missing cells among the used columns. */
  missing: z.number().int().nonnegative().default(0),
  /** Non-numeric columns that were ignored. */
  ignored: z.array(z.string()).default([]),
}).describe('rows of numeric columns, or { names, matrix }')

export const options = z.object({
  method: z.enum(['pearson', 'spearman']).default('pearson').describe('coefficient computed from rows: Pearson (linear) or Spearman (rank)'),
  columns: z.array(z.string()).optional().describe('numeric columns to correlate (default: every numeric column)'),
  triangle: z.enum(['full', 'lower']).default('full').describe("'lower' shows each pair once (plus the diagonal)"),
  showValues: z.boolean().default(true).describe('print the coefficient in each cell when it fits'),
})

export type CorrData = z.output<typeof input>
export type CorrOptions = z.output<typeof options>
