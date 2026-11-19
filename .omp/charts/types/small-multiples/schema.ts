import { z } from 'zod'
import { fields } from '../../core'

/** Any rows; the facet column splits them, the child chart reads the rest. */
export const input = fields.rows({}, "rows for the child chart, plus the facet column named by options.facet")

export const options = z.object({
  chart: z.string().default('bar').describe("the child chart's id, e.g. 'bar', 'line'"),
  facet: z.string({ required_error: "name the column to split by, e.g. { facet: 'region' }" }).describe('the column whose values each get a panel'),
  columns: z.number().int().positive().optional().describe('panels per row (default: as many as fit at ~180 px each)'),
  sharedScale: z.enum(['y', 'x', 'both', 'none']).default('y').describe('which axes every panel shares, so panels compare'),
  valueField: z.string().optional().describe("the column the shared y scale spans (default: the child's y/value mapping, else 'y', else 'value')"),
  xField: z.string().optional().describe("the column the shared x scale spans (default: the child's x mapping, else 'x')"),
  yZero: z.boolean().default(true).describe('include 0 in the shared y scale'),
  chartOptions: z.record(z.unknown()).default({}).describe("options passed to every panel's chart, e.g. { sort: 'descending' }"),
})

export type FacetRow = z.output<typeof input>[number]
export type FacetOptions = z.output<typeof options>
