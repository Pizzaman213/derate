import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import { cleanTree, toTree } from '../_shared/PartWholeB'
import icicle, { icicleLayout } from './index'

chartSuite({
  module: icicle,
  dir: import.meta.dirname,
  role: 'node',
  marks: { basic: 12, realistic: 24, 'edge-single': 1, 'edge-negatives': 5, 'edge-nulls': 7, 'edge-duplicates': 7, 'edge-large': 5417, 'edge-outliers': 15 },
  summary: /^Icicle chart, 4 groups, 8 items, total 800, largest Engineering at 390 \(4[89]%\)$/,
  empty: ['edge-empty', 'edge-zeros'],
  malformed: [
    { name: 'a text value in CSV', data: 'path,value\na/b,1\na/c,many', message: /field 'value' must be a number, got 'many' at row 2/ },
    { name: 'a list of numbers', data: [1, 2, 3], message: /the data must be a tree/ },
    { name: 'a duplicate id', data: [{ id: 'a', value: 1 }, { id: 'a', value: 2 }], message: /row 2: duplicate id 'a'/ },
    { name: 'a node without a name', data: { name: 'r', children: [{ value: 3 }] }, message: /root\.children\[0\]\.name must be a string/ },
  ],
})

const tree = { name: 'r', children: [{ name: 'a', children: [{ name: 'a1', value: 6 }, { name: 'a2', value: 2 }] }, { name: 'b', value: 4 }, { name: 'c', children: [{ name: 'c1', value: 3 }, { name: 'c2', value: 5 }] }] }

describe('icicle: geometry', () => {
  for (const orientation of ['horizontal', 'vertical'] as const) {
    it(`${orientation}: children split their parent's extent in proportion, levels are equal bands`, () => {
      const rects = icicleLayout(cleanTree(toTree(tree, {})).root!, 600, 300, orientation)
      const along = (r: (typeof rects)[number]) => (orientation === 'horizontal' ? r.height : r.width)
      const across = (r: (typeof rects)[number]) => (orientation === 'horizontal' ? r.width : r.height)
      const root = rects.find((r) => r.node.depth === 0)!
      expect(along(root)).toBeCloseTo(orientation === 'horizontal' ? 300 : 600, 9)
      for (const p of rects) {
        expect(across(p)).toBeCloseTo((orientation === 'horizontal' ? 600 : 300) / 3, 9)
        const kids = rects.filter((r) => r.node.parent === p.node)
        if (!kids.length) continue
        expect(kids.reduce((s, k) => s + along(k), 0)).toBeCloseTo(along(p), 9)
        for (const k of kids) expect(along(k) / along(p)).toBeCloseTo(k.node.data.value / p.node.data.value, 9)
      }
    })
  }
  it('levels run left to right by default and top to bottom when vertical', () => {
    const el = host(600, 300)
    const h = icicle.render(el, tree, { animate: false, height: 300 })
    const x = (key: string) => Number(el.querySelector(`[data-key="rect:${key}"]`)!.getAttribute('x'))
    const y = (key: string) => Number(el.querySelector(`[data-key="rect:${key}"]`)!.getAttribute('y'))
    expect(x('r')).toBe(0)
    expect(x('r › a')).toBeGreaterThan(0)
    expect(x('r › a › a1')).toBeGreaterThan(x('r › a'))
    h.update(tree, { orientation: 'vertical' })
    expect(y('r')).toBe(0)
    expect(y('r › a › a1')).toBeGreaterThan(y('r › a'))
    h.destroy()
  })
  it('reads a custom path separator', () => {
    const t = cleanTree(toTree([{ path: 'x > y', value: 1 }, { path: 'x > z', value: 2 }], { separator: ' > ' })).root!
    expect(t.name).toBe('x')
    expect(t.children.map((c) => c.name)).toEqual(['z', 'y'])
  })
})
