import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import groupedBar from './index'

chartSuite({
  module: groupedBar,
  dir: import.meta.dirname,
  role: 'bar',
  marks: { basic: 12, realistic: 18, 'edge-single': 1, 'edge-duplicates': 12, 'edge-large': 5200, 'edge-zeros': 12 },
  summary: /^Grouped bar chart, 4 categories, 3 series \(North, South and West\), highest Q4 \(North\) at 180, lowest Q1 \(West\) at 60$/,
  malformed: [
    { name: 'a text value', data: [{ label: 'A', series: 's', value: 1 }, { label: 'B', series: 's', value: 'abc' }], message: /field 'value' must be a number, got 'abc' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'mismatched columns', data: { label: ['a', 'b'], series: ['s', 's'], value: [1] }, message: /column 'value' has 1 values but column 'label' has 2/ },
    { name: 'a mapping to a missing column', data: [{ name: 'A', grp: 'x', amount: 1 }], options: { fields: { label: 'name', series: 'grp', value: 'amont' } }, message: /column 'amont' not found/ },
  ],
})

const rows = [
  { label: 'a', series: 'x', value: 10 }, { label: 'a', series: 'y', value: 20 },
  { label: 'b', series: 'x', value: 5 }, { label: 'b', series: 'y', value: -10 },
]
const bars = (el: Element) => [...el.querySelectorAll('[data-role="bar"]')]
const num = (e: Element, a: string) => Number(e.getAttribute(a))

describe('grouped-bar: geometry', () => {
  it('bars sit side by side inside each group, heights proportional to values', () => {
    const el = host(640, 400)
    const h = groupedBar.render(el, rows, { animate: false })
    const [ax, ay, bx, by] = bars(el)
    expect(num(ay!, 'height') / num(ax!, 'height')).toBeCloseTo(2, 1)
    expect(num(by!, 'height') / num(bx!, 'height')).toBeCloseTo(2, 1)
    expect(num(ax!, 'x') + num(ax!, 'width')).toBeLessThanOrEqual(num(ay!, 'x'))
    expect(num(ay!, 'x') + num(ay!, 'width')).toBeLessThan(num(bx!, 'x'))
    expect(num(ax!, 'width')).toBeCloseTo(num(ay!, 'width'), 5)
    // negative bar hangs from the shared zero baseline
    expect(num(by!, 'y')).toBeCloseTo(num(ax!, 'y') + num(ax!, 'height'), 1)
    h.destroy()
  })
  it('hiding a series in the legend regroups the rest to fill the band', () => {
    const el = host(640, 400)
    const h = groupedBar.render(el, rows, { animate: false })
    const w0 = num(bars(el)[0]!, 'width')
    el.querySelector<HTMLElement>('.oc-legend button[data-key="y"]')!.click()
    const left = bars(el)
    expect(left.length).toBe(2)
    expect(num(left[0]!, 'width')).toBeGreaterThan(w0 * 1.8)
    h.destroy()
  })
  it('horizontal orientation puts categories on y and values on x', () => {
    const el = host(640, 400)
    const h = groupedBar.render(el, rows, { animate: false, orientation: 'horizontal' })
    const [ax, ay] = bars(el)
    expect(num(ay!, 'width') / num(ax!, 'width')).toBeCloseTo(2, 1)
    expect(num(ax!, 'y')).toBeLessThan(num(ay!, 'y'))
    h.destroy()
  })
  it('sums duplicate pairs and tables wide', () => {
    const el = host()
    const h = groupedBar.render(el, [...rows, { label: 'a', series: 'x', value: 1 }], { animate: false })
    expect(h.exportCSV()).toBe('Label,x,y\na,11,20\nb,5,-10')
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 duplicate label\/series pairs summed/)
    h.destroy()
  })
})
