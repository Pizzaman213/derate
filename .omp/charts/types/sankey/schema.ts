import { z } from 'zod'
import { findCycle, graphInput } from '../_shared/FlowA'

/** A directed, acyclic weighted graph: every link flows forward. */
export const input = graphInput.superRefine((g, ctx) => {
  const cycle = findCycle(g.links)
  if (cycle)
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: [],
      message: `the links form a loop (${cycle.join(' → ')}); a Sankey diagram only flows forward — remove one link of the loop, or use 'chord' or 'force-network' for flows that cycle`,
    })
})

export const options = z.object({
  nodeAlign: z.enum(['justify', 'left', 'right', 'center']).default('justify').describe('column of each node: justify pushes sinks to the right edge; left/right pack toward one side; center balances'),
  showValues: z.boolean().default(false).describe('print each node total after its label'),
})

export type SankeyInput = z.output<typeof input>
export type SankeyOptions = z.output<typeof options>
