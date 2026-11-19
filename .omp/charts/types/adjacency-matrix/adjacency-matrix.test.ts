import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import adjacencyMatrix, { model } from './index'
import { options } from './schema'

chartSuite({
  module: adjacencyMatrix,
  dir: import.meta.dirname,
  role: 'cell',
  marks: { basic: 36, realistic: 144, clusters: 121, 'edge-single': 4, 'edge-duplicates': 36, 'edge-large': 5625, 'edge-long-labels': 16, 'edge-unicode-rtl': 36 },
  summary: /^Adjacency matrix, 6 nodes, 10 links, strongest Alice → Bob at 12, weakest Dave → Frank at 1, most connected Alice \(3 neighbours\)$/,
  malformed: [
    { name: 'a text weight', data: { links: [{ source: 'a', target: 'b', value: 1 }, { source: 'b', target: 'c', value: 'heavy' }] }, message: /field 'links\[1\].value' must be a number, got 'heavy'/ },
    { name: 'a scalar', data: 42, message: /the data must be a \{ nodes, links \} graph, link rows or CSV text, got 42/ },
    { name: 'a link without a target', data: { links: [{ source: 'a' }] }, message: /field 'links\[0\]\.target' is missing/ },
    { name: 'a mapping to a missing column', data: [{ from: 'a', to: 'b' }], options: { fields: { source: 'from', target: 'too' } }, message: /column 'too' not found/ },
  ],
})

const parse = (data: unknown, o: Record<string, unknown> = {}) => {
  const v = adjacencyMatrix.validate(data, o)
  expect(v.errors).toEqual([])
  return model(v.data!, { ...options.parse(o) })
}
const links = [
  { source: 'a', target: 'b', value: 3 },
  { source: 'b', target: 'a', value: 2 },
  { source: 'b', target: 'c', value: 5 },
  { source: 'c', target: 'c', value: 1 },
]

describe('adjacency-matrix: cells', () => {
  it('draws n² cells and every value matches its link', () => {
    const el = host(640, 640)
    const h = adjacencyMatrix.render(el, { nodes: [{ id: 'd' }], links }, { animate: false })
    expect(h.markCount()).toBe(16)
    const cells = [...el.querySelectorAll('[data-role="cell"]')]
    expect(cells.length).toBe(16)
    const labels = new Set<string>()
    for (let i = 0; i < 16; i++) {
      h.focusMark(i)
      labels.add(el.querySelector('[aria-live]')!.textContent!.replace(/^\d+ of 16: /, ''))
    }
    for (const l of links) expect(labels).toContain(`${l.source} → ${l.target}: ${l.value}`)
    expect(labels).toContain('d → a: no link')
    expect([...labels].filter((l) => l.endsWith('no link')).length).toBe(16 - links.length)
    h.destroy()
  })
  it('lays the cells out as a square grid', () => {
    const el = host(800, 500)
    const h = adjacencyMatrix.render(el, { links }, { animate: false })
    const r = el.querySelector('[data-role="cell"]')!
    expect(Math.abs(Number(r.getAttribute('width')) - Number(r.getAttribute('height')))).toBeLessThan(1.5)
    h.destroy()
  })
  it('symmetric: one link fills both cells; both directions are summed', () => {
    const m = parse({ links }, { symmetric: true })
    const at = (a: string, b: string) => m.value[m.ids.indexOf(a)]![m.ids.indexOf(b)]
    expect(at('a', 'b')).toBe(5)
    expect(at('b', 'a')).toBe(5)
    expect(at('c', 'b')).toBe(5)
    expect(at('c', 'c')).toBe(1)
    expect(m.links).toBe(3)
    const directed = parse({ links })
    expect(directed.value[directed.ids.indexOf('c')]![directed.ids.indexOf('b')]).toBeUndefined()
    expect(directed.links).toBe(4)
  })
  it('orders by name, degree and cluster', () => {
    const data = {
      nodes: [{ id: 'z', group: 'g2' }, { id: 'y', group: 'g1' }, { id: 'x', group: 'g2' }, { id: 'w', group: 'g1' }],
      links: [{ source: 'x', target: 'y' }, { source: 'x', target: 'w' }, { source: 'x', target: 'z' }, { source: 'w', target: 'y' }],
    }
    expect(parse(data).ids).toEqual(['z', 'y', 'x', 'w'])
    expect(parse(data, { order: 'name' }).ids).toEqual(['w', 'x', 'y', 'z'])
    expect(parse(data, { order: 'degree' }).ids).toEqual(['x', 'w', 'y', 'z'])
    // groups in first-seen order (g2 then g1), most connected first inside each
    expect(parse(data, { order: 'cluster' }).ids).toEqual(['x', 'z', 'w', 'y'])
  })
  it('a link without a value weighs 1; duplicates are summed and noted', () => {
    const m = parse({ links: [{ source: 'a', target: 'b' }, { source: 'a', target: 'b', value: 4 }] })
    expect(m.value[0]![1]).toBe(5)
    expect(m.duplicates).toBe(1)
    const el = host()
    const h = adjacencyMatrix.render(el, { links: [{ source: 'a', target: 'b' }, { source: 'a', target: 'b' }] }, { animate: false })
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 duplicate links summed/)
    expect(h.exportCSV()).toBe('Source,Target,Weight\na,b,2')
    h.destroy()
  })
  it('accepts link rows, CSV and a { names, matrix } adjacency matrix', () => {
    expect(parse([{ source: 'a', target: 'b', value: 2 }]).value[0]![1]).toBe(2)
    expect(parse('from,to,w\na,b,7', { fields: { source: 'from', target: 'to', value: 'w' } }).value[0]![1]).toBe(7)
    const m = parse({ names: ['p', 'q'], matrix: [[0, 3], [1, 0]] })
    expect(m.ids).toEqual(['p', 'q'])
    expect(m.value).toEqual([[undefined, 3], [1, undefined]])
  })
  it('draws 5,000+ cells on canvas', () => {
    const el = host()
    const ls = Array.from({ length: 72 }, (_, i) => ({ source: `n${i}`, target: `n${(i + 1) % 72}` }))
    const h = adjacencyMatrix.render(el, { links: ls }, { animate: false })
    expect(h.renderer).toBe('canvas')
    expect(h.markCount()).toBe(72 * 72)
    h.destroy()
  })
})
