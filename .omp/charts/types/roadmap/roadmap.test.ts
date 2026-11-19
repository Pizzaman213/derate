import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import roadmap, { parseQuarter, statusKind } from './index'

chartSuite({
  module: roadmap,
  dir: import.meta.dirname,
  role: 'item',
  marks: { basic: 9, realistic: 13, 'edge-single': 1, 'edge-zeros': 9, 'edge-duplicates': 18, 'edge-large': 5200 },
  summary: /^Roadmap, 9 items in 4 lanes, Q1 2026 to Q4 2026, 2 done, 2 in progress, 1 at risk, 3 planned, 1 blocked$/,
  malformed: [
    { name: 'end before start', data: [{ item: 'A', lane: 'L', start: '2026-Q3', end: '2026-03-01' }], message: /field 'end': end 2026-03-01 is before start 2026-07-01 at row 1/ },
    { name: 'a fifth quarter', data: [{ item: 'A', lane: 'L', start: '2026-Q5' }], message: /field 'start' must be a date, got '2026-Q5' at row 1/ },
  ],
})

const iso = (d: unknown) => (d instanceof Date ? d.toISOString().slice(0, 10) : String(d))

describe('roadmap: quarters', () => {
  it('parses quarter spellings', () => {
    expect(parseQuarter('2026-Q1')).toEqual([2026, 1])
    expect(parseQuarter('FY27 Q2')).toEqual([2027, 2])
    expect(parseQuarter('fy2027-q3')).toEqual([2027, 3])
    expect(parseQuarter('Q4 2026')).toEqual([2026, 4])
    expect(parseQuarter('Q4 FY27')).toEqual([2027, 4])
    expect(parseQuarter('27 Q1')).toBeNull()
    expect(parseQuarter('2026-Q5')).toBeNull()
  })
  it('reads a start quarter as its first day and an end quarter as the start of the next', () => {
    const v = roadmap.validate([{ item: 'A', lane: 'L', start: '2026-Q2', end: '2026-Q4' }])
    expect(iso(v.data![0]!.start)).toBe('2026-04-01')
    expect(iso(v.data![0]!.end)).toBe('2027-01-01')
  })
  it('reads quarters in the fiscal calendar and the time zone', () => {
    const v = roadmap.validate([{ phase: 'A', theme: 'L', start: 'FY27 Q1', end: 'FY27 Q1' }], { fiscalYearStart: 4 })
    expect(v.data![0]!.item).toBe('A')
    expect(v.data![0]!.lane).toBe('L')
    expect(iso(v.data![0]!.start)).toBe('2026-04-01')
    expect(iso(v.data![0]!.end)).toBe('2026-07-01')
    const t = roadmap.validate([{ item: 'A', lane: 'L', start: '2026-Q1' }], { timeZone: 'Asia/Tokyo' })
    expect((t.data![0]!.start as Date).toISOString()).toBe('2025-12-31T15:00:00.000Z')
  })
  it('maps status words to kinds', () => {
    expect(statusKind('Done')).toBe('done')
    expect(statusKind('in-progress')).toBe('progress')
    expect(statusKind('At risk')).toBe('risk')
    expect(statusKind('on hold')).toBe('blocked')
    expect(statusKind('backlog')).toBe('planned')
    expect(statusKind('Exploring')).toBe('planned')
    expect(statusKind('Vibes')).toBe('other')
    expect(statusKind(undefined)).toBe('none')
  })
})

describe('roadmap: geometry', () => {
  it('bars span whole quarters and labels the fiscal header', () => {
    const el = host(800, 300)
    const h = roadmap.render(el, [{ item: 'A', lane: 'L', start: 'FY27 Q1', end: 'FY27 Q2' }, { item: 'B', lane: 'L', start: 'FY27 Q3', end: 'FY27 Q4' }], { animate: false, fiscalYearStart: 4 })
    const [a, b] = [...el.querySelectorAll('[data-role="item"]')]
    const w = (e: Element) => Number(e.getAttribute('width')), x = (e: Element) => Number(e.getAttribute('x'))
    // two quarters each: equal widths (to within a day of month-length difference)
    expect(w(a!) / w(b!)).toBeCloseTo(1, 1)
    expect(x(b!)).toBeGreaterThan(x(a!) + w(a!))
    const header = [...el.querySelectorAll('.oc-axes text')].map((t) => t.textContent)
    expect(header).toContain('FY27')
    expect(header.filter((t) => /^Q[1-4]$/.test(t ?? '')).length).toBe(4)
    h.destroy()
  })
  it('rejects a fiscal start outside 1-12 with an error state', () => {
    const el = host(800, 300)
    const h = roadmap.render(el, [{ item: 'A', lane: 'L', start: '2026-Q1' }], { animate: false, fiscalYearStart: 13 })
    expect(h.state).toBe('error')
    expect(h.errors.map((e) => e.message).join()).toMatch(/fiscalYearStart/)
    h.destroy()
  })
  it('colours by status with a legend; planned work is dashed; toggling hides it', () => {
    const el = host(800, 300)
    const h = roadmap.render(el, [{ item: 'A', lane: 'L', start: '2026-Q1', status: 'done' }, { item: 'B', lane: 'L', start: '2026-Q2', status: 'planned' }], { animate: false })
    expect([...el.querySelectorAll('.oc-legend button')].map((b) => b.textContent)).toEqual(['done', 'planned'])
    const [a, b] = [...el.querySelectorAll('[data-role="item"]')]
    expect(a!.getAttribute('style')).not.toMatch(/dasharray/)
    expect(b!.getAttribute('style')).toMatch(/stroke-dasharray/)
    ;(el.querySelector('.oc-legend button[data-key="planned"]') as HTMLElement).click()
    expect(h.markCount()).toBe(1)
    h.destroy()
  })
  it('puts overlapping items in one lane on separate sub-rows', () => {
    const el = host(800, 300)
    const h = roadmap.render(el, [{ item: 'A', lane: 'L', start: '2026-Q1', end: '2026-Q2' }, { item: 'B', lane: 'L', start: '2026-Q2', end: '2026-Q3' }, { item: 'C', lane: 'L', start: '2026-Q3' }], { animate: false })
    const ys = [...el.querySelectorAll('[data-role="item"]')].map((e) => Number(e.getAttribute('y')))
    expect(ys[0]).not.toBe(ys[1])
    expect(ys[0]).toBe(ys[2])
    h.destroy()
  })
})
