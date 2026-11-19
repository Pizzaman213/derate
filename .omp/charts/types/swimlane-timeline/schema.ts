import { z } from 'zod'
import { fields } from '../../core'

const row = z.object({
  lane: fields.label('the lane (row group) the item belongs to'),
  start: fields.date('start'),
  end: fields.dateOrNull('end; missing = an instant, drawn as a diamond'),
  label: fields.label('item text, printed inside the bar when it fits'),
  category: fields.optLabel('colour and legend group (default: colour by lane)'),
}).passthrough()

const day = (d: Date) => d.toISOString().slice(0, 10)

/** One bar per row inside its lane; items that overlap in time get sub-rows. */
export const input = z.array(row).describe('rows of { lane, start, end, label, category? }').superRefine((rows, issue) => {
  rows.forEach((r, i) => {
    if (r.end && +r.end < +r.start) issue.addIssue({ code: 'custom', path: [i, 'end'], message: `end ${day(r.end)} is before start ${day(r.start)}` })
  })
})

export const options = z.object({
  showLabels: z.boolean().default(true).describe('print item labels inside bars that are wide enough'),
})

export type SwimlaneRow = z.output<typeof row>
export type SwimlaneOptions = z.output<typeof options>
