import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import pareto, { paretoItems } from './index'

chartSuite({
  module: pareto,
  dir: import.meta.dirname,
  role: 'bar',
  empty: ['edge-empty', 'edge-zeros'],
  marks: { basic: 6, realistic: 10, 'edge-single': 1, 'edge-duplicates': 6, 'edge-large': 20, 'edge-negatives': 3 },
  summary: /^Pareto chart, 6 categories, largest Misalignment at 118, 3 categories reach 80% of the total$/,
  malformed: [
    { name: 'a text value', data: [{ label: 'A', value: 1 }, { label: 'B', value: 'abc' }], message: /field 'value' must be a number, got 'abc' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'mismatched columns', data: { label: ['a', 'b'], value: [1] }, message: /column 'value' has 1 values but column 'label' has 2/ },
    { name: 'a mapping to a missing column', data: [{ name: 'A', amount: 1 }], options: { fields: { label: 'name', value: 'amont' } }, message: /column 'amont' not found/ },
  ],
})

const rows = [{ label: 'a', value: 10 }, { label: 'b', value: 50 }, { label: 'c', value: 25 }, { label: 'd', value: 15 }]
const num = (e: Element, a: string) => Number(e.getAttribute(a))

describe('pareto: math', () => {
  it('sorts descending and the cumulative share rises to exactly 100%', () => {
    const p = paretoItems(rows, 20)
    expect(p.items.map((d) => d.label)).toEqual(['b', 'c', 'd', 'a'])
    expect(p.items.map((d) => d.cumulative)).toEqual([0.5, 0.75, 0.9, 1])
    for (let i = 1; i < p.items.length; i++) expect(p.items[i]!.cumulative).toBeGreaterThanOrEqual(p.items[i - 1]!.cumulative)
    const awkward = paretoItems(Array.from({ length: 37 }, (_, i) => ({ label: `k${i}`, value: 0.1 + i / 7 })), 50)
    expect(Math.abs(awkward.items.at(-1)!.cumulative - 1)).toBeLessThan(1e-12)
  })
  it('collapses the tail into Other without changing the total', () => {
    const data = Array.from({ length: 30 }, (_, i) => ({ label: `k${i}`, value: 30 - i }))
    const p = paretoItems(data, 10)
    expect(p.items.length).toBe(10)
    expect(p.items[9]!.label).toBe('Other')
    expect(p.items[9]!.value).toBe(Array.from({ length: 21 }, (_, i) => 21 - i).reduce((a, b) => a + b, 0))
    expect(p.total).toBe(465)
    expect(p.items.at(-1)!.cumulative).toBe(1)
  })
  it('omits negatives and nulls and says so', () => {
    const el = host()
    const h = pareto.render(el, [...rows, { label: 'e', value: -3 }, { label: 'f', value: null }], { animate: false })
    expect(el.querySelectorAll('[data-role="bar"]').length).toBe(4)
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 negative values omitted; 1 missing values omitted/)
    h.destroy()
  })
  it('the cumulative points sit on the right axis scale and the threshold line at 80%', () => {
    const el = host(640, 400)
    const h = pareto.render(el, rows, { animate: false })
    const pts = [...el.querySelectorAll('[data-role="point"]')]
    expect(pts.length).toBe(4)
    const top = el.querySelector('[data-key="text:y2l:1.00"]')!, bottom = el.querySelector('[data-key="text:y2l:0.00"]')!
    const y100 = num(top, 'y'), y0 = num(bottom, 'y')
    expect(num(pts[3]!, 'cy')).toBeCloseTo(y100, 1)
    expect(num(pts[0]!, 'cy')).toBeCloseTo((y0 + y100) / 2, 1)
    const th = el.querySelector('[data-key="line:threshold"]')!
    expect(num(th, 'y1')).toBeCloseTo(y0 + (y100 - y0) * 0.8, 1)
    // bars descend
    const hs = [...el.querySelectorAll('[data-role="bar"]')].map((e) => num(e, 'height'))
    expect(hs).toEqual([...hs].sort((a, b) => b - a))
    expect(h.exportCSV()).toBe('Label,Value,Cumulative share\nb,50,0.5\nc,25,0.75\nd,15,0.9\na,10,1')
    h.destroy()
  })
})
