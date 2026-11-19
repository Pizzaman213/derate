import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import { MALFORMED } from '../_shared/FlowB'
import circlePacking from './index'

chartSuite({
  module: circlePacking,
  dir: import.meta.dirname,
  role: 'node',
  empty: ['edge-empty', 'edge-zeros'],
  marks: { basic: 15, realistic: 36, 'edge-single': 1, 'edge-nulls': 5, 'edge-negatives': 4, 'edge-duplicates': 10, 'edge-long-labels': 9, 'edge-unicode-rtl': 13, 'edge-outliers': 7, 'edge-deep': 7, 'edge-large': 5200 },
  summary: /^Circle packing, 10 leaves in 4 groups, total 76, largest Dog at 18 \(24%\)$/,
  malformed: MALFORMED,
})

const circles = (el: Element) => new Map([...el.querySelectorAll('[data-role="node"]')].map((c) => [c.getAttribute('data-key')!.replace('circle:n:', ''), { x: Number(c.getAttribute('cx')), y: Number(c.getAttribute('cy')), r: Number(c.getAttribute('r')) }]))

describe('circle-packing: geometry', () => {
  const data = { name: 'R', children: [{ name: 'G', children: [{ name: 'a', value: 1 }, { name: 'b', value: 2 }] }, { name: 'c', value: 4 }, { name: 'd', value: 8 }] }
  it('leaf circle areas are proportional to their values', () => {
    const el = host(600, 600)
    const h = circlePacking.render(el, data, { animate: false })
    const c = circles(el)
    const area = (k: string) => Math.PI * c.get(k)!.r ** 2
    expect(area('R/G/b') / area('R/G/a')).toBeCloseTo(2, 1)
    expect(area('R/c') / area('R/G/a')).toBeCloseTo(4, 1)
    expect(area('R/d') / area('R/G/a')).toBeCloseTo(8, 1)
    h.destroy()
  })
  it('nests every circle inside its parent and the whole inside the box', () => {
    const el = host(500, 400)
    const h = circlePacking.render(el, data, { animate: false })
    const c = circles(el)
    for (const [k, v] of c) {
      expect(v.x - v.r).toBeGreaterThanOrEqual(-0.5)
      expect(v.x + v.r).toBeLessThanOrEqual(500.5)
      expect(v.y - v.r).toBeGreaterThanOrEqual(-0.5)
      expect(v.y + v.r).toBeLessThanOrEqual(400.5)
      const parent = k.includes('/') ? c.get(k.slice(0, k.lastIndexOf('/')))! : null
      if (parent) expect(Math.hypot(v.x - parent.x, v.y - parent.y) + v.r).toBeLessThanOrEqual(parent.r + 0.5)
    }
    h.destroy()
  })
  it('omits non-positive leaves with a note and uses the empty state when nothing is left', () => {
    const el = host()
    const h = circlePacking.render(el, { name: 'R', children: [{ name: 'a', value: 3 }, { name: 'b', value: -1 }, { name: 'c' }] }, { animate: false })
    expect(h.markCount()).toBe(2)
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 leaves without a value omitted; 1 negative values omitted/)
    h.update({ name: 'R', children: [{ name: 'a', value: 0 }] })
    expect(h.state).toBe('empty')
    expect(el.querySelector('.oc-state')!.textContent).toMatch(/All values are zero/)
    h.destroy()
  })
  it('legend toggles re-pack without the hidden group', () => {
    const el = host(600, 600)
    const h = circlePacking.render(el, data, { animate: false })
    expect(h.markCount()).toBe(6)
    const before = circles(el).get('R/c')!.r
    const item = [...el.querySelectorAll<HTMLElement>('.oc-legend button')].find((b) => b.textContent?.trim() === 'd')!
    item.click()
    expect(h.markCount()).toBe(5)
    expect(circles(el).get('R/d')).toBeUndefined()
    expect(circles(el).get('R/c')!.r).toBeGreaterThan(before)
    h.destroy()
  })
  it('collapses below maxDepth and keeps the collapsed total', () => {
    const el = host(600, 600)
    const h = circlePacking.render(el, data, { animate: false, maxDepth: 1 })
    expect(h.markCount()).toBe(4)
    const c = circles(el)
    expect((Math.PI * c.get('R/G')!.r ** 2) / (Math.PI * c.get('R/c')!.r ** 2)).toBeCloseTo(3 / 4, 1)
    h.destroy()
  })
})
