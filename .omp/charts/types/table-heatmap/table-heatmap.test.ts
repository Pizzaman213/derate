import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import tableHeatmap, { fitWidths } from './index'

chartSuite({
  module: tableHeatmap,
  dir: import.meta.dirname,
  role: 'cell',
  marks: { basic: 25, realistic: 143, 'edge-single': 5, 'edge-duplicates': 50, 'edge-large': 5500, 'edge-long-labels': 20 },
  summary: /^Table heatmap, 5 rows by 5 columns \(4 numeric\), Revenue highest East at 1,530, lowest West at 760$/,
  malformed: [
    { name: 'an object in a cell', data: [{ a: 1 }, { a: { nested: true } }], message: /field 'a' .*at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'a missing column in options.columns', data: [{ a: 1, b: 2 }], options: { columns: ['a', 'c'] }, message: /column 'c' not found \(columns: a, b\)/ },
  ],
})

const rows = [
  { name: 'a', x: 1, y: 100 },
  { name: 'b', x: 2, y: 300 },
  { name: 'c', x: 3, y: 200 },
]
const cellsOf = (el: Element) => [...el.querySelectorAll('[data-role="cell"]')]
const styleOf = (el: Element, key: string) => el.querySelector(`[data-key="rect:${key}"]`)!.getAttribute('style') ?? ''

describe('table-heatmap: layout', () => {
  it('one cell per row and column, in column order, filling the width', () => {
    const el = host(600, 400)
    const h = tableHeatmap.render(el, rows, { animate: false })
    const cells = cellsOf(el)
    expect(cells.length).toBe(9)
    const first = cells.slice(0, 3)
    const right = Number(first[2]!.getAttribute('x')) + Number(first[2]!.getAttribute('width'))
    expect(right).toBeGreaterThan(597)
    expect(right).toBeLessThanOrEqual(600)
    expect(Number(first[0]!.getAttribute('x'))).toBeLessThan(Number(first[1]!.getAttribute('x')))
    h.destroy()
  })
  it('scale column: each numeric column spans its own ramp; scale table shares one', () => {
    const el = host(600, 400)
    const h = tableHeatmap.render(el, rows, { animate: false })
    // the column maxima both sit at the dark end, the minima at the light end
    expect(styleOf(el, '2|x')).toBe(styleOf(el, '1|y'))
    expect(styleOf(el, '0|x')).toBe(styleOf(el, '0|y'))
    h.update(rows, { scale: 'table' })
    expect(styleOf(el, '0|x')).toBe('fill:var(--oc-seq-0)')
    expect(styleOf(el, '1|y')).toBe('fill:var(--oc-seq-8)')
    expect(styleOf(el, '2|x')).not.toBe(styleOf(el, '1|y'))
    h.destroy()
  })
  it('text columns are not shaded, numeric text is right-aligned', () => {
    const el = host(600, 400)
    const h = tableHeatmap.render(el, rows, { animate: false })
    expect(styleOf(el, '0|name')).toContain('--oc-surface')
    expect(el.querySelector('[data-key="text:t:0|x"]')!.getAttribute('style')).toContain('text-anchor:end')
    expect(el.querySelector('[data-key="text:t:0|name"]')!.getAttribute('style')).toContain('text-anchor:start')
    h.destroy()
  })
  it('honours options.columns and picks numeric columns by content', () => {
    const el = host(600, 400)
    const h = tableHeatmap.render(el, [{ k: 'a', v: 1, note: 'x' }, { k: 'b', v: null, note: '2' }], { animate: false, columns: ['v', 'k'] })
    expect(h.exportCSV()).toBe('v,k\n1,a\n,b')
    expect(cellsOf(el).length).toBe(4)
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 empty numeric cells hatched/)
    h.destroy()
  })
  it('truncates long text with an ellipsis and keeps the full text', () => {
    const el = host(320, 300)
    const long = 'Supercalifragilisticexpialidocious Holdings International Limited'
    const h = tableHeatmap.render(el, [{ name: long, v: 1 }, { name: 'b', v: 2 }], { animate: false })
    const t = el.querySelector('[data-key="text:t:0|name"]')!
    expect(t.textContent!.endsWith('…')).toBe(true)
    expect(t.getAttribute('data-full')).toBe(long)
    h.destroy()
  })
  it('shows the first rows and says how many are hidden when even 1px rows do not fit', () => {
    const el = host(400, 200)
    const data = Array.from({ length: 400 }, (_, i) => ({ k: `r${i}`, v: i }))
    const h = tableHeatmap.render(el, data, { animate: false, height: 200 })
    const n = h.markCount() / 2
    expect(n).toBeLessThan(400)
    expect(el.querySelector('.oc-note')!.textContent).toMatch(new RegExp(`${400 - n} more rows not shown \\(showing ${n} of 400\\)`))
    h.destroy()
  })
})

describe('table-heatmap: fitWidths', () => {
  it('stretches to fill when the content is narrower', () => {
    const w = fitWidths([50, 100, 50], 400, 20)
    expect(w.reduce((a, b) => a + b, 0)).toBeCloseTo(400)
    expect(w[1]! / w[0]!).toBeCloseTo(2)
  })
  it('caps the widest columns first when the content is wider', () => {
    const w = fitWidths([40, 300, 200], 300, 20)
    expect(w.reduce((a, b) => a + b, 0)).toBeCloseTo(300)
    expect(w[0]).toBe(40)
    expect(w[1]).toBeCloseTo(130)
    expect(w[2]).toBeCloseTo(130)
  })
  it('splits evenly when even the minimum widths do not fit', () => {
    expect(fitWidths([100, 100, 100, 100], 60, 20)).toEqual([15, 15, 15, 15])
  })
})
