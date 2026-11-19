import { describe, expect, it } from 'vitest'
import { GRANULARITIES, bucketBy, floorTo, granularityOf, offsetBy, pickGranularity, rangeOf } from './granularity'

const NY = { timeZone: 'America/New_York' }
const LON = { timeZone: 'Europe/London' }
const iso = (d: Date) => d.toISOString()
const H = 3_600_000

describe('floorTo', () => {
  it('floors to local midnight on DST days', () => {
    expect(iso(floorTo(new Date('2026-03-08T15:00:00Z'), 'day', NY))).toBe('2026-03-08T05:00:00.000Z')
    expect(iso(floorTo(new Date('2026-11-01T20:00:00Z'), 'day', NY))).toBe('2026-11-01T04:00:00.000Z')
    expect(iso(floorTo(new Date('2026-03-29T12:00:00Z'), 'day', LON))).toBe('2026-03-29T00:00:00.000Z')
    expect(iso(floorTo(new Date('2026-03-30T12:00:00Z'), 'day', LON))).toBe('2026-03-29T23:00:00.000Z')
  })
  it('keeps the two 01:00 hours of fall-back apart', () => {
    expect(iso(floorTo(new Date('2026-11-01T05:30:00Z'), 'hour', NY))).toBe('2026-11-01T05:00:00.000Z')
    expect(iso(floorTo(new Date('2026-11-01T06:30:00Z'), 'hour', NY))).toBe('2026-11-01T06:00:00.000Z')
  })
  it('floors hours in half-hour zones on the local hour', () => {
    expect(iso(floorTo(new Date('2026-03-01T10:15:00Z'), 'hour', { timeZone: 'Asia/Kolkata' }))).toBe('2026-03-01T09:30:00.000Z')
  })
  it('floors minutes, weeks, months, quarters and years', () => {
    expect(iso(floorTo(new Date('2026-03-01T10:15:42.123Z'), 'minute'))).toBe('2026-03-01T10:15:00.000Z')
    // 2026-03-05 is a Thursday.
    expect(iso(floorTo(new Date('2026-03-05T10:00:00Z'), 'week'))).toBe('2026-03-02T00:00:00.000Z')
    expect(iso(floorTo(new Date('2026-03-05T10:00:00Z'), 'week', { weekStart: 0 }))).toBe('2026-03-01T00:00:00.000Z')
    expect(iso(floorTo(new Date('2026-03-02T00:00:00Z'), 'week'))).toBe('2026-03-02T00:00:00.000Z')
    expect(iso(floorTo(new Date('2026-03-01T00:00:00Z'), 'week'))).toBe('2026-02-23T00:00:00.000Z')
    expect(iso(floorTo(new Date('2026-03-31T23:59:59Z'), 'month'))).toBe('2026-03-01T00:00:00.000Z')
    expect(iso(floorTo(new Date('2026-08-15T00:00:00Z'), 'quarter'))).toBe('2026-07-01T00:00:00.000Z')
    expect(iso(floorTo(new Date('2026-12-31T00:00:00Z'), 'quarter'))).toBe('2026-10-01T00:00:00.000Z')
    expect(iso(floorTo(new Date('2026-08-15T00:00:00Z'), 'year'))).toBe('2026-01-01T00:00:00.000Z')
  })
  it('floors the week across a DST change in local time', () => {
    // Tuesday 2026-03-10 in New York -> Monday 2026-03-09 midnight EDT.
    expect(iso(floorTo(new Date('2026-03-10T15:00:00Z'), 'week', NY))).toBe('2026-03-09T04:00:00.000Z')
  })
})

