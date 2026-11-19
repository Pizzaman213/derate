import { describe, expect, it } from 'vitest'
import { stats } from '../../core'
import { chartSuite, host } from '../../tests/harness'
import ecdf from './index'

chartSuite({
  module: ecdf,
  dir: import.meta.dirname,
  role: 'step',
  marks: { 'edge-single': 1, 'edge-zeros': 1, 'edge-large': 5300 },
  summary: /^Empirical CDF, 30 values, from \d+ to \d+, median \d+(\.\d+)?$/,
  malformed: [
    { name: 'a text value', data: [{ value: 1 }, { value: 'abc' }], message: /field 'value' must be a number, got 'abc' at row 2/ },
    { name: 'a scalar', data: 'just text', message: /.+/ },
    { name: 'a mapping to a missing column', data: [{ ms: 1 }], options: { fields: { value: 'msec' } }, message: /column 'msec' not found/ },
  ],
})

const steps = (el: Element) => [...el.querySelectorAll('[data-role="step"]')].map((c) => ({ x: Number(c.getAttribute('cx')), y: Number(c.getAttribute('cy')) }))

describe('ecdf: math', () => {
  const values = [3, 1, 4, 1, 5, 9, 2, 6, 5, 3, 5]
  it('stats.ecdf ends at p = 1, is strictly increasing, and collapses ties', () => {
    const e = stats.ecdf(values)
    expect(e[e.length - 1]!.p).toBe(1)
    for (let i = 1; i < e.length; i++) {
      expect(e[i]!.x).toBeGreaterThan(e[i - 1]!.x)
      expect(e[i]!.p).toBeGreaterThan(e[i - 1]!.p)
    }
    expect(e.map((s) => s.x)).toEqual([1, 2, 3, 4, 5, 6, 9])
    // 5 appears 3 times: F(5) = 9/11
    expect(e.find((s) => s.x === 5)!.p).toBeCloseTo(9 / 11, 12)
  })
  it('draws one step per distinct value, monotone up the plot, the last at 100%', () => {
    const el = host(640, 400)
    const h = ecdf.render(el, values.map((value) => ({ value })), { animate: false })
    const s = steps(el)
    expect(s).toHaveLength(7)
    for (let i = 1; i < s.length; i++) {
      expect(s[i]!.x).toBeGreaterThan(s[i - 1]!.x)
      expect(s[i]!.y).toBeLessThan(s[i - 1]!.y) // higher share is higher up
    }
    const csv = h.exportCSV().trim().split('\n')
    expect(csv[0]).toBe('Value,Count ≤ value,Share ≤ value')
    expect(csv[csv.length - 1]).toBe('9,11,1')
    h.destroy()
  })
  it('the step path rises exactly at each value and spans the plot', () => {
    const el = host(640, 400)
    const h = ecdf.render(el, [{ value: 1 }, { value: 2 }], { animate: false })
    const d = el.querySelector('[data-key="path:curve:"]')!.getAttribute('d')!
    const s = steps(el)
    expect(d).toContain(`H${s[0]!.x}V${s[0]!.y}`)
    expect(d).toContain(`H${s[1]!.x}V${s[1]!.y}`)
    h.destroy()
  })
  it('one curve and one legend item per series; hiding a series drops its curve', () => {
    const el = host(640, 400)
    const data = [1, 2, 3, 4].flatMap((v) => [{ value: v, series: 'a' }, { value: v * 2, series: 'b' }])
    const h = ecdf.render(el, data, { animate: false })
    expect(el.querySelectorAll('.oc-legend button').length).toBe(2)
    expect(h.markCount()).toBe(8)
    ;(el.querySelector('.oc-legend button[data-key="b"]') as HTMLElement).click()
    expect(h.markCount()).toBe(4)
    expect(el.querySelector('[data-key="path:curve:b"]')).toBeNull()
    expect(el.querySelector('[data-key="path:curve:a"]')).not.toBeNull()
    h.destroy()
  })
  it('announces the share at each step', () => {
    const el = host(640, 400)
    const h = ecdf.render(el, [{ value: 10 }, { value: 20 }, { value: 20 }, { value: 30 }], { animate: false })
    h.focusMark(1)
    expect(el.querySelector('[aria-live]')!.textContent).toBe('2 of 3: 75% of values ≤ 20 (3 of 4)')
    h.destroy()
  })
})
