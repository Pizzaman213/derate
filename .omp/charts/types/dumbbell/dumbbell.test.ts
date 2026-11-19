import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import dumbbell from './index'

chartSuite({
  module: dumbbell,
  dir: import.meta.dirname,
  role: 'point',
  marks: { basic: 10, realistic: 40, 'edge-single': 2, 'edge-duplicates': 20, 'edge-zeros': 10, 'edge-nulls': 7, 'edge-large': 5200 },
  summary: /^Dumbbell chart, 5 rows, Before to After, 3 up, 1 down, largest change Charlie \+15$/,
  malformed: [
    { name: 'a text value', data: [{ label: 'A', start: 1, end: 2 }, { label: 'B', start: 'abc', end: 2 }], message: /field 'start' must be a number, got 'abc' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'a mapping to a missing column', data: [{ name: 'A', a: 1, b: 2 }], options: { fields: { label: 'name', start: 'a', end: 'bb' } }, message: /column 'bb' not found/ },
  ],
})

const num = (el: Element, a: string) => Number(el.getAttribute(a))
const paint = (el: Element) => /(?:^|;)fill:([^;]+)/.exec(el.getAttribute('style') ?? '')?.[1]
const strokeOf = (el: Element) => /(?:^|;)stroke:([^;]+)/.exec(el.getAttribute('style') ?? '')?.[1]

describe('dumbbell: geometry', () => {
  const data = [{ label: 'up', start: 10, end: 30 }, { label: 'down', start: 40, end: 20 }, { label: 'flat', start: 25, end: 25 }]
  it('places both endpoints on one line at their values', () => {
    const el = host(640, 400)
    const h = dumbbell.render(el, data, { animate: false })
    const s = el.querySelector('[data-key="circle:s:up"]')!, e = el.querySelector('[data-key="circle:e:up"]')!
    const s2 = el.querySelector('[data-key="circle:s:down"]')!
    expect(s.getAttribute('cy')).toBe(e.getAttribute('cy'))
    // 10 -> 30 is twice 10 -> 20
    const e2 = el.querySelector('[data-key="circle:e:down"]')!
    expect((num(e, 'cx') - num(s, 'cx')) / (num(e2, 'cx') - num(s, 'cx'))).toBeCloseTo(2, 2)
    expect(num(s2, 'cx')).toBeGreaterThan(num(e, 'cx'))
    // the connector runs between the dots
    const c = el.querySelector('[data-key="line:c:up"]')!
    expect(num(c, 'x1')).toBeGreaterThan(num(s, 'cx'))
    expect(num(c, 'x2')).toBeLessThan(num(e, 'cx'))
    h.destroy()
  })
  it('colours by the direction of change, and flips with higherIsBetter', () => {
    const el = host(640, 400)
    const h = dumbbell.render(el, data, { animate: false })
    const up = paint(el.querySelector('[data-key="circle:e:up"]')!), down = paint(el.querySelector('[data-key="circle:e:down"]')!), flat = paint(el.querySelector('[data-key="circle:e:flat"]')!)
    expect(up).toBe('var(--oc-positive)')
    expect(down).toBe('var(--oc-negative)')
    expect(flat).toBe('var(--oc-neutral)')
    // the start dot is hollow, ringed in the same colour
    expect(strokeOf(el.querySelector('[data-key="circle:s:up"]')!)).toBe(up)
    expect([...el.querySelectorAll('.oc-legend li button, .oc-legend li .oc-li')].map((b) => b.textContent)).toEqual(['Increase', 'Decrease', 'No change'])
    h.update(data, { animate: false, higherIsBetter: false })
    expect(paint(el.querySelector('[data-key="circle:e:up"]')!)).toBe('var(--oc-negative)')
    h.destroy()
  })
  it('sorts by change, largest rise first', () => {
    const el = host(640, 400)
    const h = dumbbell.render(el, data, { animate: false, sort: 'change' })
    const order = [...el.querySelectorAll('[data-role="point"][data-key^="circle:e:"]')].sort((p, q) => num(p, 'cy') - num(q, 'cy')).map((d) => d.getAttribute('data-key'))
    expect(order).toEqual(['circle:e:up', 'circle:e:flat', 'circle:e:down'])
    h.destroy()
  })
  it('counts two marks per complete row and one when a value is missing', () => {
    const el = host()
    const h = dumbbell.render(el, [{ label: 'a', start: 1, end: 2 }, { label: 'b', start: null, end: 3 }, { label: 'a', start: 4, end: 1 }], { animate: false })
    expect(h.markCount()).toBe(5)
    expect(el.querySelector('[data-key="line:c:b"]')).toBeNull()
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 repeated labels drawn as separate rows; 1 rows missing one value/)
    expect(h.exportCSV()).toBe('Label,Start,End,Change\na,1,2,1\nb,,3,\na,4,1,-3')
    h.destroy()
  })
})
