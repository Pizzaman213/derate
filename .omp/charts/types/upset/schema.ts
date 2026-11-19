import { z } from 'zod'
import { fields } from '../../core'
import { setList } from '../_shared/SetCharts'

/**
 * Either memberships -- one row per item (or per item-set pair: rows sharing an `item`
 * are merged), `sets` naming every set it belongs to -- or aggregated sizes, when any
 * row has a `size`: `{ sets: 'A,B', size }` rows or venn.js's `{ sets: [...] }`.
 */
export const input = fields.rows({
  item: fields.optLabel('item id; rows with the same item are one item (long format)'),
  sets: setList("the sets the item belongs to ('A,B', 'A;B', 'A|B' or a list); empty = in no set", 0),
  size: fields.numOrNull('aggregated input only: the size of this combination (see the sizes option)'),
}, 'rows of { item?, sets } memberships, or { sets, size } sizes')

export const options = z.object({
  sortBy: z.enum(['size', 'degree']).default('size').describe('intersection order: largest first, or by number of sets then size'),
  sizes: z.enum(['inclusive', 'exclusive']).default('inclusive').describe("aggregated input: 'inclusive' (venn.js: 'A,B' counts everything in both) or 'exclusive' (in exactly these sets)"),
  showValues: z.boolean().default(true).describe('print each size at the end of its bar when it fits'),
})

export type UpsetRow = z.output<typeof input>[number]
export type UpsetOptions = z.output<typeof options>
