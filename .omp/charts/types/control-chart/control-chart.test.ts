import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import controlChart, { controlModel } from './index'

chartSuite({
  module: controlChart,
  dir: import.meta.dirname,
  marks: { basic: 20, realistic: 40, 'edge-single': 1, 'edge-duplicates': 40, 'edge-large': 5200, 'edge-zeros': 20, 'edge-unsorted-dates': 20, 'edge-mixed-tz': 20 },
  summary: /^Control chart, 20 measurements from Mar 2 to Mar 21, mean 50\.5, control limits 44\.\d+ to 56\.\d+ \(individuals, 3σ\), 1 out of control: Mar 14 \(58\.6, above UCL\)$/,
  malformed: [
    { name: 'a text value', data: [{ x: 1, value: 2 }, { x: 2, value: 'high' }], message: /field 'value' must be a number, got 'high' at row 2/ },
    { name: 'an x that is neither date nor number', data: [{ x: 'yesterday-ish', value: 2 }], message: /field 'x': must be a date or a number at row 1/ },
    { name: 'a scalar', data: true, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'a mapping to a missing column', data: [{ n: 1, w: 2 }], options: { fields: { x: 'n', value: 'weight' } }, message: /column 'weight' not found/ },
  ],
})

const rows = (vs: Array<number | null>) => vs.map((value, i) => ({ x: i + 1, value }))

describe('control-chart: limits', () => {
  it('individuals: sigma = mean moving range / 1.128', () => {
    const m = controlModel(rows([5, 7, 6, 8, 6]), { method: 'individuals', sigmas: 3 })
    // mean 6.4; moving ranges 2,1,2,2 -> 1.75; sigma 1.75/1.128
    expect(m.limits.mean).toBeCloseTo(6.4, 12)
    expect(m.limits.sigma).toBeCloseTo(1.75 / 1.128, 12)
    expect(m.limits.ucl).toBeCloseTo(6.4 + 3 * (1.75 / 1.128), 12)
    expect(m.limits.lcl).toBeCloseTo(6.4 - 3 * (1.75 / 1.128), 12)
    expect(m.limits.ucl).toBeCloseTo(11.0543, 3)
  })
  it('sigma method: sample standard deviation, and the sigmas multiplier', () => {
    const m = controlModel(rows([5, 7, 6, 8, 6]), { method: 'sigma', sigmas: 2 })
    // squared deviations 1.96 .36 .16 2.56 .16 = 5.2; / (n-1) = 1.3
    expect(m.limits.sigma).toBeCloseTo(Math.sqrt(1.3), 12)
    expect(m.limits.ucl).toBeCloseTo(6.4 + 2 * Math.sqrt(1.3), 12)
  })
  it('flags out-of-control points after sorting by x, with their direction', () => {
    const data = [
      { x: '2026-01-05', value: 10.2 }, { x: '2026-01-01', value: 10 }, { x: '2026-01-03', value: 9.8 }, { x: '2026-01-02', value: 10.5 },
      { x: '2026-01-04', value: 25 }, { x: '2026-01-06', value: 9.9 }, { x: '2026-01-07', value: 10.3 }, { x: '2026-01-08', value: -6 },
    ]
    const v = controlChart.validate(data)
    const m = controlModel(v.data!, { method: 'sigma', sigmas: 1.5 })
    expect(m.kind).toBe('time')
    expect(m.points.map((p) => p.value)).toEqual([10, 10.5, 9.8, 25, 10.2, 9.9, 10.3, -6])
    expect(m.limits.outOfControl).toEqual([3, 7])
    expect(m.points.filter((p) => p.signal).map((p) => [p.value, p.signal])).toEqual([[25, 'above'], [-6, 'below']])
    // drawn as negative-colour diamonds, distinct from the in-control circles
    const el = host(640, 400)
    const h = controlChart.render(el, data, { animate: false, method: 'sigma', sigmas: 1.5 })
    const sig = [...el.querySelectorAll('[data-role="signal"]')]
    expect(sig.map((s) => s.tagName.toLowerCase())).toEqual(['path', 'path'])
    expect(el.querySelectorAll('[data-role="point"]').length).toBe(6)
    expect(h.exportCSV().split('\n').filter((l) => /above UCL|below LCL/.test(l))).toHaveLength(2)
    h.destroy()
  })
  it('places the limit lines at their values', () => {
    const el = host(640, 400)
    const h = controlChart.render(el, rows([5, 7, 6, 8, 6]), { animate: false })
    const y = (k: string) => Number(el.querySelector(`[data-key="line:rule:${k}"]`)!.getAttribute('y1'))
    const L = controlModel(rows([5, 7, 6, 8, 6]), { method: 'individuals', sigmas: 3 }).limits
    // equal distance mean->ucl and mean->lcl, proportional to sigma
    expect(y('mean') - y('ucl')).toBeCloseTo(y('lcl') - y('mean'), 1)
    expect(el.querySelector('svg')!.textContent).toContain(`UCL ${L.ucl.toFixed(2)}`)
    h.destroy()
  })
  it('uses row order when no x is given, numbers stay numbers, and dates win over epoch numbers', () => {
    expect(controlModel([{ x: null, value: 1 }, { x: null, value: 2 }], { method: 'individuals', sigmas: 3 }).points.map((p) => p.t)).toEqual([1, 2])
    expect(controlModel([{ x: 7, value: 1 }, { x: 3, value: 2 }], { method: 'individuals', sigmas: 3 }).points.map((p) => p.t)).toEqual([3, 7])
    const mixed = controlChart.validate([{ x: '2026-01-02T00:00:00Z', value: 1 }, { x: Date.UTC(2026, 0, 1), value: 2 }])
    expect(controlModel(mixed.data!, { method: 'individuals', sigmas: 3 }).points.map((p) => p.value)).toEqual([2, 1])
  })
  it('notes that one value or no variation gives no limits', () => {
    const el = host()
    const h = controlChart.render(el, rows([4, 4, 4]), { animate: false })
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/no variation, so no control limits/)
    expect(el.querySelector('[data-key="line:rule:ucl"]')).toBeNull()
    h.update(rows([4]))
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/control limits need at least 2 values/)
    h.destroy()
  })
})
