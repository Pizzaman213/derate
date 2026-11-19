import { z } from 'zod'
import { fields } from '../../core'
import { flag, todayOption } from '../_shared/TimelineA'

const row = z.object({
  date: fields.date('when the milestone is (or was) due'),
  label: fields.label('milestone name'),
  done: flag('reached: true/false, yes/no, 1/0, "done"; missing = upcoming'),
}).passthrough()

/** One diamond per row on a single line. */
export const input = z.array(row).describe('rows of { date, label, done? }')

export const options = z.object({
  today: todayOption,
  maxLabelWidth: z.number().positive().default(140).describe('px; longer names are truncated (full text on hover)'),
  showDates: z.boolean().default(true).describe('print the date under each name'),
})

export type MilestoneRow = z.output<typeof row>
export type MilestoneOptions = z.output<typeof options>
