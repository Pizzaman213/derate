import { describe, expect, it } from 'vitest'
import { distinct, pivot, unpivot } from './reshape'

const long = [
  { date: '2024-01', series: 'EU', value: 1 },
  { date: '2024-01', series: 'US', value: 2 },
  { date: '2024-02', series: 'EU', value: 3 },
  { date: '2024-02', series: 'US', value: 4 },
]
const wide = [
  { date: '2024-01', EU: 1, US: 2 },
  { date: '2024-02', EU: 3, US: 4 },
]

describe('pivot', () => {
  it('turns long rows wide', () => {
    expect(pivot(long, { index: 'date', columns: 'series', values: 'value' })).toEqual(wide)
  })

  it('aggregates duplicates and fills gaps with null', () => {
    const rows = [
      { k: 'x', s: 'a', v: 1 },
      { k: 'x', s: 'a', v: 2 },
      { k: 'y', s: 'b', v: 5 },
    ]
    expect(pivot(rows, { index: 'k', columns: 's', values: 'v' })).toEqual([
      { k: 'x', a: 3, b: null },
      { k: 'y', a: null, b: 5 },
    ])
    expect(pivot(rows, { index: 'k', columns: 's', values: 'v', agg: 'mean' })[0]).toEqual({ k: 'x', a: 1.5, b: null })
    expect(pivot(rows, { index: 'k', columns: 's', values: 'v', agg: 'count' })[1]).toEqual({ k: 'y', a: null, b: 1 })
  })
})

describe('unpivot', () => {
  it('turns wide rows long with default key/value names', () => {
    expect(unpivot(wide, { id: ['date'] })).toEqual(long)
  })

  it('takes custom names and a column subset', () => {
    expect(unpivot(wide, { id: ['date'], key: 'region', value: 'n', columns: ['US'] })).toEqual([
      { date: '2024-01', region: 'US', n: 2 },
      { date: '2024-02', region: 'US', n: 4 },
    ])
  })

  it('round-trips with pivot', () => {
    expect(pivot(unpivot(wide, { id: ['date'] }), { index: 'date', columns: 'series', values: 'value' })).toEqual(wide)
    expect(unpivot(pivot(long, { index: 'date', columns: 'series', values: 'value' }), { id: ['date'] })).toEqual(long)
  })
})

describe('distinct', () => {
  it('keeps first-seen order and matches Dates by time', () => {
    expect(distinct([{ a: 2 }, { a: 1 }, { a: 2 }, { a: '2' }], 'a')).toEqual([2, 1, '2'])
    expect(distinct([{ d: new Date(1) }, { d: new Date(1) }], 'd')).toHaveLength(1)
  })
})
