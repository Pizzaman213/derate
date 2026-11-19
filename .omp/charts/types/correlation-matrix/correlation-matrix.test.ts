import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import { stats } from '../../core'
import correlationMatrix from './index'

chartSuite({
  module: correlationMatrix,
  dir: import.meta.dirname,
  role: 'cell',
  marks: { basic: 16, realistic: 49, lower: 10, 'edge-single': 1, 'edge-zeros': 16, 'edge-large': 5184 },
  summary: /^Correlation matrix, 4 variables \(Pearson, 12 rows\), strongest positive weight and height at 0\.\d\d, strongest negative screenTime and age at −0\.\d\d$/,
  malformed: [
    { name: 'a scalar', data: 42, message: /must be rows of numeric columns, CSV text, column arrays, or \{ names, matrix \}/ },
    { name: 'a non-square matrix', data: { names: ['a', 'b'], matrix: [[1, 0.5], [0.5]] }, message: /'matrix' row 2 has 1 values but there are 2 names/ },
    { name: 'a coefficient outside [-1, 1]', data: { names: ['a', 'b'], matrix: [[1, 3], [3, 1]] }, message: /field 'matrix\[0\]\[1\]' must be less than or equal to 1, got 3/ },
    { name: 'a text coefficient', data: { names: ['a', 'b'], matrix: [[1, 'x'], ['x', 1]] }, message: /field 'matrix\[0\]\[1\]' must be a number, got 'x'/ },
    { name: 'a missing column', data: [{ a: 1, b: 2 }], options: { columns: ['a', 'c'] }, message: /column 'c' not found \(columns: a, b\)/ },
  ],
})

const rows = [
  { a: 1, b: 2, c: 9, d: 4 },
  { a: 2, b: 4.1, c: 7, d: 1 },
  { a: 3, b: 5.9, c: 8, d: 5 },
  { a: 4, b: 8.2, c: 4, d: 2 },
  { a: 5, b: 9.8, c: 2, d: 6 },
]
const cells = (el: Element) => [...el.querySelectorAll('[data-role="cell"]')]

describe('correlation-matrix: math', () => {
  it('the diagonal is 1 and the matrix is symmetric', () => {
    const v = correlationMatrix.validate(rows)
    expect(v.ok).toBe(true)
    const m = v.data!.matrix
    m.forEach((row, i) => {
      expect(row[i]).toBe(1)
      row.forEach((r, j) => expect(r).toBeCloseTo(m[j]![i]!, 12))
    })
  })
  it('matches stats.pearson and stats.spearman', () => {
    const p = correlationMatrix.validate(rows).data!.matrix
    expect(p[0]![2]).toBeCloseTo(stats.pearson(rows.map((r) => r.a), rows.map((r) => r.c)), 12)
    const s = correlationMatrix.validate(rows, { method: 'spearman' }).data!.matrix
    expect(s[0]![1]).toBeCloseTo(1, 12)
    expect(s[0]![2]).toBeCloseTo(stats.spearman(rows.map((r) => r.a), rows.map((r) => r.c)), 12)
  })
  it('ignores text columns and says so', () => {
    const el = host()
    const h = correlationMatrix.render(el, rows.map((r, i) => ({ name: `n${i}`, ...r })), { animate: false })
    expect(h.markCount()).toBe(16)
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 non-numeric column ignored/)
    h.destroy()
  })
  it('colours cells on the diverging ramp: +1 and −1 at the ends, the diagonal at +1', () => {
    const el = host()
    const h = correlationMatrix.render(el, { names: ['x', 'y'], matrix: [[1, -1], [-1, 1]] }, { animate: false })
    const fills = cells(el).map((c) => c.getAttribute('style')?.match(/fill:([^;]+)/)?.[1])
    expect(fills).toEqual(['var(--oc-div-10)', 'var(--oc-div-0)', 'var(--oc-div-0)', 'var(--oc-div-10)'])
    h.destroy()
  })
  it('cells are square, the same size, and the lower triangle drops j > i', () => {
    const el = host(640, 400)
    const h = correlationMatrix.render(el, rows, { animate: false, triangle: 'lower' })
    const cs = cells(el)
    expect(cs.length).toBe(10)
    const ws = cs.map((c) => Number(c.getAttribute('width')))
    const hs = cs.map((c) => Number(c.getAttribute('height')))
    expect(new Set(ws).size).toBe(1)
    expect(ws).toEqual(hs)
    const xs = cs.map((c) => Number(c.getAttribute('x'))), ys = cs.map((c) => Number(c.getAttribute('y')))
    const x0 = Math.min(...xs), y0 = Math.min(...ys)
    cs.forEach((_, k) => expect(xs[k]! - x0).toBeLessThanOrEqual(ys[k]! - y0 + 0.01))
  })
  it('prints values in big cells and announces pairs', () => {
    const el = host(640, 480)
    const h = correlationMatrix.render(el, { names: ['x', 'y'], matrix: [[1, 0.5], [0.5, 1]] }, { animate: false })
    expect([...el.querySelectorAll('text')].map((t) => t.textContent)).toContain('0.50')
    h.focusMark(1)
    expect(el.querySelector('[aria-live]')!.textContent).toBe('2 of 4: x and y: 0.50, moderate positive')
    h.destroy()
  })
})
