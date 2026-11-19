import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import { overlapArea } from '../_shared/SetCharts'
import venn from './index'

chartSuite({
  module: venn,
  dir: import.meta.dirname,
  role: 'set',
  empty: ['edge-empty', 'edge-zeros'],
  marks: { basic: 3, realistic: 3, 'edge-single': 1, 'edge-nulls': 2, 'edge-disjoint': 2, 'edge-large': 3 },
  summary: /^Venn diagram, 3 sets \(Design, Engineering and Research\), largest Engineering at 80, largest overlap Design and Engineering at 20$/,
  malformed: [
    { name: 'four sets', data: [{ sets: 'A', size: 1 }, { sets: 'B', size: 1 }, { sets: 'C', size: 1 }, { sets: 'D', size: 1 }], message: /at most 3 sets, got 4 .*use 'upset'/ },
    { name: 'a text size', data: [{ sets: 'A', size: 1 }, { sets: 'B', size: 'lots' }], message: /field 'size' must be a number, got 'lots' at row 2/ },
    { name: 'an overlap without its sets', data: [{ sets: 'A', size: 10 }, { sets: 'A,B', size: 4 }], message: /set 'B' appears in an overlap but has no size of its own/ },
    { name: 'empty sets', data: [{ sets: '', size: 3 }], message: /field 'sets' needs at least 1 item\(s\) at row 1/ },
    { name: 'a scalar', data: 7, message: /must be a list of \{ sets, size \} rows/ },
  ],
})

const circles = (el: Element) => [...el.querySelectorAll('circle[data-role="set"]')].map((c) => ({ x: Number(c.getAttribute('cx')), y: Number(c.getAttribute('cy')), r: Number(c.getAttribute('r')) }))

describe('venn: geometry', () => {
  it('2 sets: radii and the overlap are exact to 1%', () => {
    const el = host(640, 400)
    const h = venn.render(el, [{ sets: 'A', size: 300 }, { sets: 'B', size: 120 }, { sets: 'A,B', size: 45 }], { animate: false })
    const [a, b] = circles(el) as [{ x: number; y: number; r: number }, { x: number; y: number; r: number }]
    const unit = (Math.PI * a.r * a.r) / 300 // px² per item
    expect((Math.PI * b.r * b.r) / unit / 120).toBeCloseTo(1, 2)
    const lens = overlapArea(a.r, b.r, Math.hypot(a.x - b.x, a.y - b.y)) / unit
    expect(Math.abs(lens - 45) / 45).toBeLessThan(0.01)
    h.destroy()
  })
  it('3 sets: every pairwise overlap is exact when the sizes are consistent', () => {
    const el = host(640, 480)
    const data = [
      { sets: 'A', size: 60 }, { sets: 'B', size: 80 }, { sets: 'C', size: 50 },
      { sets: 'A,B', size: 20 }, { sets: 'A,C', size: 15 }, { sets: 'B,C', size: 18 }, { sets: 'A,B,C', size: 8 },
    ]
    const h = venn.render(el, data, { animate: false })
    const cs = circles(el)
    const unit = (Math.PI * cs[0]!.r ** 2) / 60
    const pair = (i: number, j: number) => overlapArea(cs[i]!.r, cs[j]!.r, Math.hypot(cs[i]!.x - cs[j]!.x, cs[i]!.y - cs[j]!.y)) / unit
    expect(Math.abs(pair(0, 1) - 20) / 20).toBeLessThan(0.01)
    expect(Math.abs(pair(0, 2) - 15) / 15).toBeLessThan(0.01)
    expect(Math.abs(pair(1, 2) - 18) / 18).toBeLessThan(0.01)
    expect(Math.abs((Math.PI * cs[2]!.r ** 2) / unit - 50) / 50).toBeLessThan(0.01)
    // one region per drawn overlap, every one hoverable
    expect(el.querySelectorAll('[data-role="intersection"]').length).toBe(4)
    h.destroy()
  })
  it('labels each region with its exclusive count', () => {
    const el = host(800, 560)
    const h = venn.render(el, [{ sets: 'A', size: 300 }, { sets: 'B', size: 200 }, { sets: 'A,B', size: 50 }], { animate: false })
    const texts = [...el.querySelectorAll('text')].map((t) => t.textContent)
    expect(texts).toEqual(expect.arrayContaining(['A', '250', 'B', '150', '50']))
    h.destroy()
  })
  it('disjoint sets do not touch; a subset sits inside its superset', () => {
    const el = host()
    const h = venn.render(el, [{ sets: 'A', size: 30 }, { sets: 'B', size: 45 }], { animate: false })
    const [a, b] = circles(el)
    expect(Math.hypot(a!.x - b!.x, a!.y - b!.y)).toBeGreaterThan(a!.r + b!.r)
    h.update([{ sets: 'A', size: 500 }, { sets: 'B', size: 80 }, { sets: 'A,B', size: 80 }])
    const [p, q] = circles(el)
    expect(Math.hypot(p!.x - q!.x, p!.y - q!.y) + q!.r).toBeLessThanOrEqual(p!.r + 0.05)
    h.destroy()
  })
  it('notes clamped and approximate overlaps', () => {
    const el = host()
    const h = venn.render(el, [{ sets: 'A', size: 30 }, { sets: 'B', size: 30 }, { sets: 'C', size: 30 }, { sets: 'A,B', size: 40 }, { sets: 'A,C', size: 0 }, { sets: 'B,C', size: 29 }], { animate: false })
    const note = el.querySelector('.oc-note')!.textContent!
    expect(note).toMatch(/1 overlaps larger than a member set clamped/)
    expect(note).toMatch(/cannot all be drawn exactly/)
    h.destroy()
  })
  it('the table lists inclusive and exclusive sizes', () => {
    const el = host()
    const h = venn.render(el, [{ sets: 'A', size: 10 }, { sets: 'B', size: 8 }, { sets: 'A,B', size: 3 }], { animate: false })
    expect(h.exportCSV()).toBe('Sets,Size,Only these sets\nA,10,7\nB,8,5\nA ∩ B,3,3')
    h.destroy()
  })
})