describe('offsetBy', () => {
  it('adds wall-clock days: 23h and 25h of elapsed time across DST', () => {
    const sat = new Date('2026-03-07T17:00:00Z') // noon EST
    expect(offsetBy(sat, 'day', 1, NY).getTime() - sat.getTime()).toBe(23 * H)
    const sat2 = new Date('2026-10-31T16:00:00Z') // noon EDT
    expect(offsetBy(sat2, 'day', 1, NY).getTime() - sat2.getTime()).toBe(25 * H)
    const lon = new Date('2026-03-28T12:00:00Z')
    expect(offsetBy(lon, 'day', 1, LON).getTime() - lon.getTime()).toBe(23 * H)
  })
  it('adds elapsed hours and minutes', () => {
    expect(iso(offsetBy(new Date('2026-03-08T06:30:00Z'), 'hour', 1, NY))).toBe('2026-03-08T07:30:00.000Z')
    expect(iso(offsetBy(new Date('2026-03-08T06:30:00Z'), 'minute', -90))).toBe('2026-03-08T05:00:00.000Z')
  })
  it('clamps month-end and leap days', () => {
    expect(iso(offsetBy(new Date('2026-01-31T00:00:00Z'), 'month', 1))).toBe('2026-02-28T00:00:00.000Z')
    expect(iso(offsetBy(new Date('2024-01-31T00:00:00Z'), 'month', 1))).toBe('2024-02-29T00:00:00.000Z')
    expect(iso(offsetBy(new Date('2024-02-29T00:00:00Z'), 'year', 1))).toBe('2025-02-28T00:00:00.000Z')
    expect(iso(offsetBy(new Date('2024-02-29T00:00:00Z'), 'year', 4))).toBe('2028-02-29T00:00:00.000Z')
    expect(iso(offsetBy(new Date('2096-02-29T00:00:00Z'), 'year', 4))).toBe('2100-02-28T00:00:00.000Z')
    expect(iso(offsetBy(new Date('1996-02-29T00:00:00Z'), 'year', 4))).toBe('2000-02-29T00:00:00.000Z')
    expect(iso(offsetBy(new Date('2026-11-30T00:00:00Z'), 'quarter', 1))).toBe('2027-02-28T00:00:00.000Z')
    expect(iso(offsetBy(new Date('2026-03-15T00:00:00Z'), 'month', -3))).toBe('2025-12-15T00:00:00.000Z')
    expect(iso(offsetBy(new Date('2026-03-15T00:00:00Z'), 'week', 2))).toBe('2026-03-29T00:00:00.000Z')
  })
  it('rejects fractional steps', () => {
    expect(() => offsetBy(new Date(0), 'day', 1.5)).toThrow(RangeError)
  })
})

describe('rangeOf', () => {
  it('counts the hours in DST days', () => {
    const springDay = floorTo(new Date('2026-03-08T12:00:00Z'), 'day', NY)
    const springNext = offsetBy(springDay, 'day', 1, NY)
    expect(springNext.getTime() - springDay.getTime()).toBe(23 * H)
    expect(rangeOf(springDay, new Date(springNext.getTime() - 1), 'hour', NY)).toHaveLength(23)

    const fallDay = floorTo(new Date('2026-11-01T12:00:00Z'), 'day', NY)
    const fallNext = offsetBy(fallDay, 'day', 1, NY)
    expect(fallNext.getTime() - fallDay.getTime()).toBe(25 * H)
    expect(rangeOf(fallDay, new Date(fallNext.getTime() - 1), 'hour', NY)).toHaveLength(25)
  })
  it('steps days by local midnight across both transitions', () => {
    const days = rangeOf(new Date('2026-03-07T12:00:00Z'), new Date('2026-03-09T12:00:00Z'), 'day', NY).map(iso)
    expect(days).toEqual(['2026-03-07T05:00:00.000Z', '2026-03-08T05:00:00.000Z', '2026-03-09T04:00:00.000Z'])
    const fall = rangeOf(new Date('2026-10-31T12:00:00Z'), new Date('2026-11-02T12:00:00Z'), 'day', NY).map(iso)
    expect(fall).toEqual(['2026-10-31T04:00:00.000Z', '2026-11-01T04:00:00.000Z', '2026-11-02T05:00:00.000Z'])
    const lon = rangeOf(new Date('2026-03-28T00:00:00Z'), new Date('2026-03-30T00:00:00Z'), 'day', LON).map(iso)
    // 2026-03-30T00:00Z is 01:00 BST, so the 30th's midnight (23:00Z on the 29th) is in range.
    expect(lon).toEqual(['2026-03-28T00:00:00.000Z', '2026-03-29T00:00:00.000Z', '2026-03-29T23:00:00.000Z'])
  })
  it('includes the floored start and an exactly-matching end', () => {
    const months = rangeOf(new Date('2026-01-15T00:00:00Z'), new Date('2026-04-01T00:00:00Z'), 'month').map(iso)
    expect(months).toEqual(['2026-01-01T00:00:00.000Z', '2026-02-01T00:00:00.000Z', '2026-03-01T00:00:00.000Z', '2026-04-01T00:00:00.000Z'])
    expect(rangeOf(new Date('2026-02-01T00:00:00Z'), new Date('2026-01-01T00:00:00Z'), 'month')).toEqual([])
  })
  it('steps from a day whose midnight does not exist', () => {
    // America/Santiago springs forward at 00:00 on 2026-09-06; the day starts at 01:00.
    const tz = { timeZone: 'America/Santiago' }
    const days = rangeOf(new Date('2026-09-05T12:00:00Z'), new Date('2026-09-07T12:00:00Z'), 'day', tz).map(iso)
    expect(days).toEqual(['2026-09-05T04:00:00.000Z', '2026-09-06T04:00:00.000Z', '2026-09-07T03:00:00.000Z'])
  })
})

