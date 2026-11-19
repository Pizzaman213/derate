import { describe, expect, it } from 'vitest'
import { chartSuite, host, loadSamples } from '../../tests/harness'
import type { ChartHandle } from '../../core'
import hexbin, { autoHexRadius, countShare } from './index'

chartSuite({
  module: hexbin,
  dir: import.meta.dirname,
  role: 'hex',
  marks: { 'edge-single': 1, 'edge-zeros': 1 },
  summary: /^Hexbin plot, 300 points, x from -?[\d.]+ to [\d.]+, y from -?[\d.]+ to [\d.]+, (strong|moderate) positive correlation \(r = 0\.\d+\)$/,
  malformed: [
    { name: 'a text value', data: [{ x: 1, y: 2 }, { x: 2, y: 'lots' }], message: /field 'y' must be a number, got 'lots' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'mismatched columns', data: { x: [1, 2], y: [1] }, message: /column 'y' has 1 values but column 'x' has 2/ },
  ],
})

/** Each hexagon's count, read back from its keyboard announcement ("3 of 40: Around x 1, y 2: 14 points"). */
const counts = (el: Element, h: ChartHandle) => Array.from({ length: h.markCount() }, (_, i) => {
  h.focusMark(i)
  const m = /: ([\d,]+) points?$/.exec(el.querySelector('[aria-live]')!.textContent ?? '')
  return m ? Number(m[1]!.replace(/,/g, '')) : NaN
})

describe('hexbin: counts', () => {
  for (const s of loadSamples(import.meta.dirname).filter((s) => !s.name.includes('empty'))) {
    it(`${s.name}: hexagon counts sum to the number of drawable points`, () => {
      const v = hexbin.validate(s.data, s.options)
      const rows = v.data as Array<{ x: number | null; y: number | null }>
      const n = rows.filter((r) => r.x !== null && r.y !== null).length
      const el = host(640, 400)
      const h = hexbin.render(el, s.data, { animate: false, ...s.options, renderer: 'svg' })
      const c = counts(el, h)
      expect(c.every(Number.isFinite)).toBe(true)
      expect(c.reduce((a, b) => a + b, 0)).toBe(n)
      h.destroy()
      el.remove()
    })
  }
  it('two far-apart clusters land in different hexagons', () => {
    const el = host(640, 400)
    const data = [...Array.from({ length: 5 }, () => ({ x: 0, y: 0 })), ...Array.from({ length: 3 }, () => ({ x: 10, y: 10 }))]
    const h = hexbin.render(el, data, { animate: false })
    expect(counts(el, h).sort()).toEqual([3, 5])
    expect(el.querySelector('.oc-gradient')!.textContent).toMatch(/1.*5/)
    h.destroy()
  })
  it('colour share: densest is 1, single points are 0, log lifts sparse bins', () => {
    expect(countShare(10, 10, 'linear')).toBe(1)
    expect(countShare(1, 10, 'linear')).toBe(0)
    expect(countShare(1, 1, 'linear')).toBe(1)
    expect(countShare(3, 100, 'log')).toBeGreaterThan(countShare(3, 100, 'linear'))
  })
  it('radius option and auto radius', () => {
    expect(autoHexRadius(100)).toBe(5)
    expect(autoHexRadius(400)).toBe(10)
    expect(autoHexRadius(2000)).toBe(16)
    const el = host(640, 400)
    const data = Array.from({ length: 400 }, (_, i) => ({ x: (i * 37) % 101, y: (i * 53) % 97 }))
    const small = hexbin.render(el, data, { animate: false, radius: 4 })
    const nSmall = small.markCount()
    small.destroy()
    const big = hexbin.render(el, data, { animate: false, radius: 30 })
    expect(big.markCount()).toBeLessThan(nSmall)
    big.destroy()
  })
})
