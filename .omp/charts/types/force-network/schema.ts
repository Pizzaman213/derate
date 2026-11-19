import { z } from 'zod'
import { graphInput } from '../_shared/FlowA'

/** Any graph; links are drawn undirected. */
export const input = graphInput

export const options = z.object({
  sizeBy: z.enum(['degree', 'value']).default('degree').describe("node area: number of links, or each node's own value (nodes[].value)"),
  labels: z.number().int().min(0).default(10).describe('label at most this many nodes, largest first (dropped where they would collide)'),
  iterations: z.number().int().min(1).max(1000).default(300).describe('simulation ticks; fixed, so the same data and seed always give the same picture'),
})

export type ForceInput = z.output<typeof input>
export type ForceOptions = z.output<typeof options>
