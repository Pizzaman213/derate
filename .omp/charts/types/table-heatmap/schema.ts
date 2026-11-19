import { z } from 'zod'

const cell = z.union([z.string(), z.number(), z.boolean(), z.null()], { invalid_type_error: 'text, a number or empty' })

/** Any table: rows of named cells. Numeric columns (every non-empty cell a number) are colour-scaled. */
export const input = z.array(z.record(cell), { invalid_type_error: 'list of rows' }).describe('rows of { column: text | number | null }')

export const options = z.object({
  columns: z.array(z.string()).optional().describe('columns to show, in order (default: every column, first seen first)'),
  scale: z.enum(['column', 'table']).default('column').describe("colour each numeric column on its own range, or every number on one shared range"),
  diverging: z.boolean().optional().describe('force (true) or forbid (false) a diverging ramp; default: diverging where values span zero'),
  showValues: z.boolean().default(true).describe('print the cell text (dropped when rows are too short to hold it)'),
})

export type TableRow = z.output<typeof input>[number]
export type TableHeatmapOptions = z.output<typeof options>
