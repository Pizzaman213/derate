import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import pyramid from './index'

chartSuite({
  module: pyramid,
  dir: import.meta.dirname,
  role: 'bar',
  empty: ['edge-empty', 'edge-zeros'],
  marks: { basic: 12, realistic: 36, 'edge-single': 1, 'edge-duplicates': 12, 'edge-negatives': 6, 'edge-three-groups': 12, 'edge-large': 202, 'edge-unsorted': 12 },
  summary: /^Population pyramid, male and female across 6 age bands, total 5,365 \(male 49%, female 51%\), largest 0–14 male at 620$/,
  malformed: [
    { name: 'a text value', data: [{ age: '0–4', sex: 'male', value: 1 }, { age: '0–4', sex: 'female', value: 'abc' }], message: /field 'value' must be a number, got 'abc' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'a mapping to a missing column', data: [{ band: '0–4', gender: 'm', n: 1 }], options: { fields: { age: 'band', sex: 'sex', value: 'n' } }, message: /column 'sex' not found/ },
  ],
})

type Bar = { key: string; x: number; y: number; w: number }
const bars = (el: Element): Bar[] =>
  [...el.querySelectorAll('[data-role="bar"]')].map((r) => ({ key: r.getAttribute('data-key')!, x: Number(r.getAttribute('x')), y: Number(r.getAttribute('y')), w: Number(r.getAttribute('width')) }))
const byKey = (bs: Bar[], k: string) => bs.find((b) => b.key === `rect:${k}`)!

describe('population-pyramid: geometry', () => {
  const data = [
    { age: '0–9', sex: 'male', value: 100 }, { age: '0–9', sex: 'female', value: 100 },
    { age: '10–19', sex: 'male', value: 50 }, { age: '10–19', sex: 'female', value: 200 },
  ]
  it('is symmetric: equal values have equal lengths and meet at the centre', () => {
    const el = host(640, 400)
    const h = pyramid.render(el, data, { animate: false })
    const bs = bars(el)
    const l = byKey(bs, '0:0–9'), r = byKey(bs, '1:0–9')
    expect(l.w).toBeCloseTo(r.w, 1)
    expect(l.x + l.w).toBeCloseTo(r.x, 1) // left bar ends where the right one starts: the zero line
    // the axis is symmetric: the outermost tick labels read the same on both sides
    const ticks = [...el.querySelectorAll('[data-key^="text:xl:"]')].map((t) => t.textContent)
    expect(ticks[0]).toBe(ticks[ticks.length - 1])
    expect(ticks.length % 2).toBe(1) // ..., 0, ... : an odd count centred on zero
    h.destroy()
  })
  it('bar length is proportional to value on both sides', () => {
    const el = host(640, 400)
    const h = pyramid.render(el, data, { animate: false })
    const bs = bars(el)
    const base = byKey(bs, '0:0–9').w
    expect(byKey(bs, '0:10–19').w / base).toBeCloseTo(0.5, 2)
    expect(byKey(bs, '1:10–19').w / base).toBeCloseTo(2, 2)
    // male on the left, female on the right
    expect(byKey(bs, '0:10–19').x).toBeLessThan(byKey(bs, '1:10–19').x)
    h.destroy()
  })
  it('puts the youngest band at the bottom, even when the input lists the oldest first', () => {
    const el = host(640, 400)
    const h = pyramid.render(el, [...data].reverse(), { animate: false })
    const bs = bars(el)
    expect(byKey(bs, '0:0–9').y).toBeGreaterThan(byKey(bs, '0:10–19').y)
    h.update([...data].reverse(), { order: 'as-given' })
    const as = bars(el)
    expect(byKey(as, '0:10–19').y).toBeLessThan(byKey(as, '0:0–9').y)
    h.destroy()
  })
  it('percentages: each bar is its share of the whole population', () => {
    const el = host(640, 400)
    const h = pyramid.render(el, data, { animate: false, percentages: true })
    h.focusMark(3)
    expect(el.querySelector('[aria-live]')!.textContent).toBe('4 of 4: 0–9, female: 22.2%')
    const ticks = [...el.querySelectorAll('[data-key^="text:xl:"]')].map((t) => t.textContent!)
    expect(ticks.every((t) => t.endsWith('%'))).toBe(true)
    h.destroy()
  })
  it('honours left/right, sums duplicates, omits negatives and extra groups, and notes each', () => {
    const el = host(640, 400)
    const h = pyramid.render(el, [
      { age: '0–9', sex: 'A', value: 10 }, { age: '0–9', sex: 'A', value: 5 }, { age: '0–9', sex: 'B', value: -3 },
      { age: '0–9', sex: 'C', value: 7 }, { age: '10–19', sex: 'B', value: 8 },
    ], { animate: false, left: 'b', right: 'a' })
    expect(h.markCount()).toBe(2)
    expect(el.querySelector('.oc-note')!.textContent).toBe("1 duplicate age/group rows summed; 1 negative values omitted; 1 rows for 'C' omitted (a pyramid has two sides)")
    expect(h.exportCSV()).toBe('Age,B,A\n0–9,,15\n10–19,8,')
    const bs = bars(el)
    expect(byKey(bs, '0:10–19').x).toBeLessThan(byKey(bs, '1:0–9').x) // B is on the left
    h.destroy()
  })
})
