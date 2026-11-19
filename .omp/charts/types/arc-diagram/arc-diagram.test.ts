import { describe, expect, it } from 'vitest'
import { chartSuite, host, loadSamples } from '../../tests/harness'
import arcDiagram from './index'

chartSuite({
  module: arcDiagram,
  dir: import.meta.dirname,
  role: 'link',
  marks: { basic: 14, realistic: 30, 'edge-single': 1, 'edge-duplicates': 14, 'edge-zeros': 0, 'edge-nulls': 12, 'edge-negatives': 12, 'edge-large': 5200 + 300 },
  summary: /^Arc diagram, 10 nodes and 14 links, 5 groups, most connected Valjean \(6 links\)$/,
  malformed: [
    { name: 'links that are not a list', data: { links: 'a-b' }, message: /field 'links' must be an array/ },
    { name: 'a link without a target', data: { links: [{ source: 'a' }] }, message: /field 'links\[0\]\.target' is missing/ },
    { name: 'an empty node id', data: { nodes: [{ id: '' }], links: [] }, message: /field 'nodes\[0\]\.id' must contain at least 1 character/ },
    { name: 'a text weight', data: 'source,target,value\na,b,heavy\n', message: /field 'value' must be a number, got 'heavy' at row 1/ },
  ],
})

const num = (el: Element, a: string) => Number(el.getAttribute(a))
const sample = (name: string) => loadSamples(import.meta.dirname).find((s) => s.name === name)!

describe('arc-diagram: geometry', () => {
  it('every arc leaves from the top of its two nodes and rises above them', () => {
    const el = host(800, 400)
    const h = arcDiagram.render(el, sample('basic').data, { animate: false })
    const node = (id: string) => el.querySelector(`[data-key="circle:node:${id}"]`)!
    for (const p of el.querySelectorAll('[data-role="link"]')) {
      const n = p.getAttribute('d')!.match(/-?[\d.]+/g)!.map(Number)
      // M x1,y1 A rx,ry 0 0 1 x2,y2
      const ends = p.getAttribute('data-key')!.replace('path:link:', '').split('→').map(node).sort((a, b) => num(a, 'cx') - num(b, 'cx'))
      expect(n[0]).toBeCloseTo(num(ends[0]!, 'cx'), 1)
      expect(n[1]).toBeCloseTo(num(ends[0]!, 'cy') - num(ends[0]!, 'r'), 1)
      expect(n[7]).toBeCloseTo(num(ends[1]!, 'cx'), 1)
      expect(n[8]).toBeCloseTo(num(ends[1]!, 'cy') - num(ends[1]!, 'r'), 1)
      expect(n[3]!).toBeGreaterThan(0)
    }
    h.destroy()
  })

  it('arc thickness grows with the square root of the weight', () => {
    const el = host(800, 400)
    const h = arcDiagram.render(el, { links: [{ source: 'a', target: 'b', value: 1 }, { source: 'b', target: 'c', value: 4 }, { source: 'c', target: 'd', value: 16 }] }, { animate: false })
    const w = (k: string) => Number(/stroke-width:([\d.]+)/.exec(el.querySelector(`[data-key="path:link:${k}"]`)!.getAttribute('style')!)![1])
    const [w1, w4, w16] = [w('a→b'), w('b→c'), w('c→d')]
    expect((w16 - w4) / (w4 - w1)).toBeCloseTo(2, 1)
    h.destroy()
  })

  it('orders nodes by degree and by group', () => {
    const el = host(800, 400)
    const data = sample('basic').data
    const h = arcDiagram.render(el, data, { animate: false, order: 'degree' })
    const xs = () => [...el.querySelectorAll('[data-role="node"]')].map((c) => ({ id: c.getAttribute('data-key')!.replace('circle:node:', ''), x: num(c, 'cx') })).sort((a, b) => a.x - b.x).map((d) => d.id)
    expect(xs()[0]).toBe('Valjean')
    h.update(data, { order: 'group' })
    const groups = (data as { nodes: Array<{ id: string; group: string }> }).nodes
    const seq = xs().map((id) => groups.find((n) => n.id === id)!.group)
    // each group is one contiguous run
    expect(new Set(seq).size).toBe(seq.filter((g, i) => i === 0 || seq[i - 1] !== g).length)
    h.destroy()
  })

  it('keeps nodes and labels inside the box when narrow', () => {
    const el = host(320, 240)
    const h = arcDiagram.render(el, sample('basic').data, { animate: false })
    for (const c of el.querySelectorAll('[data-role="node"]')) {
      expect(num(c, 'cx') - num(c, 'r')).toBeGreaterThanOrEqual(0)
      expect(num(c, 'cx') + num(c, 'r')).toBeLessThanOrEqual(320)
    }
    h.destroy()
  })

  it('merges reversed links into one undirected arc', () => {
    const el = host()
    const h = arcDiagram.render(el, { links: [{ source: 'a', target: 'b', value: 2 }, { source: 'b', target: 'a', value: 3 }] }, { animate: false })
    expect(el.querySelectorAll('[data-role="link"]').length).toBe(1)
    expect(h.exportCSV()).toBe('Source,Target,Value\na,b,5')
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 duplicate link summed/)
    h.destroy()
  })
})
