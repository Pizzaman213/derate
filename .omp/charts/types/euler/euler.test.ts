import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import { overlapArea } from '../_shared/SetCharts'
import euler from './index'

chartSuite({
  module: euler,
  dir: import.meta.dirname,
  role: 'set',
  empty: ['edge-empty', 'edge-zeros'],
  marks: { basic: 5, realistic: 6, 'edge-single': 1, 'edge-nulls': 3, 'edge-disjoint': 4, 'edge-large': 8, 'edge-nested': 4 },
  summary: /^Euler diagram, 5 sets \(Netflix, Prime, Disney\+ and 2 more\), largest Netflix at 50, largest overlap Netflix and Prime at 22$/,
  malformed: [
    { name: 'nine sets', data: 'ABCDEFGHI'.split('').map((s) => ({ sets: s, size: 1 })), message: /at most 8 sets, got 9.*use 'upset'/ },
    { name: 'a text size', data: [{ sets: 'A', size: 'x' }], message: /field 'size' must be a number, got 'x' at row 1/ },
    { name: 'an overlap without its sets', data: [{ sets: 'A', size: 10 }, { sets: 'A,Z', size: 4 }], message: /set 'Z' appears in an overlap/ },
  ],
})

const circles = (el: Element) => [...el.querySelectorAll('circle[data-role="set"]')].map((c) => ({ key: c.getAttribute('data-key'), x: Number(c.getAttribute('cx')), y: Number(c.getAttribute('cy')), r: Number(c.getAttribute('r')) }))

describe('euler: layout', () => {
  it('draws one circle per set, areas proportional to size', () => {
    const el = host(640, 420)
    const data = [{ sets: 'A', size: 100 }, { sets: 'B', size: 50 }, { sets: 'C', size: 25 }, { sets: 'D', size: 10 }, { sets: 'A,B', size: 20 }, { sets: 'B,C', size: 5 }]
    const h = euler.render(el, data, { animate: false })
    const cs = circles(el)
    expect(cs.length).toBe(4)
    const unit = cs[0]!.r ** 2 / 100
    expect(cs[1]!.r ** 2 / unit).toBeCloseTo(50, 0)
    expect(cs[3]!.r ** 2 / unit).toBeCloseTo(10, 0)
    // the requested overlap is close to right (layout is a best fit, not exact)
    const ab = overlapArea(cs[0]!.r, cs[1]!.r, Math.hypot(cs[0]!.x - cs[1]!.x, cs[0]!.y - cs[1]!.y)) / (Math.PI * unit)
    expect(Math.abs(ab - 20) / 20).toBeLessThan(0.1)
    h.destroy()
  })
  it('disjoint sets do not overlap', () => {
    const el = host(640, 420)
    const data = [{ sets: 'A', size: 40 }, { sets: 'B', size: 30 }, { sets: 'C', size: 20 }, { sets: 'D', size: 15 }, { sets: 'A,B', size: 10 }]
    const h = euler.render(el, data, { animate: false })
    const cs = circles(el)
    expect(cs.length).toBe(4)
    for (let i = 0; i < cs.length; i++)
      for (let j = i + 1; j < cs.length; j++) {
        if (i === 0 && j === 1) continue
        const a = cs[i]!, b = cs[j]!
        expect(Math.hypot(a.x - b.x, a.y - b.y)).toBeGreaterThanOrEqual(a.r + b.r - 0.05)
      }
    h.destroy()
  })
  it('lays out the same way every time', () => {
    const el = host(640, 420)
    const data = Array.from({ length: 8 }, (_, i) => ({ sets: `S${i}`, size: 100 - i * 9 })).concat([{ sets: 'S0,S1', size: 20 }, { sets: 'S2,S5', size: 9 }, { sets: 'S1,S7', size: 6 }])
    const h = euler.render(el, data, { animate: false })
    const first = JSON.stringify(circles(el))
    h.update(data, { animate: false })
    expect(JSON.stringify(circles(el))).toBe(first)
    h.destroy()
  })
  it('re-lays out without a set hidden through the legend', () => {
    const el = host(640, 420)
    const h = euler.render(el, [{ sets: 'A', size: 40 }, { sets: 'B', size: 30 }, { sets: 'A,B', size: 10 }], { animate: false })
    const item = [...el.querySelectorAll('.oc-legend li')].find((li) => li.textContent?.includes('B'))! as HTMLElement
    ;(item.querySelector('button') ?? item).dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(circles(el).map((c) => c.key)).toEqual(['circle:set:A'])
    h.destroy()
  })
})
