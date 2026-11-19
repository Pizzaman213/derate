import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import { makeFormatters, palette } from '../../core'
import { colorScale, hatchPath, inkOn } from '../_shared/MatrixCharts'
import heatmap, { model } from './index'
import { options } from './schema'

chartSuite({
  module: heatmap,
  dir: import.meta.dirname,
  role: 'cell',
  marks: { basic: 16, realistic: 144, 'edge-single': 1, 'edge-duplicates': 16, 'edge-large': 5408, 'edge-nulls': 16, 'edge-unsorted-dates': 30, 'edge-mixed-tz': 30 },
  summary: /^Heatmap, 4 rows by 4 columns, highest East, Q4 at 77, lowest West, Q1 at 29$/,
  malformed: [
    { name: 'a text value', data: [{ x: 'a', y: 'b', value: 1 }, { x: 'a', y: 'c', value: 'lots' }], message: /field 'value' must be a number, got 'lots' at row 2/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
    { name: 'an object as a category', data: [{ x: { a: 1 }, y: 'b', value: 1 }], message: /field 'x' .*row 1/ },
    { name: 'a mapping to a missing column', data: [{ col: 'A', row: 'B', v: 1 }], options: { fields: { x: 'col', y: 'row', value: 'vv' } }, message: /column 'vv' not found/ },
  ],
})

const fmt = makeFormatters()
const opts = (o: Record<string, unknown> = {}) => options.parse(o)
const cells = (el: Element) => [...el.querySelectorAll('[data-role="cell"]')]

describe('heatmap: model and geometry', () => {
  it('places one cell per (x, y) in a regular grid', () => {
    const el = host(640, 400)
    const data = ['a', 'b', 'c'].flatMap((y, i) => ['p', 'q'].map((x, j) => ({ x, y, value: i * 2 + j })))
    const h = heatmap.render(el, data, { animate: false })
    const rs = cells(el)
    expect(rs.length).toBe(6)
    const xs = new Set(rs.map((r) => r.getAttribute('x'))), ys = new Set(rs.map((r) => r.getAttribute('y')))
    expect(xs.size).toBe(2)
    expect(ys.size).toBe(3)
    const ws = new Set(rs.map((r) => r.getAttribute('width')))
    expect(ws.size).toBe(1)
    h.destroy()
  })
  it('combines duplicate cells by the aggregate and notes it', () => {
    const data = [{ x: 'a', y: 'b', value: 2 }, { x: 'a', y: 'b', value: 4 }, { x: 'c', y: 'b', value: 1 }]
    expect(model(data, opts(), fmt).cells.map((c) => c.value)).toEqual([6, 1])
    expect(model(data, opts({ aggregate: 'mean' }), fmt).cells.map((c) => c.value)).toEqual([3, 1])
    expect(model(data, opts({ aggregate: 'count' }), fmt).cells.map((c) => c.value)).toEqual([2, 1])
    const el = host()
    const h = heatmap.render(el, data, { animate: false })
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 duplicate cells combined \(sum\)/)
    expect(h.exportCSV()).toBe('Row,Column,Value\nb,a,6\nb,c,1')
    h.destroy()
  })
  it('sorts date columns chronologically and merges the same instant written in different offsets', () => {
    const data = [
      { x: '2026-03-02T12:00:00Z', y: 'r', value: 2 },
      { x: '2026-03-01T17:30:00+05:30', y: 'r', value: 1 },
      { x: 1772366400000, y: 's', value: 3 }, // 2026-03-01T12:00Z as epoch ms
    ]
    const m = model(heatmap.validate(data).data!, opts(), fmt)
    expect(m.x.dates).toBe(true)
    expect(m.x.keys.map(Number)).toEqual([Date.UTC(2026, 2, 1, 12), Date.UTC(2026, 2, 2, 12)])
    expect(m.cells.find((c) => c.y === 's')!.x).toBe(m.x.keys[0])
  })
  it('orders categories by name and by total', () => {
    const data = [{ x: 'b', y: 'r', value: 1 }, { x: 'a', y: 'r', value: 5 }, { x: 'c', y: 'r', value: 3 }]
    expect(model(data, opts({ xOrder: 'name' }), fmt).x.keys).toEqual(['a', 'b', 'c'])
    expect(model(data, opts({ xOrder: 'value' }), fmt).x.keys).toEqual(['a', 'c', 'b'])
  })
  it('hatches null cells and keeps them focusable', () => {
    const el = host()
    const h = heatmap.render(el, [{ x: 'a', y: 'b', value: null }, { x: 'c', y: 'b', value: 1 }], { animate: false })
    expect(h.markCount()).toBe(2)
    expect(el.querySelectorAll('path[data-key^="path:hatch"], path[data-key*="hatch"]').length).toBe(1)
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 missing values hatched/)
    h.focusMark(0)
    expect(el.querySelector('[aria-live]')!.textContent).toBe('1 of 2: b, a: no value')
    h.destroy()
  })
  it('uses a diverging ramp when values span zero, and contrast-aware ink', () => {
    const seq = colorScale(palette, [0, 10])
    expect(seq.diverging).toBe(false)
    expect(seq.t(10)).toBe(1)
    const div = colorScale(palette, [-2, 6])
    expect(div.diverging).toBe(true)
    expect(div.t(0)).toBe(0.5)
    expect(div.t(-6)).toBe(0)
    expect(div.lo).toBe(-6)
    expect(colorScale(palette, [1, 5], { diverging: true, center: 3 }).t(3)).toBe(0.5)
    expect(inkOn(palette, 0.1, false)).toBe(palette.ink)
    expect(inkOn(palette, 0.95, false)).toBe(palette.surface)
    expect(inkOn(palette, 0.5, true)).toBe(palette.ink)
    expect(inkOn(palette, 0.02, true)).toBe(palette.surface)
    const el = host(640, 400)
    const h = heatmap.render(el, [{ x: 'a', y: 'r', value: 0 }, { x: 'b', y: 'r', value: 100 }], { animate: false })
    const texts = [...el.querySelectorAll('.oc-labels text')]
    expect(texts.map((t) => t.textContent)).toEqual(['0', '100'])
    expect(texts[0]!.getAttribute('style')).toContain('--oc-ink')
    expect(texts[1]!.getAttribute('style')).toContain('--oc-surface')
    h.destroy()
  })
  it('log colour scale keeps outliers from flattening the rest', () => {
    const lin = colorScale(palette, [1, 10, 10000]), log = colorScale(palette, [1, 10, 10000], { transform: 'log' })
    expect(lin.t(10)).toBeLessThan(0.01)
    expect(log.t(10)).toBeGreaterThan(0.2)
  })
  it('drops cell values when they do not fit', () => {
    const el = host(200, 160)
    const data = Array.from({ length: 30 }, (_, i) => ({ x: `c${i}`, y: 'r', value: 12345.6 }))
    const h = heatmap.render(el, data, { animate: false })
    expect(el.querySelectorAll('.oc-labels text').length).toBe(0)
    h.destroy()
  })
  it('hatch lines stay inside their cell', () => {
    const d = hatchPath(10, 20, 30, 12)
    const nums = [...d.matchAll(/[ML](-?[\d.]+),(-?[\d.]+)/g)].map((m) => [+m[1]!, +m[2]!] as const)
    expect(nums.length).toBeGreaterThan(4)
    for (const [x, y] of nums) {
      expect(x).toBeGreaterThanOrEqual(10)
      expect(x).toBeLessThanOrEqual(40)
      expect(y).toBeGreaterThanOrEqual(20)
      expect(y).toBeLessThanOrEqual(32)
    }
  })
  it('switches to canvas above 5,000 cells', () => {
    const el = host()
    const data = Array.from({ length: 5100 }, (_, i) => ({ x: `x${i % 100}`, y: `y${Math.floor(i / 100)}`, value: i }))
    const h = heatmap.render(el, data, { animate: false })
    expect(h.renderer).toBe('canvas')
    expect(h.markCount()).toBe(5100)
    h.destroy()
  })
})
