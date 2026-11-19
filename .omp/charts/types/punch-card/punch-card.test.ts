import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import { makeFormatters } from '../../core'
import punchCard, { bucketOf, model } from './index'
import { options } from './schema'

chartSuite({
  module: punchCard,
  dir: import.meta.dirname,
  role: 'point',
  marks: { basic: 84, 'edge-single': 1, 'edge-duplicates': 84, 'edge-zeros': 84, 'edge-long-labels': 84, 'edge-unicode-rtl': 84 },
  summary: /^Punch card, 1,440 in total across 84 weekday-hour slots, busiest Tue 14:00 at 46, quietest Sat 08:00 at 3$/,
  malformed: [
    { name: 'a text value', data: [{ day: 'Mon', hour: 1, value: 1 }, { day: 'Tue', hour: 2, value: 'many' }], message: /field 'value' must be a number, got 'many' at row 2/ },
    { name: 'an hour out of range', data: [{ day: 'Mon', hour: 24, value: 1 }], message: /field 'hour': must be a whole hour from 0 to 23 at row 1/ },
    { name: 'an unknown weekday', data: [{ day: 'Someday', hour: 3, value: 1 }], message: /field 'day': must be a weekday name .* at row 1/ },
    { name: 'a row with neither date nor day', data: [{ hour: 3, value: 1 }], message: /field 'day': needs a 'date', or both a 'day' and an 'hour' at row 1/ },
    { name: 'a timestamp that is not a date', data: [{ date: 'yesterday-ish' }], message: /field 'date': must be a date .* at row 1/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
  ],
})

const parse = (data: unknown, o: Record<string, unknown> = {}) => {
  const v = punchCard.validate(data, o)
  expect(v.errors).toEqual([])
  return v.data!
}
const opts = (o: Record<string, unknown> = {}) => ({ ...options.parse(o), ...o })

describe('punch-card: bucketing', () => {
  it('buckets raw timestamps by weekday and hour in the time zone', () => {
    // Monday 2026-03-02 01:30 UTC is Sunday 20:30 in New York (EST, UTC-5)
    const rows = parse([{ date: '2026-03-02T01:30:00Z' }])
    expect(bucketOf(rows[0]!, 'UTC')).toEqual({ day: 1, hour: 1 })
    expect(bucketOf(rows[0]!, 'America/New_York')).toEqual({ day: 0, hour: 20 })
    // ...and 07:00 on the Tuesday in Kolkata (UTC+5:30)
    expect(bucketOf(rows[0]!, 'Asia/Kolkata')).toEqual({ day: 1, hour: 7 })
  })
  it('reads tz-less timestamps as wall-clock time in the time zone', () => {
    const rows = parse([{ date: '2026-03-04 09:15' }])
    expect(bucketOf(rows[0]!, 'America/Los_Angeles')).toEqual({ day: 3, hour: 9 })
    expect(bucketOf(rows[0]!, 'Asia/Tokyo')).toEqual({ day: 3, hour: 9 })
  })
  it('the same instant in different offsets lands in one bucket and the values add up', () => {
    const fmt = makeFormatters({ timeZone: 'UTC' })
    const m = model(parse([
      { date: '2026-03-05T14:10:00Z', value: 2 },
      { date: '2026-03-05T19:40:00+05:30', value: 3 },
      { date: Date.UTC(2026, 2, 5, 14, 59), value: 4 },
    ]), opts(), fmt)
    expect(m.value[4]![14]).toBe(9)
    expect(m.total).toBe(9)
    expect(m.duplicates).toBe(0)
  })
  it('counts rows without a value column once each', () => {
    const m = model(parse({ date: ['2026-03-02T10:00:00Z', '2026-03-02T10:30:00Z', '2026-03-03T11:00:00Z'] }), opts(), makeFormatters())
    expect(m.value[1]![10]).toBe(2)
    expect(m.value[2]![11]).toBe(1)
  })
  it('accepts weekday names, 0-6 numbers and calendar dates as the day', () => {
    const rows = parse([{ day: 'Thursday', hour: 3 }, { day: 0, hour: 3 }, { day: '2026-03-07', hour: 3 }, { day: 'fri', hour: 3 }])
    expect(rows.map((r) => bucketOf(r, 'Pacific/Kiritimati')!.day)).toEqual([4, 0, 6, 5])
  })
  it('sums duplicate day/hour rows and notes it; skips negatives with a note', () => {
    const el = host()
    const h = punchCard.render(el, [{ day: 'Mon', hour: 9, value: 2 }, { day: 'Mon', hour: 9, value: 3 }, { day: 'Tue', hour: 9, value: -1 }], { animate: false })
    expect(h.markCount()).toBe(1)
    expect(el.querySelector('.oc-note')!.textContent).toBe('1 duplicate day/hour rows summed; 1 negative values omitted')
    expect(h.exportCSV()).toBe('Day,Hour,Value\nMon,9,5')
    h.destroy()
  })
})

describe('punch-card: geometry', () => {
  it('circle area is proportional to the value', () => {
    const el = host(800, 320)
    const h = punchCard.render(el, [{ day: 'Mon', hour: 1, value: 100 }, { day: 'Mon', hour: 2, value: 25 }], { animate: false })
    const rs = [...el.querySelectorAll('[data-role="point"]')].map((c) => Number(c.getAttribute('r')))
    expect((rs[0]! / rs[1]!) ** 2).toBeCloseTo(4, 1)
    h.destroy()
  })
  it('rows follow weekStart and hours run 0-23 left to right', () => {
    const el = host(800, 320)
    const data = [{ day: 'Sun', hour: 0, value: 1 }, { day: 'Mon', hour: 23, value: 1 }]
    const h = punchCard.render(el, data, { animate: false })
    const [mon, sun] = ['1:23', '0:0'].map((k) => el.querySelector(`[data-key="circle:${k}"]`)!)
    expect(Number(mon!.getAttribute('cy'))).toBeLessThan(Number(sun!.getAttribute('cy')))
    expect(Number(mon!.getAttribute('cx'))).toBeGreaterThan(Number(sun!.getAttribute('cx')))
    h.update(data, { weekStart: 'sunday' })
    const [mon2, sun2] = ['1:23', '0:0'].map((k) => el.querySelector(`[data-key="circle:${k}"]`)!)
    expect(Number(mon2!.getAttribute('cy'))).toBeGreaterThan(Number(sun2!.getAttribute('cy')))
    h.destroy()
  })
})
