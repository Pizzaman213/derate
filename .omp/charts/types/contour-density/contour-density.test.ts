import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import contourDensity, { densityContours, inside } from './index'

chartSuite({
  module: contourDensity,
  dir: import.meta.dirname,
  role: 'point',
  marks: { basic: 240, realistic: 272, 'edge-single': 1, 'edge-zeros': 240 },
  summary: /^Contour density plot, 240 points, x from [\d.]+ to [\d.]+, y from [\d.]+ to [\d.]+, (strong|moderate) negative correlation \(r = -0\.\d+\), bandwidth 20px$/,
  malformed: [
    { name: 'a text value', data: [{ x: 1, y: 2 }, { x: 'far', y: 3 }], message: /field 'x' must be a number, got 'far' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'a mapping to a missing column', data: [{ a: 1, b: 2 }], options: { fields: { x: 'a', y: 'bb' } }, message: /column 'bb' not found/ },
  ],
})

const cloud = (n: number, cx: number, cy: number, s: number) => Array.from({ length: n }, (_, i) => {
  const a = i * 2.399963, r = s * Math.sqrt((i + 0.5) / n)
  return [cx + r * Math.cos(a), cy + r * Math.sin(a)] as [number, number]
})

describe('contour-density: math', () => {
  it('thresholds are strictly increasing and each level encloses no more points than the one below', () => {
    const pts = [...cloud(300, 100, 100, 60), ...cloud(100, 260, 160, 30)]
    const cs = densityContours(pts, 400, 300, 20, 12)
    expect(cs.length).toBeGreaterThan(3)
    for (let i = 1; i < cs.length; i++) expect(cs[i]!.value).toBeGreaterThan(cs[i - 1]!.value)
    const enclosed = cs.map((c) => pts.filter((p) => inside(c, p)).length)
    for (let i = 1; i < enclosed.length; i++) expect(enclosed[i]!).toBeLessThanOrEqual(enclosed[i - 1]!)
    expect(enclosed[0]!).toBeGreaterThan(pts.length * 0.9)
  })
  it('more thresholds give more levels; a wider bandwidth lowers the peak', () => {
    const pts = cloud(200, 150, 150, 50)
    expect(densityContours(pts, 300, 300, 20, 20).length).toBeGreaterThan(densityContours(pts, 300, 300, 20, 4).length)
    const peak = (bw: number) => Math.max(...densityContours(pts, 300, 300, bw, 20).map((c) => c.value))
    expect(peak(40)).toBeLessThan(peak(10))
  })
  it('even-odd containment handles holes', () => {
    const ring = (x0: number, y0: number, s: number) => [[x0, y0], [x0 + s, y0], [x0 + s, y0 + s], [x0, y0 + s], [x0, y0]]
    const c = { type: 'MultiPolygon' as const, value: 1, coordinates: [[ring(0, 0, 10), ring(3, 3, 4)]] }
    expect(inside(c, [1, 1])).toBe(true)
    expect(inside(c, [5, 5])).toBe(false)
    expect(inside(c, [20, 20])).toBe(false)
  })
})

describe('contour-density: rendering', () => {
  it('draws filled contour levels, darker inside, and the points on top', () => {
    const el = host(640, 400)
    const data = cloud(150, 5, 5, 3).map(([x, y]) => ({ x, y }))
    const h = contourDensity.render(el, data, { animate: false })
    const levels = el.querySelectorAll('[data-role="contour"]')
    expect(levels.length).toBeGreaterThan(2)
    expect(el.querySelectorAll('[data-role="point"]').length).toBe(150)
    h.focusMark(0)
    expect(el.querySelector('[aria-live]')!.textContent).toMatch(/Density level 1 of \d+: at least .* points per unit², encloses \d+% of points/)
    expect(el.querySelector('.oc-gradient')).not.toBeNull()
    h.destroy()
  })
  it("hides points above 2,000 in auto and says so; 'never' hides them always", () => {
    const el = host(640, 400)
    const data = cloud(2100, 0, 0, 10).map(([x, y]) => ({ x, y }))
    const h = contourDensity.render(el, data, { animate: false })
    expect(el.querySelectorAll('[data-role="point"]').length).toBe(0)
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/2,100 points shown as density only/)
    h.update(data.slice(0, 50), { showPoints: 'never' })
    expect(el.querySelectorAll('[data-role="point"]').length).toBe(0)
    h.destroy()
  })
})
