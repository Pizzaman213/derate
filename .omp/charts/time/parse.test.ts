import { describe, expect, it } from 'vitest'
import { type ParseOptions, describeDateError, isDateLike, parseDate } from './parse'

const ms = (v: unknown, opts?: ParseOptions) => parseDate(v, opts)?.getTime() ?? null

describe('parseDate: offsets and zones', () => {
  it('honours explicit offsets to the millisecond', () => {
    expect(ms('2026-03-01T10:00:00+05:30')).toBe(Date.UTC(2026, 2, 1, 4, 30))
    expect(ms('2026-03-01T10:00:00Z')).toBe(Date.UTC(2026, 2, 1, 10))
    expect(ms('2026-03-01T10:00:00-0800')).toBe(Date.UTC(2026, 2, 1, 18))
    expect(ms('2026-03-01T10:00:00.250Z')).toBe(Date.UTC(2026, 2, 1, 10, 0, 0, 250))
  })
  it('lets the offset win over timeZone', () => {
    expect(ms('2026-03-01T10:00:00Z', { timeZone: 'Asia/Tokyo' })).toBe(Date.UTC(2026, 2, 1, 10))
    expect(ms('2026-03-01T10:00:00+05:30', { timeZone: 'America/New_York' })).toBe(Date.UTC(2026, 2, 1, 4, 30))
  })
  it('reads offset-less times as wall time in timeZone', () => {
    expect(ms('2026-03-01T10:00:00', { timeZone: 'Asia/Tokyo' })).toBe(Date.UTC(2026, 2, 1, 1))
    expect(ms('2026-03-01T10:00:00')).toBe(Date.UTC(2026, 2, 1, 10))
    expect(ms('2026-03-01', { timeZone: 'Asia/Tokyo' })).toBe(Date.UTC(2026, 1, 28, 15))
    expect(ms('2026-07-01 09:30', { timeZone: 'America/New_York' })).toBe(Date.UTC(2026, 6, 1, 13, 30))
  })
})

describe('parseDate: epochs', () => {
  it('splits seconds from milliseconds at 1e11', () => {
    expect(ms(1772359200)).toBe(1772359200000)
    expect(ms(1772359200000)).toBe(1772359200000)
    expect(ms('1772359200')).toBe(1772359200000)
    expect(ms('1772359200000')).toBe(1772359200000)
    expect(ms(0)).toBe(0)
  })
  it('rejects non-finite and unrepresentable numbers', () => {
    expect(parseDate(Number.NaN)).toBeNull()
    expect(parseDate(Infinity)).toBeNull()
    expect(parseDate(1e20)).toBeNull()
  })
})

describe('parseDate: shapes', () => {
  const utc = (y: number, m: number, d: number, h = 0, mi = 0, s = 0) => Date.UTC(y, m - 1, d, h, mi, s)
  it.each<[string, number]>([
    ['2026-03-01', utc(2026, 3, 1)],
    ['2026-03', utc(2026, 3, 1)],
    ['2026', utc(2026, 1, 1)],
    ['2026-Q3', utc(2026, 7, 1)],
    ['2026 Q2', utc(2026, 4, 1)],
    ['Q4 2026', utc(2026, 10, 1)],
    ['q1 2026', utc(2026, 1, 1)],
    ['2026/03/01', utc(2026, 3, 1)],
    ['2026/3/1 14:05', utc(2026, 3, 1, 14, 5)],
    ['03/01/2026', utc(2026, 3, 1)],
    ['3/1/2026 2:05:09 pm', utc(2026, 3, 1, 14, 5, 9)],
    ['1 Mar 2026', utc(2026, 3, 1)],
    ['01 march 2026 08:00', utc(2026, 3, 1, 8)],
    ['Mar 1, 2026', utc(2026, 3, 1)],
    ['SEPT 30, 2026', utc(2026, 9, 30)],
    ['March 1 2026', utc(2026, 3, 1)],
    ['December 31st, 2026 23:59', utc(2026, 12, 31, 23, 59)],
  ])('%s', (input, expected) => {
    expect(ms(input)).toBe(expected)
  })
  it('switches to day/month with dayFirst', () => {
    expect(ms('03/01/2026', { dayFirst: true })).toBe(utc(2026, 1, 3))
    expect(ms('13/01/2026', { dayFirst: true })).toBe(utc(2026, 1, 13))
    expect(ms('13/01/2026')).toBeNull()
  })
  it('validates leap days', () => {
    expect(ms('2024-02-29')).toBe(utc(2024, 2, 29))
    expect(ms('2000-02-29')).toBe(utc(2000, 2, 29))
    expect(parseDate('2100-02-29')).toBeNull()
    expect(parseDate('2026-02-29')).toBeNull()
  })
  it('copies Date input and rejects Invalid Date', () => {
    const d = new Date(5)
    expect(parseDate(d)).not.toBe(d)
    expect(ms(d)).toBe(5)
    expect(parseDate(new Date('nope'))).toBeNull()
  })
  it.each(['abc', '', '   ', '2026-13-01', '2026-04-31', '2026-03-01T25:00', '2026-03-01T10:00+25:00', 'Foo 1, 2026', '12345', '2026-Q5', 'tomorrow', 'Mon, 02 Mar 2026', '3/1/26'])(
    'rejects %j',
    (input) => {
      expect(parseDate(input)).toBeNull()
    },
  )
  it('rejects non-date types', () => {
    expect(parseDate(null)).toBeNull()
    expect(parseDate(undefined)).toBeNull()
    expect(parseDate({})).toBeNull()
    expect(parseDate(true)).toBeNull()
  })
})

describe('isDateLike / describeDateError', () => {
  it('classifies values', () => {
    expect(isDateLike('2026-03-01')).toBe(true)
    expect(isDateLike(1772359200)).toBe(true)
    expect(isDateLike(1772359200000)).toBe(true)
    expect(isDateLike(42)).toBe(false)
    expect(isDateLike('abc')).toBe(false)
    expect(isDateLike(new Date())).toBe(true)
  })
  it('explains failures', () => {
    expect(describeDateError('abc')).toBe("'abc' is not a date (try ISO 8601 like 2026-03-01)")
    expect(describeDateError('13/01/2026')).toContain('dayFirst')
    expect(describeDateError(null)).toBe('null is not a date (try ISO 8601 like 2026-03-01)')
  })
})
