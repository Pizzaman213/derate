import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import pyramid, { pyramidBands } from './index'

chartSuite({
  module: pyramid,
  dir: import.meta.dirname,
  role: 'band',
  empty: ['edge-empty', 'edge-zeros'],
  marks: { basic: 5, realistic: 4, 'edge-single': 1, 'edge-negatives': 3, 'edge-nulls': 2, 'edge-duplicates': 5, 'edge-large': 40 },
  summary: /^Pyramid chart, 5 levels totalling 1,600, largest Staff at 59%, smallest Executives at 0\.8%$/,
  malformed: [
    { name: 'a text value', data: [{ label: 'A', value: 1 }, { label: 'B', value: 'abc' }], message: /field 'value' must be a number, got 'abc' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
  ],
})

const announce = (el: HTMLElement) => el.querySelector('[aria-live]')!.textContent ?? ''

/** Area of a trapezoid band of a triangle with base B and height T. */
const area = (t0: number, t1: number, B: number, T: number) => ((t0 * B + t1 * B) / 2) * (t1 - t0) * T

describe('pyramid: area-proportional bands', () => {
  it('band areas are exactly the shares of the triangle', () => {
    const vals = [12, 48, 180, 420, 940]
    const sum = vals.reduce((s, v) => s + v, 0)
    const b = pyramidBands(vals)
    expect(b[0]!.t0).toBe(0)
    expect(b[4]!.t1).toBeCloseTo(1, 12)
    const whole = area(0, 1, 3, 2)
    b.forEach((d, i) => {
      expect(area(d.t0, d.t1, 3, 2) / whole).toBeCloseTo(vals[i]! / sum, 12)
      if (i) expect(d.t0).toBe(b[i - 1]!.t1)
    })
  })
  it('equal values get equal areas but not equal heights', () => {
    const b = pyramidBands([1, 1])
    expect(b[0]!.t1).toBeCloseTo(Math.SQRT1_2, 12)
    expect(b[0]!.t1 - b[0]!.t0).toBeGreaterThan(b[1]!.t1 - b[1]!.t0)
  })
  it('the drawn band polygons have areas proportional to the values', () => {
    const el = host(640, 400)
    const h = pyramid.render(el, [{ label: 'a', value: 1 }, { label: 'b', value: 3 }, { label: 'c', value: 4 }], { animate: false })
    const areas = [...el.querySelectorAll('[data-role="band"]')].map((p) => {
      const pts = [...(p.getAttribute('d') ?? '').matchAll(/(-?[\d.]+),(-?[\d.]+)/g)].map((m) => [Number(m[1]), Number(m[2])] as const)
      let s = 0
      pts.forEach(([x, y], i) => { const [x2, y2] = pts[(i + 1) % pts.length]!; s += x * y2 - x2 * y })
      return Math.abs(s) / 2
    })
    // core/scene.ts rounds path coordinates to 0.01px, so the drawn ratio is exact only to ~1e-4;
    // the unrounded geometry is checked to 1e-12 above.
    expect(areas[1]! / areas[0]!).toBeCloseTo(3, 2)
    expect(areas[2]! / areas[0]!).toBeCloseTo(4, 2)
    ;(el.querySelector('.oc-legend button[data-key="c"]') as HTMLElement).click()
    h.focusMark(1)
    expect(announce(el)).toBe('2 of 2: b: 3 (75%)')
    h.destroy()
  })
  it('base-first puts the first row at the bottom', () => {
    const el = host()
    const h = pyramid.render(el, [{ label: 'base', value: 5 }, { label: 'top', value: 1 }], { animate: false, order: 'base-first' })
    h.focusMark(0)
    expect(announce(el)).toMatch(/^1 of 2: top/)
    h.destroy()
  })
})
