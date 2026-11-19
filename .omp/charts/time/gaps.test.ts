import { describe, expect, it } from 'vitest'
import { type FilledPoint, fillGaps, findGaps } from './gaps'

const d = (s: string) => new Date(`${s}T00:00:00Z`)
const pts = [
  { date: d('2026-03-01'), value: 10 },
  { date: d('2026-03-04'), value: 40 },
  { date: d('2026-03-05'), value: null },
  { date: d('2026-03-06'), value: 20 },
]
const view = (r: FilledPoint[]) => r.map((p) => [p.date.toISOString().slice(0, 10), p.value, p.filled])

describe('fillGaps', () => {
  it("'gap' leaves nulls so the line lifts", () => {
    expect(view(fillGaps(pts, { granularity: 'day', mode: 'gap' }))).toEqual([
      ['2026-03-01', 10, false],
      ['2026-03-02', null, true],
      ['2026-03-03', null, true],
      ['2026-03-04', 40, false],
      ['2026-03-05', null, true],
      ['2026-03-06', 20, false],
    ])
  })
  it("'zero' writes 0", () => {
    expect(fillGaps(pts, { granularity: 'day', mode: 'zero' }).map((p) => p.value)).toEqual([10, 0, 0, 40, 0, 20])
  })
  it("'previous' carries forward and leaves a leading hole null", () => {
    expect(fillGaps(pts, { granularity: 'day', mode: 'previous' }).map((p) => p.value)).toEqual([10, 10, 10, 40, 40, 20])
    const lead = fillGaps(pts, { granularity: 'day', mode: 'previous', start: d('2026-02-27') })
    expect(lead.map((p) => p.value)).toEqual([null, null, 10, 10, 10, 40, 40, 20])
  })
  it("'interpolate' draws straight lines in time and leaves the edges null", () => {
    const out = fillGaps(pts, { granularity: 'day', mode: 'interpolate', end: d('2026-03-08') })
    expect(out.map((p) => p.value)).toEqual([10, 20, 30, 40, 30, 20, null, null])
    expect(out.map((p) => p.filled)).toEqual([false, true, true, false, true, false, true, true])
  })
  it('lets the last point in a bucket win', () => {
    const out = fillGaps(
      [
        { date: new Date('2026-03-01T01:00:00Z'), value: 1 },
        { date: new Date('2026-03-01T23:00:00Z'), value: 2 },
        { date: new Date('2026-03-01T12:00:00Z'), value: 3 },
      ],
      { granularity: 'day', mode: 'gap' },
    )
    expect(view(out)).toEqual([['2026-03-01', 3, false]])
  })
  it('sorts unsorted input and fills a monthly grid', () => {
    const out = fillGaps(
      [
        { date: d('2026-04-15'), value: 4 },
        { date: d('2026-01-20'), value: 1 },
      ],
      { granularity: 'month', mode: 'zero' },
    )
    expect(view(out)).toEqual([
      ['2026-01-01', 1, false],
      ['2026-02-01', 0, true],
      ['2026-03-01', 0, true],
      ['2026-04-01', 4, false],
    ])
  })
  it('builds a 25-bucket hourly grid on fall-back day', () => {
    const tz = { timeZone: 'America/New_York' }
    const out = fillGaps([{ date: new Date('2026-11-01T04:00:00Z'), value: 1 }], { granularity: 'hour', mode: 'zero', end: new Date('2026-11-02T04:59:00Z'), ...tz })
    expect(out).toHaveLength(25)
  })
  it('returns nothing for no points and no bounds', () => {
    expect(fillGaps([], { granularity: 'day', mode: 'gap' })).toEqual([])
  })
})

describe('findGaps', () => {
  it('reports runs of missing buckets', () => {
    const dates = ['2026-03-01', '2026-03-02', '2026-03-05', '2026-03-06', '2026-03-08'].map(d)
    expect(findGaps(dates, 'day').map((g) => [g.from.toISOString().slice(0, 10), g.to.toISOString().slice(0, 10), g.missing])).toEqual([
      ['2026-03-03', '2026-03-04', 2],
      ['2026-03-07', '2026-03-07', 1],
    ])
  })
  it('finds no gaps in a complete series, duplicates included', () => {
    expect(findGaps([d('2026-03-02'), d('2026-03-01'), d('2026-03-01')], 'day')).toEqual([])
  })
  it('steps local days across DST without inventing gaps', () => {
    const tz = { timeZone: 'America/New_York' }
    const dates = [new Date('2026-03-07T17:00:00Z'), new Date('2026-03-08T16:00:00Z'), new Date('2026-03-10T16:00:00Z')]
    const gaps = findGaps(dates, 'day', tz)
    expect(gaps).toHaveLength(1)
    expect(gaps[0]?.from.toISOString()).toBe('2026-03-09T04:00:00.000Z')
    expect(gaps[0]?.missing).toBe(1)
  })
  it('counts missing weeks', () => {
    expect(findGaps([d('2026-03-02'), d('2026-03-30')], 'week')[0]?.missing).toBe(3)
  })
})
