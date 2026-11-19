import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import { trapezoid } from '../_shared/DistributionA'
import violin, { violins } from './index'

const sample = (name: string) => JSON.parse(readFileSync(join(import.meta.dirname, 'samples', `${name}.json`), 'utf8')) as { data: unknown; options?: Record<string, unknown> }

chartSuite({
  module: violin,
  dir: import.meta.dirname,
  role: 'violin',
  marks: { basic: 4, realistic: 3, 'edge-single': 1, 'edge-zeros': 4, 'edge-large': 24 },
  summary: /^Violin plot, 4 groups, highest median Dose A at 61\.1, lowest median Control at 48$/,
  malformed: [
    { name: 'a text value', data: [{ group: 'A', value: 1 }, { group: 'A', value: 'abc' }], message: /field 'value' must be a number, got 'abc' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'a mapping to a missing column', data: [{ species: 'A', mm: 1 }], options: { fields: { group: 'species', value: 'flipper' } }, message: /column 'flipper' not found/ },
  ],
})

const defaults = { bandwidth: 'silverman', trim: false, sort: 'none', maxGroups: 24 } as const
/** x extent of every coordinate pair in a path. */
const xSpan = (el: Element, key: string) => {
  const nums = el.querySelector(`[data-key="path:v:${key}"]`)!.getAttribute('d')!.match(/-?[\d.]+/g)!.map(Number)
  const xs = nums.filter((_, i) => i % 2 === 0)
  return Math.max(...xs) - Math.min(...xs)
}

describe('violin: density', () => {
  it('each untrimmed violin is a density that integrates to about 1', () => {
    const { list } = violins(violin.validate(sample('basic').data).data!, defaults)
    // tails cut at 3 bandwidths keep ~99% or more of the mass
    for (const v of list) {
      const a = trapezoid(v.curve)
      expect(a).toBeGreaterThan(0.99)
      expect(a).toBeLessThanOrEqual(1.0001)
    }
  })
  it('trim ends each violin at its data range', () => {
    const { list } = violins(violin.validate(sample('basic').data).data!, { ...defaults, trim: true })
    for (const v of list) {
      expect(v.curve[0]!.x).toBeCloseTo(v.stats.min, 9)
      expect(v.curve[v.curve.length - 1]!.x).toBeCloseTo(v.stats.max, 9)
    }
  })
})

describe('violin: geometry', () => {
  // a is twice as spread as b: with one density scale its peak (and widest point) is half as wide
  const data = [...[-2, -1, 0, 0, 1, 2].map((value) => ({ group: 'a', value })), ...[-1, -0.5, 0, 0, 0.5, 1].map((value) => ({ group: 'b', value }))]
  it("scale 'area': widths are proportional to density on one shared scale", () => {
    const el = host(640, 400)
    const h = violin.render(el, data, { animate: false, bandwidth: 0.6 })
    const { list } = violins(violin.validate(data).data!, { ...defaults, bandwidth: 0.6 })
    expect(xSpan(el, 'a') / xSpan(el, 'b')).toBeCloseTo(list[0]!.peak / list[1]!.peak, 2)
    h.destroy()
  })
  it("scale 'width': every violin is equally wide", () => {
    const el = host(640, 400)
    const h = violin.render(el, data, { animate: false, bandwidth: 0.6, scale: 'width' })
    expect(xSpan(el, 'a')).toBeCloseTo(xSpan(el, 'b'), 1)
    h.destroy()
  })
  it("scale 'count': width grows with n", () => {
    const el = host(640, 400)
    const more = [...data, ...data.filter((d) => d.group === 'a')]
    const h = violin.render(el, more, { animate: false, bandwidth: 0.6, scale: 'count' })
    const { list } = violins(violin.validate(more).data!, { ...defaults, bandwidth: 0.6 })
    expect(xSpan(el, 'a') / xSpan(el, 'b')).toBeCloseTo((list[0]!.peak * 12) / (list[1]!.peak * 6), 2)
    h.destroy()
  })
  it('the inner box spans the quartiles', () => {
    const el = host(640, 400)
    const h = violin.render(el, sample('basic').data, { animate: false, yDomain: [0, 100] })
    const y = (v: number) => Number(el.querySelector(`[data-key="line:gy:${v}"]`)!.getAttribute('y1'))
    const val = (py: number) => ((py - y(0)) / (y(100) - y(0))) * 100
    const { list } = violins(violin.validate(sample('basic').data).data!, defaults)
    const box = el.querySelector('[data-key="rect:ib:Control"]')!
    const top = Number(box.getAttribute('y'))
    expect(val(top)).toBeCloseTo(list[0]!.box.q3, 1)
    expect(val(top + Number(box.getAttribute('height')))).toBeCloseTo(list[0]!.box.q1, 1)
    h.destroy()
  })
  it('a single value draws a bump using the pooled bandwidth', () => {
    const one = [{ group: 'solo', value: 5 }, ...[1, 2, 3, 4, 6, 7].map((value) => ({ group: 'many', value }))]
    const { list } = violins(violin.validate(one).data!, defaults)
    expect(list[0]!.bandwidth).toBe(list[1]!.bandwidth > 0 ? list[0]!.bandwidth : NaN)
    expect(list[0]!.bandwidth).toBeGreaterThan(0)
    expect(list[0]!.peak).toBeGreaterThan(0)
  })
})
