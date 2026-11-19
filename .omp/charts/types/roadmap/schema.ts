import { z } from 'zod'
import { fields } from '../../core'

const row = z.object({
  item: fields.label("the item (or 'phase')"),
  lane: fields.label("the lane it sits in (or 'theme')"),
  start: fields.date("start: a date, or a quarter like '2026-Q1' / 'FY27 Q2'"),
  end: fields.dateOrNull("end: a date, or a quarter ('2026-Q3' ends with that quarter); missing = end of the start quarter"),
  status: fields.optLabel('done | in progress | planned | at risk | blocked, or your own word'),
}).passthrough()

const day = (d: Date) => d.toISOString().slice(0, 10)

/** One rounded bar per item, in its lane, on a quarter axis. */
export const input = z.array(row).describe("rows of { item | phase, lane | theme, start, end, status? }").superRefine((rows, issue) => {
  rows.forEach((r, i) => {
    if (r.end && +r.end < +r.start) issue.addIssue({ code: 'custom', path: [i, 'end'], message: `end ${day(r.end)} is before start ${day(r.start)}` })
  })
})

export const options = z.object({
  fiscalYearStart: z.number().int().min(1).max(12).default(1).describe("month the fiscal year starts (1-12); quarters on the axis and in 'YYYY-Qn' inputs follow it"),
  showLabels: z.boolean().default(true).describe('print item names inside bars that are wide enough'),
})

export type RoadmapRow = z.output<typeof row>
export type RoadmapOptions = z.output<typeof options>
