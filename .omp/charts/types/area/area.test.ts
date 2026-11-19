import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import area from './index'

chartSuite({
  module: area,
  dir: import.meta.dirname,
  role: 'point',
  marks: { basic: 12, realistic: 72, 'edge-single': 1, 'edge-duplicates': 12, 'edge-large': 5200 },
  summary: /^Area chart, 12 points, Jan 2026 to Dec 2026, peak 44k in Dec 2026$/,
  malformed: [
    { name: 'a text value', data: [{ x: '2026-01-01', y: 'abc' }], message: /field 'y' must be a number, got 'abc' at row 1/ },
    { name: 'a bad date', data: [{ x: 'tomorrow-ish', y: 1 }], message: /field 'x' must be a date, got 'tomorrow-ish' at row 1/ },
  ],
})

describe('area: fill', () => {
  it('fills down to the zero line', () => {
    const el = host(640, 400)
    const h = area.render(el, [{ x: 1, y: 10 }, { x: 2, y: 20 }], { animate: false })
    const d = el.querySelector('[data-key^="path:area:"]')!.getAttribute('d')!
    const ys = [...d.matchAll(/[ML]\s*([-\d.]+)[ ,]([-\d.]+)/g)].map((m) => Number(m[2]))
    const base = Math.max(...ys)
    const pts = [...el.querySelectorAll('[data-role="point"]')].map((c) => Number(c.getAttribute('cy')))
    // the bottom edge is y=0: the 10-point sits halfway between 0 and the 20-point
    expect((base - pts[0]!) / (base - pts[1]!)).toBeCloseTo(0.5, 2)
    h.destroy()
  })
  it('splits the fill at a null gap', () => {
    const el = host(640, 400)
    const h = area.render(el, [{ x: 1, y: 1 }, { x: 2, y: 2 }, { x: 3, y: null }, { x: 4, y: 3 }, { x: 5, y: 2 }], { animate: false })
    expect(el.querySelector('[data-key^="path:area:"]')!.getAttribute('d')!.match(/M/g)!.length).toBe(2)
    h.destroy()
  })
})
