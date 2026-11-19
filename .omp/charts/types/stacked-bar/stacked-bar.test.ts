import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import stackedBar from './index'

chartSuite({
  module: stackedBar,
  dir: import.meta.dirname,
  role: 'bar',
  empty: ['edge-empty', 'edge-zeros'],
  marks: { basic: 12, realistic: 24, 'edge-single': 1, 'edge-duplicates': 12, 'edge-large': 5200, 'edge-negatives': 12 },
  summary: /^Stacked bar chart, 4 bars of 3 series \(North, South and West\), totals: highest Q4 at 425, lowest Q1 at 275$/,
  malformed: [
    { name: 'a text value', data: [{ label: 'A', series: 's', value: 1 }, { label: 'B', series: 's', value: 'abc' }], message: /field 'value' must be a number, got 'abc' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'mismatched columns', data: { label: ['a', 'b'], series: ['s', 's'], value: [1] }, message: /column 'value' has 1 values but column 'label' has 2/ },
    { name: 'a mapping to a missing column', data: [{ name: 'A', grp: 'x', amount: 1 }], options: { fields: { label: 'name', series: 'grp', value: 'amont' } }, message: /column 'amont' not found/ },
  ],
})

const rows = [
  { label: 'a', series: 'x', value: 10 }, { label: 'a', series: 'y', value: 20 }, { label: 'a', series: 'z', value: -5 },
  { label: 'b', series: 'x', value: 5 }, { label: 'b', series: 'y', value: 5 }, { label: 'b', series: 'z', value: -15 },
]
const seg = (el: Element, key: string) => el.querySelector(`[data-role="bar"][data-key="rect:${key}"]`)!
const num = (e: Element, a: string) => Number(e.getAttribute(a))

describe('stacked-bar: geometry', () => {
  it('positives stack up from zero, negatives down from zero', () => {
    const el = host(640, 400)
    const h = stackedBar.render(el, rows, { animate: false })
    const ax = seg(el, 'a::x'), ay = seg(el, 'a::y'), az = seg(el, 'a::z')
    const zero = num(ax, 'y') + num(ax, 'height')
    // y sits directly on top of x
    expect(num(ay, 'y') + num(ay, 'height')).toBeCloseTo(num(ax, 'y'), 1)
    expect(num(ay, 'height') / num(ax, 'height')).toBeCloseTo(2, 1)
    // the negative hangs from zero, not from the top of the stack
    expect(num(az, 'y')).toBeCloseTo(zero, 1)
    expect(num(az, 'height') / num(ax, 'height')).toBeCloseTo(0.5, 1)
    h.destroy()
  })
  it('a stack height equals the sum of its positive segments', () => {
    const el = host(640, 400)
    const h = stackedBar.render(el, rows, { animate: false })
    const ax = seg(el, 'a::x'), ay = seg(el, 'a::y'), bx = seg(el, 'b::x'), by = seg(el, 'b::y')
    const a = num(ax, 'height') + num(ay, 'height'), b = num(bx, 'height') + num(by, 'height')
    expect(a / b).toBeCloseTo(3, 1)
    h.destroy()
  })
  it('hiding a series restacks the others onto the baseline', () => {
    const el = host(640, 400)
    const h = stackedBar.render(el, rows, { animate: false })
    el.querySelector<HTMLElement>('.oc-legend button[data-key="x"]')!.click()
    const ay = seg(el, 'a::y'), az = seg(el, 'a::z')
    expect(el.querySelector('[data-key="rect:a::x"][data-mark]')).toBeNull()
    expect(num(ay, 'y') + num(ay, 'height')).toBeCloseTo(num(az, 'y'), 1)
    h.destroy()
  })
  it('horizontal: segments run left to right; totals print the net value', () => {
    const el = host(640, 400)
    const h = stackedBar.render(el, rows, { animate: false, orientation: 'horizontal', showTotals: true })
    const ax = seg(el, 'a::x'), ay = seg(el, 'a::y')
    expect(num(ax, 'x') + num(ax, 'width')).toBeCloseTo(num(ay, 'x'), 1)
    const texts = [...el.querySelectorAll('text')].map((e) => e.textContent)
    expect(texts).toContain('25')
    expect(texts).toContain('-5')
    h.destroy()
  })
  it('tables wide with a total column', () => {
    const el = host()
    const h = stackedBar.render(el, rows, { animate: false })
    expect(h.exportCSV()).toBe('Label,x,y,z,Total\na,10,20,-5,25\nb,5,5,-15,-5')
    h.destroy()
  })
})
