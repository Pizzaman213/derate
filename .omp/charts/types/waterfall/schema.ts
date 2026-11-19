import { z } from 'zod'
import { fields } from '../../core'

/** One step per row: a change (`delta`, the default) or a subtotal (`total`). */
export const input = fields.rows({
  label: fields.label('step name along the x axis'),
  value: fields.numOrNull("the change; for a 'total' row optional (it is drawn at the running total)"),
  kind: z.preprocess(
    (v) => (v === null || v === undefined || v === '' ? undefined : typeof v === 'string' ? v.trim().toLowerCase() : v),
    z.enum(['delta', 'total'], { invalid_type_error: 'text' }).optional(),
  ).describe("'delta' (default) or 'total'"),
}, "rows of { label, value, kind?: 'delta' | 'total' }")

export const options = z.object({
  showTotal: z.boolean().default(true).describe('append a final total bar when the last row is not already a total'),
  totalLabel: z.string().default('Total').describe('label of the appended total bar'),
  connectors: z.boolean().default(true).describe('thin lines carrying each running total to the next bar'),
  showValues: z.boolean().default(true).describe('print each change (signed) and each total on its bar'),
  xTitle: z.string().optional(),
  yTitle: z.string().optional(),
})

export type WaterfallRow = z.output<typeof input>[number]
export type WaterfallOptions = z.output<typeof options>
