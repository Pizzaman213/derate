import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import splom from './index'

chartSuite({
  module: splom,
  dir: import.meta.dirname,
  role: 'point',
  // edge-large is canvas: the count there is every data mark (6,000 points + 67 histogram segments)
  marks: { basic: 360, realistic: 858, 'edge-single': 12, 'edge-duplicates': 720, 'edge-large': 6067 },
  summary: /^Scatter-plot matrix, 30 rows across 4 columns \(sepalLength, sepalWidth, petalLength and petalWidth\), 3 series$/,
  malformed: [
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'an unknown column', data: [{ a: 1, b: 2 }], options: { columns: ['a', 'z'] }, message: /column 'z' not found \(columns: a, b\)/ },
    { name: 'a text column picked', data: [{ a: 1, b: 'x' }], options: { columns: ['a', 'b'] }, message: /column 'b' is not numeric/ },
    { name: 'mismatched columns', data: { a: [1, 2], b: [1] }, message: /column 'b' has 1 values but column 'a' has 2/ },
  ],
})

const rows = [
  { a: 1, b: 10, c: 5 },
  { a: 2, b: 20, c: 3 },
  { a: 3, b: 30, c: 1 },
]
const pts = (el: Element) => [...el.querySelectorAll('[data-role="point"]')].map((p) => ({ key: p.getAttribute('data-key')!, x: Number(p.getAttribute('cx')), y: Number(p.getAttribute('cy')) }))

describe('splom: geometry', () => {
  it('draws k(k-1) scatters and k histograms', () => {
    const el = host(640, 640)
    const h = splom.render(el, rows, { animate: false })
    const bars = el.querySelectorAll('[data-role="bar"]').length
    expect(bars).toBeGreaterThanOrEqual(3)
    // every row is counted once in each column's histogram (bars come first in focus order)
    const counts = new Map<string, number>()
    for (let i = 0; i < bars; i++) {
      h.focusMark(i)
      const m = /^\d+ of \d+: (\w+) .*: (\d+) rows?$/.exec(el.querySelector('[aria-live]')!.textContent ?? '')!
      counts.set(m[1]!, (counts.get(m[1]!) ?? 0) + Number(m[2]))
    }
    expect(Object.fromEntries(counts)).toEqual({ a: 3, b: 3, c: 3 })
    h.destroy()
  })
  it('shares scales: the same row sits at the same x down a column and the same y along a row', () => {
    const el = host(640, 640)
    const h = splom.render(el, rows, { animate: false })
    const p = pts(el)
    const at = (cx: string, cy: string, i: number) => p.find((q) => q.key === `circle:p:${cx}×${cy}:${i}`)!
    for (let i = 0; i < 3; i++) {
      expect(at('a', 'b', i).x).toBeCloseTo(at('a', 'c', i).x, 6) // column a
      expect(at('a', 'b', i).y).toBeCloseTo(at('c', 'b', i).y, 6) // row b
    }
    // b rises with a; c falls with a
    expect(at('a', 'b', 2).y).toBeLessThan(at('a', 'b', 0).y)
    expect(at('a', 'c', 2).y).toBeGreaterThan(at('a', 'c', 0).y)
    h.destroy()
  })
  it('caps at 6 columns by default and honours columns', () => {
    const wide = [{ a: 1, b: 2, c: 3, d: 4, e: 5, f: 6, g: 7, h: 8 }, { a: 2, b: 1, c: 4, d: 3, e: 6, f: 5, g: 8, h: 7 }]
    const el = host(640, 640)
    const h = splom.render(el, wide, { animate: false })
    expect(pts(el).length).toBe(6 * 5 * 2)
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/2 more numeric columns not shown/)
    h.update(wide, { columns: ['h', 'a'] })
    expect(pts(el).length).toBe(2 * 1 * 2)
    expect(h.summary()).toMatch(/2 columns \(h and a\)/)
    h.destroy()
  })
  it('hides a series from the scatters and the histograms', () => {
    const el = host(640, 640)
    const h = splom.render(el, [{ series: 'x', a: 1, b: 2 }, { series: 'y', a: 2, b: 1 }, { series: 'y', a: 3, b: 3 }], { animate: false })
    expect(pts(el).length).toBe(6)
    ;(el.querySelector('.oc-legend button[data-key="y"]') as HTMLButtonElement).click()
    expect(pts(el).length).toBe(2)
    expect([...el.querySelectorAll('[data-role="bar"]')].every((b) => b.getAttribute('data-series') === 'x')).toBe(true)
    h.destroy()
  })
})
