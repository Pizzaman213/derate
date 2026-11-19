import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import burnup, { projectCompletion, type BurnupPoint } from './index'

const day = (n: number) => new Date(Date.UTC(2026, 0, 1) + n * 864e5)
const pts = (rows: Array<[number, number | null, number | null]>): BurnupPoint[] => rows.map(([d, completed, scope]) => ({ date: day(d), completed, scope }))

chartSuite({
  module: burnup,
  dir: import.meta.dirname,
  role: 'point',
  marks: { basic: 20, realistic: 24, 'edge-single': 2, 'edge-duplicates': 20, 'edge-large': 5201, 'edge-zeros': 20, 'edge-unsorted-dates': 20, 'edge-mixed-tz': 20 },
  summary: /^Burnup chart, 10 dates from Jan 2026 to Mar 2026, 89 of 140 completed \(64%\), scope 120 to 140, projected to finish Apr \d+, 2026$/,
  malformed: [
    { name: 'a text completed', data: [{ date: '2026-01-01', completed: 1, scope: 5 }, { date: '2026-01-02', completed: 'half', scope: 5 }], message: /field 'completed' must be a number, got 'half' at row 2/ },
    { name: 'a bad date', data: [{ date: 'next sprint', completed: 1, scope: 2 }], message: /field 'date' must be a date, got 'next sprint' at row 1/ },
    { name: 'a scalar', data: 'nope', message: /must be a list of rows, CSV text, or column arrays|field 'date'/ },
    { name: 'mismatched columns', data: { date: ['2026-01-01', '2026-01-02'], completed: [1], scope: [3, 3] }, message: /column 'completed' has 1 values but column 'date' has 2/ },
  ],
})

describe('burnup: projection', () => {
  it('extends a perfectly linear trend to the scope', () => {
    const p = projectCompletion(pts([[0, 0, 100], [1, 10, 100], [2, 20, 100], [3, 30, 100]]))
    expect(p.status).toBe('projected')
    if (p.status !== 'projected') return
    expect(p.perDay).toBeCloseTo(10, 9)
    expect(+p.date).toBe(+day(10))
    expect(p.target).toBe(100)
  })
  it('matches the closed-form least-squares line and targets the latest scope', () => {
    const rows: Array<[number, number, number]> = [[0, 2, 50], [2, 7, 50], [5, 11, 60], [9, 20, 60]]
    const xs = rows.map((r) => r[0]), ys = rows.map((r) => r[1])
    const mx = xs.reduce((a, b) => a + b) / 4, my = ys.reduce((a, b) => a + b) / 4
    const slope = xs.reduce((s, x, i) => s + (x - mx) * (ys[i]! - my), 0) / xs.reduce((s, x) => s + (x - mx) ** 2, 0)
    const intercept = my - slope * mx
    const p = projectCompletion(pts(rows))
    expect(p.status).toBe('projected')
    if (p.status !== 'projected') return
    expect(p.perDay).toBeCloseTo(slope, 9)
    expect((+p.date - +day(0)) / 864e5).toBeCloseTo((60 - intercept) / slope, 6)
  })
  it('fits only the window', () => {
    // slow start, then 5 a day over the last three readings
    const p = projectCompletion(pts([[0, 0, 40], [5, 1, 40], [10, 2, 40], [11, 7, 40], [12, 12, 40]]), 3)
    expect(p.status).toBe('projected')
    if (p.status !== 'projected') return
    expect(p.perDay).toBeCloseTo(5, 9)
    expect((+p.date - +day(0)) / 864e5).toBeCloseTo(17.6, 6)
  })
  it('reports done, stalled and too-few readings', () => {
    expect(projectCompletion(pts([[0, 5, 10], [1, 10, 10]])).status).toBe('done')
    expect(projectCompletion(pts([[0, 5, 10], [1, 5, 10], [2, 4, 10]])).status).toBe('stalled')
    expect(projectCompletion(pts([[0, 5, 10]])).status).toBe('insufficient')
    expect(projectCompletion(pts([[0, 5, null], [1, 6, null]])).status).toBe('no-scope')
  })
  it('draws the projected date as a focusable marker on the scope level', () => {
    const el = host(640, 400)
    const h = burnup.render(el, [
      { date: '2026-01-01', completed: 0, scope: 100 },
      { date: '2026-01-02', completed: 10, scope: 100 },
      { date: '2026-01-03', completed: 20, scope: 100 },
      { date: '2026-01-04', completed: 30, scope: 100 },
      { date: '2026-01-05', completed: 40, scope: 100 },
      { date: '2026-01-06', completed: 50, scope: 100 },
    ], { animate: false })
    const m = el.querySelector('[data-role="projection"]')!
    expect(m).not.toBeNull()
    expect(h.markCount()).toBe(13)
    expect(el.querySelector('svg')!.textContent).toMatch(/Done ~Jan 11/)
    // the marker sits on the scope level: same y as a scope point
    const scopeY = Number(el.querySelector('[data-key="circle:scope:' + Date.UTC(2026, 0, 1) + '"]')!.getAttribute('cy'))
    const d = m.getAttribute('d')!.match(/-?[\d.]+/g)!.map(Number)
    expect(d[1]! + 5.5).toBeCloseTo(scopeY, 1)
    h.destroy()
  })
  it('notes a stalled project instead of projecting', () => {
    const el = host()
    const h = burnup.render(el, [{ date: '2026-01-01', completed: 3, scope: 9 }, { date: '2026-01-02', completed: 3, scope: 9 }], { animate: false })
    expect(el.querySelector('[data-role="projection"]')).toBeNull()
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/no projection: completed work is not increasing/)
    h.destroy()
  })
})
