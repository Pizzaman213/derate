import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import { measure, SIZES } from '../../core'
import { MALFORMED } from '../_shared/FlowB'
import tree from './index'

chartSuite({
  module: tree,
  dir: import.meta.dirname,
  role: 'node',
  marks: { basic: 15, realistic: 25, 'edge-single': 1, 'edge-duplicates': 10, 'edge-large': 5200, 'edge-nulls': 6, 'edge-deep': 9, 'edge-wide': 61, 'edge-vertical': 15 },
  summary: /^Tree, 15 nodes over 3 levels, root Animals with 4 branches \(Mammals, Birds, Reptiles and 1 more\), 10 leaves$/,
  malformed: MALFORMED,
})

const nodesOf = (el: Element) => [...el.querySelectorAll('[data-role="node"]')].map((c) => ({ key: c.getAttribute('data-key')!, x: Number(c.getAttribute('cx')), y: Number(c.getAttribute('cy')) }))
const textBoxes = (el: Element) =>
  [...el.querySelectorAll('svg.oc-svg text')].map((t) => {
    const x = Number(t.getAttribute('x')), y = Number(t.getAttribute('y'))
    const w = measure(t.textContent ?? '', SIZES.tick)
    const style = t.getAttribute('style') ?? ''
    const x0 = style.includes('text-anchor:end') ? x - w : style.includes('text-anchor:middle') ? x - w / 2 : x
    return { x0, x1: x0 + w, y0: y - SIZES.tick / 2, y1: y + SIZES.tick / 2, rotated: !!t.getAttribute('transform') }
  })

describe('tree: geometry', () => {
  const data = { name: 'R', children: [{ name: 'A', children: [{ name: 'A1' }, { name: 'A2' }] }, { name: 'B' }] }
  it('horizontal: depth runs left to right, children fan out vertically in order', () => {
    const el = host(640, 400)
    const h = tree.render(el, data, { animate: false })
    const n = Object.fromEntries(nodesOf(el).map((d) => [d.key.replace('circle:n:', ''), d]))
    expect(n['R/A']!.x).toBeGreaterThan(n['R']!.x)
    expect(n['R/A/A1']!.x).toBeGreaterThan(n['R/A']!.x)
    expect(n['R/A']!.x).toBeCloseTo(n['R/B']!.x, 1)
    expect(n['R/A/A1']!.y).toBeLessThan(n['R/A/A2']!.y)
    // the parent sits between its children
    expect(n['R/A']!.y).toBeCloseTo((n['R/A/A1']!.y + n['R/A/A2']!.y) / 2, 1)
    h.destroy()
  })
  it('vertical: depth runs top to bottom', () => {
    const el = host(640, 400)
    const h = tree.render(el, data, { animate: false, orientation: 'vertical' })
    const n = Object.fromEntries(nodesOf(el).map((d) => [d.key.replace('circle:n:', ''), d]))
    expect(n['R/A']!.y).toBeGreaterThan(n['R']!.y)
    expect(n['R/A/A1']!.x).toBeLessThan(n['R/A/A2']!.x)
    h.destroy()
  })
  it('every node and label stays inside the box; labels never overlap', () => {
    for (const orientation of ['horizontal', 'vertical'] as const) {
      const el = host(320, 300)
      const wide = { name: 'Root', children: Array.from({ length: 60 }, (_, i) => ({ name: `Item number ${i}` })) }
      const h = tree.render(el, wide, { animate: false, orientation })
      for (const d of nodesOf(el)) {
        expect(d.x).toBeGreaterThanOrEqual(0)
        expect(d.x).toBeLessThanOrEqual(320)
        expect(d.y).toBeGreaterThanOrEqual(0)
        expect(d.y).toBeLessThanOrEqual(300)
      }
      const boxes = textBoxes(el).filter((b) => !b.rotated)
      expect(boxes.length).toBeGreaterThan(0)
      for (const b of boxes) {
        expect(b.x0).toBeGreaterThanOrEqual(-0.5)
        expect(b.x1).toBeLessThanOrEqual(320.5)
      }
      for (let i = 0; i < boxes.length; i++)
        for (let j = i + 1; j < boxes.length; j++) {
          const a = boxes[i]!, c = boxes[j]!
          expect(a.x0 < c.x1 - 0.5 && a.x1 > c.x0 + 0.5 && a.y0 < c.y1 - 0.5 && a.y1 > c.y0 + 0.5).toBe(false)
        }
      h.destroy()
    }
  })
  it('maxDepth collapses deeper nodes and says how many', () => {
    const el = host()
    const h = tree.render(el, data, { animate: false, maxDepth: 1 })
    expect(h.markCount()).toBe(3)
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/2 nodes below depth 1 collapsed/)
    expect(el.querySelector('[data-key="circle:n:R/A"]')!.getAttribute('aria-label') ?? el.textContent).toBeTruthy()
    h.focusMark(1)
    expect(el.querySelector('[aria-live]')!.textContent).toMatch(/A, under R, 2 collapsed/)
    // the table keeps the whole tree
    expect(h.exportCSV().trim().split('\n').length - 1).toBe(5)
    h.destroy()
  })
  it('accepts id/parent rows, path rows and a field mapping', () => {
    const el = host()
    const h = tree.render(el, 'emp,boss\nceo,\ncto,ceo\ncfo,ceo\ndev,cto', { animate: false, fields: { id: 'emp', parent: 'boss' } })
    expect(h.markCount()).toBe(4)
    h.update([{ path: 'a/b', value: 1 }, { path: 'a/c', value: 2 }], { fields: undefined })
    expect(h.markCount()).toBe(3)
    h.destroy()
  })
  it('keeps duplicate sibling names apart with distinct keys', () => {
    const el = host()
    const h = tree.render(el, { name: 'R', children: [{ name: 'x' }, { name: 'x' }] }, { animate: false })
    expect(nodesOf(el).map((d) => d.key)).toEqual(['circle:n:R', 'circle:n:R/x', 'circle:n:R/x~2'])
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 duplicate sibling names kept as separate nodes/)
    h.destroy()
  })
})
