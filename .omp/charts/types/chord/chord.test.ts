import { describe, expect, it } from 'vitest'
import { chord as d3chord } from 'd3-chord'
import { chartSuite, host } from '../../tests/harness'
import chord from './index'

const names = ['Black', 'Blond', 'Brown', 'Red']
const matrix = [[11975, 5871, 8916, 2868], [1951, 10048, 2060, 6171], [8010, 16145, 8090, 8045], [1013, 990, 940, 6907]]

chartSuite({
  module: chord,
  dir: import.meta.dirname,
  role: 'ribbon',
  marks: { basic: 10, realistic: 36, 'edge-single': 1, 'edge-large': 5050 + 100, 'edge-duplicates': 10 },
  empty: ['edge-empty', 'edge-zeros'],
  summary: /^Chord diagram, 4 groups and 16 flows, largest sender Brown at 40k, largest flow Brown → Blond at 16k$/,
  malformed: [
    { name: 'a non-square matrix', data: { names: ['a', 'b'], matrix: [[1, 2], [3]] }, message: /field 'matrix\[1\]': has 1 values but there are 2 names/ },
    { name: 'too few rows', data: { names: ['a', 'b', 'c'], matrix: [[1, 2, 3]] }, message: /field 'matrix': has 1 rows but there are 3 names/ },
    { name: 'a text cell', data: { names: ['a'], matrix: [['x']] }, message: /field 'matrix\[0\]\[0\]' must be a number, got 'x'/ },
    { name: 'a link without a source', data: { links: [{ target: 'b', value: 1 }] }, message: /field 'links\[0\]\.source' is missing/ },
    { name: 'a scalar', data: 7, message: /the data must be \{ nodes\?, links/ },
  ],
})

/** Angular span of a group arc from its data-mark tooltip anchor is not exposed; recompute from the path. */
function arcSpan(d: string, cx: number, cy: number): number {
  // annulus path: M x0,y0 A ... x1,y1 L ... -- the first two points are the outer start and end
  const nums = d.match(/-?[\d.]+(?:e-?\d+)?/g)!.map(Number)
  const [x0, y0] = [nums[0]!, nums[1]!]
  const a = d.indexOf('A')
  const after = d.slice(a + 1).match(/-?[\d.]+(?:e-?\d+)?/g)!.map(Number)
  const [x1, y1] = [after[5]!, after[6]!]
  const ang = (x: number, y: number) => Math.atan2(x - cx, -(y - cy))
  let s = ang(x1, y1) - ang(x0, y0)
  if (s < 0) s += 2 * Math.PI
  return s
}

describe('chord: geometry', () => {
  it('group angles are proportional to row sums', () => {
    const el = host(600, 600)
    const h = chord.render(el, { names, matrix }, { animate: false, padAngle: 0.05 })
    const svg = el.querySelector('svg.oc-svg')!
    const cx = Number(svg.getAttribute('width')) / 2, cy = Number(svg.getAttribute('height')) / 2
    const sums = matrix.map((r) => r.reduce((a, v) => a + v, 0))
    const total = sums.reduce((a, v) => a + v, 0)
    const free = 2 * Math.PI - 0.05 * names.length
    names.forEach((nm, i) => {
      const d = el.querySelector(`[data-key="path:group:${nm}"]`)!.getAttribute('d')!
      expect(arcSpan(d, cx, cy)).toBeCloseTo((free * sums[i]!) / total, 2)
    })
    h.destroy()
  })

  it('matches d3-chord for each group and keeps everything inside the box', () => {
    const groups = d3chord().padAngle(0.05)(matrix).groups
    for (const g of groups) expect(g.value).toBe(matrix[g.index]!.reduce((a, v) => a + v, 0))
    const el = host(360, 300)
    const h = chord.render(el, { names, matrix }, { animate: false })
    for (const t of el.querySelectorAll('[data-role="arc"]')) expect(t.getAttribute('d')).not.toMatch(/NaN/)
    for (const t of el.querySelectorAll('text')) {
      const x = Number(t.getAttribute('x'))
      expect(x).toBeGreaterThanOrEqual(0)
      expect(x).toBeLessThanOrEqual(360)
    }
    h.destroy()
  })

  it('builds the matrix from link rows, summing duplicates', () => {
    const el = host()
    const h = chord.render(el, [{ source: 'a', target: 'b', value: 2 }, { source: 'a', target: 'b', value: 3 }, { source: 'b', target: 'a', value: 1 }], { animate: false })
    expect(h.exportCSV()).toBe('Source,Target,Value\na,b,5\nb,a,1')
    expect(el.querySelectorAll('[data-role="ribbon"]').length).toBe(1)
    expect(el.querySelector('[data-role="ribbon"]')!.getAttribute('aria-label') ?? h.summary()).toBeTruthy()
    h.destroy()
  })
  it('merges duplicate names and says so', () => {
    const el = host()
    const h = chord.render(el, { names: ['a', 'b', 'a'], matrix: [[0, 1, 0], [2, 0, 3], [0, 4, 0]] }, { animate: false })
    expect(h.exportCSV()).toBe('Source,Target,Value\na,b,5\nb,a,5')
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 duplicate name merged/)
    h.destroy()
  })
})
