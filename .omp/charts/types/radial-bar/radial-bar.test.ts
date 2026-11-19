import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import radialBar from './index'

chartSuite({
  module: radialBar,
  dir: import.meta.dirname,
  role: 'arc',
  marks: { basic: 5, realistic: 10, 'edge-single': 1, 'edge-duplicates': 5, 'edge-zeros': 5, 'edge-negatives': 3, 'edge-outliers': 5 },
  summary: /^Radial bar chart, 5 rings, highest Bravo at 340, lowest Delta at 90$/,
  malformed: [
    { name: 'a text value', data: [{ label: 'A', value: 1 }, { label: 'B', value: 'abc' }], message: /field 'value' must be a number, got 'abc' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'a mapping to a missing column', data: [{ name: 'A', amount: 1 }], options: { fields: { label: 'name', value: 'amont' } }, message: /column 'amont' not found/ },
  ],
})

/**
 * Where an arc path's outer edge ends, as a clockwise angle from 12 o'clock: the endpoint
 * of its largest-radius "A" command. Rounded corners pull it back by corner/radius (~0.02).
 */
function endAngle(path: Element): number {
  const d = path.getAttribute('d') ?? ''
  let best = { r: -1, a: 0 }
  for (const m of d.matchAll(/A(-?[\d.e-]+),(-?[\d.e-]+),[\d.e-]+,[01],[01],(-?[\d.e-]+),(-?[\d.e-]+)/g)) {
    const r = Number(m[1]), x = Number(m[3]), y = Number(m[4])
    if (r > best.r) best = { r, a: (Math.atan2(x, -y) + 2 * Math.PI) % (2 * Math.PI) }
  }
  return best.a
}
const translate = (el: Element) => (el.getAttribute('transform') ?? '').match(/-?[\d.]+/g)!.map(Number) as [number, number]

describe('radial-bar: geometry', () => {
  it('sweeps are proportional to value; a full ring is `max` over `sweep` degrees', () => {
    const el = host(640, 400)
    const h = radialBar.render(el, [{ label: 'a', value: 50 }, { label: 'b', value: 25 }, { label: 'c', value: 100 }], { animate: false, max: 100, sweep: 180 })
    const [a, b, c] = [...el.querySelectorAll('[data-role="arc"]')]
    expect(endAngle(a!)).toBeCloseTo(Math.PI / 2, 1)
    expect(endAngle(b!)).toBeCloseTo(Math.PI / 4, 1)
    expect(endAngle(c!)).toBeCloseTo(Math.PI, 1)
    h.focusMark(0)
    expect(el.querySelector('[aria-live]')!.textContent).toMatch(/a: 50 \(50% of 100\)/)
    h.destroy()
  })
  it('without max, the largest value sweeps the full 270°', () => {
    const el = host(640, 400)
    const h = radialBar.render(el, [{ label: 'a', value: 8 }, { label: 'b', value: 4 }], { animate: false })
    const [a, b] = [...el.querySelectorAll('[data-role="arc"]')]
    expect(endAngle(a!)).toBeCloseTo((3 * Math.PI) / 2, 1)
    expect(endAngle(b!)).toBeCloseTo((3 * Math.PI) / 4, 1)
    h.destroy()
  })
  it('draws concentric rings, first row outermost, each labelled at its start', () => {
    const el = host(640, 400)
    const h = radialBar.render(el, [{ label: 'a', value: 3 }, { label: 'b', value: 2 }, { label: 'c', value: 1 }], { animate: false })
    expect(el.querySelectorAll('[data-key^="path:t:"]').length).toBe(3)
    const ly = (k: string) => Number(el.querySelector(`[data-key="text:l:${k}"]`)!.getAttribute('y'))
    // labels sit above the centre on the 12 o'clock side, outermost highest
    expect(ly('a')).toBeLessThan(ly('b'))
    expect(ly('b')).toBeLessThan(ly('c'))
    expect(ly('c')).toBeLessThan(0)
    h.destroy()
  })
  it('omits negatives and clamps values above max, with notes', () => {
    const el = host()
    const h = radialBar.render(el, [{ label: 'a', value: 150 }, { label: 'b', value: -5 }, { label: 'c', value: 50 }], { animate: false, max: 100 })
    expect(h.markCount()).toBe(2)
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 negative values omitted; 1 values above the max \(100\) drawn as a full ring/)
    expect(endAngle(el.querySelector('[data-role="arc"]')!)).toBeCloseTo((3 * Math.PI) / 2, 1)
    h.destroy()
  })
  it('keeps the circle inside the box at 320px', () => {
    const el = host(320, 0)
    const h = radialBar.render(el, Array.from({ length: 8 }, (_, i) => ({ label: `Region number ${i}`, value: 10 + i })), { animate: false })
    const svg = el.querySelector('svg.oc-svg')!
    const W = Number(svg.getAttribute('width')), H = Number(svg.getAttribute('height'))
    const outer = el.querySelector('[data-key^="path:t:"]')!
    const [cx, cy] = translate(outer)
    // the outermost track's radius, from its outer arc command
    const R = Math.max(...[...(outer.getAttribute('d') ?? '').matchAll(/A([\d.]+)/g)].map((m) => Number(m[1])))
    expect(cx - R).toBeGreaterThanOrEqual(0)
    expect(cx + R).toBeLessThanOrEqual(W)
    expect(cy - R).toBeGreaterThanOrEqual(0)
    expect(cy + R).toBeLessThanOrEqual(H)
    h.destroy()
  })
  it('draws the rings that fit and notes the rest', () => {
    const el = host(640, 400)
    const h = radialBar.render(el, Array.from({ length: 60 }, (_, i) => ({ label: `t${i}`, value: i + 1 })), { animate: false })
    expect(h.markCount()).toBeLessThan(60)
    expect(el.querySelector('.oc-note')!.textContent).toMatch(new RegExp(`${60 - h.markCount()} more rings did not fit \\(all 60 are in the table\\)`))
    h.destroy()
  })
})
