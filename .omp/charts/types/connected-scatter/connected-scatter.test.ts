import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import connectedScatter, { runs, segmentMid } from './index'

chartSuite({
  module: connectedScatter,
  dir: import.meta.dirname,
  role: 'point',
  marks: { basic: 12, realistic: 33, 'edge-single': 1, 'edge-duplicates': 24, 'edge-nulls': 6, 'edge-large': 5200 },
  summary: /^Connected scatter plot, 12 steps from 2010 to 2021, Unemployment \(%\) from 3\.7 to 9\.6, Inflation \(%\) from 0\.1 to 4\.7, no clear correlation \(r = -?0\.\d+\), from \(9\.6, 1\.6\) to \(5\.3, 4\.7\)$/,
  malformed: [
    { name: 'a text value', data: [{ x: 1, y: 2 }, { x: 2, y: 'n/a' }], message: /field 'y' must be a number, got 'n\/a' at row 2/ },
    { name: 'a scalar', data: true, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'a mapping to a missing column', data: [{ a: 1, b: 2 }], options: { fields: { x: 'a', y: 'bb' } }, message: /column 'bb' not found/ },
  ],
})

describe('connected-scatter: geometry', () => {
  it('keeps input order and breaks the path at missing rows', () => {
    const r = runs([{ x: 1, y: 1 }, { x: 2, y: 2 }, { x: null, y: 3 }, { x: 4, y: 4 }, { x: 5, y: 5 }])
    expect(r.map((s) => s.map((p) => p.i))).toEqual([[0, 1], [3, 4]])
    const el = host(640, 400)
    const h = connectedScatter.render(el, [{ x: 3, y: 1 }, { x: 1, y: 2 }, { x: null, y: 3 }, { x: 4, y: 4 }, { x: 5, y: 1 }], { animate: false })
    expect(el.querySelectorAll('[data-key^="path:path:"]').length).toBe(2)
    expect(el.querySelector('.oc-note')!.textContent).toBe('1 rows missing x or y break the path')
    // points are announced in input order, not sorted by x
    const keys = [...el.querySelectorAll('[data-role="point"]')].map((c) => Number(c.getAttribute('cx')))
    expect(keys[0]!).toBeGreaterThan(keys[1]!)
    h.destroy()
  })
  it('arrows point along the direction of travel', () => {
    const lin = segmentMid([0, 0], [0, 0], [10, 0], [10, 0], false)
    expect(lin).toEqual({ x: 5, y: 0, dx: 10, dy: 0 })
    // a straight run of points: the smooth curve degenerates to the same line
    const sm = segmentMid([0, 0], [10, 0], [20, 0], [30, 0], true)
    expect(sm.x).toBeCloseTo(15, 9)
    expect(sm.y).toBeCloseTo(0, 9)
    expect(sm.dx).toBeGreaterThan(0)
  })
  it('labels the first and last step and marks them in the announcement', () => {
    const el = host(640, 400)
    const h = connectedScatter.render(el, [{ x: 1, y: 1, label: '2001' }, { x: 5, y: 3, label: '2002' }, { x: 9, y: 2, label: '2003' }], { animate: false, labels: 'ends' })
    const texts = [...el.querySelectorAll('.oc-labels text')].map((t) => t.textContent)
    expect(texts.sort()).toEqual(['2001', '2003'])
    h.focusMark(0)
    expect(el.querySelector('[aria-live]')!.textContent).toMatch(/2001 \(start\)/)
    h.focusMark(2)
    expect(el.querySelector('[aria-live]')!.textContent).toMatch(/2003 \(end\)/)
    h.destroy()
  })
  it('thins arrows to about two dozen on long paths', () => {
    const el = host(800, 500)
    const data = Array.from({ length: 400 }, (_, i) => ({ x: Math.cos(i / 20) * i, y: Math.sin(i / 20) * i }))
    const h = connectedScatter.render(el, data, { animate: false })
    const arrows = el.querySelectorAll('[data-key^="path:arrow:"]').length
    expect(arrows).toBeGreaterThan(5)
    expect(arrows).toBeLessThanOrEqual(25)
    h.destroy()
  })
})
