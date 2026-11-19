import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import eventTimeline, { fillLabels, placeEvents } from './index'
import { packLanes } from '../_shared/TimelineA'

chartSuite({
  module: eventTimeline,
  dir: import.meta.dirname,
  marks: { basic: 10, realistic: 12, 'edge-single': 1, 'edge-zeros': 10, 'edge-duplicates': 20, 'edge-large': 5200 },
  summary: /^Event timeline, 10 events \(4 ranges\) in 3 categories, Jan 2026 to Oct 2026, from v3\.0 release to User conference$/,
  malformed: [
    { name: 'end before date', data: [{ date: '2026-03-05', end: '2026-03-01', label: 'A' }], message: /field 'end': end 2026-03-01 is before date 2026-03-05 at row 1/ },
    { name: 'a bad date', data: [{ date: 'yesterday', label: 'A' }], message: /field 'date' must be a date, got 'yesterday' at row 1/ },
    { name: 'a scalar', data: 'x', message: /field 'date' is missing|must be a list of rows/ },
  ],
})

const noOverlap = (items: Array<{ lane: number; left: number; right: number }>) => {
  const byLane = new Map<number, Array<[number, number]>>()
  for (const p of items) byLane.set(p.lane, [...(byLane.get(p.lane) ?? []), [p.left, p.right]])
  for (const spans of byLane.values()) {
    spans.sort((a, b) => a[0] - b[0])
    for (let i = 1; i < spans.length; i++) expect(spans[i]![0]).toBeGreaterThanOrEqual(spans[i - 1]![1])
  }
}

describe('event-timeline: lane packing', () => {
  it('packLanes never puts overlapping spans in one lane, and uses the minimum lanes for intervals', () => {
    let s = 3
    const rnd = () => ((s = (s * 1664525 + 1013904223) % 4294967296), s / 4294967296)
    const spans = Array.from({ length: 400 }, () => { const a = rnd() * 1000; return { start: a, end: a + rnd() * 60 } })
    const { lane, lanes } = packLanes(spans)
    noOverlap(spans.map((sp, i) => ({ lane: lane[i]!, left: sp.start, right: sp.end })))
    // greedy first-fit by start is optimal for intervals: lanes = max overlap depth
    const pts = spans.flatMap((sp) => [[sp.start, 1], [sp.end, -1]] as Array<[number, number]>).sort((a, b) => a[0] - b[0] || a[1] - b[1])
    let depth = 0, max = 0
    for (const [, d] of pts) { depth += d; max = Math.max(max, depth) }
    expect(lanes).toBe(max)
  })
  it('placeEvents keeps marks and labels apart within each lane', () => {
    const spans = Array.from({ length: 60 }, (_, i) => ({ x0: 10 + ((i * 37) % 500), x1: 10 + ((i * 37) % 500) + (i % 3 ? 0 : 40), point: i % 3 !== 0, text: `Event number ${i}` }))
    const { placed } = placeEvents(spans, { r: 4, minX: 0, maxX: 640, gap: 8, labelMax: 120, measure: (t) => t.length * 6, truncate: (t) => t })
    noOverlap(placed)
    for (const p of placed) expect(p.label).not.toBeNull()
  })
  it('dense mode labels only where a label has room, never over a neighbour', () => {
    const spans = Array.from({ length: 80 }, (_, i) => ({ x0: (i * 13) % 600, x1: (i * 13) % 600, point: true, text: `E${i}` }))
    const { placed } = placeEvents(spans, { r: 3, gap: 1, labels: false })
    fillLabels(placed, spans.map((s) => s.text), { minX: 0, maxX: 640, gap: 4, labelMax: 60, measure: (t) => t.length * 6, truncate: (t) => t })
    const boxes = placed.flatMap((p) => [{ lane: p.lane, left: p.x0, right: p.x1 }, ...(p.label ? [{ lane: p.lane, left: p.label.anchor === 'start' ? p.label.x : p.label.x - p.label.w, right: p.label.anchor === 'start' ? p.label.x + p.label.w : p.label.x }] : [])])
    noOverlap(boxes)
    expect(placed.some((p) => p.label)).toBe(true)
  })
  it('rendered marks in one lane do not overlap', () => {
    const el = host(640, 360)
    const data = Array.from({ length: 30 }, (_, i) => ({ date: `2026-01-${String(1 + (i % 28)).padStart(2, '0')}`, end: i % 4 === 0 ? `2026-02-${String(1 + (i % 20)).padStart(2, '0')}` : null, label: `Ev ${i}` }))
    const h = eventTimeline.render(el, data, { animate: false })
    const items = [...el.querySelectorAll('[data-mark]')].map((m) => m.tagName === 'circle'
      ? { lane: Number(m.getAttribute('cy')), left: Number(m.getAttribute('cx')) - Number(m.getAttribute('r')), right: Number(m.getAttribute('cx')) + Number(m.getAttribute('r')) }
      : { lane: Number(m.getAttribute('y')) + Number(m.getAttribute('height')) / 2, left: Number(m.getAttribute('x')), right: Number(m.getAttribute('x')) + Number(m.getAttribute('width')) })
    expect(items.length).toBe(30)
    noOverlap(items.map((it) => ({ ...it, lane: Math.round(it.lane) })))
    h.destroy()
  })
  it('draws ranges as bars from date to end and points as dots', () => {
    const el = host(640, 300)
    const h = eventTimeline.render(el, [{ date: '2026-01-01', end: '2026-01-21', label: 'A' }, { date: '2026-01-11', label: 'B' }, { date: '2026-01-21', end: '2026-01-31', label: 'C' }], { animate: false })
    const [a, c] = [...el.querySelectorAll('[data-role="range"]')]
    const b = el.querySelector('[data-role="point"]')!
    const w = (e: Element) => Number(e.getAttribute('width'))
    expect(w(a!) / w(c!)).toBeCloseTo(2, 1)
    expect(Number(b.getAttribute('cx'))).toBeCloseTo(Number(a!.getAttribute('x')) + w(a!) / 2, 0)
    h.destroy()
  })
  it('legend toggling hides a category and repacks', () => {
    const el = host(640, 300)
    const data = [{ date: '2026-01-01', label: 'A', category: 'x' }, { date: '2026-01-01', label: 'B', category: 'y' }]
    const h = eventTimeline.render(el, data, { animate: false })
    expect(h.markCount()).toBe(2)
    ;(el.querySelector('.oc-legend button[data-key="y"]') as HTMLElement).click()
    expect(h.markCount()).toBe(1)
    h.destroy()
  })
})
