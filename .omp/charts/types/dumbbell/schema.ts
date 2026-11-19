import { z } from 'zod'
import { fields } from '../../core'

/** One row per category: two values (before/after, plan/actual) joined by a line. */
export const input = fields.rows({
  label: fields.label('category'),
  start: fields.numOrNull('first value (hollow dot); null draws only the end'),
  end: fields.numOrNull('second value (filled dot); null draws only the start'),
}, 'rows of { label, start, end }')

export const options = z.object({
  startLabel: z.string().default('Start').describe("name of the first value, e.g. '2019' or 'Before'"),
  endLabel: z.string().default('End').describe("name of the second value, e.g. '2024' or 'After'"),
  sort: z.enum(['none', 'start', 'end', 'change']).default('none').describe('row order: none, or descending by start, end or change'),
  higherIsBetter: z.boolean().default(true).describe('false: an increase is coloured as bad (costs, error rates)'),
  zero: z.boolean().default(false).describe('force zero into the value axis'),
  xTitle: z.string().optional(),
  yTitle: z.string().optional(),
})

export type DumbbellRow = z.output<typeof input>[number]
export type DumbbellOptions = z.output<typeof options>
