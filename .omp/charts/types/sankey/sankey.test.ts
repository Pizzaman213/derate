import { describe, expect, it } from 'vitest'
import { chartSuite, host, loadSamples } from '../../tests/harness'
import sankey from './index'

chartSuite({
  module: sankey,
  dir: import.meta.dirname,
  role: 'link',
  marks: { basic: 12, realistic: 36, 'edge-single': 1, 'edge-duplicates': 12, 'edge-negatives': 10, 'edge-nulls': 9, 'edge-large': 5400 + 150 },
  empty: ['edge-empty', 'edge-zeros'],
  summary: /^Sankey diagram, 9 nodes and 12 flows, 430 in total from Coal, Gas, Solar and 1 more, largest flow Electricity → Homes at 140$/,
  malformed: [
    { name: 'a cycle', data: { links: [{ source: 'A', target: 'B', value: 1 }, { source: 'B', target: 'C', value: 1 }, { source: 'C', target: 'A', value: 1 }] }, message: /the links form a loop \(A → B → C → A\); a Sankey diagram only flows forward/ },
    { name: 'a self-loop', data: { links: [{ source: 'A', target: 'A', value: 1 }] }, message: /loop \(A → A\)/ },
    { name: 'a link without a target', data: { links: [{ source: 'A', value: 1 }] }, message: /field 'links\[0\]\.target' is missing/ },
    { name: 'a text value', data: { links: [{ source: 'A', target: 'B', value: 'lots' }] }, message: /field 'links\[0\]\.value' must be a number, got 'lots'/ },
    { name: 'a scalar', data: 42, message: /the data must be \{ nodes\?, links/ },
    { name: 'rows without a target column', data: [{ from: 'A', to: 'B', value: 1 }], message: /link rows need a 'source' column/ },
    { name: 'a mapping to a missing column', data: [{ from: 'A', to: 'B', n: 1 }], options: { fields: { source: 'from', target: 'too' } }, message: /column 'too' not found/ },
  ],
})

const num = (el: Element, a: string) => Number(el.getAttribute(a))
const strokeWidth = (el: Element) => Number(/stroke-width:([\d.]+)/.exec(el.getAttribute('style') ?? '')![1])

describe('sankey: geometry', () => {
  const basic = loadSamples(import.meta.dirname).find((s) => s.name === 'basic')!
  const data = basic.data as { links: Array<{ source: string; target: string; value: number }> }

  it('conserves flow: every interior node has equal in and out, and its bar is that tall', () => {
    const el = host(900, 500)
    const h = sankey.render(el, basic.data, { animate: false })
    const links = [...el.querySelectorAll('[data-role="link"]')]
    const width = (key: string) => strokeWidth(links.find((l) => l.getAttribute('data-key') === `path:link:${key}`)!)
    for (const node of ['Electricity', 'Heat']) {
      const rect = el.querySelector(`[data-key="rect:node:${node}"]`)!
      const ins = data.links.filter((l) => l.target === node)
      const outs = data.links.filter((l) => l.source === node)
      expect(ins.reduce((s, l) => s + l.value, 0)).toBe(outs.reduce((s, l) => s + l.value, 0))
      const inW = ins.reduce((s, l) => s + width(`${l.source}→${l.target}`), 0)
      const outW = outs.reduce((s, l) => s + width(`${l.source}→${l.target}`), 0)
      expect(inW).toBeCloseTo(num(rect, 'height'), 0)
      expect(outW).toBeCloseTo(num(rect, 'height'), 0)
    }
    h.destroy()
  })

  it('node heights are proportional to node values', () => {
    const el = host(900, 500)
    const h = sankey.render(el, basic.data, { animate: false })
    const value = (id: string) => Math.max(
      data.links.filter((l) => l.target === id).reduce((s, l) => s + l.value, 0),
      data.links.filter((l) => l.source === id).reduce((s, l) => s + l.value, 0),
    )
    const ratios = ['Coal', 'Gas', 'Solar', 'Wind', 'Electricity', 'Heat', 'Homes', 'Industry', 'Losses'].map((id) => num(el.querySelector(`[data-key="rect:node:${id}"]`)!, 'height') / value(id))
    for (const r of ratios) expect(r).toBeCloseTo(ratios[0]!, 2)
    h.destroy()
  })

  it('keeps every node and label inside the box at any nodeAlign', () => {
    for (const nodeAlign of ['justify', 'left', 'right', 'center'] as const) {
      const el = host(360, 300)
      const h = sankey.render(el, basic.data, { animate: false, nodeAlign })
      for (const r of el.querySelectorAll('[data-role="node"]')) {
        expect(num(r, 'x')).toBeGreaterThanOrEqual(0)
        expect(num(r, 'x') + num(r, 'width')).toBeLessThanOrEqual(360)
        expect(num(r, 'y') + num(r, 'height')).toBeLessThanOrEqual(Number(el.querySelector('svg.oc-svg')!.getAttribute('height')))
      }
      h.destroy()
    }
  })

  it('justify puts every sink in the last column; left puts every source in the first', () => {
    const el = host(900, 500)
    const g = { links: [{ source: 'a', target: 'b', value: 2 }, { source: 'b', target: 'c', value: 1 }, { source: 'a', target: 'd', value: 1 }] }
    const h = sankey.render(el, g, { animate: false, nodeAlign: 'justify' })
    const x = (id: string) => num(el.querySelector(`[data-key="rect:node:${id}"]`)!, 'x')
    expect(x('d')).toBeCloseTo(x('c'), 5)
    h.update(g, { nodeAlign: 'left' })
    expect(x('d')).toBeCloseTo(x('b'), 5)
    h.destroy()
  })

  it('accepts link rows and CSV text, and sums duplicate links with a note', () => {
    const el = host()
    const h = sankey.render(el, 'src,dst,n\nA,B,1\nA,B,2\nB,C,3\n', { animate: false, fields: { source: 'src', target: 'dst', value: 'n' } })
    expect(h.markCount()).toBe(2 + 3)
    expect(h.exportCSV()).toBe('Source,Target,Value\nA,B,3\nB,C,3')
    h.update({ links: [{ source: 'A', target: 'B', value: 1 }, { source: 'A', target: 'B', value: 2 }] }, { fields: undefined })
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 duplicate link summed/)
    h.destroy()
  })
})
