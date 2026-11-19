import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import bar from './index'

chartSuite({
  module: bar,
  dir: import.meta.dirname,
  role: 'bar',
  marks: { basic: 5, realistic: 8, 'edge-single': 1, 'edge-duplicates': 5, 'edge-large': 5200, 'edge-zeros': 5 },
  summary: /^Bar chart, 5 bars, highest Bravo at 340, lowest Delta at 90$/,
  malformed: [
    { name: 'a text value', data: [{ label: 'A', value: 1 }, { label: 'B', value: 'abc' }], message: /field 'value' must be a number, got 'abc' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'mismatched columns', data: { label: ['a', 'b'], value: [1] }, message: /column 'value' has 1 values but column 'label' has 2/ },
    { name: 'a mapping to a missing column', data: [{ name: 'A', amount: 1 }], options: { fields: { label: 'name', value: 'amont' } }, message: /column 'amont' not found/ },
  ],
})

describe('bar: geometry', () => {
  it('bar heights are proportional to values and start at zero', () => {
    const el = host(640, 400)
    const h = bar.render(el, [{ label: 'a', value: 10 }, { label: 'b', value: 20 }, { label: 'c', value: -5 }], { animate: false })
    const rects = [...el.querySelectorAll('[data-role="bar"]')]
    const hs = rects.map((r) => Number(r.getAttribute('height')))
    expect(hs[1]! / hs[0]!).toBeCloseTo(2, 1)
    expect(hs[2]! / hs[0]!).toBeCloseTo(0.5, 1)
    // negative bar hangs below the zero line: its top is the baseline
    const zeroTop = Number(rects[2]!.getAttribute('y'))
    const posBottom = Number(rects[0]!.getAttribute('y')) + hs[0]!
    expect(zeroTop).toBeCloseTo(posBottom, 1)
    h.destroy()
  })
  it('sums duplicate labels and says so', () => {
    const el = host()
    const h = bar.render(el, [{ label: 'a', value: 1 }, { label: 'a', value: 2 }], { animate: false })
    expect(h.markCount()).toBe(1)
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 duplicate labels summed/)
    expect(h.exportCSV()).toBe('Label,Value\na,3')
    h.destroy()
  })
  it('sorts descending', () => {
    const el = host()
    const h = bar.render(el, [{ label: 'a', value: 1 }, { label: 'b', value: 3 }, { label: 'c', value: 2 }], { animate: false, sort: 'descending' })
    expect([...el.querySelectorAll('[data-role="bar"]')].map((r) => r.getAttribute('data-key'))).toEqual(['rect:b', 'rect:c', 'rect:a'])
    h.destroy()
  })
  it('switches to canvas above the threshold and keeps keyboard access', () => {
    const el = host()
    const data = Array.from({ length: 6000 }, (_, i) => ({ label: `k${i}`, value: i }))
    const h = bar.render(el, data, { animate: false })
    expect(h.renderer).toBe('canvas')
    expect(h.markCount()).toBe(6000)
    expect(el.querySelector('canvas.oc-canvas')).not.toBeNull()
    h.focusMark(5999)
    expect(el.querySelector('[aria-live]')!.textContent).toBe('6000 of 6000: k5999: 5,999')
    h.destroy()
  })
})
