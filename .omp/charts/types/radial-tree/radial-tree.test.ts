import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import { MALFORMED } from '../_shared/FlowB'
import radialTree from './index'

chartSuite({
  module: radialTree,
  dir: import.meta.dirname,
  role: 'node',
  marks: { basic: 15, realistic: 43, 'edge-single': 1, 'edge-duplicates': 10, 'edge-large': 5200, 'edge-nulls': 6, 'edge-deep': 9, 'edge-wide': 61 },
  summary: /^Radial tree, 15 nodes in 3 rings around Animals, 4 branches \(Mammals, Birds, Reptiles and 1 more\), 10 leaves$/,
  malformed: MALFORMED,
})

const nodes = (el: Element) => new Map([...el.querySelectorAll('[data-role="node"]')].map((c) => [c.getAttribute('data-key')!.replace('circle:n:', ''), { x: Number(c.getAttribute('cx')), y: Number(c.getAttribute('cy')) }]))

describe('radial-tree: geometry', () => {
  const data = { name: 'R', children: [{ name: 'A', children: [{ name: 'A1' }, { name: 'A2' }, { name: 'A3' }] }, { name: 'B', children: [{ name: 'B1' }] }, { name: 'C' }] }
  it('puts the root in the centre and each depth on its own ring', () => {
    const el = host(500, 500)
    const h = radialTree.render(el, data, { animate: false })
    const n = nodes(el)
    const c = n.get('R')!
    const svg = el.querySelector('svg.oc-svg')!
    expect(c.x).toBeCloseTo(Number(svg.getAttribute('width')) / 2, 1)
    expect(c.y).toBeCloseTo(Number(svg.getAttribute('height')) / 2, 1)
    const dist = (k: string) => Math.hypot(n.get(k)!.x - c.x, n.get(k)!.y - c.y)
    expect(dist('R/A')).toBeCloseTo(dist('R/B'), 1)
    expect(dist('R/A')).toBeCloseTo(dist('R/C'), 1)
    expect(dist('R/A/A1')).toBeCloseTo(2 * dist('R/A'), 1)
    expect(dist('R/B/B1')).toBeCloseTo(dist('R/A/A2'), 1)
    h.destroy()
  })
  it('rotates leaf labels along their ray and flips the left half so text reads left to right', () => {
    const el = host(500, 500)
    const h = radialTree.render(el, data, { animate: false })
    const texts = [...el.querySelectorAll('svg.oc-svg text[transform]')]
    expect(texts.length).toBeGreaterThan(0)
    for (const t of texts) {
      const deg = Number(/rotate\((-?[\d.e+-]+)/.exec(t.getAttribute('transform')!)![1])
      expect(deg).toBeGreaterThanOrEqual(-90.001)
      expect(deg).toBeLessThanOrEqual(90.001)
    }
    h.destroy()
  })
  it('collapses below maxDepth', () => {
    const el = host()
    const h = radialTree.render(el, data, { animate: false, maxDepth: 1 })
    expect(h.markCount()).toBe(4)
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/4 nodes below depth 1 collapsed/)
    h.destroy()
  })
})