describe('bucketBy', () => {
  it('puts both 01:30s of fall-back day in one daily bucket', () => {
    const rows = [
      { t: new Date('2026-11-01T05:30:00Z'), v: 1 }, // 01:30 EDT
      { t: new Date('2026-11-01T06:30:00Z'), v: 2 }, // 01:30 EST
      { t: new Date('2026-11-02T04:59:00Z'), v: 4 }, // 23:59 EST, same day
      { t: new Date('2026-11-02T05:00:00Z'), v: 8 }, // next day
    ]
    const out = bucketBy(rows, { date: (r) => r.t, value: (r) => r.v, granularity: 'day', ...NY })
    expect(out.map((b) => [iso(b.date), b.value, b.count])).toEqual([
      ['2026-11-01T04:00:00.000Z', 7, 3],
      ['2026-11-02T05:00:00.000Z', 8, 1],
    ])
  })
  it('buckets by Monday-start week', () => {
    const rows = ['2026-03-01', '2026-03-02', '2026-03-04', '2026-03-08', '2026-03-09'].map((s, i) => ({ t: new Date(`${s}T12:00:00Z`), v: i + 1 }))
    const out = bucketBy(rows, { date: (r) => r.t, value: (r) => r.v, granularity: 'week' })
    expect(out.map((b) => [iso(b.date).slice(0, 10), b.value])).toEqual([
      ['2026-02-23', 1],
      ['2026-03-02', 2 + 3 + 4],
      ['2026-03-09', 5],
    ])
  })
  it('buckets by quarter with each aggregate', () => {
    const rows = [
      { t: '2026-01-10', v: 4 },
      { t: '2026-03-31', v: 2 },
      { t: '2026-02-01', v: null },
      { t: '2026-04-01', v: 10 },
      { t: 'bad', v: 99 },
    ]
    const run = (agg: 'sum' | 'mean' | 'count' | 'min' | 'max' | 'last') =>
      bucketBy(rows, { date: (r) => (r.t === 'bad' ? null : new Date(r.t)), value: (r) => r.v, granularity: 'quarter', agg }).map((b) => [iso(b.date).slice(0, 10), b.value, b.count])
    expect(run('sum')).toEqual([['2026-01-01', 6, 2], ['2026-04-01', 10, 1]])
    expect(run('mean')).toEqual([['2026-01-01', 3, 2], ['2026-04-01', 10, 1]])
    expect(run('count')).toEqual([['2026-01-01', 2, 2], ['2026-04-01', 1, 1]])
    expect(run('min')[0]?.[1]).toBe(2)
    expect(run('max')[0]?.[1]).toBe(4)
    expect(run('last')[0]?.[1]).toBe(2)
  })
  it('counts rows when no value accessor is given', () => {
    const out = bucketBy([new Date('2026-01-01'), new Date('2026-01-01T05:00Z'), new Date('2026-01-02')], { date: (d) => d, granularity: 'day' })
    expect(out.map((b) => b.value)).toEqual([2, 1])
  })
})

describe('pickGranularity / granularityOf', () => {
  it('lists granularities finest first', () => {
    expect(GRANULARITIES).toEqual(['minute', 'hour', 'day', 'week', 'month', 'quarter', 'year'])
  })
  it('picks the finest granularity within maxBuckets', () => {
    expect(pickGranularity(30 * 60_000)).toBe('minute')
    expect(pickGranularity(2 * 86_400_000)).toBe('hour')
    expect(pickGranularity(30 * 86_400_000)).toBe('day')
    expect(pickGranularity(365 * 86_400_000)).toBe('week')
    expect(pickGranularity(3 * 365 * 86_400_000)).toBe('month')
    expect(pickGranularity(10 * 365 * 86_400_000)).toBe('quarter')
    expect(pickGranularity(100 * 365 * 86_400_000)).toBe('year')
    expect(pickGranularity(10 * 86_400_000, 5)).toBe('week')
    expect(pickGranularity(0)).toBe('minute')
  })
  it('infers granularity from median spacing', () => {
    const daily = Array.from({ length: 10 }, (_, i) => new Date(Date.UTC(2026, 2, 1 + i)))
    expect(granularityOf(daily)).toBe('day')
    const monthly = Array.from({ length: 12 }, (_, i) => new Date(Date.UTC(2026, i, 1)))
    expect(granularityOf(monthly)).toBe('month')
    const quarterly = [0, 3, 6, 9].map((m) => new Date(Date.UTC(2026, m, 1)))
    expect(granularityOf(quarterly)).toBe('quarter')
    const hourly = [0, 1, 2, 3, 5].map((h) => new Date(Date.UTC(2026, 0, 1, h)))
    expect(granularityOf(hourly)).toBe('hour')
    expect(granularityOf([new Date(0)])).toBe('day')
  })
})
