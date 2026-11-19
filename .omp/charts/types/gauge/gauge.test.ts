import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import gauge, { bandRanges, gaugeAngle, gaugeStatus } from './index'

chartSuite({
  module: gauge,
  dir: import.meta.dirname,
  role: 'gauge',
  marks: { basic: 1, realistic: 4, 'edge-single': 1, 'edge-zeros': 1, 'edge-nulls': 3, 'edge-outliers': 2, 'edge-duplicates': 2, 'edge-all-equal': 3 },
  summary: /^Gauge, GPU memory at 72% on 0% to 100%, warning, target 80%$/,
  malformed: [
    { name: 'a text value', data: { value: 'high' }, message: /field 'value' must be a number, got 'high' at row 1/ },
    { name: 'max not above min', data: { value: 5, min: 10, max: 10 }, message: /max \(10\) must be greater than min \(10\)/ },
    { name: 'an unknown status', data: { value: 5, bands: [{ to: 50, status: 'ok' }] }, message: /must be one of 'good', 'warning', 'bad', got 'ok'/ },
    { name: 'a scalar', data: 42, message: /the data must be/ },
    { name: 'a mapping to a missing column', data: 'node,used\na,1', options: { fields: { value: 'usd' } }, message: /column 'usd' not found/ },
  ],
})

describe('gauge: geometry', () => {
  it('angle is proportional to (value - min) / (max - min)', () => {
    expect(gaugeAngle(0, 0, 100)).toBeCloseTo(-Math.PI / 2)
    expect(gaugeAngle(100, 0, 100)).toBeCloseTo(Math.PI / 2)
    expect(gaugeAngle(50, 0, 100)).toBeCloseTo(0)
    for (const [v, lo, hi] of [[25, 0, 100], [-10, -40, 40], [3.3, 1, 7]] as const) {
      const t = (v - lo) / (hi - lo)
      expect((gaugeAngle(v, lo, hi) + Math.PI / 2) / Math.PI).toBeCloseTo(t, 10)
    }
  })
  it('clamps out-of-range values to the ends and notes it', () => {
    expect(gaugeAngle(340, 0, 100)).toBeCloseTo(Math.PI / 2)
    expect(gaugeAngle(-5, 0, 100)).toBeCloseTo(-Math.PI / 2)
    const el = host(640, 400)
    const h = gauge.render(el, { value: 340, label: 'Queue' }, { animate: false })
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/Queue is 340, above the maximum 100; the dial stops at the end/)
    expect(el.querySelector('[data-key$="readout"]')!.textContent).toBe('340')
    h.destroy()
  })
  it('classifies the value into its band', () => {
    const bands = [{ to: 85, status: 'warning' as const }, { to: 60, status: 'good' as const }, { to: 100, status: 'bad' as const }]
    expect(bandRanges(bands, 0, 100).map((b) => [b.from, b.to, b.status])).toEqual([[0, 60, 'good'], [60, 85, 'warning'], [85, 100, 'bad']])
    expect(gaugeStatus({ value: 72, min: 0, max: 100, bands })).toBe('warning')
    expect(gaugeStatus({ value: 60, min: 0, max: 100, bands })).toBe('good')
    expect(gaugeStatus({ value: 500, min: 0, max: 100, bands })).toBe('bad')
    expect(gaugeStatus({ value: 50, min: 0, max: 100, bands: [{ to: 40, status: 'good' }] })).toBeNull()
  })
  it('a missing value is a dashed dial, not zero', () => {
    const el = host(640, 400)
    const h = gauge.render(el, [{ label: 'a', value: null }, { label: 'b', value: 0 }], { animate: false })
    expect(el.querySelectorAll('[data-role="gauge"][data-mark]').length).toBe(2)
    expect(el.querySelector('[data-key="path:a:empty"]')!.getAttribute('style')).toMatch(/stroke-dasharray/)
    expect(el.querySelector('[data-key="path:b:fill"]')!.getAttribute('style')).not.toMatch(/stroke-dasharray/)
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 value missing/)
    h.destroy()
  })
})
