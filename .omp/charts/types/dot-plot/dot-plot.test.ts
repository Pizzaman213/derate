import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import dotPlot from './index'

chartSuite({
  module: dotPlot,
  dir: import.meta.dirname,
  role: 'point',
  marks: { basic: 10, realistic: 22, 'edge-single': 1, 'edge-duplicates': 20, 'edge-zeros': 10, 'edge-large': 5202, 'edge-long-labels': 10 },
  summary: /^Dot plot, 5 categories, 2 series \(2000 and 2022\), highest Japan \(2022\) at 84\.1, lowest Brazil \(2000\) at 70\.1$/,
  malformed: [
    { name: 'a text value', data: [{ label: 'A', value: 1 }, { label: 'B', value: 'abc' }], message: /field 'value' must be a number, got 'abc' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'a mapping to a missing column', data: [{ name: 'A', amount: 1 }], options: { fields: { label: 'name', value: 'amont' } }, message: /column 'amont' not found/ },
  ],
})

const num = (el: Element, a: string) => Number(el.getAttribute(a))
const rows = [
  { label: 'a', series: 'x', value: 100 }, { label: 'a', series: 'y', value: 110 },
  { label: 'b', series: 'x', value: 120 }, { label: 'b', series: 'y', value: 101 },
]

describe('dot-plot: geometry', () => {
  it('positions dots linearly on a shared axis that does not force zero', () => {
    const el = host(640, 400)
    const h = dotPlot.render(el, rows, { animate: false })
    const cx = [...el.querySelectorAll('[data-role="point"]')].map((d) => num(d, 'cx'))
    // 100 -> 110 is half of 100 -> 120
    expect((cx[1]! - cx[0]!) / (cx[2]! - cx[0]!)).toBeCloseTo(0.5, 2)
    // without zero, 100 sits well inside the plot rather than 100/120 of the way along
    expect(el.querySelector('[data-key="text:xl:0"]')).toBeNull()
    h.destroy()
  })
  it('zero: true brings zero into the axis', () => {
    const el = host(640, 400)
    const h = dotPlot.render(el, rows, { animate: false, zero: true })
    expect(el.querySelector('[data-key="text:xl:0"]')).not.toBeNull()
    h.destroy()
  })
  it('one row per category, one colour per series, with a legend', () => {
    const el = host(640, 400)
    const h = dotPlot.render(el, rows, { animate: false })
    const dots = [...el.querySelectorAll('[data-role="point"]')]
    expect(dots[0]!.getAttribute('cy')).toBe(dots[1]!.getAttribute('cy'))
    expect(dots[0]!.getAttribute('cy')).not.toBe(dots[2]!.getAttribute('cy'))
    const fill = (d: Element) => /fill:([^;]+)/.exec(d.getAttribute('style') ?? '')?.[1]
    expect(fill(dots[0]!)).toBeTruthy()
    expect(fill(dots[0]!)).toBe(fill(dots[2]!))
    expect(fill(dots[0]!)).not.toBe(fill(dots[1]!))
    expect(el.querySelectorAll('.oc-legend li').length).toBe(2)
    h.destroy()
  })
  it('sorts categories by their mean', () => {
    const el = host(640, 400)
    const h = dotPlot.render(el, [...rows, { label: 'c', series: 'x', value: 50 }], { animate: false, sort: 'descending' })
    const order = [...el.querySelectorAll('[data-role="point"][data-key$="|x"]')].sort((p, q) => num(p, 'cy') - num(q, 'cy')).map((d) => d.getAttribute('data-key'))
    // means: b 110.5, a 105, c 50
    expect(order).toEqual(['circle:b|x', 'circle:a|x', 'circle:c|x'])
    h.destroy()
  })
  it('keeps repeated category/series pairs as separate dots and says so', () => {
    const el = host()
    const h = dotPlot.render(el, [{ label: 'a', value: 1 }, { label: 'a', value: 2 }], { animate: false })
    expect(h.markCount()).toBe(2)
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 repeated categories drawn as separate dots/)
    expect(el.querySelector('.oc-legend li')).toBeNull()
    h.destroy()
  })
})
