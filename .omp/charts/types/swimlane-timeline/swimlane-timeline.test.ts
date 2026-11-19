import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import swimlane from './index'

chartSuite({
  module: swimlane,
  dir: import.meta.dirname,
  role: 'item',
  marks: { basic: 11, realistic: 18, 'edge-single': 1, 'edge-zeros': 11, 'edge-duplicates': 22, 'edge-large': 5200 },
  summary: /^Swimlane timeline, 11 items in 4 lanes, Jan 2026 to Apr 2026, busiest Design with 3 items$/,
  malformed: [
    { name: 'end before start', data: [{ lane: 'A', start: '2026-03-05', end: '2026-03-01', label: 'x' }], message: /field 'end': end 2026-03-01 is before start 2026-03-05 at row 1/ },
    { name: 'a bad start', data: [{ lane: 'A', start: 'later', label: 'x' }], message: /field 'start' must be a date, got 'later' at row 1/ },
    { name: 'mismatched columns', data: { lane: ['a', 'b'], start: ['2026-01-01'] }, message: /column 'start' has 1 values but column 'lane' has 2/ },
  ],
})

const box = (e: Element) => ({ x: Number(e.getAttribute('x')), y: Number(e.getAttribute('y')), w: Number(e.getAttribute('width')), h: Number(e.getAttribute('height')) })

describe('swimlane-timeline: lanes and sub-rows', () => {
  it('overlapping items in a lane get separate sub-rows; touching ones share a row', () => {
    const el = host(800, 400)
    const h = swimlane.render(el, [
      { lane: 'A', start: '2026-01-01', end: '2026-01-10', label: 'a1' },
      { lane: 'A', start: '2026-01-05', end: '2026-01-15', label: 'a2' },
      { lane: 'A', start: '2026-01-10', end: '2026-01-20', label: 'a3' },
      { lane: 'B', start: '2026-01-01', end: '2026-01-20', label: 'b1' },
    ], { animate: false })
    const rects = [...el.querySelectorAll('[data-role="item"]')].map(box)
    const [a1, a2, a3, b1] = [rects[0]!, rects[1]!, rects[2]!, rects[3]!]
    expect(a1.y).not.toBe(a2.y)
    expect(a1.y).toBe(a3.y)
    // lane A (2 sub-rows) is above lane B and twice as tall
    expect(b1.y).toBeGreaterThan(Math.max(a1.y, a2.y))
    h.destroy()
  })
  it('never overlaps two items in the same sub-row', () => {
    const el = host(900, 500)
    const data = Array.from({ length: 60 }, (_, i) => ({ lane: `L${i % 3}`, start: `2026-02-${String(1 + ((i * 7) % 27)).padStart(2, '0')}`, end: `2026-03-${String(1 + ((i * 5) % 27)).padStart(2, '0')}`, label: `i${i}` }))
    const h = swimlane.render(el, data, { animate: false })
    const rows = new Map<number, Array<[number, number]>>()
    for (const r of [...el.querySelectorAll('[data-role="item"]')].map(box)) rows.set(r.y, [...(rows.get(r.y) ?? []), [r.x, r.x + r.w]])
    for (const spans of rows.values()) {
      spans.sort((a, b) => a[0] - b[0])
      for (let i = 1; i < spans.length; i++) expect(spans[i]![0]).toBeGreaterThanOrEqual(spans[i - 1]![1] - 0.01)
    }
    h.destroy()
  })
  it('bar length is proportional to duration', () => {
    const el = host(800, 300)
    const h = swimlane.render(el, [{ lane: 'A', start: '2026-01-01', end: '2026-01-31', label: 'long' }, { lane: 'B', start: '2026-01-01', end: '2026-01-11', label: 'short' }], { animate: false })
    const [a, b] = [...el.querySelectorAll('[data-role="item"]')].map(box)
    expect((a!.w + 1) / (b!.w + 1)).toBeCloseTo(3, 1)
    expect(a!.x).toBeCloseTo(b!.x, 5)
    h.destroy()
  })
  it('colours by category with a legend, and hiding a category repacks the lane', () => {
    const el = host(800, 300)
    const h = swimlane.render(el, [{ lane: 'A', start: '2026-01-01', end: '2026-01-31', label: 'x', category: 'p' }, { lane: 'A', start: '2026-01-05', end: '2026-01-20', label: 'y', category: 'q' }], { animate: false })
    expect(el.querySelectorAll('.oc-legend button').length).toBe(2)
    ;(el.querySelector('.oc-legend button[data-key="q"]') as HTMLElement).click()
    expect(h.markCount()).toBe(1)
    h.destroy()
  })
})
