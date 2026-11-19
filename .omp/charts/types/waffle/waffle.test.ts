import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import { largestRemainder } from '../_shared/PartWholeA'
import waffle from './index'

chartSuite({
  module: waffle,
  dir: import.meta.dirname,
  role: 'cell',
  empty: ['edge-empty', 'edge-zeros'],
  marks: { basic: 100, realistic: 100, 'edge-single': 100, 'edge-negatives': 100, 'edge-large': 6400 },
  summary: /^Waffle chart, 5 parts totalling 1,035, largest Bravo at 33%, smallest Delta at 8\.7%, Bravo fills 33 of 100 squares$/,
  malformed: [
    { name: 'a text value', data: [{ label: 'A', value: 1 }, { label: 'B', value: 'abc' }], message: /field 'value' must be a number, got 'abc' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
  ],
})

describe('waffle: largest remainder', () => {
  it('always sums exactly to the cell count', () => {
    let seed = 3
    const rnd = () => ((seed = (seed * 16807) % 2147483647) / 2147483647)
    for (let k = 0; k < 300; k++) {
      const vals = Array.from({ length: 1 + Math.floor(rnd() * 12) }, () => rnd() * 1000)
      const cells = [4, 50, 100, 400, 6400][k % 5]!
      const out = largestRemainder(vals, cells)
      expect(out.reduce((s, v) => s + v, 0)).toBe(cells)
      const sum = vals.reduce((s, v) => s + v, 0)
      // each count is its exact quota rounded down or up
      out.forEach((n, i) => { const q = (vals[i]! / sum) * cells; expect(n).toBeGreaterThanOrEqual(Math.floor(q)); expect(n).toBeLessThanOrEqual(Math.ceil(q)) })
    }
  })
  it('hands leftover cells to the largest remainders', () => {
    // thirds of 100: 33.33 each, one leftover goes to the first (tie -> earlier)
    expect(largestRemainder([1, 1, 1], 100)).toEqual([34, 33, 33])
    // 0.5 / 0.3 / 0.2 of 10 are exact
    expect(largestRemainder([5, 3, 2], 10)).toEqual([5, 3, 2])
    // 12.6 / 37.4 / 50 -> remainders .6 and .4: 13, 37, 50
    expect(largestRemainder([12.6, 37.4, 50], 100)).toEqual([13, 37, 50])
    expect(largestRemainder([0, 0], 100)).toEqual([0, 0])
  })
  it('draws exactly cells squares, and the legend toggle re-allocates them', () => {
    const el = host()
    const h = waffle.render(el, [{ label: 'a', value: 1 }, { label: 'b', value: 1 }, { label: 'c', value: 2 }], { animate: false })
    expect(h.markCount()).toBe(100)
    expect(el.querySelectorAll('[data-series="c"][data-mark]').length).toBe(50)
    ;(el.querySelector('.oc-legend button[data-key="c"]') as HTMLElement).click()
    expect(h.markCount()).toBe(100)
    expect(el.querySelectorAll('[data-series="a"][data-mark]').length).toBe(50)
    h.update([{ label: 'a', value: 1 }, { label: 'b', value: 2 }], { cells: 50, columns: 10 })
    expect(h.markCount()).toBe(50)
    h.destroy()
  })
})
