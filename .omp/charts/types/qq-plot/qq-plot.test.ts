import { describe, expect, it } from 'vitest'
import { stats } from '../../core'
import { chartSuite, host } from '../../tests/harness'
import qqPlot from './index'

chartSuite({
  module: qqPlot,
  dir: import.meta.dirname,
  role: 'point',
  marks: { basic: 30, realistic: 120, 'edge-single': 1, 'edge-zeros': 30, 'edge-duplicates': 60, 'edge-nulls': 15, 'edge-large': 5400, 'edge-skewed': 200 },
  summary: /^Q-Q plot, 30 values against a normal distribution, from [\d.,]+ to [\d.,]+, median [\d.,]+, close to normal \(r = 0\.9\d+\)$/,
  malformed: [
    { name: 'a text value', data: [{ value: 1 }, { value: 'abc' }], message: /field 'value' must be a number, got 'abc' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'a mapping to a missing column', data: [{ mm: 1 }], options: { fields: { value: 'diameter' } }, message: /column 'diameter' not found/ },
  ],
})

const circles = (el: Element) => [...el.querySelectorAll('[data-role="point"]')].map((c) => ({ x: Number(c.getAttribute('cx')), y: Number(c.getAttribute('cy')) }))

describe('qq-plot: points', () => {
  const sample = [5, 1, 4, 2, 3].map((value) => ({ value }))
  it('pairs each sorted value with the normal quantile of (i - 0.5) / n', () => {
    const q = stats.qqPoints(sample.map((d) => d.value))
    expect(q.map((p) => p.sample)).toEqual([1, 2, 3, 4, 5])
    const want = [-1.2815516, -0.5244005, 0, 0.5244005, 1.2815516]
    q.forEach((p, i) => expect(p.theoretical).toBeCloseTo(want[i]!, 6))
    // the line through the quartiles: (z25, 2) and (z75, 4)
    const line = stats.qqLine(sample.map((d) => d.value))!
    expect(line.intercept).toBeCloseTo(3, 10)
    expect(line.slope).toBeCloseTo(2 / (2 * 0.6744897502), 8)
  })
  it('places the points at those quantiles and lists them in the table', () => {
    const el = host(640, 480)
    const h = qqPlot.render(el, sample, { animate: false })
    const cs = circles(el).sort((a, b) => a.x - b.x)
    // x is linear in the theoretical quantile: the middle point (z = 0) sits half way
    expect(cs[2]!.x).toBeCloseTo((cs[0]!.x + cs[4]!.x) / 2, 6)
    // ratio of the inner to the outer quantile carries through the scale
    // (pixel coordinates are rounded to 0.01 px, hence 3 digits)
    expect((cs[3]!.x - cs[2]!.x) / (cs[4]!.x - cs[2]!.x)).toBeCloseTo(0.5244005 / 1.2815516, 3)
    // y increases with the sorted value (higher is up)
    for (let i = 1; i < 5; i++) expect(cs[i]!.y).toBeLessThan(cs[i - 1]!.y)
    const rows = h.exportCSV().trim().split('\n')
    expect(rows[0]).toBe('Value,Normal quantile')
    expect(rows[1]!.startsWith('1,-1.28155')).toBe(true)
    expect(rows[3]!.startsWith('3,')).toBe(true)
    h.destroy()
  })
  it('the reference line passes through the quartile points', () => {
    const el = host(640, 480)
    const h = qqPlot.render(el, sample, { animate: false })
    const l = el.querySelector('[data-key="line:qq-line"]')!
    const [x1, y1, x2, y2] = ['x1', 'y1', 'x2', 'y2'].map((a) => Number(l.getAttribute(a)))
    // z = 0 maps to the median (3), which is the middle point
    const mid = circles(el).sort((a, b) => a.x - b.x)[2]!
    const yAtMid = y1! + ((mid.x - x1!) / (x2! - x1!)) * (y2! - y1!)
    expect(yAtMid).toBeCloseTo(mid.y, 4)
    h.destroy()
  })
  it('names the departure from normal in the summary', () => {
    const el = host(640, 480)
    const skewed = Array.from({ length: 200 }, (_, i) => ({ value: Math.exp(stats.normalQuantile((i + 0.5) / 200)) }))
    const h = qqPlot.render(el, skewed, { animate: false })
    expect(el.querySelector('svg.oc-svg')!.getAttribute('aria-label')).toMatch(/skewed right \(r = 0\.\d+\)$/)
    h.update(Array.from({ length: 200 }, (_, i) => ({ value: Math.tan(Math.PI * ((i + 0.5) / 200 - 0.5)) })))
    expect(el.querySelector('svg.oc-svg')!.getAttribute('aria-label')).toMatch(/heavy tails \(r = 0\.\d+\)$/)
    h.destroy()
  })
  it('standardize plots z-scores', () => {
    const el = host(640, 480)
    const h = qqPlot.render(el, [{ value: 10 }, { value: 20 }, { value: 30 }], { animate: false, standardize: true })
    h.focusMark(0)
    expect(el.querySelector('[aria-live]')!.textContent).toMatch(/^1 of 3: Rank 1 of 3: 10 \(z [-−]1(\.00)?\) at normal quantile [-−]0\.97$/)
    h.destroy()
  })
})
