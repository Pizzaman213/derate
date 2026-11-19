import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import cyclePlot, { cycleModel } from './index'

chartSuite({
  module: cyclePlot,
  dir: import.meta.dirname,
  role: 'point',
  marks: { basic: 36, realistic: 182, 'edge-single': 1, 'edge-duplicates': 36, 'edge-nulls': 31, 'edge-quarter': 28, 'edge-large': 5200, 'edge-mixed-tz': 36 },
  summary: /^Cycle plot, 36 values by month of year, 2024 to 2026, highest mean \w{3} at [\d.]+, lowest mean \w{3} at [\d.]+$/,
  malformed: [
    { name: 'a text value', data: [{ date: '2026-01-01', value: 'n/a' }], message: /field 'value' must be a number, got 'n\/a' at row 1/ },
    { name: 'a bad date', data: [{ date: 'soon', value: 1 }], message: /field 'date' must be a date, got 'soon' at row 1/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
  ],
})

const d = (date: string, value: number | null) => ({ date: new Date(date), value })

describe('cycle-plot: grouping', () => {
  it('month-of-year: one panel per month, one point per year, mean per month', () => {
    const m = cycleModel([d('2024-01-15', 10), d('2025-01-15', 20), d('2024-02-01', 5), d('2026-01-03', 30)], 'month-of-year', 'UTC', 1)
    expect(m.positions.length).toBe(12)
    expect(m.cycles).toEqual([2024, 2025, 2026])
    expect(m.positions[0]!.points.map((p) => [p.cycle, p.value])).toEqual([[2024, 10], [2025, 20], [2026, 30]])
    expect(m.positions[0]!.mean).toBe(20)
    expect(m.positions[1]!.mean).toBe(5)
    expect(m.positions[2]!.mean).toBeNull()
  })
  it('averages readings in the same month and year', () => {
    const m = cycleModel([d('2024-03-01', 10), d('2024-03-20', 30)], 'month-of-year', 'UTC', 1)
    expect(m.positions[2]!.points).toMatchObject([{ value: 20, n: 2 }])
    expect(m.averaged).toBe(1)
  })
  it('quarter groups three months together', () => {
    const m = cycleModel([d('2024-01-01', 1), d('2024-03-31', 3), d('2024-04-01', 7)], 'quarter', 'UTC', 1)
    expect(m.positions.map((p) => p.label)).toEqual(['Q1', 'Q2', 'Q3', 'Q4'])
    expect(m.positions[0]!.points[0]!.value).toBe(2)
    expect(m.positions[1]!.points[0]!.value).toBe(7)
  })
  it('day-of-week orders by weekStart and groups by week', () => {
    // 2026-03-02 is a Monday
    const rows = Array.from({ length: 14 }, (_, i) => d(new Date(Date.UTC(2026, 2, 2 + i)).toISOString(), i))
    const mon = cycleModel(rows, 'day-of-week', 'UTC', 1)
    expect(mon.positions.map((p) => p.label)).toEqual(['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'])
    expect(mon.cycles.length).toBe(2)
    expect(mon.positions[0]!.points.map((p) => p.value)).toEqual([0, 7])
    const sun = cycleModel(rows, 'day-of-week', 'UTC', 0)
    expect(sun.positions[0]!.label).toBe('Sun')
    expect(sun.positions[0]!.points.map((p) => p.value)).toEqual([6, 13])
  })
  it('reads the month in the time zone', () => {
    const m = cycleModel([d('2026-02-01T03:00:00Z', 1)], 'month-of-year', 'America/New_York', 1)
    expect(m.positions[0]!.points.length).toBe(1)
  })
  it('a year sits at the same place in every panel', () => {
    const el = host(800, 400)
    const h = cyclePlot.render(el, [d('2024-01-01', 1), d('2025-01-01', 2), d('2025-02-01', 3)], { animate: false })
    const pts = [...el.querySelectorAll('[data-role="point"]')]
    const cx = (k: string) => Number(pts.find((p) => p.getAttribute('data-key')!.endsWith(k))!.getAttribute('cx'))
    const band = cx('p:1:2025') - cx('p:0:2025')
    expect(cx('p:0:2025')).toBeGreaterThan(cx('p:0:2024'))
    expect(cx('p:1:2025') - band).toBeCloseTo(cx('p:0:2025'))
    expect(el.querySelectorAll('[data-role="mean"]').length).toBe(2)
    h.destroy()
  })
})
