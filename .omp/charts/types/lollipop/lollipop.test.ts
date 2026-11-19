import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import lollipop from './index'

chartSuite({
  module: lollipop,
  dir: import.meta.dirname,
  role: 'point',
  marks: { basic: 5, realistic: 14, 'edge-single': 1, 'edge-duplicates': 5, 'edge-zeros': 5, 'edge-nulls': 2, 'edge-large': 5200 },
  summary: /^Lollipop chart, 5 items, highest Bravo at 340, lowest Delta at 90$/,
  malformed: [
    { name: 'a text value', data: [{ label: 'A', value: 1 }, { label: 'B', value: 'abc' }], message: /field 'value' must be a number, got 'abc' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'a mapping to a missing column', data: [{ name: 'A', amount: 1 }], options: { fields: { label: 'name', value: 'amont' } }, message: /column 'amont' not found/ },
  ],
})

const num = (el: Element, a: string) => Number(el.getAttribute(a))

describe('lollipop: geometry', () => {
  it('dots sit at their values and stems start at the baseline (horizontal)', () => {
    const el = host(640, 400)
    const h = lollipop.render(el, [{ label: 'a', value: 10 }, { label: 'b', value: 20 }, { label: 'c', value: 0 }], { animate: false })
    const dots = [...el.querySelectorAll('[data-role="point"]')]
    const x0 = num(dots[2]!, 'cx')
    expect((num(dots[1]!, 'cx') - x0) / (num(dots[0]!, 'cx') - x0)).toBeCloseTo(2, 2)
    const stem = el.querySelector('[data-key="line:s:a"]')!
    expect(num(stem, 'x1')).toBeCloseTo(x0, 1)
    expect(num(stem, 'y1')).toBeCloseTo(num(dots[0]!, 'cy'), 3)
    h.destroy()
  })
  it('vertical orientation puts categories along x and values up y', () => {
    const el = host(640, 400)
    const h = lollipop.render(el, [{ label: 'a', value: 10 }, { label: 'b', value: 30 }], { animate: false, orientation: 'vertical' })
    const [a, b] = [...el.querySelectorAll('[data-role="point"]')]
    expect(num(b!, 'cx')).toBeGreaterThan(num(a!, 'cx'))
    expect(num(b!, 'cy')).toBeLessThan(num(a!, 'cy'))
    h.destroy()
  })
  it('stems run from a non-zero baseline in both directions', () => {
    const el = host(640, 400)
    const h = lollipop.render(el, [{ label: 'up', value: 120 }, { label: 'down', value: 80 }], { animate: false, baseline: 100 })
    const up = el.querySelector('[data-key="line:s:up"]')!, down = el.querySelector('[data-key="line:s:down"]')!
    expect(num(up, 'x1')).toBeCloseTo(num(down, 'x1'), 3)
    expect(num(up, 'x2')).toBeGreaterThan(num(up, 'x1'))
    expect(num(down, 'x2')).toBeLessThan(num(down, 'x1'))
    h.destroy()
  })
  it('sorts descending, top to bottom', () => {
    const el = host()
    const h = lollipop.render(el, [{ label: 'a', value: 1 }, { label: 'b', value: 3 }, { label: 'c', value: 2 }], { animate: false, sort: 'descending' })
    const dots = [...el.querySelectorAll('[data-role="point"]')].sort((p, q) => num(p, 'cy') - num(q, 'cy'))
    expect(dots.map((d) => d.getAttribute('data-key'))).toEqual(['circle:b', 'circle:c', 'circle:a'])
    h.destroy()
  })
  it('sums duplicate labels and says so', () => {
    const el = host()
    const h = lollipop.render(el, [{ label: 'a', value: 1 }, { label: 'a', value: 2 }], { animate: false })
    expect(h.markCount()).toBe(1)
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 duplicate labels summed/)
    expect(h.exportCSV()).toBe('Label,Value\na,3')
    h.destroy()
  })
})
