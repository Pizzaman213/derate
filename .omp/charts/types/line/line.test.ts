import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import line from './index'

chartSuite({
  module: line,
  dir: import.meta.dirname,
  role: 'point',
  marks: { basic: 12, realistic: 120, 'edge-single': 1, 'edge-duplicates': 12, 'edge-large': 5200, 'edge-zeros': 12, 'edge-nulls': 6 },
  summary: /^Line chart, 12 points, Jan 2026 to Dec 2026, peak 48k in Nov 2026$/,
  malformed: [
    { name: 'a text value', data: [{ x: '2026-01-01', y: 1 }, { x: '2026-02-01', y: 'abc' }], message: /field 'y' must be a number, got 'abc' at row 2/ },
    { name: 'a bad date', data: [{ x: '2026-01-01', y: 1 }, { x: 'someday', y: 2 }], message: /field 'x' must be a date, got 'someday' at row 2/ },
    { name: 'a missing x', data: [{ y: 2 }], message: /field 'x' is missing at row 1/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
  ],
})

const pts = (el: HTMLElement) => [...el.querySelectorAll('[data-role="point"]')].map((c) => ({ x: Number(c.getAttribute('cx')), y: Number(c.getAttribute('cy')) }))
const linePath = (el: HTMLElement) => el.querySelector('[data-key^="path:line:"]')?.getAttribute('d') ?? ''

describe('line: geometry', () => {
  it('sorts by x and maps y linearly', () => {
    const el = host(640, 400)
    const h = line.render(el, [{ x: '2026-03-01', y: 30 }, { x: '2026-01-01', y: 10 }, { x: '2026-02-01', y: 20 }], { animate: false })
    const p = pts(el)
    expect(p.map((d) => d.x)).toEqual([...p.map((d) => d.x)].sort((a, b) => a - b))
    expect(p[1]!.y - p[0]!.y).toBeCloseTo(p[2]!.y - p[1]!.y, 3)
    expect(p[2]!.y).toBeLessThan(p[0]!.y)
    h.destroy()
  })
  it('lifts the pen at nulls, or zeroes / interpolates them', () => {
    const data = [{ x: 1, y: 1 }, { x: 2, y: 2 }, { x: 3, y: null }, { x: 4, y: 4 }, { x: 5, y: 5 }]
    const el = host(640, 400)
    const h = line.render(el, data, { animate: false })
    expect(linePath(el).match(/M/g)!.length).toBe(2)
    expect(h.markCount()).toBe(4)
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 missing values left as gaps/)
    h.update(data, { animate: false, gaps: 'interpolate' })
    expect(linePath(el).match(/M/g)!.length).toBe(1)
    const p = pts(el)
    expect(p[2]!.y).toBeCloseTo((p[1]!.y + p[3]!.y) / 2, 3)
    h.update(data, { animate: false, gaps: 'zero' })
    expect(h.markCount()).toBe(5)
    h.destroy()
  })
  it('accepts numeric x and keeps it numeric', () => {
    const el = host()
    const h = line.render(el, [{ x: 10, y: 1 }, { x: 20, y: 3 }], { animate: false })
    expect(h.exportCSV()).toBe('X,Value\n10,1\n20,3')
    expect(h.summary()).toMatch(/x 10 to 20, peak 3 in 20/)
    h.destroy()
  })
  it('sums duplicate dates and says so', () => {
    const el = host()
    const h = line.render(el, [{ x: '2026-01-01', y: 1 }, { x: '2026-01-01', y: 2 }], { animate: false })
    expect(h.markCount()).toBe(1)
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 duplicate dates summed/)
    h.destroy()
  })
  it('switches to canvas above 5,000 points', () => {
    const el = host()
    const data = Array.from({ length: 6000 }, (_, i) => ({ x: Date.UTC(2020, 0, 1) + i * 36e5, y: Math.sin(i / 50) }))
    const h = line.render(el, data, { animate: false })
    expect(h.renderer).toBe('canvas')
    expect(h.markCount()).toBe(6000)
    h.destroy()
  })
})
