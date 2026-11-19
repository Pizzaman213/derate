import { describe, expect, it } from 'vitest'
import { ewma, rollingMean, rollingMedian, rollingSum } from './rolling'

describe('rollingMean', () => {
  it('trails the window and needs a full window by default', () => {
    expect(rollingMean([1, 2, 3, 4, 5], 3)).toEqual([null, null, 2, 3, 4])
  })
  it('skips nulls and nulls out below minPeriods', () => {
    expect(rollingMean([1, null, 3, 4, null, null], 3)).toEqual([null, null, null, null, null, null])
    expect(rollingMean([1, null, 3, 4, null, null], 3, { minPeriods: 2 })).toEqual([null, null, 2, 3.5, 3.5, null])
    expect(rollingMean([1, null, 3, 4, null, null], 3, { minPeriods: 1 })).toEqual([1, 1, 2, 3.5, 3.5, 4])
  })
  it('centres odd and even windows like pandas', () => {
    expect(rollingMean([1, 2, 3, 4, 5], 3, { center: true })).toEqual([null, 2, 3, 4, null])
    expect(rollingMean([1, 2, 3, 4, 5], 3, { center: true, minPeriods: 1 })).toEqual([1.5, 2, 3, 4, 4.5])
    expect(rollingMean([0, 1, 2, 3, 4, 5], 4, { center: true })).toEqual([null, null, 1.5, 2.5, 3.5, null])
    expect(rollingMean([1, null, 3, null, 5], 3, { center: true, minPeriods: 2 })).toEqual([null, 2, null, 4, null])
  })
  it('rejects bad windows', () => {
    expect(() => rollingMean([1], 0)).toThrow(RangeError)
    expect(() => rollingMean([1], 2, { minPeriods: 3 })).toThrow(RangeError)
  })
})

describe('rollingSum / rollingMedian', () => {
  it('sums', () => {
    expect(rollingSum([1, 2, null, 4], 2, { minPeriods: 1 })).toEqual([1, 3, 2, 4])
    expect(rollingSum([1, 2, 3], 2)).toEqual([null, 3, 5])
  })
  it('takes medians', () => {
    expect(rollingMedian([5, 1, 9, 3, 7], 3)).toEqual([null, null, 5, 3, 7])
    expect(rollingMedian([5, 1, null, 3], 3, { minPeriods: 2 })).toEqual([null, 3, 3, 2])
  })
})

describe('ewma', () => {
  it('smooths with alpha, skipping nulls', () => {
    expect(ewma([10, 20, null, 40], 0.5)).toEqual([10, 15, null, 27.5])
    expect(ewma([null, 4, 8], 0.25)).toEqual([null, 4, 5])
    expect(ewma([3, 7], 1)).toEqual([3, 7])
  })
  it('rejects alpha outside (0, 1]', () => {
    expect(() => ewma([1], 0)).toThrow(RangeError)
    expect(() => ewma([1], 1.5)).toThrow(RangeError)
  })
})
