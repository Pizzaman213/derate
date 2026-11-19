import { describe, expect, it } from 'vitest'
import { binValues } from './bin'

const range = (n: number): number[] => Array.from({ length: n }, (_, i) => i)

describe('binValues', () => {
  it('puts 0..99 into 10 bins of 10', () => {
    const bins = binValues(range(100), { count: 10 })
    expect(bins).toHaveLength(10)
    expect(bins.map((b) => b.count)).toEqual(Array(10).fill(10))
    expect(bins.map((b) => [b.x0, b.x1])).toEqual(range(10).map((i) => [i * 10, i * 10 + 10]))
    expect(bins[3]?.values).toEqual([30, 31, 32, 33, 34, 35, 36, 37, 38, 39])
  })

  it('counts every finite input once and ignores null/NaN/Infinity', () => {
    const vals = [1.2, null, 3.7, Number.NaN, 9.9, Number.POSITIVE_INFINITY, 4.4, 0.1, 7]
    for (const method of ['sturges', 'freedman-diaconis', 'scott'] as const) {
      const bins = binValues(vals, { method })
      expect(bins.reduce((s, b) => s + b.count, 0)).toBe(6)
    }
  })

  it('includes the maximum in the last bin', () => {
    const bins = binValues([0, 5, 10], { count: 2 })
    expect(bins.map((b) => b.count)).toEqual([1, 2])
    expect(bins.at(-1)?.values).toContain(10)
  })

  it('nices an awkward domain', () => {
    const bins = binValues([0.3, 9.6], { count: 5 })
    expect(bins[0]?.x0).toBe(0)
    expect(bins.at(-1)?.x1).toBe(10)
  })

  it('uses explicit thresholds', () => {
    const bins = binValues([1, 2, 3, 4, 5, 6], { thresholds: [3, 5] })
    expect(bins.map((b) => [b.x0, b.x1, b.count])).toEqual([
      [1, 3, 2],
      [3, 5, 2],
      [5, 6, 2],
    ])
  })

  it('drops values outside an explicit domain', () => {
    const bins = binValues([-5, 0, 1, 2, 3, 15], { domain: [0, 4], count: 2 })
    expect(bins.reduce((s, b) => s + b.count, 0)).toBe(4)
  })

  it('handles one repeated value and no values', () => {
    const bins = binValues([7, 7, 7])
    expect(bins.reduce((s, b) => s + b.count, 0)).toBe(3)
    expect(binValues([null, Number.NaN])).toEqual([])
  })
})
