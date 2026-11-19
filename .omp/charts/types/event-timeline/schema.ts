import { z } from 'zod'
import { fields } from '../../core'

const row = z.object({
  date: fields.date('when it happens (or starts)'),
  end: fields.dateOrNull('end of a range event; missing = a point event'),
  label: fields.label('what happened'),
  category: fields.optLabel('colour and legend group'),
}).passthrough()

const day = (d: Date) => d.toISOString().slice(0, 10)

/** One mark per row: a dot for a point event, a bar for a range. `end` must not precede `date`. */
export const input = z.array(row).describe('rows of { date, end?, label, category? }').superRefine((rows, issue) => {
  rows.forEach((r, i) => {
    if (r.end && +r.end < +r.date) issue.addIssue({ code: 'custom', path: [i, 'end'], message: `end ${day(r.end)} is before date ${day(r.date)}` })
  })
})

export const options = z.object({
  labels: z.boolean().default(true).describe('print event labels beside their marks (dropped when the lanes would not fit)'),
  maxLabelWidth: z.number().positive().default(160).describe('px; longer labels are truncated (full text on hover)'),
})

export type EventRow = z.output<typeof row>
export type EventOptions = z.output<typeof options>
