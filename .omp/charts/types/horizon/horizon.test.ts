import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import horizon, { fold, horizonSeries } from './index'

chartSuite({
  module: horizon,
  dir: import.meta.dirname,
  role: 'cell',
  marks: { basic: 72, realistic: 720, 'edge-single': 1, 'edge-large': 5200 },
  summary: /^Horizon chart, 3 series, Jan 2026 to Dec 2027, 3 bands of [\d.]+, highest \w+ at [\d.]+ on \w+ 20\d\d, lowest \w+ at -[\d.]+ on \w+ 20\d\d$/,
  malformed: [
    { name: 'a text value', data: [{ x: '2026-01-01', y: 'abc' }], message: /field 'y' must be a number, got 'abc' at row 1/ },
    { name: 'a bad date', data: [{ x: 'yesterday', y: 1 }], message: /field 'x' must be a date, got 'yesterday' at row 1/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
  ],
})

describe('horizon: band folding', () => {
  it('fills bands bottom-up, one slice each', () => {
    expect(fold(0, 3, 30)).toEqual([0, 0, 0])
    expect(fold(5, 3, 30)).toEqual([0.5, 0, 0])
    expect(fold(10, 3, 30)).toEqual([1, 0, 0])
    expect(fold(25, 3, 30)).toEqual([1, 1, 0.5])
    expect(fold(30, 3, 30)).toEqual([1, 1, 1])
    expect(fold(99, 3, 30)).toEqual([1, 1, 1])
  })
  it('folds negatives by magnitude', () => {
    expect(fold(-15, 3, 30)).toEqual([1, 0.5, 0])
  })
  it('band heights sum to |v| / band size', () => {
    for (const v of [0.3, 7, 12.5, 29]) expect(fold(v, 4, 40).reduce((a, b) => a + b, 0)).toBeCloseTo(v / 10)
  })
  it('one row per series, points sorted by time', () => {
    const s = horizonSeries([
      { x: new Date('2026-02-01'), y: 1, series: 'a' },
      { x: new Date('2026-01-01'), y: 2, series: 'a' },
      { x: new Date('2026-01-01'), y: 3, series: 'b' },
    ])
    expect(s.map((d) => d.name)).toEqual(['a', 'b'])
    expect(s[0]!.pts.map((p) => p.v)).toEqual([2, 1])
  })
  it('draws positive and negative bands in separate hues, never past the row', () => {
    const el = host(600, 200)
    const h = horizon.render(el, [{ x: '2026-01-01', y: -30 }, { x: '2026-01-02', y: 30 }, { x: '2026-01-03', y: 15 }], { animate: false })
    const bands = [...el.querySelectorAll('[data-role="band"]')]
    const keys = bands.map((b) => b.getAttribute('data-key')!)
    expect(keys.filter((k) => k.includes(':1:')).length).toBe(3)
    expect(keys.filter((k) => k.includes(':-1:')).length).toBe(3)
    const cell = el.querySelector('[data-role="cell"]')!
    const top = Number(cell.getAttribute('y')), bottom = top + Number(cell.getAttribute('height'))
    for (const b of bands) for (const m of b.getAttribute('d')!.matchAll(/,(-?[\d.]+)/g)) {
      expect(Number(m[1])).toBeGreaterThanOrEqual(top - 0.01)
      expect(Number(m[1])).toBeLessThanOrEqual(bottom + 0.01)
    }
    h.destroy()
  })
})
