/**
 * Strict date parsing. Every accepted shape is listed below; anything else is null.
 * `Date.parse` is never consulted, because it guesses (and guesses differently per engine).
 */
import { daysInMonth, fromZoned, wallToUtcMs } from './tz'

export interface ParseOptions {
  /** Zone for wall times that carry no offset; default 'UTC'. An explicit offset wins. */
  timeZone?: string
  /** Read `01/02/2026` as 1 February instead of the US 2 January. */
  dayFirst?: boolean
}

const MONTHS: Record<string, number> = {
  jan: 1, january: 1, feb: 2, february: 2, mar: 3, march: 3, apr: 4, april: 4, may: 5,
  jun: 6, june: 6, jul: 7, july: 7, aug: 8, august: 8, sep: 9, sept: 9, september: 9,
  oct: 10, october: 10, nov: 11, november: 11, dec: 12, december: 12,
}

const MAX_MS = 8.64e15
const TIME = String.raw`(?:(?:\s*,\s*|\s+|T)(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\s*([ap])\.?m\.?)?)?`
const ISO = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d{1,9}))?)?\s*(Z|[+-]\d{2}(?::?\d{2})?)?)?$/i
const SLASH_YMD = new RegExp(String.raw`^(\d{4})/(\d{1,2})/(\d{1,2})${TIME}$`, 'i')
const NUMERIC_DMY = new RegExp(String.raw`^(\d{1,2})([/.-])(\d{1,2})\2(\d{4})${TIME}$`, 'i')
const DAY_MONTH = new RegExp(String.raw`^(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)\.?,?\s+(\d{4})${TIME}$`, 'i')
const MONTH_DAY = new RegExp(String.raw`^([a-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})${TIME}$`, 'i')

function fromEpoch(n: number): Date | null {
  if (!Number.isFinite(n)) return null
  const ms = Math.abs(n) < 1e11 ? n * 1000 : n
  return Math.abs(ms) <= MAX_MS ? new Date(Math.round(ms)) : null
}

/** Validated wall time -> instant in `timeZone`; null for impossible fields. */
function wall(
  timeZone: string,
  year: number,
  month: number,
  day: number,
  time: [string | undefined, string | undefined, string | undefined, string | undefined] = [undefined, undefined, undefined, undefined],
): Date | null {
  if (month < 1 || month > 12 || day < 1 || day > daysInMonth(year, month)) return null
  let hour = Number(time[0] ?? 0)
  const minute = Number(time[1] ?? 0)
  const second = Number(time[2] ?? 0)
  const meridiem = time[3]?.toLowerCase()
  if (meridiem) {
    if (hour < 1 || hour > 12) return null
    hour = (hour % 12) + (meridiem === 'p' ? 12 : 0)
  }
  if (hour > 23 || minute > 59 || second > 59) return null
  return fromZoned({ year, month, day, hour, minute, second }, timeZone)
}

function parseIso(m: RegExpExecArray, timeZone: string): Date | null {
  const [, y, mo, d, h, mi, s, frac, offset] = m
  const year = Number(y)
  const month = Number(mo)
  const day = Number(d)
  const ms = frac ? Number(frac.slice(0, 3).padEnd(3, '0')) : 0
  if (!offset) {
    const base = wall(timeZone, year, month, day, [h, mi, s, undefined])
    return base && new Date(base.getTime() + ms)
  }
  if (month < 1 || month > 12 || day < 1 || day > daysInMonth(year, month)) return null
  const hour = Number(h)
  const minute = Number(mi)
  const second = Number(s ?? 0)
  if (hour > 23 || minute > 59 || second > 59) return null
  let offsetMin = 0
  if (offset.toUpperCase() !== 'Z') {
    const digits = offset.slice(1).replace(':', '')
    const oh = Number(digits.slice(0, 2))
    const om = Number(digits.slice(2) || 0)
    if (oh > 23 || om > 59) return null
    offsetMin = (offset[0] === '-' ? -1 : 1) * (oh * 60 + om)
  }
  return new Date(wallToUtcMs(year, month, day, hour, minute, second, ms) - offsetMin * 60_000)
}

