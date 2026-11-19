import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import { cleanTree, toTree } from '../_shared/PartWholeB'
import treemap, { treemapLayout } from './index'

chartSuite({
  module: treemap,
  dir: import.meta.dirname,
  role: 'cell',
  marks: { basic: 8, realistic: 19, 'edge-single': 1, 'edge-negatives': 4, 'edge-nulls': 4, 'edge-duplicates': 4, 'edge-large': 5200, 'edge-outliers': 13 },
  summary: /^Treemap, 4 groups, 8 items, total 800, largest Engineering at 390 \(4[89]%\)$/,
  empty: ['edge-empty', 'edge-zeros'],
  malformed: [
    { name: 'a text value in rows', data: [{ label: 'A', value: 1 }, { label: 'B', value: 'abc' }], message: /field 'value' must be a number, got 'abc' at row 2/ },
    { name: 'a scalar', data: 42, message: /the data must be a tree \{ name, value\?, children\? \}/ },
    { name: 'a text value in a tree', data: { name: 'r', children: [{ name: 'a', value: 'x' }] }, message: /root\.children\[0\]\.value must be a finite number/ },
    { name: 'a parent cycle', data: [{ id: 'a', parent: 'b', value: 1 }, { id: 'b', parent: 'a', value: 2 }], message: /cycle 'a' -> 'b' -> 'a'|cycle 'b' -> 'a' -> 'b'/ },
    { name: 'rows without a usable column', data: [{ foo: 1, value: 2 }], message: /rows need a 'path' column/ },
    { name: 'a mapping to a missing column', data: [{ folder: 'a', mb: 1 }], options: { fields: { path: 'folder', value: 'size' } }, message: /column 'size' not found/ },
  ],
})

const areas = (el: Element) => [...el.querySelectorAll('[data-role="cell"]:not([data-exiting])')].map((r) => Number(r.getAttribute('width')) * Number(r.getAttribute('height')))

describe('treemap: geometry', () => {
  it('leaf areas are proportional to values (ratio constant within 1%)', () => {
    const tree = toTree({ name: 'r', children: [{ name: 'a', children: [{ name: 'a1', value: 7 }, { name: 'a2', value: 3 }] }, { name: 'b', value: 11 }, { name: 'c', children: [{ name: 'c1', value: 1 }, { name: 'c2', value: 23 }] }] }, {})
    const root = cleanTree(tree).root!
    const laid = treemapLayout(root, 640, 400)
    const ratios = laid.leaves().map((l) => ((l.x1 - l.x0) * (l.y1 - l.y0)) / l.data.value)
    const mean = ratios.reduce((s, r) => s + r, 0) / ratios.length
    for (const r of ratios) expect(Math.abs(r / mean - 1)).toBeLessThan(0.01)
    // and the leaves tile the whole box
    expect(laid.leaves().reduce((s, l) => s + (l.x1 - l.x0) * (l.y1 - l.y0), 0)).toBeCloseTo(640 * 400, 0)
  })
  it('rendered cells keep the proportion', () => {
    const el = host(600, 400)
    const h = treemap.render(el, [{ label: 'a', value: 10 }, { label: 'b', value: 20 }, { label: 'c', value: 40 }], { animate: false, height: 400 })
    const a = areas(el)
    const total = a.reduce((s, v) => s + v, 0)
    expect(a.map((v) => v / total).sort()).toEqual([10 / 70, 20 / 70, 40 / 70].map((v) => expect.closeTo(v, 2)).sort())
    h.destroy()
  })
  it('path, id/parent and nested inputs build the same tree', () => {
    const nested = cleanTree(toTree({ name: 'All', children: [{ name: 'x', children: [{ name: 'y', value: 2 }, { name: 'z', value: 3 }] }, { name: 'w', value: 1 }] }, {})).root!
    const paths = cleanTree(toTree('p,v\nx/y,2\nx/z,3\nw,1', { fields: { path: 'p', value: 'v' } })).root!
    const ids = cleanTree(toTree([{ id: 'All' }, { id: 'x', parent: 'All' }, { id: 'y', parent: 'x', value: 2 }, { id: 'z', parent: 'x', value: '3' }, { id: 'w', parent: 'All', value: 1 }], {})).root!
    const shape = (n: typeof nested): unknown => ({ name: n.name, value: n.value, children: n.children.map(shape) })
    expect(shape(paths)).toEqual(shape(nested))
    expect(shape(ids)).toEqual(shape(nested))
  })
  it('omits negatives and notes them; all zeros is an empty state', () => {
    const el = host()
    const h = treemap.render(el, [{ label: 'a', value: 5 }, { label: 'b', value: -2 }, { label: 'c', value: 0 }], { animate: false })
    expect(h.markCount()).toBe(1)
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/1 negative values omitted; 1 zero values not drawn/)
    h.update([{ label: 'a', value: 0 }])
    expect(h.state).toBe('empty')
    expect(el.querySelector('.oc-state')!.textContent).toMatch(/All values are zero/)
    h.destroy()
  })
  it('colours by first-level branch and re-lays out when a branch is hidden', () => {
    const el = host(600, 400)
    const data = { name: 'r', children: [{ name: 'A', children: [{ name: 'a1', value: 1 }, { name: 'a2', value: 1 }] }, { name: 'B', children: [{ name: 'b1', value: 2 }] }] }
    const h = treemap.render(el, data, { animate: false, height: 400 })
    const cells = [...el.querySelectorAll('[data-role="cell"]')]
    expect(cells.map((c) => c.getAttribute('data-series'))).toEqual(['A', 'A', 'B'])
    const fills = cells.map((c) => c.getAttribute('style'))
    expect(fills[0]).toBe(fills[1])
    expect(fills[0]).not.toBe(fills[2])
    ;(el.querySelector('.oc-legend button') as HTMLElement).click()
    const left = areas(el)
    expect(left).toHaveLength(1)
    expect(left[0]).toBeCloseTo(600 * Number(el.querySelector('svg.oc-svg')!.getAttribute('height')), -2)
    h.destroy()
  })
  it('switches to canvas above the threshold', () => {
    const el = host()
    const data = { path: Array.from({ length: 6000 }, (_, i) => `G${i % 5}/n${i}`), value: Array.from({ length: 6000 }, (_, i) => 1 + (i % 9)) }
    const h = treemap.render(el, data, { animate: false })
    expect(h.renderer).toBe('canvas')
    expect(h.markCount()).toBe(6000)
    h.destroy()
  })
})
