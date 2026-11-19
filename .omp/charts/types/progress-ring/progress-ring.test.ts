import { describe, expect, it } from 'vitest'
import { makeFormatters } from '../../core'
import { chartSuite, host } from '../../tests/harness'
import progressRing, { ringFraction, ringPercent } from './index'

chartSuite({
  module: progressRing,
  dir: import.meta.dirname,
  role: 'ring',
  marks: { basic: 1, realistic: 4, 'edge-concentric': 3, 'edge-single': 1, 'edge-zeros': 2, 'edge-nulls': 3, 'edge-duplicates': 2, 'edge-outliers': 2 },
  summary: /^Progress ring, Sprint 42 at 68%, 34 of 50$/,
  malformed: [
    { name: 'a text value', data: { value: 'half' }, message: /field 'value' must be a number, got 'half' at row 1/ },
    { name: 'a zero max', data: { value: 1, max: 0 }, message: /max must be greater than 0, got 0/ },
    { name: 'a scalar', data: true, message: /the data must be/ },
  ],
})

describe('progress-ring: math', () => {
  const fmt = makeFormatters()
  it('fraction is value / max, negatives clamp to 0, overshoot is kept', () => {
    expect(ringFraction({ value: 34, max: 50 })).toBeCloseTo(0.68)
    expect(ringFraction({ value: -5, max: 50 })).toBe(0)
    expect(ringFraction({ value: 150, max: 100 })).toBeCloseTo(1.5)
    expect(ringFraction({ value: null, max: 100 })).toBeNull()
    expect(ringPercent(0.68, fmt)).toBe('68%')
    expect(ringPercent(0.045, fmt)).toBe('4.5%')
    expect(ringPercent(null, fmt)).toBe('—')
  })
  it('overshoot adds a darker second lap and keeps the true percentage', () => {
    const el = host(640, 300)
    const h = progressRing.render(el, [{ label: 'a', value: 25 }, { label: 'b', value: 150 }], { animate: false })
    expect(el.querySelector('[data-key="path:a:lap1"]')).not.toBeNull()
    expect(el.querySelector('[data-key="path:a:lap2"]')).toBeNull()
    expect(el.querySelector('[data-key="path:b:lap2"]')).not.toBeNull()
    expect(el.querySelector('[data-key="text:b:pct"]')!.textContent).toBe('150%')
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 ring past 100%/)
    h.destroy()
  })
  it('a missing value is a dashed ring and says so; zero is solid', () => {
    const el = host(640, 300)
    const h = progressRing.render(el, [{ label: 'a', value: null }, { label: 'b', value: 0 }], { animate: false })
    expect(el.querySelector('[data-key="path:a:track"]')!.getAttribute('style')).toMatch(/stroke-dasharray/)
    expect(el.querySelector('[data-key="path:b:track"]')!.getAttribute('style')).not.toMatch(/stroke-dasharray/)
    expect(el.querySelector('[data-key="text:a:pct"]')!.textContent).toBe('—')
    expect(el.querySelector('[data-key="text:b:pct"]')!.textContent).toBe('0%')
    h.destroy()
  })
})