/**
 * A Date from a Date, an epoch number (|n| < 1e11 is seconds, else ms) or a string in one
 * of the documented shapes; null for anything else.
 */
export function parseDate(v: unknown, opts: ParseOptions = {}): Date | null {
  const tz = opts.timeZone ?? 'UTC'
  if (v instanceof Date) return Number.isNaN(v.getTime()) ? null : new Date(v.getTime())
  if (typeof v === 'number') return fromEpoch(v)
  if (typeof v !== 'string') return null
  const s = v.trim()
  if (s === '') return null

  if (/^-?\d{10}$/.test(s)) return fromEpoch(Number(s))
  if (/^-?\d{13}$/.test(s)) return new Date(Number(s))
  if (/^\d{4}$/.test(s)) return wall(tz, Number(s), 1, 1)

  let m: RegExpExecArray | null
  if ((m = ISO.exec(s))) return parseIso(m, tz)
  if ((m = /^(\d{4})-(\d{2})$/.exec(s))) return wall(tz, Number(m[1]), Number(m[2]), 1)
  if ((m = /^(\d{4})\s*-?\s*Q([1-4])$/i.exec(s))) return wall(tz, Number(m[1]), (Number(m[2]) - 1) * 3 + 1, 1)
  if ((m = /^Q([1-4])\s*-?\s*(\d{4})$/i.exec(s))) return wall(tz, Number(m[2]), (Number(m[1]) - 1) * 3 + 1, 1)
  if ((m = SLASH_YMD.exec(s))) return wall(tz, Number(m[1]), Number(m[2]), Number(m[3]), [m[4], m[5], m[6], m[7]])
  if ((m = NUMERIC_DMY.exec(s))) {
    const a = Number(m[1])
    const b = Number(m[3])
    // No swapping when the preferred reading is impossible: 13/01/2026 under US order is
    // null rather than a silent guess.
    return wall(tz, Number(m[4]), opts.dayFirst ? b : a, opts.dayFirst ? a : b, [m[5], m[6], m[7], m[8]])
  }
  if ((m = DAY_MONTH.exec(s))) {
    const month = MONTHS[(m[2] ?? '').toLowerCase()]
    return month ? wall(tz, Number(m[3]), month, Number(m[1]), [m[4], m[5], m[6], m[7]]) : null
  }
  if ((m = MONTH_DAY.exec(s))) {
    const month = MONTHS[(m[1] ?? '').toLowerCase()]
    return month ? wall(tz, Number(m[3]), month, Number(m[2]), [m[4], m[5], m[6], m[7]]) : null
  }
  return null
}

/**
 * Whether `v` would parse as a date. Stricter than `parseDate` for numbers: only 10-digit
 * (seconds) and 13-digit (ms) magnitudes count, so a column of counts is not a time axis.
 */
export function isDateLike(v: unknown): boolean {
  if (typeof v === 'number') {
    const digits = Math.trunc(Math.abs(v)).toString().length
    return Number.isFinite(v) && (digits === 10 || digits === 13)
  }
  return parseDate(v) !== null
}

/** Human sentence saying why `v` is not a date and what shape to use instead. */
export function describeDateError(v: unknown): string {
  const hint = '(try ISO 8601 like 2026-03-01)'
  if (v instanceof Date) return `Invalid Date is not a date ${hint}`
  if (typeof v === 'number') {
    return Number.isFinite(v) ? `${v} is outside the range of dates a Date can hold ${hint}` : `${v} is not a date ${hint}`
  }
  if (typeof v === 'string') {
    if (v.trim() === '') return `'' is empty, not a date ${hint}`
    const m = /^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})/.exec(v.trim())
    if (m && Number(m[1]) > 12 && Number(m[2]) <= 12) return `'${v}' is not a date in month/day order; pass dayFirst for day/month ${hint}`
    return `'${v}' is not a date ${hint}`
  }
  return `${v === null ? 'null' : typeof v} is not a date ${hint}`
}
