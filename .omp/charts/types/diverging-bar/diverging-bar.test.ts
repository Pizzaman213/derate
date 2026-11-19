import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import divergingBar from './index'

chartSuite({
  module: divergingBar,
  dir: import.meta.dirname,
  role: 'bar',
  marks: { basic: 5, realistic: 8, 'edge-single': 1, 'edge-duplicates': 5, 'edge-large': 5200, 'edge-zeros': 5 },
  summary: /^Diverging bar chart, 5 bars around zero, 3 above and 2 below, highest Charlie at 23, lowest Delta at -15$/,
  malformed: [
    { name: 'a text value', data: [{ label: 'A', value: 1 }, { label: 'B', value: 'abc' }], message: /field 'value' must be a number, got 'abc' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'mismatched columns', data: { label: ['a', 'b'], value: [1] }, message: /column 'value' has 1 values but column 'label' has 2/ },
    { name: 'a mapping to a missing column', data: [{ name: 'A', amount: 1 }], options: { fields: { label: 'name', value: 'amont' } }, message: /column 'amont' not found/ },
  ],
})

const bars = (el: Element) => [...el.querySelectorAll('[data-role="bar"]')]
const num = (e: Element, a: string) => Number(e.getAttribute(a))

describe('diverging-bar: geometry', () => {
  it('bars leave the zero line to the right when positive and to the left when negative', () => {
    const el = host(640, 400)
    const h = divergingBar.render(el, [{ label: 'a', value: 10 }, { label: 'b', value: -5 }], { animate: false })
    const [a, b] = bars(el)
    expect(num(b!, 'x') + num(b!, 'width')).toBeCloseTo(num(a!, 'x'), 1)
    expect(num(a!, 'width') / num(b!, 'width')).toBeCloseTo(2, 1)
    expect(a!.getAttribute('data-series')).toBe('above')
    expect(b!.getAttribute('data-series')).toBe('below')
    h.destroy()
  })
  it('symmetric domain puts the midpoint in the centre of the plot', () => {
    const el = host(640, 400)
    const h = divergingBar.render(el, [{ label: 'a', value: 4.5 }, { label: 'b', value: 2 }, { label: 'c', value: 3 }], { animate: false, midpoint: 3 })
    const [a, b] = bars(el)
    const mid = num(a!, 'x')
    expect(num(b!, 'x') + num(b!, 'width')).toBeCloseTo(mid, 1)
    expect(num(a!, 'width') / num(b!, 'width')).toBeCloseTo(1.5, 1)
    const line = el.querySelector('[data-key="line:midline"]')!
    expect(num(line, 'x1')).toBeCloseTo(mid, 1)
    expect(el.querySelector('[aria-live]')).not.toBeNull()
    h.focusMark(0)
    expect(el.querySelector('[aria-live]')!.textContent).toMatch(/a: 4\.5, \+1\.5 vs 3/)
    expect(h.exportCSV()).toBe('Label,Value,Difference from midpoint\na,4.5,1.5\nb,2,-1\nc,3,0')
    h.destroy()
  })
  it('legend names both sides from the midpoint', () => {
    const el = host(640, 400)
    const h = divergingBar.render(el, [{ label: 'a', value: 4 }, { label: 'b', value: 2 }], { animate: false, midpoint: 3 })
    expect(el.querySelector('.oc-legend')!.textContent).toMatch(/Above 3.*Below 3/)
    h.destroy()
  })
})
