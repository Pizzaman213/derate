import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import bubble, { radiusScale } from './index'

chartSuite({
  module: bubble,
  dir: import.meta.dirname,
  role: 'bubble',
  marks: { basic: 6, realistic: 37, 'edge-single': 1, 'edge-duplicates': 12, 'edge-negatives': 3, 'edge-large': 5200, 'edge-zeros': 6 },
  summary: /^Bubble chart, 6 bubbles, Price \(USD\) from 8 to 40, Rating from 3\.2 to 4\.8, strong positive correlation \(r = 0\.86\), largest Units sold Charlie at 540$/,
  malformed: [
    { name: 'a text size', data: [{ x: 1, y: 2, size: 3 }, { x: 1, y: 3, size: 'big' }], message: /field 'size' must be a number, got 'big' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'a mapping to a missing column', data: [{ a: 1, b: 2, c: 3 }], options: { fields: { x: 'a', y: 'b', size: 'cc' } }, message: /column 'cc' not found/ },
  ],
})

const circles = (el: Element) => [...el.querySelectorAll('[data-role="bubble"]')].map((c) => ({ key: c.getAttribute('data-key')!, r: Number(c.getAttribute('r')) }))

describe('bubble: geometry', () => {
  it('area is proportional to size (sqrt radius through zero)', () => {
    const r = radiusScale(400, 20)
    expect(r(400)).toBe(20)
    expect(r(100)).toBe(10)
    expect(r(0)).toBe(0)
    expect((Math.PI * r(300) ** 2) / (Math.PI * r(100) ** 2)).toBeCloseTo(3, 10)
  })
  it('rendered areas follow the sizes', () => {
    const el = host(640, 400)
    const h = bubble.render(el, [{ x: 1, y: 1, size: 100, label: 'a' }, { x: 2, y: 2, size: 25, label: 'b' }, { x: 3, y: 1, size: 400, label: 'c' }], { animate: false, maxRadius: 30 })
    const by = Object.fromEntries(circles(el).map((c) => [c.key.replace(/^circle:/, ''), c.r]))
    const area = (k: string) => by[`:${k}`]! ** 2
    expect(area('c') / area('a')).toBeCloseTo(4, 1)
    expect(area('a') / area('b')).toBeCloseTo(4, 1)
    expect(by[':c']).toBeCloseTo(30, 1)
    h.destroy()
  })
  it('draws the largest first so small bubbles stay on top', () => {
    const el = host(640, 400)
    const h = bubble.render(el, [{ x: 1, y: 1, size: 1 }, { x: 1, y: 1, size: 9 }], { animate: false })
    const rs = circles(el).map((c) => c.r)
    expect(rs[0]).toBeGreaterThan(rs[1]!)
    h.destroy()
  })
  it('omits negative sizes, rings zero sizes, and says so', () => {
    const el = host(640, 400)
    const h = bubble.render(el, [{ x: 1, y: 1, size: 5 }, { x: 2, y: 2, size: -3 }, { x: 3, y: 3, size: 0 }, { x: 4, y: 4, size: null }], { animate: false })
    expect(h.markCount()).toBe(2)
    expect(el.querySelector('.oc-note')!.textContent).toBe('1 rows missing a size skipped; 1 negative sizes omitted; 1 zero sizes drawn as rings')
    const ring = [...el.querySelectorAll('[data-role="bubble"]')].find((c) => /fill:var\(--oc-surface\)/.test(c.getAttribute('style') ?? ''))
    expect(ring).toBeDefined()
    h.destroy()
  })
  it('keeps every bubble inside the plot', () => {
    const el = host(640, 400)
    const h = bubble.render(el, [{ x: 0, y: 0, size: 100 }, { x: 10, y: 10, size: 100 }], { animate: false, maxRadius: 30 })
    const svg = el.querySelector('svg')!
    const W = Number(svg.getAttribute('width')), H = Number(svg.getAttribute('height'))
    for (const c of el.querySelectorAll('[data-role="bubble"]')) {
      const cx = Number(c.getAttribute('cx')), cy = Number(c.getAttribute('cy')), r = Number(c.getAttribute('r'))
      expect(cx - r).toBeGreaterThan(30)
      expect(cx + r).toBeLessThan(W)
      expect(cy - r).toBeGreaterThan(0)
      expect(cy + r).toBeLessThan(H - 15)
    }
    h.destroy()
  })
  it('draws a nested size key with nice values', () => {
    const el = host(640, 400)
    const h = bubble.render(el, [{ x: 1, y: 1, size: 870 }, { x: 2, y: 2, size: 30 }], { animate: false, sizeTitle: 'Units' })
    const texts = [...el.querySelectorAll('.oc-labels text')].map((t) => t.textContent)
    expect(texts).toContain('500')
    expect(texts).toContain('Units')
    h.destroy()
  })
})
