import { z } from 'zod'
import { graphInput } from '../_shared/FlowA'

/** Any graph; links are drawn undirected (a→b and b→a are one arc). */
export const input = graphInput

export const options = z.object({
  order: z.enum(['input', 'degree', 'group']).default('input').describe('node order along the line: as given, most connected first, or grouped (then as given)'),
})

export type ArcInput = z.output<typeof input>
export type ArcOptions = z.output<typeof options>
