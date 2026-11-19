import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import { makeFormatters } from '../../core'
import sparkline, { sparkPoints } from './index'

chartSuite({
  module: sparkline,
  dir: import.meta.dirname,
  role: 'point',
  marks: { basic: 12, realistic: 30, 'edge-single': 1, 'edge-large': 5200, 'edge-duplicates': 24, 'edge-index': 10 },
  summary: /^Sparkline, 12 points, Jan 2026 to Dec 2026, first 10, last 18, min 8 at Feb 2026, max 20 at Nov 2026$/,
  malformed: [
    { name: 'a text value', data: [{ x: 1, y: 1 }, { x: 2, y: 'abc' }], message: /field 'y' must be a number, got 'abc' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'an unreadable x', data: [{ x: 'someday', y: 1 }], message: /field 'x' must be a date, got 'someday' at row 1/ },
  ],
})

const fmt = makeFormatters()

describe('sparkline: geometry', () => {
  it('sorts dates and keeps the line inside the box', () => {
    const el = host(120, 40)
    const h = sparkline.render(el, [{ x: '2026-03-01', y: 5 }, { x: '2026-01-01', y: 1 }, { x: '2026-02-01', y: 9 }], { animate: false })
    const pts = [...el.querySelectorAll('[data-role="point"]')]
    const xs = pts.map((p) => Number(p.getAttribute('cx')))
    expect(xs[0]!).toBeLessThan(xs[1]!)
    expect(xs[1]!).toBeLessThan(xs[2]!)
    const H = Number(el.querySelector('svg')!.getAttribute('height'))
    for (const p of pts) {
      const cx = Number(p.getAttribute('cx')), cy = Number(p.getAttribute('cy')), r = Number(p.getAttribute('r'))
      expect(cx - r).toBeGreaterThanOrEqual(0)
      expect(cx + r).toBeLessThanOrEqual(120)
      expect(cy - r).toBeGreaterThanOrEqual(0)
      expect(cy + r).toBeLessThanOrEqual(H)
    }
    // max (Feb, 9) is the topmost point, min (Jan, 1) the lowest
    const ys = pts.map((p) => Number(p.getAttribute('cy')))
    expect(Math.min(...ys)).toBe(ys[1])
    expect(Math.max(...ys)).toBe(ys[0])
    h.destroy()
  })
  it('shows the last, min and max dots and hides the rest', () => {
    const el = host(300, 40)
    const h = sparkline.render(el, { y: [4, 2, 6, 5, 3] }, { animate: false })
    const shown = [...el.querySelectorAll('[data-role="point"]')].filter((p) => p.getAttribute('style')!.includes('fill-opacity:1'))
    expect(shown.length).toBe(3)
    h.destroy()
  })
  it('breaks the line at nulls', () => {
    const el = host(300, 40)
    const h = sparkline.render(el, { y: [1, 2, null, 4, 5] }, { animate: false })
    const d = el.querySelector('[data-role="line"]')!.getAttribute('d')!
    expect(d.match(/M/g)!.length).toBe(2)
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 missing values left as gaps/)
    h.destroy()
  })
  it('reads numeric x as an index and missing x as row order', () => {
    expect(sparkPoints([{ x: 3, y: 1 }, { x: 1, y: 2 }], fmt).map((p) => p.t)).toEqual([1, 3])
    expect(sparkPoints([{ x: undefined, y: 1 }, { x: undefined, y: 2 }], fmt).map((p) => p.at)).toEqual(['#1', '#2'])
  })
  it('auto height is word-sized and fits 120px', () => {
    const el = host(120, 0)
    const h = sparkline.render(el, { y: [1, 3, 2] }, { animate: false })
    const svg = el.querySelector('svg')!
    expect(Number(svg.getAttribute('height'))).toBeLessThanOrEqual(48)
    expect(svg.getAttribute('width')).toBe('120')
    h.destroy()
  })
})
