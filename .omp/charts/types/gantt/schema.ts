import { z } from 'zod'
import { fields } from '../../core'
import { flag, todayOption } from '../_shared/TimelineA'

/** "a, b", "a;b", ["a", 2] or 7 -> ['a', 'b'] / ['a', '2'] / ['7']. */
const idList = z.preprocess((v) => {
  if (v === null || v === undefined) return []
  if (typeof v === 'number') return [String(v)]
  if (typeof v === 'string') return v.split(/[,;|]/).map((s) => s.trim()).filter(Boolean)
  if (Array.isArray(v)) return v.map((x) => (typeof x === 'number' ? String(x) : typeof x === 'string' ? x.trim() : x)).filter((x) => x !== '')
  return v
}, z.array(z.string({ invalid_type_error: 'text' }), { invalid_type_error: 'list of task ids' })).describe('ids of the tasks that must finish first: a comma list or an array')

const day = (d: Date) => d.toISOString().slice(0, 10)

const row = z.object({
  id: fields.optLabel('unique task id (default: the name)'),
  name: fields.label('task name, shown in the label column'),
  start: fields.date('start'),
  end: fields.dateOrNull('end (exclusive); or give duration'),
  duration: fields.numOrNull('length in days, when end is missing'),
  progress: fields.numOrNull('share done: 0-1, or 0-100 when any value is above 1'),
  dependsOn: idList.optional().transform((v) => v ?? []),
  milestone: flag('a milestone (also when start equals end)'),
  group: fields.optLabel('group: tasks are listed under a header per group'),
}).passthrough()

/**
 * One bar per task. `end` must not be before `start`; every `dependsOn` id must name a task.
 */
export const input = z.array(row).describe('rows of { id, name, start, end | duration, progress, dependsOn, milestone, group }').superRefine((rows, issue) => {
  const ids = new Set(rows.map((r) => r.id ?? r.name))
  rows.forEach((r, i) => {
    if (r.end && +r.end < +r.start) issue.addIssue({ code: 'custom', path: [i, 'end'], message: `end ${day(r.end)} is before start ${day(r.start)}` })
    if (r.duration !== null && r.duration < 0) issue.addIssue({ code: 'custom', path: [i, 'duration'], message: `duration ${r.duration} is negative` })
    if (r.progress !== null && (r.progress < 0 || r.progress > 100)) issue.addIssue({ code: 'custom', path: [i, 'progress'], message: `progress ${r.progress} is outside 0-1 (or 0-100)` })
    const self = r.id ?? r.name
    for (const d of r.dependsOn) {
      if (!ids.has(d)) issue.addIssue({ code: 'custom', path: [i, 'dependsOn'], message: `unknown task id '${d}' (no row has that id)` })
      else if (d === self) issue.addIssue({ code: 'custom', path: [i, 'dependsOn'], message: `task '${d}' depends on itself` })
    }
  })
})

export const options = z.object({
  today: todayOption,
  showProgress: z.boolean().default(true).describe('print the percent done after each bar'),
  arrows: z.boolean().default(true).describe('draw finish-to-start dependency arrows'),
})

export type GanttRow = z.output<typeof row>
export type GanttOptions = z.output<typeof options>
