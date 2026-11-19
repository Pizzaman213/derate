import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import { trapezoid } from '../_shared/DistributionA'
import density, { curves } from './index'

const sample = (name: string) => JSON.parse(readFileSync(join(import.meta.dirname, 'samples', `${name}.json`), 'utf8')) as { data: unknown; options?: Record<string, unknown> }
const rows = (name: string) => { const s = sample(name); return density.validate(s.data, s.options).data! }

chartSuite({
  module: density,
  dir: import.meta.dirname,
  role: 'density',
  marks: { basic: 1, realistic: 3, 'edge-single': 1, 'edge-zeros': 1, 'edge-large': 1, 'edge-unicode-rtl': 3, 'edge-long-labels': 2 },
  summary: /^Density plot, 60 values from 1\.\d to 5\.\d, highest peak at 4\.\d+$/,
  malformed: [
    { name: 'a text value', data: [{ value: 1 }, { value: 'abc' }], message: /field 'value' must be a number, got 'abc' at row 2/ },
    { name: 'a text value in a number list', data: [1, 'abc'], message: /field 'value' must be a number, got 'abc' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of numbers, a list of rows, CSV text, or column arrays/ },
    { name: 'a mapping to a missing column', data: [{ minutes: 1 }], options: { fields: { value: 'mins' } }, message: /column 'mins' not found/ },
  ],
})

describe('density: estimate', () => {
  it('each curve integrates to 1 (trapezoid, data units)', () => {
    for (const kernel of ['gaussian', 'epanechnikov'] as const) {
      for (const name of ['basic', 'realistic', 'edge-outliers']) {
        for (const s of curves(rows(name), { bandwidth: 'silverman', kernel }).list) expect(trapezoid(s.curve)).toBeCloseTo(1, 2)
      }
    }
  })
  it('a fixed bandwidth is used as given, and still integrates to 1', () => {
    const { list } = curves(rows('basic'), { bandwidth: 0.25, kernel: 'gaussian' })
    expect(list[0]!.bandwidth).toBe(0.25)
    expect(trapezoid(list[0]!.curve)).toBeCloseTo(1, 2)
  })
  it('finds the main mode of a bimodal sample', () => {
    const { list } = curves(rows('basic'), { bandwidth: 'silverman', kernel: 'gaussian' })
    expect(list[0]!.peak.x).toBeGreaterThan(3.8)
    expect(list[0]!.peak.x).toBeLessThan(5)
    // the second mode near 2 is a local maximum too
    const c = list[0]!.curve
    const local = c.filter((p, i) => i > 0 && i < c.length - 1 && p.y > c[i - 1]!.y && p.y > c[i + 1]!.y)
    expect(local.length).toBe(2)
    expect(local[0]!.x).toBeGreaterThan(1.5)
    expect(local[0]!.x).toBeLessThan(2.5)
  })
  it('a single value falls back to a positive bandwidth and integrates to 1', () => {
    const { list } = curves(density.validate([7]).data!, { bandwidth: 'silverman', kernel: 'gaussian' })
    expect(list[0]!.bandwidth).toBeGreaterThan(0)
    expect(trapezoid(list[0]!.curve)).toBeCloseTo(1, 2)
    expect(list[0]!.peak.x).toBeCloseTo(7, 1)
  })
})

describe('density: geometry', () => {
  it('curves of different size share one density scale (not one height)', () => {
    const el = host(640, 400)
    // b is twice as spread as a, so its peak is half as tall
    const data = [...[-1, 0, 0, 1].map((v) => ({ value: v, series: 'a' })), ...[-2, 0, 0, 2].map((v) => ({ value: v, series: 'b' }))]
    const h = density.render(el, data, { animate: false, bandwidth: 0.5 })
    const top = (k: string) => Math.min(...(el.querySelector(`[data-key="path:d:${k}"]`)!.getAttribute('d')!.match(/-?[\d.]+/g)!.map(Number).filter((_, i) => i % 2 === 1)))
    const base = Math.max(...(el.querySelector('[data-key="path:d:a"]')!.getAttribute('d')!.match(/-?[\d.]+/g)!.map(Number).filter((_, i) => i % 2 === 1)))
    const { list } = curves(density.validate(data).data!, { bandwidth: 0.5, kernel: 'gaussian' })
    expect((base - top('b')) / (base - top('a'))).toBeCloseTo(list[1]!.peak.y / list[0]!.peak.y, 1)
    h.destroy()
  })
  it('rug ticks one line per value; hidden series leave the y scale', () => {
    const el = host()
    const h = density.render(el, sample('basic').data, { animate: false, rug: true })
    expect(el.querySelectorAll('[data-key^="line:r:"]').length).toBe(60)
    h.destroy()
  })
})
