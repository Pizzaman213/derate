import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import calendarHeatmap, { calendarDays, cellOf, cellOfDay } from './index'

chartSuite({
  module: calendarHeatmap,
  dir: import.meta.dirname,
  role: 'cell',
  marks: { basic: 365, 'edge-single': 1, 'edge-duplicates': 120, 'edge-large': 5200, 'edge-leap-week': 366, 'edge-mixed-tz': 4 },
  summary: /^Calendar heatmap, 365 days, Jan 1, 2026 to Dec 31, 2026, total [\d,]+, highest \w+ \d+, 2026 at \d+, lowest \w+ \d+, 2026 at 0$/,
  malformed: [
    { name: 'a text value', data: [{ date: '2026-01-01', value: 'lots' }], message: /field 'value' must be a number, got 'lots' at row 1/ },
    { name: 'a bad date', data: [{ date: '31/31/2026', value: 1 }], message: /field 'date' must be a date, got '31\/31\/2026' at row 1/ },
  ],
})

describe('calendar-heatmap: date -> cell', () => {
  it('Jan 1 is in column 0 on its own weekday row', () => {
    // 2026-01-01 is a Thursday
    expect(cellOfDay(2026, 1, 1, 0)).toMatchObject({ week: 0, row: 4 })
    expect(cellOfDay(2026, 1, 1, 1)).toMatchObject({ week: 0, row: 3 })
    // the following Sunday starts column 1 in Sunday-first weeks, stays in column 0 Monday-first
    expect(cellOfDay(2026, 1, 4, 0)).toMatchObject({ week: 1, row: 0 })
    expect(cellOfDay(2026, 1, 4, 1)).toMatchObject({ week: 0, row: 6 })
  })
  it('leap day 2024-02-29 is a Thursday in week 8', () => {
    // Jan 1 2024 is a Monday: Sunday-first puts it in column 0 row 1
    expect(cellOf(new Date('2024-02-29T00:00:00Z'), 'UTC', 0)).toMatchObject({ month: 2, day: 29, week: 8, row: 4 })
    expect(cellOf(new Date('2024-02-29T00:00:00Z'), 'UTC', 1)).toMatchObject({ week: 8, row: 3 })
    expect(cellOfDay(2024, 3, 1, 0)).toMatchObject({ week: 8, row: 5 })
    expect(cellOfDay(2024, 12, 31, 1)).toMatchObject({ week: 52, row: 1 })
  })
  it('reads days in the time zone, across the DST change', () => {
    // 03:30Z on Mar 9 is 23:30 on Sunday Mar 8 in New York (the day clocks went forward)
    const ny = cellOf(new Date('2026-03-09T03:30:00Z'), 'America/New_York', 0)
    expect(ny).toMatchObject({ month: 3, day: 8, row: 0 })
    expect(cellOf(new Date('2026-03-09T03:30:00Z'), 'UTC', 0)).toMatchObject({ day: 9, row: 1 })
    // fall back: 04:30Z on Nov 1 is 00:30 EDT, still Nov 1
    expect(cellOf(new Date('2026-11-01T04:30:00Z'), 'America/New_York', 0)).toMatchObject({ month: 11, day: 1, row: 0 })
    expect(cellOf(new Date('2026-11-01T03:30:00Z'), 'America/New_York', 0)).toMatchObject({ month: 10, day: 31 })
  })
  it('consecutive days walk down rows and then to the next column', () => {
    let prev = cellOfDay(2025, 1, 1, 1)
    for (let i = 1; i < 365; i++) {
      const d = new Date(Date.UTC(2025, 0, 1 + i))
      const c = cellOfDay(2025, d.getUTCMonth() + 1, d.getUTCDate(), 1)
      if (c.row === 0) { expect(c.week).toBe(prev.week + 1); expect(prev.row).toBe(6) } else { expect(c.week).toBe(prev.week); expect(c.row).toBe(prev.row + 1) }
      prev = c
    }
  })
  it('sums rows on the same local day', () => {
    const { days, duplicates } = calendarDays([{ date: new Date('2026-03-08T23:30:00-04:00'), value: 1 }, { date: new Date('2026-03-09T03:00:00Z'), value: 2 }], 'America/New_York', 0)
    expect(days.length).toBe(1)
    expect(days[0]!.value).toBe(3)
    expect(duplicates).toBe(1)
  })
  it('draws every day of the year and keeps squares inside the box', () => {
    const el = host(800, 200)
    const h = calendarHeatmap.render(el, [{ date: '2026-06-01', value: 1 }], { animate: false })
    const rects = [...el.querySelectorAll('svg rect[data-key]')]
    expect(rects.length).toBe(365)
    for (const r of rects) expect(Number(r.getAttribute('x')) + Number(r.getAttribute('width'))).toBeLessThanOrEqual(800)
    h.destroy()
  })
})
