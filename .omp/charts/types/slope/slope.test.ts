import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import { nudge, placeLabels } from '../_shared/TrendB'
import slope from './index'

chartSuite({
  module: slope,
  dir: import.meta.dirname,
  role: 'slope',
  marks: { basic: 5, realistic: 13, 'edge-single': 1, 'edge-duplicates': 10, 'edge-large': 5200 },
  summary: /^Slope chart, 5 items, 2024 to 2025, 3 rose and 2 fell, biggest rise Echo \(\+50\), biggest fall Charlie \(−20\)$/,
  malformed: [
    { name: 'a text value', data: [{ label: 'A', start: 1, end: 'abc' }], message: /field 'end' must be a number, got 'abc' at row 1/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'a mapping to a missing column', data: [{ name: 'A', a: 1, b: 2 }], options: { fields: { label: 'name', start: 'a', end: 'bb' } }, message: /column 'bb' not found/ },
  ],
})

const labelsOf = (el: Element, side: 'start' | 'end') =>
  [...el.querySelectorAll('[data-role="slope-label"]')].filter((t) => t.getAttribute('data-key')!.includes(`l${side}:`)).map((t) => Number(t.getAttribute('y'))).sort((a, b) => a - b)

describe('slope: geometry', () => {
  it('a rise slopes up and a fall slopes down', () => {
    const el = host(600, 400)
    const h = slope.render(el, [{ label: 'up', start: 1, end: 5 }, { label: 'down', start: 5, end: 1 }], { animate: false })
    const [a, b] = [...el.querySelectorAll('[data-role="slope"]')]
    expect(Number(a!.getAttribute('y2'))).toBeLessThan(Number(a!.getAttribute('y1')))
    expect(Number(b!.getAttribute('y2'))).toBeGreaterThan(Number(b!.getAttribute('y1')))
    h.destroy()
  })
  it('nudges labels of near-equal values apart so they never overlap', () => {
    const el = host(600, 300)
    const data = Array.from({ length: 8 }, (_, i) => ({ label: `Item ${i}`, start: 50 + i * 0.01, end: 50 - i * 0.01 }))
    const h = slope.render(el, [...data, { label: 'far', start: 0, end: 100 }], { animate: false })
    for (const side of ['start', 'end'] as const) {
      const ys = labelsOf(el, side)
      expect(ys.length).toBe(9)
      for (let i = 1; i < ys.length; i++) expect(ys[i]! - ys[i - 1]!).toBeGreaterThanOrEqual(11)
      for (const y of ys) { expect(y).toBeGreaterThan(0); expect(y).toBeLessThan(300) }
    }
    h.destroy()
  })
  it('drops labels it cannot fit and keeps the biggest movers', () => {
    const el = host(400, 220)
    const data = Array.from({ length: 60 }, (_, i) => ({ label: `k${i}`, start: i, end: i + (i === 30 ? 50 : 1) }))
    const h = slope.render(el, data, { animate: false })
    const ys = labelsOf(el, 'end')
    expect(ys.length).toBeLessThan(60)
    for (let i = 1; i < ys.length; i++) expect(ys[i]! - ys[i - 1]!).toBeGreaterThanOrEqual(11)
    expect([...el.querySelectorAll('[data-role="slope-label"]')].some((t) => t.textContent!.includes('k30'))).toBe(true)
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/labels hidden for space/)
    h.destroy()
  })
  it('nudge keeps order, spacing and bounds', () => {
    const out = nudge([10, 10, 10, 50], 10, 0, 100)!
    expect(out[1]! - out[0]!).toBeCloseTo(10)
    expect(out[2]! - out[1]!).toBeCloseTo(10)
    expect(out[3]).toBe(50)
    expect(nudge([0, 0, 0], 10, 0, 100)![0]).toBe(0)
    expect(nudge([0, 0, 0], 60, 0, 100)).toBeNull()
    expect(placeLabels([0, 0, 0], [1, 2, 3], 60, 0, 100).size).toBe(2)
  })
  it('leaves out rows missing an end and says so', () => {
    const el = host()
    const h = slope.render(el, [{ label: 'a', start: 1, end: 2 }, { label: 'b', start: null, end: 2 }], { animate: false })
    expect(h.markCount()).toBe(1)
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 rows without both values left out/)
    h.destroy()
  })
})
