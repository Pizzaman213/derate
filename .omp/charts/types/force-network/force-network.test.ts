import { describe, expect, it } from 'vitest'
import { chartSuite, host, loadSamples } from '../../tests/harness'
import forceNetwork from './index'

chartSuite({
  module: forceNetwork,
  dir: import.meta.dirname,
  role: 'node',
  marks: { basic: 14, realistic: 30, 'edge-single': 1, 'edge-duplicates': 14, 'edge-zeros': 14, 'edge-large': 1199 + 4000 },
  summary: /^Network graph, 14 nodes and 23 links, 4 groups, most connected Ana \(4 links\)$/,
  malformed: [
    { name: 'a link without a source', data: { links: [{ target: 'b' }] }, message: /field 'links\[0\]\.source' is missing/ },
    { name: 'a text weight', data: { links: [{ source: 'a', target: 'b', value: 'lots' }] }, message: /field 'links\[0\]\.value' must be a number, got 'lots'/ },
    { name: 'a scalar', data: true, message: /the data must be \{ nodes\?, links/ },
    { name: 'rows without a target column', data: [{ source: 'a', to: 'b' }], message: /link rows need a 'target' column \(columns: source, to\)/ },
  ],
})

const sample = (name: string) => loadSamples(import.meta.dirname).find((s) => s.name === name)!
const positions = (el: HTMLElement) => [...el.querySelectorAll('[data-role="node"]')].map((c) => [c.getAttribute('data-key'), c.getAttribute('cx'), c.getAttribute('cy')].join(' '))

describe('force-network: layout', () => {
  it('is deterministic: the same seed gives the same positions, another seed another layout', () => {
    const data = sample('basic').data
    const a = host(640, 440), b = host(640, 440), c = host(640, 440)
    const ha = forceNetwork.render(a, data, { animate: false, seed: 7 })
    const hb = forceNetwork.render(b, structuredClone(data), { animate: false, seed: 7 })
    const hc = forceNetwork.render(c, structuredClone(data), { animate: false, seed: 8 })
    expect(positions(a)).toEqual(positions(b))
    expect(positions(a)).not.toEqual(positions(c))
    for (const h of [ha, hb, hc]) h.destroy()
  })

  it('keeps every node and label inside the box at any size', () => {
    for (const [w, h] of [[320, 240], [640, 440], [1280, 720]] as const) {
      const el = host(w, h)
      const hd = forceNetwork.render(el, sample('realistic').data, { animate: false, ...sample('realistic').options })
      const svgH = Number(el.querySelector('svg.oc-svg')!.getAttribute('height'))
      for (const n of el.querySelectorAll('[data-role="node"]')) {
        const cx = Number(n.getAttribute('cx')), cy = Number(n.getAttribute('cy')), r = Number(n.getAttribute('r'))
        expect(cx - r).toBeGreaterThanOrEqual(0)
        expect(cx + r).toBeLessThanOrEqual(w)
        expect(cy - r).toBeGreaterThanOrEqual(0)
        expect(cy + r).toBeLessThanOrEqual(svgH)
      }
      for (const t of el.querySelectorAll('text')) {
        const y = Number(t.getAttribute('y'))
        expect(y).toBeGreaterThanOrEqual(0)
        expect(y).toBeLessThanOrEqual(svgH)
      }
      hd.destroy()
    }
  })

  it('pulls linked nodes closer than unlinked ones, and sizes nodes by degree', () => {
    const el = host(640, 440)
    const h = forceNetwork.render(el, sample('basic').data, { animate: false })
    const at = (id: string) => {
      const c = el.querySelector(`[data-key="circle:node:${id}"]`)!
      return { x: Number(c.getAttribute('cx')), y: Number(c.getAttribute('cy')), r: Number(c.getAttribute('r')) }
    }
    const dist = (p: string, q: string) => Math.hypot(at(p).x - at(q).x, at(p).y - at(q).y)
    expect(dist('Ana', 'Ben')).toBeLessThan(dist('Ana', 'Lea'))
    expect(dist('Fay', 'Gus')).toBeLessThan(dist('Fay', 'Kai'))
    expect(at('Ana').r).toBeGreaterThan(at('Lea').r) // 4 links vs 2
    h.destroy()
  })

  it('reports separate components and draws a legend per group', () => {
    const el = host()
    const h = forceNetwork.render(el, { nodes: [{ id: 'a', group: 'x' }, { id: 'b', group: 'y' }, { id: 'c', group: 'x' }], links: [{ source: 'a', target: 'b' }] }, { animate: false })
    expect(h.summary()).toMatch(/2 separate components/)
    expect(el.querySelectorAll('.oc-legend li').length).toBe(2)
    h.destroy()
  })
})
