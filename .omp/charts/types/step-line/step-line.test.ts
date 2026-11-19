import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import stepLine from './index'

chartSuite({
  module: stepLine,
  dir: import.meta.dirname,
  role: 'point',
  marks: { basic: 10, realistic: 13, 'edge-single': 1, 'edge-duplicates': 10, 'edge-large': 5200 },
  summary: /^Step line chart, 10 points, Jan 2025 to Nov 2026, peak 4\.5 in Jan 2025$/,
  malformed: [
    { name: 'a text value', data: [{ x: '2026-01-01', y: 'high' }], message: /field 'y' must be a number, got 'high' at row 1/ },
    { name: 'a bad date', data: [{ x: '2026-13-45', y: 1 }], message: /field 'x' must be a date/ },
    { name: 'a scalar', data: 'x', message: /must be a list of rows|column 'x'|field 'x'/ },
  ],
})

const path = (el: HTMLElement) => el.querySelector('[data-key^="path:line:"]')!.getAttribute('d')!
const nums = (d: string) => d.replace(/[MLHVZ]/g, ' ').trim().split(/[\s,]+/).map(Number)

describe('step-line: steps', () => {
  const data = [{ x: 0, y: 1 }, { x: 10, y: 3 }, { x: 20, y: 2 }]
  it("'after' holds each value until the next x (horizontal then vertical)", () => {
    const el = host(640, 400)
    const h = stepLine.render(el, data, { animate: false })
    const v = nums(path(el))
    // M x0 y0 L x1 y0 L x1 y1 ...: the second vertex keeps the first y
    expect(v[3]).toBeCloseTo(v[1]!, 3)
    expect(v[2]).toBeCloseTo(v[4]!, 3)
    h.destroy()
  })
  it("'before' changes at the previous x (vertical first)", () => {
    const el = host(640, 400)
    const h = stepLine.render(el, data, { animate: false, step: 'before' })
    const v = nums(path(el))
    expect(v[2]).toBeCloseTo(v[0]!, 3)
    h.destroy()
  })
  it("'center' changes halfway", () => {
    const el = host(640, 400)
    const h = stepLine.render(el, data, { animate: false, step: 'center' })
    const v = nums(path(el))
    const cx = [...el.querySelectorAll('[data-role="point"]')].map((c) => Number(c.getAttribute('cx')))
    expect(v[2]).toBeCloseTo((cx[0]! + cx[1]!) / 2, 1)
    h.destroy()
  })
  it('one stepped line per series when series is given', () => {
    const el = host(800, 400)
    const h = stepLine.render(el, [{ x: 1, y: 1, series: 'a' }, { x: 2, y: 2, series: 'a' }, { x: 1, y: 3, series: 'b' }, { x: 2, y: 1, series: 'b' }], { animate: false })
    expect(el.querySelectorAll('[data-key^="path:line:"]').length).toBe(2)
    expect(h.exportCSV().split('\n')[0]).toBe('X,Series,Value')
    h.destroy()
  })
})
