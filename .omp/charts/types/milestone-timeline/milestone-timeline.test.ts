import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import milestoneTimeline, { placeLabels, type Slot } from './index'

chartSuite({
  module: milestoneTimeline,
  dir: import.meta.dirname,
  role: 'milestone',
  marks: { basic: 8, realistic: 12, 'edge-single': 1, 'edge-zeros': 8, 'edge-duplicates': 16, 'edge-large': 5200 },
  summary: /^Milestone timeline, 8 milestones, Jan 2026 to Nov 2026, 3 done, next Beta on May 22, 1 overdue$/,
  malformed: [
    { name: 'a bad date', data: [{ date: 'someday', label: 'A' }], message: /field 'date' must be a date, got 'someday' at row 1/ },
    { name: 'a bad done flag', data: [{ date: '2026-01-01', label: 'A', done: 'maybe' }], message: /field 'done' must be a yes\/no value, got 'maybe' at row 1/ },
    { name: 'a scalar', data: 7, message: /must be a list of rows, CSV text, or column arrays/ },
  ],
})

const boxesOf = (slots: Array<Slot | null>, ws: number[]) => slots.flatMap((s, i) => (s ? [{ key: `${s.side}:${s.tier}`, a: s.left, b: s.left + ws[i]! }] : []))
const noCollision = (boxes: Array<{ key: string; a: number; b: number }>, gap: number) => {
  for (const x of boxes) for (const y of boxes) if (x !== y && x.key === y.key) expect(x.b + gap <= y.a || y.b + gap <= x.a).toBe(true)
}

describe('milestone-timeline: label placement', () => {
  it('alternates sides and never lets two labels in one tier touch', () => {
    const items = Array.from({ length: 40 }, (_, i) => ({ x: 20 + i * 15, w: 30 + (i % 5) * 12 }))
    const slots = placeLabels(items, { minX: 0, maxX: 640, gap: 8, tiers: 99 })
    expect(slots.every(Boolean)).toBe(true)
    expect(slots[0]!.side).toBe(-1)
    expect(slots[1]!.side).toBe(1)
    noCollision(boxesOf(slots, items.map((d) => d.w)), 8)
  })
  it('keeps boxes inside the bounds and drops only what exceeds the tier budget', () => {
    const items = Array.from({ length: 30 }, () => ({ x: 5, w: 50 }))
    const slots = placeLabels(items, { minX: 0, maxX: 300, gap: 4, tiers: 2 })
    expect(slots.filter(Boolean).length).toBe(4)
    for (const s of slots) if (s) expect(s.left).toBeGreaterThanOrEqual(0)
  })
})

describe('milestone-timeline: rendering', () => {
  it('fills done milestones, hollows upcoming ones, and keeps labels apart', () => {
    const el = host(800, 320)
    const data = Array.from({ length: 12 }, (_, i) => ({ date: `2026-${String(i + 1).padStart(2, '0')}-01`, label: `Milestone ${i + 1}`, done: i < 5 }))
    const h = milestoneTimeline.render(el, data, { animate: false })
    const ms = [...el.querySelectorAll('[data-role="milestone"]')]
    expect(ms.length).toBe(12)
    const fill = (m: Element) => /fill:([^;]+)/.exec(m.getAttribute('style') ?? '')?.[1]
    expect(new Set(ms.slice(0, 5).map(fill)).size).toBe(1)
    expect(fill(ms[0]!)).not.toBe(fill(ms[11]!))
    // the diamonds sit on one line, in date order
    const cy = ms.map((m) => Number(/L[\d.]+,([\d.]+)/.exec(m.getAttribute('d')!)![1]))
    const xs = ms.map((m) => Number(/M([\d.]+),/.exec(m.getAttribute('d')!)![1]))
    for (let i = 1; i < 12; i++) expect(xs[i]!).toBeGreaterThan(xs[i - 1]!)
    expect(new Set(cy).size).toBe(1)
    h.destroy()
  })
  it('marks overdue milestones once today is given, and draws the today line', () => {
    const el = host(640, 300)
    const data = [{ date: '2026-01-01', label: 'A', done: true }, { date: '2026-02-01', label: 'B' }, { date: '2026-03-01', label: 'C' }]
    let h = milestoneTimeline.render(el, data, { animate: false })
    expect(el.querySelector('[data-key="line:today-line"]')).toBeNull()
    expect([...el.querySelectorAll('.oc-legend button')].map((b) => b.textContent)).toEqual(['Done', 'Upcoming'])
    h.destroy()
    h = milestoneTimeline.render(el, data, { animate: false, today: '2026-02-15' })
    expect(el.querySelector('[data-key="line:today-line"]')).not.toBeNull()
    expect([...el.querySelectorAll('.oc-legend button')].map((b) => b.textContent)).toEqual(['Done', 'Upcoming', 'Overdue'])
    expect(h.summary()).toMatch(/1 overdue/)
    h.destroy()
  })
})
