import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import { MALFORMED } from '../_shared/FlowB'
import orgChart from './index'

chartSuite({
  module: orgChart,
  dir: import.meta.dirname,
  role: 'node',
  marks: { basic: 9, realistic: 46, 'edge-single': 1, 'edge-nulls': 4, 'edge-duplicates': 4, 'edge-long-labels': 3, 'edge-unicode-rtl': 4, 'edge-wide': 41, 'edge-deep': 17, 'edge-zeros': 2, 'edge-outliers': 29, 'edge-large': 5200 },
  summary: /^Org chart, 9 people in 3 levels under Maya Chen \(Chief Executive Officer\), largest team Maya Chen with 3 direct reports$/,
  malformed: MALFORMED,
})

const team = (n: number) => [{ id: 'm', parent: null, name: 'Manager', title: 'Director' }, ...Array.from({ length: n }, (_, i) => ({ id: `r${i}`, parent: 'm', name: `Report ${i + 1}`, title: 'Associate' }))]
interface Box { key: string; x: number; y: number; w: number; h: number }
const boxes = (el: Element): Box[] =>
  [...el.querySelectorAll('[data-role="node"]')].map((r) => ({ key: r.getAttribute('data-key')!, x: Number(r.getAttribute('x')), y: Number(r.getAttribute('y')), w: Number(r.getAttribute('width')), h: Number(r.getAttribute('height')) }))
const overlap = (a: Box, b: Box) => a.x < b.x + b.w - 0.5 && a.x + a.w > b.x + 0.5 && a.y < b.y + b.h - 0.5 && a.y + a.h > b.y + 0.5
const MANAGER = 'rect:n:Manager'

describe('org-chart: layout', () => {
  it('draws one box per person, managers above their reports', () => {
    const el = host(800, 400)
    const h = orgChart.render(el, team(3), { animate: false })
    const b = boxes(el)
    expect(b.length).toBe(4)
    const m = b.find((x) => x.key === MANAGER)!
    for (const r of b.filter((x) => x !== m)) expect(r.y).toBeGreaterThan(m.y + m.h)
    h.destroy()
  })
  for (const width of [320, 640, 1280]) {
    it(`fits ${width}px without scrolling: every box inside, none overlapping`, () => {
      for (const n of [5, 40]) {
        const el = host(width, 400)
        const h = orgChart.render(el, team(n), { animate: false })
        const b = boxes(el)
        expect(b.length).toBe(n + 1)
        for (const x of b) {
          expect(x.x).toBeGreaterThanOrEqual(-0.01)
          expect(x.x + x.w).toBeLessThanOrEqual(width + 0.01)
          expect(x.y).toBeGreaterThanOrEqual(-0.01)
          expect(x.y + x.h).toBeLessThanOrEqual(400.01)
        }
        for (let i = 0; i < b.length; i++) for (let j = i + 1; j < b.length; j++) expect(overlap(b[i]!, b[j]!)).toBe(false)
        h.destroy()
      }
    })
  }
  it('wraps a too-wide team into several rows instead of shrinking to nothing', () => {
    const el = host(640, 400)
    const h = orgChart.render(el, team(40), { animate: false })
    const rows = new Set(boxes(el).filter((x) => x.key !== MANAGER).map((x) => Math.round(x.y)))
    expect(rows.size).toBeGreaterThan(1)
    expect(boxes(el)[0]!.w).toBeGreaterThan(50)
    h.destroy()
  })
  it('keeps a small team on one row at full box size', () => {
    const el = host(1280, 400)
    const h = orgChart.render(el, team(4), { animate: false })
    const reports = boxes(el).filter((x) => x.key !== MANAGER)
    expect(new Set(reports.map((x) => x.y)).size).toBe(1)
    h.destroy()
  })
  it('truncates long names and keeps the full text for the tooltip', () => {
    const el = host(320, 300)
    const long = 'Maximiliana Alexandra Featherstonehaugh-Montgomery'
    const h = orgChart.render(el, [{ id: '1', parent: null, name: long, title: 'CEO' }, { id: '2', parent: '1', name: 'B' }, { id: '3', parent: '1', name: 'C' }], { animate: false })
    const t = el.querySelector('[data-full]')!
    expect(t.textContent).toMatch(/…$/)
    expect(t.getAttribute('data-full')).toBe(long)
    h.destroy()
  })
  it('box count equals node count for a nested tree too', () => {
    const el = host()
    const h = orgChart.render(el, { name: 'A', children: [{ name: 'B', title: 'x', children: [{ name: 'C' }] }, { name: 'D' }] }, { animate: false })
    expect(boxes(el).length).toBe(4)
    expect(h.exportCSV().split('\n')[0]).toBe('Path,Name,Title,Depth,Value')
    h.destroy()
  })
})
