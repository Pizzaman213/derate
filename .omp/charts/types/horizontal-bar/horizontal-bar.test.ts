import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import horizontalBar from './index'

chartSuite({
  module: horizontalBar,
  dir: import.meta.dirname,
  role: 'bar',
  marks: { basic: 5, realistic: 12, 'edge-single': 1, 'edge-duplicates': 5, 'edge-large': 5200, 'edge-zeros': 5, 'edge-nulls': 2 },
  summary: /^Horizontal bar chart, 5 bars, highest Bravo at 340, lowest Delta at 90$/,
  malformed: [
    { name: 'a text value', data: [{ label: 'A', value: 1 }, { label: 'B', value: 'abc' }], message: /field 'value' must be a number, got 'abc' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'mismatched columns', data: { label: ['a', 'b'], value: [1] }, message: /column 'value' has 1 values but column 'label' has 2/ },
    { name: 'a mapping to a missing column', data: [{ name: 'A', amount: 1 }], options: { fields: { label: 'name', value: 'amont' } }, message: /column 'amont' not found/ },
  ],
})

const bars = (el: Element) => [...el.querySelectorAll('[data-role="bar"]')]
const num = (e: Element, a: string) => Number(e.getAttribute(a))

describe('horizontal-bar: geometry', () => {
  it('bar widths are proportional to values; negatives extend left of zero', () => {
    const el = host(640, 400)
    const h = horizontalBar.render(el, [{ label: 'a', value: 10 }, { label: 'b', value: 20 }, { label: 'c', value: -5 }], { animate: false })
    const r = bars(el)
    const w = r.map((e) => num(e, 'width'))
    expect(w[1]! / w[0]!).toBeCloseTo(2, 1)
    expect(w[2]! / w[0]!).toBeCloseTo(0.5, 1)
    // all bars share one zero baseline: positive bars start where the negative one ends
    expect(num(r[2]!, 'x') + w[2]!).toBeCloseTo(num(r[0]!, 'x'), 1)
    // categories run top to bottom with equal heights
    expect(num(r[0]!, 'y')).toBeLessThan(num(r[1]!, 'y'))
    expect(num(r[0]!, 'height')).toBeCloseTo(num(r[1]!, 'height'), 5)
    h.destroy()
  })
  it('sorts descending, top to bottom', () => {
    const el = host()
    const h = horizontalBar.render(el, [{ label: 'a', value: 1 }, { label: 'b', value: 3 }, { label: 'c', value: 2 }], { animate: false, sort: 'descending' })
    const order = bars(el).sort((p, q) => num(p, 'y') - num(q, 'y')).map((e) => e.getAttribute('data-key'))
    expect(order).toEqual(['rect:b', 'rect:c', 'rect:a'])
    h.destroy()
  })
  it('labels long names horizontally, truncated with the full text kept', () => {
    const el = host(400, 300)
    const long = 'Department of Redundancy Department, Northern Region'
    const h = horizontalBar.render(el, [{ label: long, value: 3 }, { label: 'b', value: 1 }], { animate: false, labelWidth: 120 })
    const t = [...el.querySelectorAll('text')].find((e) => e.getAttribute('data-full') === long || e.textContent?.startsWith('Department'))!
    expect(t.getAttribute('transform') ?? '').not.toMatch(/rotate/)
    expect(t.textContent!.length).toBeLessThan(long.length)
    h.destroy()
  })
  it('prints values past the bar end when asked', () => {
    const el = host()
    const h = horizontalBar.render(el, [{ label: 'a', value: 1200 }, { label: 'b', value: 300 }], { animate: false, showValues: true })
    const texts = [...el.querySelectorAll('text')].map((e) => e.textContent)
    expect(texts).toContain('1,200')
    expect(texts).toContain('300')
    h.destroy()
  })
  it('sums duplicates with a note, and uses canvas above the threshold', () => {
    const el = host()
    const h = horizontalBar.render(el, [{ label: 'a', value: 1 }, { label: 'a', value: 2 }], { animate: false })
    expect(h.exportCSV()).toBe('Label,Value\na,3')
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 duplicate labels summed/)
    h.update(Array.from({ length: 6000 }, (_, i) => ({ label: `k${i}`, value: i })))
    expect(h.renderer).toBe('canvas')
    expect(h.markCount()).toBe(6000)
    h.destroy()
  })
})
