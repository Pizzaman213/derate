import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import strip from './index'

chartSuite({
  module: strip,
  dir: import.meta.dirname,
  role: 'point',
  marks: { basic: 40, realistic: 180, 'edge-single': 1, 'edge-duplicates': 80, 'edge-nulls': 21, 'edge-large': 5400, 'edge-zeros': 40 },
  summary: /^Strip plot, 40 values in 5 groups, from \d+ to \d+, median \d+(\.\d+)?; group medians highest \w+ at \d+(\.\d+)?, lowest \w+ at \d+(\.\d+)?$/,
  malformed: [
    { name: 'a text value', data: [{ group: 'a', value: 1 }, { group: 'a', value: 'abc' }], message: /field 'value' must be a number, got 'abc' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'mismatched columns', data: { group: ['a', 'b'], value: [1] }, message: /column 'value' has 1 values but column 'group' has 2/ },
  ],
})

const points = (el: Element) => [...el.querySelectorAll('[data-role="point"]')].map((c) => [Number(c.getAttribute('cx')), Number(c.getAttribute('cy'))] as const)
const data = Array.from({ length: 60 }, (_, i) => ({ group: ['a', 'b', 'c'][i % 3]!, value: (i * 37) % 50 }))

describe('strip: jitter', () => {
  it('is deterministic: the same seed draws the same points', () => {
    const a = host(640, 400), b = host(640, 400), c = host(640, 400)
    const ha = strip.render(a, data, { animate: false, seed: 42 })
    const hb = strip.render(b, data, { animate: false, seed: 42 })
    const hc = strip.render(c, data, { animate: false, seed: 7 })
    expect(points(a)).toEqual(points(b))
    expect(points(a)).not.toEqual(points(c))
    // a redraw (resize back and forth) does not reshuffle
    const before = points(a)
    ha.resize(500, 400)
    ha.resize(640, 400)
    expect(points(a)).toEqual(before)
    ha.destroy(); hb.destroy(); hc.destroy()
  })
  it('keeps every point inside its own band and at its value', () => {
    const el = host(640, 400)
    const h = strip.render(el, [{ group: 'a', value: 0 }, { group: 'a', value: 10 }, { group: 'b', value: 5 }, { group: 'b', value: 5 }], { animate: false })
    const ps = points(el)
    // value axis (y) is linear: 5 is half way between 0 and 10
    expect(ps[2]![1]).toBeCloseTo((ps[0]![1] + ps[1]![1]) / 2, 5)
    // group a's points are all left of group b's
    expect(Math.max(ps[0]![0], ps[1]![0])).toBeLessThan(Math.min(ps[2]![0], ps[3]![0]))
    // ties are spread across the band by the jitter
    expect(ps[2]![0]).not.toBe(ps[3]![0])
    h.destroy()
  })
  it('jitter 0 lines every group up on its centre', () => {
    const el = host(640, 400)
    const h = strip.render(el, data, { animate: false, jitter: 0 })
    const xs = new Set(points(el).map((p) => p[0]))
    expect(xs.size).toBe(3)
    h.destroy()
  })
  it('draws a median rule per group and notes skipped nulls', () => {
    const el = host()
    const h = strip.render(el, [{ group: 'a', value: 1 }, { group: 'a', value: 3 }, { group: 'a', value: null }], { animate: false })
    const rule = el.querySelector('[data-key="line:med:a"]')!
    expect(rule).not.toBeNull()
    expect(el.querySelector('.oc-note')!.textContent).toBe('1 missing values skipped')
    h.destroy()
  })
})
