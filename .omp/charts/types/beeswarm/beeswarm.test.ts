import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import beeswarm from './index'

chartSuite({
  module: beeswarm,
  dir: import.meta.dirname,
  role: 'point',
  marks: { basic: 24, realistic: 160, 'edge-single': 1, 'edge-all-equal': 30, 'edge-duplicates': 48, 'edge-nulls': 12, 'edge-large': 5400, 'edge-log': 118, 'edge-unicode-rtl': 8 },
  summary: /^Beeswarm chart, 24 values in 3 groups, from \d+ to \d+, median \d+(\.\d+)?, highest median \w+ at \d+/,
  malformed: [
    { name: 'a text value', data: [{ value: 1 }, { value: 'abc' }], message: /field 'value' must be a number, got 'abc' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'a mapping to a missing column', data: [{ ms: 1 }], options: { fields: { value: 'msec' } }, message: /column 'msec' not found/ },
  ],
})

type Circle = { x: number; y: number; r: number }
const circles = (el: Element): Circle[] =>
  [...el.querySelectorAll('[data-role="point"]')].map((c) => ({ x: Number(c.getAttribute('cx')), y: Number(c.getAttribute('cy')), r: Number(c.getAttribute('r')) }))
const overlaps = (cs: Circle[]) => {
  let n = 0
  for (let i = 0; i < cs.length; i++) for (let j = i + 1; j < cs.length; j++) {
    const a = cs[i]!, b = cs[j]!
    if (Math.hypot(a.x - b.x, a.y - b.y) < a.r + b.r - 0.01) n++
  }
  return n
}

describe('beeswarm: geometry', () => {
  it('no two circles overlap, even when many values tie', () => {
    const el = host(640, 400)
    const data = Array.from({ length: 200 }, (_, i) => ({ value: Math.round(Math.sin(i) * 10), group: ['a', 'b'][i % 2] }))
    const h = beeswarm.render(el, data, { animate: false })
    const cs = circles(el)
    expect(cs).toHaveLength(200)
    expect(overlaps(cs)).toBe(0)
    expect(el.querySelector('.oc-note')).toSatisfy((n: Element | null) => n === null || (n as HTMLElement).hidden || !/pressed/.test(n.textContent ?? ''))
    h.destroy()
  })
  it('places each circle at its value along the axis and inside its band', () => {
    const el = host(640, 400)
    const h = beeswarm.render(el, [{ value: 0, group: 'a' }, { value: 10, group: 'a' }, { value: 5, group: 'b' }], { animate: false })
    const cs = circles(el)
    // x is linear in the value: 5 sits half way between 0 and 10
    expect(cs[2]!.x).toBeCloseTo((cs[0]!.x + cs[1]!.x) / 2, 5)
    // groups get separate bands (a above b)
    expect(cs[0]!.y).toBeLessThan(cs[2]!.y)
    h.destroy()
  })
  it('a vertical swarm puts values on y (higher is up)', () => {
    const el = host(640, 400)
    const h = beeswarm.render(el, [{ value: 1 }, { value: 9 }], { animate: false, orientation: 'vertical' })
    const cs = circles(el)
    expect(cs[1]!.y).toBeLessThan(cs[0]!.y)
    h.destroy()
  })
  it('skips nulls and non-positive log values, and says so', () => {
    const el = host()
    const h = beeswarm.render(el, [{ value: 1 }, { value: null }, { value: 0 }, { value: 100 }], { animate: false, scale: 'log' })
    expect(h.markCount()).toBe(2)
    expect(el.querySelector('.oc-note')!.textContent).toBe('1 missing values skipped; 1 values ≤ 0 skipped on the log scale')
    expect(h.exportCSV()).toBe('Value\n1\n\n0\n100')
    h.destroy()
  })
  it('goes to canvas above 5,000 circles', () => {
    const el = host()
    const h = beeswarm.render(el, Array.from({ length: 5200 }, (_, i) => ({ value: (i * 7919) % 1000 })), { animate: false })
    expect(h.renderer).toBe('canvas')
    expect(h.markCount()).toBe(5200)
    h.destroy()
  })
})
