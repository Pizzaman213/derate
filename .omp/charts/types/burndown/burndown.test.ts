import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import burndown, { computeIdeal, prepareBurndown } from './index'

const d = (s: string) => new Date(`${s}T00:00:00Z`)

chartSuite({
  module: burndown,
  dir: import.meta.dirname,
  role: 'point',
  marks: { basic: 10, realistic: 18, 'edge-single': 1, 'edge-duplicates': 10, 'edge-large': 5200, 'edge-zeros': 10, 'edge-nulls': 5, 'edge-unsorted-dates': 10, 'edge-mixed-tz': 10 },
  summary: /^Burndown chart, 10 dates from Mar 2 to Mar 13, remaining 80 to 18, 18 behind ideal at Mar 13$/,
  malformed: [
    { name: 'a text remaining', data: [{ date: '2026-03-02', remaining: 1 }, { date: '2026-03-03', remaining: 'lots' }], message: /field 'remaining' must be a number, got 'lots' at row 2/ },
    { name: 'a bad date', data: [{ date: 'someday', remaining: 1 }], message: /field 'date' must be a date, got 'someday' at row 1/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'a mapping to a missing column', data: [{ day: '2026-03-02', left: 1 }], options: { fields: { date: 'day', remaining: 'lefft' } }, message: /column 'lefft' not found/ },
  ],
})

describe('burndown: ideal line', () => {
  it('runs straight from the first reading to zero at the last date', () => {
    const rows = [
      { date: d('2026-03-01'), remaining: 100 },
      { date: d('2026-03-03'), remaining: 90 },
      { date: d('2026-03-06'), remaining: 40 },
      { date: d('2026-03-11'), remaining: 30 },
    ]
    const ideal = computeIdeal(rows)
    expect(ideal.given).toBe(false)
    expect(ideal.line).toEqual({ t0: +d('2026-03-01'), start: 100, tEnd: +d('2026-03-11') })
    expect(ideal.at(+d('2026-03-01'))).toBe(100)
    expect(ideal.at(+d('2026-03-03'))).toBeCloseTo(80, 9)
    expect(ideal.at(+d('2026-03-06'))).toBeCloseTo(50, 9)
    expect(ideal.at(+d('2026-03-11'))).toBe(0)
    expect(ideal.at(+d('2026-02-28'))).toBeNull()
  })
  it('starts at the first non-null reading and reaches zero at the end option', () => {
    const p = prepareBurndown([
      { date: d('2026-03-01'), remaining: null, ideal: null, scopeChange: null },
      { date: d('2026-03-02'), remaining: 60, ideal: null, scopeChange: null },
      { date: d('2026-03-05'), remaining: 50, ideal: null, scopeChange: null },
    ], { end: '2026-03-08' })
    expect(p.points.map((r) => r.ideal)).toEqual([null, 60, 30])
    expect(p.end?.toISOString()).toBe('2026-03-08T00:00:00.000Z')
  })
  it('uses given ideal values verbatim', () => {
    const p = prepareBurndown([
      { date: d('2026-03-01'), remaining: 10, ideal: 12, scopeChange: null },
      { date: d('2026-03-02'), remaining: 9, ideal: 7, scopeChange: null },
    ], {})
    expect(p.ideal.given).toBe(true)
    expect(p.points.map((r) => r.ideal)).toEqual([12, 7])
  })
  it('sorts, merges duplicate dates (last reading, summed scope) and says so', () => {
    const el = host()
    const h = burndown.render(el, [
      { date: '2026-03-03', remaining: 8 },
      { date: '2026-03-01', remaining: 10, scopeChange: 2 },
      { date: '2026-03-01', remaining: 11, scopeChange: 3 },
    ], { animate: false })
    expect(h.markCount()).toBe(3) // two points + one scope flag
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 duplicate dates merged/)
    expect(h.exportCSV().split('\n')[1]).toMatch(/^"?Mar 1, 2026"?,11,11,5,$/)
    h.destroy()
  })
  it('draws the computed ideal line from the first point to zero at the right edge', () => {
    const el = host(640, 400)
    const h = burndown.render(el, [{ date: '2026-03-01', remaining: 40 }, { date: '2026-03-05', remaining: 30 }], { animate: false, legend: false })
    const ideal = el.querySelector('[data-key="path:ideal"]')!.getAttribute('d')!
    const nums = ideal.match(/-?[\d.]+/g)!.map(Number)
    const first = el.querySelector('[data-role="point"]')!
    expect(nums[0]).toBeCloseTo(Number(first.getAttribute('cx')), 1)
    expect(nums[1]).toBeCloseTo(Number(first.getAttribute('cy')), 1)
    // ends on the zero baseline = the x axis line
    const axis = el.querySelector('[data-key="line:x-domain"]')!
    expect(nums[3]).toBeCloseTo(Number(axis.getAttribute('y1')), 1)
    h.destroy()
  })
  it('hides the ideal line through the legend', () => {
    const el = host()
    const h = burndown.render(el, [{ date: '2026-03-01', remaining: 40 }, { date: '2026-03-05', remaining: 30 }], { animate: false })
    const btn = [...el.querySelectorAll('.oc-legend button')].find((b) => /Ideal/.test(b.textContent ?? ''))! as HTMLElement
    btn.click()
    expect(el.querySelector('[data-key="path:ideal"]')).toBeNull()
    expect(h.markCount()).toBe(2)
    h.destroy()
  })
})
