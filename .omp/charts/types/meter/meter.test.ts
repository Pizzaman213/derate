import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import meter, { meterBounds, meterSegments, meterZone } from './index'

chartSuite({
  module: meter,
  dir: import.meta.dirname,
  role: 'meter',
  marks: { basic: 4, realistic: 6, 'edge-single': 1, 'edge-zeros': 3, 'edge-nulls': 4, 'edge-duplicates': 3, 'edge-outliers': 3 },
  summary: /^Meter, 4 meters, 1 optimum, 1 suboptimum, 2 poor$/,
  malformed: [
    { name: 'a text value', data: [{ label: 'a', value: 'high' }], message: /field 'value' must be a number, got 'high' at row 1/ },
    { name: 'max not above min', data: { value: 1, min: 5, max: 5 }, message: /max \(5\) must be greater than min \(5\)/ },
  ],
})

const m = (value: number | null, o: { min?: number; max?: number; low?: number | null; high?: number | null; optimum?: number | null } = {}) =>
  ({ value, min: o.min ?? 0, max: o.max ?? 1, low: o.low ?? null, high: o.high ?? null, optimum: o.optimum ?? null })

describe('meter: zone classification (HTML <meter> rules)', () => {
  it('applies the HTML defaults and clamps', () => {
    expect(meterBounds(m(0.5))).toEqual({ min: 0, max: 1, low: 0, high: 1, optimum: 0.5 })
    // high below low is raised to low; out-of-range bounds clamp
    expect(meterBounds(m(0.5, { low: 0.7, high: 0.4, optimum: 3 }))).toEqual({ min: 0, max: 1, low: 0.7, high: 0.7, optimum: 1 })
  })
  it('optimum in the middle: middle is optimum, both ends suboptimum', () => {
    const o = { low: 0.3, high: 0.7, optimum: 0.5 }
    expect(meterZone(m(0.5, o))).toBe('optimum')
    expect(meterZone(m(0.3, o))).toBe('optimum') // low..high is inclusive
    expect(meterZone(m(0.7, o))).toBe('optimum')
    expect(meterZone(m(0.1, o))).toBe('suboptimum')
    expect(meterZone(m(0.9, o))).toBe('suboptimum')
  })
  it('optimum low: low is optimum, middle suboptimum, high poor', () => {
    const o = { low: 0.3, high: 0.7, optimum: 0.1 }
    expect(meterZone(m(0.2, o))).toBe('optimum')
    expect(meterZone(m(0.5, o))).toBe('suboptimum')
    expect(meterZone(m(0.8, o))).toBe('poor')
  })
  it('optimum high: high is optimum, middle suboptimum, low poor', () => {
    const o = { low: 0.3, high: 0.7, optimum: 0.9 }
    expect(meterZone(m(0.8, o))).toBe('optimum')
    expect(meterZone(m(0.5, o))).toBe('suboptimum')
    expect(meterZone(m(0.2, o))).toBe('poor')
  })
  it('out-of-range values clamp before classifying; null has no zone', () => {
    expect(meterZone(m(9, { low: 0.3, high: 0.7, optimum: 0.1 }))).toBe('poor')
    expect(meterZone(m(-9, { low: 0.3, high: 0.7, optimum: 0.1 }))).toBe('optimum')
    expect(meterZone(m(null))).toBeNull()
  })
  it('segments cover [min, max] without gaps and skip empty zones', () => {
    const s = meterSegments(meterBounds(m(0, { min: 10, max: 50, low: 20, high: 40, optimum: 45 })))
    expect(s.map((x) => [x.from, x.to, x.zone])).toEqual([[10, 20, 'poor'], [20, 40, 'suboptimum'], [40, 50, 'optimum']])
    expect(meterSegments(meterBounds(m(0))).length).toBe(1)
  })
})

describe('meter: geometry', () => {
  it('zone rects are proportional, the fill reaches the value, and the readout names the zone', () => {
    const el = host(640, 160)
    const h = meter.render(el, { label: 'a', value: 25, max: 100, low: 20, high: 80, optimum: 50 }, { animate: false })
    const w = (k: string) => Number(el.querySelector(`[data-key="rect:a:${k}"]`)!.getAttribute('width'))
    const total = w('zone:low') + w('zone:mid') + w('zone:high')
    expect(w('zone:low') / total).toBeCloseTo(0.2, 2)
    expect(w('zone:mid') / total).toBeCloseTo(0.6, 2)
    expect(w('fill') / total).toBeCloseTo(0.25, 2)
    expect(el.querySelector('[data-key="text:a:readout"]')!.textContent).toBe('25 · optimum')
    h.destroy()
  })
  it('a missing value is a dashed empty track, distinct from zero', () => {
    const el = host(640, 160)
    const h = meter.render(el, [{ label: 'gone', value: null }, { label: 'zero', value: 0 }], { animate: false })
    expect(el.querySelector('[data-key="rect:gone:track"]')!.getAttribute('style')).toMatch(/stroke-dasharray/)
    expect(el.querySelector('[data-key="rect:zero:track"]')).toBeNull()
    expect(el.querySelector('[data-key="text:gone:readout"]')!.textContent).toBe('No data')
    h.destroy()
  })
})
