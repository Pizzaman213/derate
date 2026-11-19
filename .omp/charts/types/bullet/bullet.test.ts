import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import bullet from './index'

chartSuite({
  module: bullet,
  dir: import.meta.dirname,
  role: 'bar',
  marks: { basic: 5, realistic: 8, 'edge-single': 1, 'edge-duplicates': 10, 'edge-zeros': 5, 'edge-nulls': 3 },
  summary: /^Bullet chart, 5 measures, 2 of 5 at or above target, furthest below Avg order size at 330 against 550$/,
  malformed: [
    { name: 'a text value', data: [{ label: 'A', value: 1 }, { label: 'B', value: 'abc' }], message: /field 'value' must be a number, got 'abc' at row 2/ },
    { name: 'a text band', data: [{ label: 'A', value: 1, ranges: '1;lots;3' }], message: /ranges/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
  ],
})

const num = (el: Element, a: string) => Number(el.getAttribute(a))
const fill = (el: Element) => /fill:([^;]+)/.exec(el.getAttribute('style') ?? '')?.[1]

describe('bullet: geometry', () => {
  it('measure, target and bands share the row scale', () => {
    const el = host(640, 200)
    const h = bullet.render(el, [{ label: 'Revenue', value: 150, target: 250, ranges: [100, 200, 300] }], { animate: false })
    const bar = el.querySelector('[data-role="bar"]')!
    const bands = [...el.querySelectorAll('[data-key^="rect:b:"]')]
    expect(bands.length).toBe(3)
    const x0 = num(bands[0]!, 'x'), unit = num(bands[0]!, 'width') / 100
    // bands end at 100, 200, 300
    expect(num(bands[1]!, 'x') + num(bands[1]!, 'width')).toBeCloseTo(x0 + 200 * unit, 1)
    expect(num(bands[2]!, 'x') + num(bands[2]!, 'width')).toBeCloseTo(x0 + 300 * unit, 1)
    // the bar starts at 0 and is 150 long
    expect(num(bar, 'x')).toBeCloseTo(x0, 1)
    expect(num(bar, 'width')).toBeCloseTo(150 * unit, 1)
    // the target tick sits at 250, across the bar
    const t = el.querySelector('[data-key="line:t:Revenue"]')!
    expect(num(t, 'x1')).toBeCloseTo(x0 + 250 * unit, 1)
    expect(num(t, 'y1')).toBeLessThan(num(bar, 'y'))
    // the lowest band is the darkest: grid, fading toward track
    expect(fill(bands[0]!)).toMatch(/^color-mix\(in oklab, var\(--oc-grid\) \d+%, var\(--oc-ink-muted\)\)$/)
    expect(fill(bands[1]!)).toMatch(/^color-mix\(in oklab, var\(--oc-grid\) 50%, var\(--oc-track\)\)$/)
    expect(fill(bands[2]!)).toBe('var(--oc-track)')
    h.destroy()
  })
  it('missing target and ranges: a plain track, no tick', () => {
    const el = host(640, 200)
    const h = bullet.render(el, [{ label: 'Users', value: 42 }], { animate: false })
    expect(el.querySelectorAll('[data-key^="line:t:"]').length).toBe(0)
    const track = [...el.querySelectorAll('[data-key^="rect:b:"]')]
    expect(track.length).toBe(1)
    expect(fill(track[0]!)).toBe('var(--oc-track)')
    expect(h.markCount()).toBe(1)
    h.destroy()
  })
  it('stacks one bullet per row, each with its own axis unless shared', () => {
    const el = host(640, 400)
    const rows = [{ label: 'a', value: 5, target: 8 }, { label: 'b', value: 500, target: 400 }]
    const h = bullet.render(el, rows, { animate: false })
    const [a, b] = [...el.querySelectorAll('[data-role="bar"]')]
    expect(num(b!, 'y')).toBeGreaterThan(num(a!, 'y'))
    // independent scales: both bars fill a similar share of their own axis
    expect(num(a!, 'width') / num(b!, 'width')).toBeGreaterThan(0.5)
    h.update(rows, { animate: false, scale: 'shared' })
    const [a2, b2] = [...el.querySelectorAll('[data-role="bar"]')]
    expect(num(b2!, 'width') / num(a2!, 'width')).toBeCloseTo(100, 0)
    h.destroy()
  })
  it('parses bands written as text in a CSV column', () => {
    const v = bullet.validate('label,value,target,ranges\nA,5,6,2;4;8\nB,1,,"[1, 2, 3]"\nC,2,,')
    expect(v.ok).toBe(true)
    expect(v.data!.map((d) => d.ranges)).toEqual([[2, 4, 8], [1, 2, 3], undefined])
  })
  it('draws the rows that fit and notes the rest; the table keeps all', () => {
    const el = host(640, 400)
    const data = Array.from({ length: 200 }, (_, i) => ({ label: `s${i}`, value: i, target: 100 }))
    const h = bullet.render(el, data, { animate: false })
    expect(h.markCount()).toBeGreaterThan(3)
    expect(h.markCount()).toBeLessThan(200)
    expect(el.querySelector('.oc-note')!.textContent).toMatch(new RegExp(`${200 - h.markCount()} more rows did not fit \\(all 200 are in the table\\)`))
    expect(el.querySelectorAll('table tbody tr').length).toBe(200)
    h.destroy()
  })
})
