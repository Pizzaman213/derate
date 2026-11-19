import { z } from 'zod'
import { fields } from '../../core'

/** Long rows: how many items sat in each workflow state on each date. Wide rows are unpivoted first (see index.ts). */
export const input = fields.rows({
  date: fields.date('the day (or instant) of the snapshot'),
  state: fields.label('workflow state ("Backlog", "In progress", "Done")'),
  count: fields.numOrNull('items in that state on that date; null counts as 0 (noted)'),
}, 'rows of { date, state, count } -- or one row per date with a numeric column per state')

export const options = z.object({
  states: z.array(z.string()).optional().describe('workflow order, first stage first ("Backlog" ... "Done"); the last state is stacked at the bottom. States not listed follow in first-seen order'),
  labels: z.boolean().default(true).describe('name each band at the right edge where it is tall enough'),
  xTitle: z.string().optional(),
  yTitle: z.string().optional(),
})

export type FlowRow = z.output<typeof input>[number]
export type FlowOptions = z.output<typeof options>
