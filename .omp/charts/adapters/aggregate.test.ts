import { describe, expect, it } from 'vitest'
import { aggregate, groupAggregate, groupRows, normalize, topN } from './aggregate'

describe('aggregate', () => {
  const xs = [3, null, 1, Number.NaN, 4, undefined, 2]
  it.each([
    ['sum', 10],
    ['mean', 2.5],
    ['median', 2.5],
    ['count', 4],
    ['min', 1],
    ['max', 4],
    ['first', 3],
    ['last', 2],
  ] as const)('%s ignores null/NaN', (agg, expected) => {
    expect(aggregate(xs, agg)).toBe(expected)
  })

  it('takes the middle value for an odd median', () => {
    expect(aggregate([5, 1, 3], 'median')).toBe(3)
  })

  it('is 0 for an empty sum/count and null otherwise', () => {
    expect(aggregate([null], 'sum')).toBe(0)
    expect(aggregate([], 'count')).toBe(0)
    expect(aggregate([], 'mean')).toBeNull()
    expect(aggregate([null, Number.NaN], 'max')).toBeNull()
  })
})

describe('groupAggregate', () => {
  const rows = [
    { region: 'US', kind: 'a', v: 1 },
    { region: 'EU', kind: 'a', v: 2 },
    { region: 'US', kind: 'b', v: 3 },
    { region: 'US', kind: 'a', v: '4' },
    { region: 'EU', kind: 'a', v: null },
  ]

  it('sums per group in first-seen order', () => {
    expect(groupAggregate(rows, { by: ['region'], value: 'v', agg: 'sum' })).toEqual([
      { region: 'US', v: 8 },
      { region: 'EU', v: 2 },
    ])
  })

  it('groups by several columns and renames the output', () => {
    expect(groupAggregate(rows, { by: ['region', 'kind'], value: 'v', agg: 'max', as: 'top' })).toEqual([
      { region: 'US', kind: 'a', top: 4 },
      { region: 'EU', kind: 'a', top: 2 },
      { region: 'US', kind: 'b', top: 3 },
    ])
  })

  it('counts rows when no value is given', () => {
    expect(groupAggregate(rows, { by: ['region'], agg: 'count' })).toEqual([
      { region: 'US', count: 3 },
      { region: 'EU', count: 2 },
    ])
  })

  it('refuses a non-count aggregate without a value column', () => {
    expect(() => groupAggregate(rows, { by: ['region'], agg: 'sum' })).toThrow("groupAggregate: 'sum' needs a value column")
  })

  it('treats equal Dates as one group and 1 vs "1" as two', () => {
    const g = groupRows([{ d: new Date(5) }, { d: new Date(5) }, { d: 1 }, { d: '1' }], ['d'])
    expect(g.map((x) => x.length)).toEqual([2, 1, 1])
  })
})

describe('normalize', () => {
  const rows = [
    { g: 'a', v: 1 },
    { g: 'a', v: 3 },
    { g: 'b', v: 0 },
    { g: 'b', v: 0 },
    { g: 'c', v: 1 },
    { g: 'c', v: 1 },
    { g: 'c', v: 1 },
  ]

  it('makes each group total 100, and an all-zero group all 0', () => {
    const out = normalize(rows, { value: 'v', by: 'g', as: 'pct' })
    expect(out.slice(0, 4).map((r) => r.pct)).toEqual([25, 75, 0, 0])
    for (const r of out.slice(4)) expect(r.pct).toBeCloseTo(100 / 3, 12)
    for (const g of ['a', 'c']) {
      const total = out.filter((r) => r.g === g).reduce((s, r) => s + (r.pct as number), 0)
      expect(Math.abs(total - 100)).toBeLessThan(1e-9)
    }
    expect(out.filter((r) => r.g === 'b').every((r) => r.pct === 0)).toBe(true)
    expect(out.some((r) => Number.isNaN(r.pct))).toBe(false)
    expect(out[0]).toEqual({ g: 'a', v: 1, pct: 25 })
  })

  it('normalises over everything to a custom total, in place by default', () => {
    expect(normalize([{ v: 1 }, { v: 1 }, { v: 2 }], { value: 'v', to: 1 })).toEqual([{ v: 0.25 }, { v: 0.25 }, { v: 0.5 }])
  })

  it('leaves null values null', () => {
    expect(normalize([{ v: null }, { v: 2 }], { value: 'v' })).toEqual([{ v: null }, { v: 100 }])
  })
})

describe('topN', () => {
  const rows = [
    { name: 'a', v: 5 },
    { name: 'b', v: 50 },
    { name: 'c', v: 1 },
    { name: 'd', v: 20 },
    { name: 'e', v: 2 },
  ]

  it('keeps the n largest and collapses the rest into Other', () => {
    expect(topN(rows, { value: 'v', n: 2 })).toEqual([
      { name: 'b', v: 50 },
      { name: 'd', v: 20 },
      { name: 'Other', v: 8 },
    ])
  })

  it('uses a custom label and other name', () => {
    expect(topN(rows, { value: 'v', n: 3, other: 'Rest', label: 'name' }).at(-1)).toEqual({ name: 'Rest', v: 3 })
  })

  it('does not add Other when nothing is left over', () => {
    expect(topN(rows, { value: 'v', n: 5 })).toHaveLength(5)
    expect(topN(rows, { value: 'v', n: 9 }).map((r) => r.name)).toEqual(['b', 'd', 'a', 'e', 'c'])
  })
})
