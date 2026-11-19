import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import { MALFORMED } from '../_shared/FlowB'
import dendrogram from './index'

chartSuite({
  module: dendrogram,
  dir: import.meta.dirname,
  role: 'node',
  marks: { basic: 15, realistic: 31, 'edge-single': 1, 'edge-duplicates': 10, 'edge-large': 5200, 'edge-nulls': 6, 'edge-deep': 9, 'edge-wide': 41, 'edge-inconsistent-heights': 14 },
  summary: /^Dendrogram, 8 leaves joined in 7 merges, heights up to 9\.2, closest Lemon and Lime at 1\.8$/,
  malformed: [...MALFORMED, { name: 'a text height', data: 'id,parent,height\na,,x\nb,a,', message: /field 'height' must be a number, got 'x' at row 1/ }],
})

const nodes = (el: Element) => new Map([...el.querySelectorAll('[data-role="node"]')].map((c) => [c.getAttribute('data-key')!.replace('circle:n:', ''), { x: Number(c.getAttribute('cx')), y: Number(c.getAttribute('cy')) }]))
const tree = { name: 'R', height: 10, children: [{ name: 'A', height: 2, children: [{ name: 'a1' }, { name: 'a2' }] }, { name: 'b' }] }

describe('dendrogram: geometry', () => {
  it('aligns every leaf on one vertical line', () => {
    const el = host(640, 400)
    const h = dendrogram.render(el, { name: 'R', children: [{ name: 'A', children: [{ name: 'a1' }, { name: 'a2' }] }, { name: 'b' }] }, { animate: false })
    const n = nodes(el)
    expect(n.get('R/A/a1')!.x).toBeCloseTo(n.get('R/b')!.x, 1)
    expect(n.get('R/A/a2')!.x).toBeCloseTo(n.get('R/b')!.x, 1)
    h.destroy()
  })
  it('places inner nodes by merge height, linearly', () => {
    const el = host(640, 400)
    const h = dendrogram.render(el, tree, { animate: false })
    const n = nodes(el)
    const leafX = n.get('R/b')!.x, rootX = n.get('R')!.x
    // A merged at 2 of 10: 20% of the way from the leaves to the root
    expect((leafX - n.get('R/A')!.x) / (leafX - rootX)).toBeCloseTo(0.2, 2)
    // distance axis ticks
    expect([...el.querySelectorAll('.oc-axes text')].map((t) => t.textContent)).toContain('10')
    h.update(tree, { useHeight: false })
    const d = nodes(el)
    expect((d.get('R/b')!.x - d.get('R/A')!.x) / (d.get('R/b')!.x - d.get('R')!.x)).toBeCloseTo(0.5, 2)
    expect(el.querySelectorAll('.oc-axes text').length).toBe(0)
    h.destroy()
  })
  it('draws elbow links (vertical then horizontal)', () => {
    const el = host(640, 400)
    const h = dendrogram.render(el, tree, { animate: false })
    const paths = [...el.querySelectorAll('svg.oc-svg path')].map((p) => p.getAttribute('d') ?? '')
    expect(paths.length).toBe(4)
    for (const d of paths) expect(d).toMatch(/^M[\d.]+,[\d.]+V[\d.]+H[\d.]+$/)
    h.destroy()
  })
  it('raises a merge lower than its child and notes it', () => {
    const el = host(640, 400)
    const h = dendrogram.render(el, { name: '', height: 1, children: [{ name: '', height: 3, children: [{ name: 'x' }, { name: 'y' }] }, { name: 'z' }] }, { animate: false })
    const n = nodes(el)
    const keys = [...n.keys()]
    expect(n.get(keys[0]!)!.x).toBeCloseTo(n.get(keys[1]!)!.x, 1)
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 merge heights raised/)
    h.focusMark(0)
    expect(el.querySelector('[aria-live]')!.textContent).toMatch(/Group of 3, root, 2 children, merged at 3/)
    h.destroy()
  })
})
