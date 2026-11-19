import { z } from 'zod'
import { fields } from '../../core'
import { parseDate } from '../../time'

const WEEKDAYS = ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat']
const blank = (v: unknown) => (v === null || v === undefined || (typeof v === 'string' && v.trim() === '') ? undefined : v)

/** 0 (Sunday) .. 6 for a weekday name or number, 'date' for something that parses as a date, null otherwise. */
export function dayToken(v: unknown): number | 'date' | null {
  if (typeof v === 'number') return Number.isInteger(v) && v >= 0 && v <= 6 ? v : parseDate(v) ? 'date' : null
  if (v instanceof Date) return Number.isNaN(+v) ? null : 'date'
  if (typeof v !== 'string') return null
  const s = v.trim().toLowerCase()
  if (/^[0-6]$/.test(s)) return Number(s)
  const i = WEEKDAYS.findIndex((w) => s.startsWith(w) && /^[a-z]+\.?$/.test(s))
  if (i >= 0) return i
  return parseDate(v) ? 'date' : null
}

const when = z.union([z.date(), z.string(), z.number()])

/**
 * Either `{ day, hour, value }` (day: a weekday name, 0-6 with Sunday = 0, or a date) or a raw
 * timestamp `{ date, value? }` bucketed by weekday and hour in `timeZone`. A missing `value`
 * counts the row once.
 */
export const input = fields.rows({
  day: z.preprocess(blank, when.optional()).refine((v) => v === undefined || dayToken(v) !== null, 'must be a weekday name (Mon, Tuesday), 0-6 with Sunday = 0, or a date').describe('weekday name, 0-6 (Sunday = 0), or a date'),
  hour: fields.numOrNull('hour of day, 0-23').refine((v) => v === null || (Number.isInteger(v) && v >= 0 && v <= 23), 'must be a whole hour from 0 to 23'),
  date: z.preprocess(blank, when.optional()).refine((v) => v === undefined || parseDate(v) !== null, 'must be a date (try ISO 8601 like 2026-03-01T14:30)').describe('a timestamp, bucketed by weekday and hour in timeZone'),
  value: z.preprocess((v) => (v === undefined ? 1 : v), fields.numOrNull('amount; missing counts the row once')),
}, 'rows of { day, hour, value } or { date, value? }').superRefine((rows, ctx) => {
  rows.forEach((r, i) => {
    if (r.date === undefined && (r.day === undefined || r.hour === null)) {
      ctx.addIssue({ code: 'custom', path: [i, r.day === undefined ? 'day' : 'hour'], message: "needs a 'date', or both a 'day' and an 'hour'" })
    }
  })
})

export const options = z.object({
  weekStart: z.enum(['monday', 'sunday']).default('monday').describe('first row'),
  dayFormat: z.enum(['short', 'long']).default('short').describe('weekday names, in the chart locale'),
  color: z.enum(['single', 'value']).default('single').describe('one colour (size carries the value), or also a colour ramp'),
  xTitle: z.string().optional(),
})

export type PunchRow = z.output<typeof input>[number]
export type PunchOptions = z.output<typeof options>
