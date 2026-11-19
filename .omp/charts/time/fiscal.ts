/**
 * Fiscal calendars that start on the first of any month. A fiscal year is named by the
 * calendar year in which it ENDS: with a 1 April start, 2026-04-01 opens FY2027.
 */
import { fromZoned, zonedParts } from './tz'

export interface FiscalOptions {
  /** Month the fiscal year starts in, 1-12; default 1 (fiscal = calendar). */
  startMonth?: number
  /** Zone the calendar is read in; default 'UTC'. */
  timeZone?: string
}

export interface FiscalQuarter {
  fiscalYear: number
  quarter: 1 | 2 | 3 | 4
  /** e.g. 'FY27 Q1' */
  label: string
}

function startMonthOf(opts: FiscalOptions): number {
  const m = opts.startMonth ?? 1
  if (!Number.isInteger(m) || m < 1 || m > 12) throw new RangeError(`startMonth must be a whole month 1-12, got ${m}`)
  return m
}

/** Fiscal year, quarter and label of the day `d` falls on in the zone. */
export function fiscalQuarter(d: Date, opts: FiscalOptions = {}): FiscalQuarter {
  const start = startMonthOf(opts)
  const p = zonedParts(d, opts.timeZone ?? 'UTC')
  const fiscalYear = start === 1 || p.month < start ? p.year : p.year + 1
  const quarter = (Math.floor(((p.month - start + 12) % 12) / 3) + 1) as 1 | 2 | 3 | 4
  const yy = String(((fiscalYear % 100) + 100) % 100).padStart(2, '0')
  return { fiscalYear, quarter, label: `FY${yy} Q${quarter}` }
}

/** First instant of fiscal year `fy` (midnight on the 1st of the start month, in the zone). */
export function fiscalYearStart(fy: number, opts: FiscalOptions = {}): Date {
  return fiscalQuarterStart(fy, 1, opts)
}

/** First instant of quarter `q` of fiscal year `fy`. */
export function fiscalQuarterStart(fy: number, q: 1 | 2 | 3 | 4, opts: FiscalOptions = {}): Date {
  if (![1, 2, 3, 4].includes(q)) throw new RangeError(`quarter must be 1-4, got ${q}`)
  const start = startMonthOf(opts)
  const index = (start === 1 ? fy : fy - 1) * 12 + (start - 1) + (q - 1) * 3
  const year = Math.floor(index / 12)
  return fromZoned({ year, month: index - year * 12 + 1, day: 1 }, opts.timeZone ?? 'UTC')
}
