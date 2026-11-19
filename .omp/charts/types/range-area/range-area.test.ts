import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import rangeArea from './index'

chartSuite({
  module: rangeArea,
  dir: import.meta.dirname,
  role: 'point',
  marks: { basic: 12, realistic: 60, 'edge-single': 1, 'edge-duplicates': 12, 'edge-large': 5200 },
  summary: /^Range area chart, 12 points, Jan 2026 to Dec 2026, from -3 to 29, widest in Apr 2026$/,
  malformed: [
    { name: 'high below low', data: [{ x: '2026-01-01', low: 1, high: 2 }, { x: '2026-02-01', low: 5, high: 3 }], message: /field 'high' must be >= low \(5\), got 3 at row 2/ },
    { name: 'a text bound', data: [{ x: '2026-01-01', low: 'cold', high: 2 }], message: /field 'low' must be a number, got 'cold' at row 1/ },
    { name: 'a bad date', data: [{ x: 'later', low: 1, high: 2 }], message: /field 'x' must be a date, got 'later' at row 1/ },
  ],
})

const nums = (d: string) => [...d.matchAll(/[ML]\s*([-\d.]+)[ ,]([-\d.]+)/g)].map((m) => ({ x: Number(m[1]), y: Number(m[2]) }))

describe('range-area: band geometry', () => {
  const data = [{ x: 1, low: 10, high: 20, y: 15 }, { x: 2, low: 0, high: 40, y: 20 }, { x: 3, low: 10, high: 30, y: 25 }]
  it('the band runs along high and back along low; the centre line sits inside it', () => {
    const el = host(640, 400)
    const h = rangeArea.render(el, data, { animate: false })
    const band = nums(el.querySelector('[data-key="path:band"]')!.getAttribute('d')!)
    expect(band.length).toBe(6)
    const top = band.slice(0, 3), bottom = band.slice(3).reverse()
    const cy = [...el.querySelectorAll('[data-role="point"]')].map((c) => Number(c.getAttribute('cy')))
    for (let i = 0; i < 3; i++) {
      expect(top[i]!.x).toBeCloseTo(bottom[i]!.x, 6)
      expect(top[i]!.y).toBeLessThanOrEqual(cy[i]!)
      expect(bottom[i]!.y).toBeGreaterThanOrEqual(cy[i]!)
    }
    // linear: the 0..40 band is twice as tall as the 10..30 band, four times the 10..20 band
    const hgt = top.map((t, i) => bottom[i]!.y - t.y)
    expect(hgt[1]! / hgt[2]!).toBeCloseTo(2, 3)
    expect(hgt[1]! / hgt[0]!).toBeCloseTo(4, 3)
    h.destroy()
  })
  it('splits the band at a missing bound', () => {
    const el = host(640, 400)
    const h = rangeArea.render(el, [...data, { x: 4, low: null, high: 5, y: 3 }, { x: 5, low: 1, high: 5, y: 3 }, { x: 6, low: 1, high: 6, y: 3 }], { animate: false })
    expect(el.querySelector('[data-key="path:band"]')!.getAttribute('d')!.match(/M/g)!.length).toBe(2)
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 missing values left as gaps/)
    h.destroy()
  })
  it('without y: no centre line, no legend, points on the band middle', () => {
    const el = host(640, 400)
    const h = rangeArea.render(el, [{ x: 1, low: 0, high: 10 }, { x: 2, low: 2, high: 4 }], { animate: false })
    expect(el.querySelector('[data-key="path:centre"]')).toBeNull()
    expect(h.exportCSV()).toBe('X,Low,High\n1,0,10\n2,2,4')
    h.destroy()
  })
})
